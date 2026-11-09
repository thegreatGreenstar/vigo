"""The host runner's contract: argv without a shell, paths under the data root.

The runner is the one door from the Linux agent to a program only the Mac can
run.  A later edit that lets an argument name any path, or that lists a writer's
MCP tool, would still pass every other test and would put the Mac's user behind
the agent's arguments.
"""

import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("host_runner", ROOT / "deploy" / "host-runner.py")
assert SPEC and SPEC.loader
host_runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host_runner)

TOKEN = "a" * 32

# A stand-in for ScarletNX's MCP: two tools, one of which writes and must be
# filtered out by the program's allowlist.
STUB = '''
import json, sys
TOOLS = [
    {"name": "scan_log", "description": "read a log", "inputSchema": {"type": "object"}},
    {"name": "write_case", "description": "write a case", "inputSchema": {"type": "object"}},
]
for line in sys.stdin:
    try:
        request = json.loads(line)
    except json.JSONDecodeError:
        continue
    ident = request.get("id")
    if ident is None:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "stub"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = (request.get("params") or {}).get("name")
        result = {"content": [{"type": "text", "text": f"called {name}"}], "isError": False}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}) + "\\n")
    sys.stdout.flush()
'''


class HostRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.base = pathlib.Path(self.scratch.name)
        self.data = self.base / "data"
        self.data.mkdir()
        (self.data / "state").mkdir()
        self.token_file = self.base / "token"
        self.token_file.write_text(TOKEN, encoding="utf-8")
        self.stub = self.base / "stub-mcp.py"
        self.stub.write_text(STUB, encoding="utf-8")
        self.config_file = self.base / "host-runner.json"

    def make(self, **program: object) -> "host_runner.Config":
        entry = {
            "binary": sys.executable,
            "cwd": str(self.data),
            "dataRoot": str(self.data),
            "mcp": {"command": [sys.executable, str(self.stub)], "tools": ["scan_log"]},
        }
        entry.update(program)
        raw = {"bind": "127.0.0.1", "port": 4099,
               "tokenFile": str(self.token_file),
               "programs": {"sample": entry}}
        self.config_file.write_text(json.dumps(raw), encoding="utf-8")
        return host_runner.Config(self.config_file)

    def runner(self, **program: object) -> tuple["host_runner.Runner", "host_runner.Program"]:
        config = self.make(**program)
        runner = host_runner.Runner(config)
        self.addCleanup(runner.close)
        return runner, config.programs["sample"]

    # --- the config ---------------------------------------------------------
    def test_a_program_is_a_config_entry(self) -> None:
        config = self.make()
        self.assertTrue(config.programs["sample"].available)
        self.assertEqual(config.programs["sample"].data_root, pathlib.Path(self.data).resolve())

    def test_an_unknown_program_key_stops_it(self) -> None:
        with self.assertRaises(host_runner.ConfigError) as caught:
            self.make(shell=True)
        self.assertIn("shell", str(caught.exception))

    def test_a_short_token_stops_it(self) -> None:
        self.token_file.write_text("short", encoding="utf-8")
        with self.assertRaises(host_runner.ConfigError):
            self.make()

    def test_a_log_file_outside_the_data_root_stops_it(self) -> None:
        outside = str(self.base.parent / "outside.log")
        with self.assertRaises(host_runner.ConfigError):
            self.make(logFile=outside)

    def test_a_program_is_a_binary_or_a_step_table(self) -> None:
        with self.assertRaises(host_runner.ConfigError) as caught:
            self.make(steps={"build": ["true"]})
        self.assertIn("both", str(caught.exception))
        with self.assertRaises(host_runner.ConfigError) as caught:
            self.make(binary=None)
        self.assertIn("neither", str(caught.exception))

    def test_a_step_must_be_a_list_of_strings(self) -> None:
        for bad in ({"build": "true"}, {"build": []}, {"build": [1]}):
            with self.assertRaises(host_runner.ConfigError, msg=bad):
                self.make(binary=None, steps=bad)

    def test_a_step_timeout_is_a_positive_number(self) -> None:
        with self.assertRaises(host_runner.ConfigError):
            self.make(timeout=0)

    # --- steps, the fixed commands ------------------------------------------
    def steps(self, **program: object) -> tuple["host_runner.Runner", "host_runner.Program"]:
        table = {
            "hello": [sys.executable, "-c", "print('step')"],
            "slow": [sys.executable, "-c", "import time; time.sleep(2)"],
        }
        return self.runner(binary=None, steps=table, **program)

    def test_a_step_runs_its_command_and_says_which(self) -> None:
        runner, _ = self.steps()
        ran = runner.run({"program": "sample", "step": "hello"})
        self.assertEqual(ran["step"], "hello")
        self.assertEqual(ran["code"], 0)
        self.assertEqual(ran["stdout"].strip(), "step")

    def test_a_step_takes_no_argument_and_no_unknown_name(self) -> None:
        runner, _ = self.steps()
        with self.assertRaises(ValueError) as caught:
            runner.run({"program": "sample", "step": "nope"})
        self.assertIn("no step", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            runner.run({"program": "sample", "step": "hello", "args": ["--any"]})
        self.assertIn("takes no args", str(caught.exception))

    def test_a_programs_timeout_applies_to_its_steps(self) -> None:
        runner, _ = self.steps(timeout=0.2)
        ran = runner.run({"program": "sample", "step": "slow"})
        self.assertTrue(ran["timed_out"])

    def test_the_mcp_run_tool_lists_the_steps_and_takes_a_timeout(self) -> None:
        runner, program = self.steps()
        listed = runner.mcp(program, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        run = next(tool for tool in listed["result"]["tools"] if tool["name"] == "sample_run")
        self.assertEqual(run["inputSchema"]["required"], ["step"])
        self.assertEqual(run["inputSchema"]["properties"]["step"]["enum"], ["hello", "slow"])
        ran = runner.mcp(program, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "sample_run",
                       "arguments": {"step": "slow", "timeout_seconds": 0.2}},
        })
        self.assertTrue(json.loads(ran["result"]["content"][0]["text"])["timed_out"])

    def test_mcp_refuses_an_unknown_step(self) -> None:
        runner, program = self.steps()
        refused = runner.mcp(program, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "sample_run", "arguments": {"step": "nope"}},
        })
        self.assertTrue(refused["result"]["isError"])
        self.assertIn("no step", refused["result"]["content"][0]["text"])

    @unittest.skipUnless(sys.platform == "darwin", "the example names Mac paths")
    def test_the_example_config_names_a_build_door(self) -> None:
        raw = json.loads((ROOT / "deploy" / "host-runner.example.json").read_text(encoding="utf-8"))
        raw["tokenFile"] = str(self.token_file)
        self.config_file.write_text(json.dumps(raw), encoding="utf-8")
        config = host_runner.Config(self.config_file)
        build = config.programs["scarletnx-build"]
        self.assertEqual(build.steps["build"][:3], ["cmake", "--build", "--preset"])
        self.assertIn("configure", build.steps)
        self.assertIn("install", build.steps)
        self.assertNotEqual(config.programs["scarletnx-built"].binary,
                            config.programs["scarletnx"].binary)

    # --- argument rooting, the boundary -------------------------------------
    def test_a_path_outside_the_data_root_is_refused(self) -> None:
        _, program = self.runner()
        outside = str(self.base.parent / "outside")
        for args in ([f"--game={outside}"], [outside], ["--shot=../out"]):
            with self.assertRaises(ValueError, msg=args):
                host_runner.check_args(program, args)

    def test_a_path_inside_the_data_root_is_allowed(self) -> None:
        _, program = self.runner()
        inside = self.data / "recompiled" / "0100" / "0100"
        inside.mkdir(parents=True)
        self.assertEqual(host_runner.check_args(program, [f"--game={inside}", "--seconds=20"]),
                         [f"--game={inside}", "--seconds=20"])

    def test_data_must_be_the_programs_own_root(self) -> None:
        _, program = self.runner()
        elsewhere = str(self.base.parent / "elsewhere")
        with self.assertRaises(ValueError):
            host_runner.check_args(program, [f"--data={elsewhere}"])
        self.assertEqual(host_runner.check_args(program, [f"--data={self.data}"]),
                         [f"--data={self.data}"])

    def test_a_control_character_is_refused(self) -> None:
        _, program = self.runner()
        for bad in ("--script=a\nb", "--script=a\0b"):
            with self.assertRaises(ValueError):
                host_runner.check_args(program, [bad])

    def test_too_many_arguments_are_refused(self) -> None:
        _, program = self.runner()
        with self.assertRaises(ValueError):
            host_runner.check_args(program, ["x"] * (host_runner.MOST_ARGS + 1))

    # --- the log read -------------------------------------------------------
    def test_the_log_reads_only_under_the_data_root(self) -> None:
        _, program = self.runner()
        log = self.data / "state" / "last-run.log"
        log.write_text("\n".join(f"line {number}" for number in range(1, 11)), encoding="utf-8")
        self.assertEqual(host_runner.tail_of(log, 2), "line 9\nline 10")
        with self.assertRaises(ValueError):
            host_runner.inside(program, str(self.base.parent / "outside.log"), "path")
        self.assertEqual(host_runner.inside(program, "state/last-run.log", "path"), log.resolve())

    # --- MCP ----------------------------------------------------------------
    def test_mcp_lists_its_tools_and_the_allowlisted_child_tools(self) -> None:
        runner, program = self.runner()
        listed = runner.mcp(program, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        names = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertIn("sample_run", names)
        self.assertIn("scan_log", names)
        self.assertNotIn("write_case", names)
        # The program's own MCP already reads its logs; a second name for that
        # read would only confuse the choice.
        self.assertNotIn("sample_log", names)

    def test_a_program_without_an_mcp_gets_the_log_tool(self) -> None:
        runner, program = self.runner(mcp=None)
        listed = runner.mcp(program, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        names = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertEqual(names, ["sample_run", "sample_log"])

    def test_mcp_forwards_a_child_tool_and_runs_the_program(self) -> None:
        runner, program = self.runner()
        forwarded = runner.mcp(program, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "scan_log", "arguments": {}},
        })
        self.assertIn("called scan_log", forwarded["result"]["content"][0]["text"])
        ran = runner.mcp(program, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "sample_run",
                       "arguments": {"args": ["-c", "print('hello')"]}},
        })
        result = json.loads(ran["result"]["content"][0]["text"])
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["stdout"].strip(), "hello")

    def test_mcp_refuses_an_unknown_tool(self) -> None:
        runner, program = self.runner()
        refused = runner.mcp(program, {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        })
        self.assertTrue(refused["result"]["isError"])
        self.assertIn("not offer", refused["result"]["content"][0]["text"])

    def test_the_mcp_run_tool_honours_its_timeout(self) -> None:
        runner, program = self.runner()
        ran = runner.mcp(program, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "sample_run",
                       "arguments": {"args": ["-c", "import time; time.sleep(2)"],
                                     "timeout_seconds": 0.2}},
        })
        self.assertTrue(json.loads(ran["result"]["content"][0]["text"])["timed_out"])

    def test_a_missing_binary_is_not_run(self) -> None:
        runner, program = self.runner(binary=str(self.base / "missing"))
        with self.assertRaises(ValueError) as caught:
            runner.run({"program": "sample", "args": ["x"]})
        self.assertIn("missing", str(caught.exception))

    # --- the HTTP door ------------------------------------------------------
    def serve(self) -> tuple["host_runner.HttpServer", str]:
        runner, _ = self.runner()
        server = host_runner.HttpServer(("127.0.0.1", 0), runner)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address
        return server, f"http://{host}:{port}"

    def call(self, url: str, token: str | None = TOKEN, body: dict | None = None) -> tuple[int, str]:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as answer:
                return answer.status, answer.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode()

    def test_the_door_needs_the_token(self) -> None:
        _, base = self.serve()
        self.assertEqual(self.call(f"{base}/health", token=None)[0], 401)
        self.assertEqual(self.call(f"{base}/health", token="b" * 32)[0], 401)
        self.assertEqual(self.call(f"{base}/health")[0], 200)

    def test_http_refuses_a_path_outside_the_data_root(self) -> None:
        _, base = self.serve()
        outside = str(self.base.parent / "outside")
        status, body = self.call(f"{base}/run", body={"program": "sample", "args": [f"--game={outside}"]})
        self.assertEqual(status, 400)
        self.assertIn("outside the data root", body)

    def test_http_answers_mcp(self) -> None:
        _, base = self.serve()
        status, body = self.call(f"{base}/mcp?program=sample", body={
            "jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {},
        })
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["serverInfo"]["name"], "vigo-host-runner")


if __name__ == "__main__":
    unittest.main()
