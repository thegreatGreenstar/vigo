#!/usr/bin/env python3
"""Run a macOS-only program, its logs and its MCP server, for Vigo's agent.

The agent runs in a Linux container, so a program that only the Mac can run (an
emulator rendering on Metal) is out of its reach.  This service is the one door
to such a program, and nothing else:

    POST /run     argv, no shell, with a path that must stay in the data root
    GET  /log     a file under that data root, read-only
    POST /mcp     MCP over HTTP: the program's own MCP, plus `<name>_run` and
                  (when the program has no MCP) `<name>_log` made here

A program is a JSON entry in the config, not code.  A second one (Eden, another
emulator) is a config change.  A program with `steps` instead of `binary` runs
one named command from a fixed table, so a build takes no argument from the
agent at all:

    {
      "bind": "127.0.0.1",
      "port": 4099,
      "tokenFile": "~/.config/vigo/host-runner.token",
      "programs": {
        "scarletnx": {
          "binary": "/Applications/ScarletNX.app/Contents/MacOS/scarletnx",
          "cwd": "~/Library/Application Support/scarletnx",
          "dataRoot": "~/Library/Application Support/scarletnx",
          "logFile": "state/last-run.log",
          "mcp": {
            "command": ["/Applications/ScarletNX.app/Contents/MacOS/scarletnx",
                        "--mcp", "--repo", "~/Projects/Emulation/ScarletNX",
                        "--data", "~/Library/Application Support/scarletnx"],
            "tools": ["scan_log", "read_log"]
          }
        },
        "scarletnx-build": {
          "cwd": "~/Projects/Emulation/ScarletNX",
          "dataRoot": "~/Projects/Emulation/ScarletNX/build",
          "timeout": 1800,
          "steps": {
            "configure": ["cmake", "--preset", "macos",
                          "-DSNX_MOLTENVK=~/Projects/Emulation/ScarletNX/.cache/moltenvk-macos"],
            "build": ["cmake", "--build", "--preset", "macos"],
            "test": ["ctest", "--test-dir", "build/macos", "--output-on-failure"],
            "package": ["cpack", "--config", "build/macos/CPackConfig.cmake",
                        "-B", "~/Library/Caches/vigo-host-build"],
            "install": ["ditto", "-x", "-k",
                        "~/Library/Caches/vigo-host-build/ScarletNX-macos-arm64.zip",
                        "~/.local/share/vigo-built"]
          }
        }
      }
    }

The contract is narrow on purpose.  An argument may name a path only under the
program's data root.  A program that loads a native module from the path it is
given (ScarletNX dlopens `recompiled/<id>/<id>_code.so`) would otherwise let the
agent run its own code as the Mac's user.  The MCP child is limited to the tool
names the config lists, so a writer (edenprobe creates a case in a checkout) is
out unless the host's owner asks for it.

It listens on loopback and needs a bearer token.  The agent reaches it through
the podman machine's host gateway (`host.containers.internal`), and holds the
same token as a podman secret, so nothing on the tailnet can use it.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

VERSION = "1.1"
MOST_ARGS = 128
MOST_ARG = 8192
MOST_BODY = 1024 * 1024
MOST_OUTPUT = 256 * 1024
MOST_LOG = 4 * 1024 * 1024
DEFAULT_TAIL = 200
DEFAULT_TIMEOUT = 60
MOST_TIMEOUT = 3600
MCP_TIMEOUT = 180.0

# The PATH a program and its MCP child get: what the Mac's own user has without
# a shell profile, and where Homebrew's cmake, ninja and ctest live.
RUN_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"

# What a program's entry may say.  An unknown key stops the runner and names
# itself, like the bot's settings.
PROGRAM_KEYS = {"binary", "steps", "cwd", "dataRoot", "logFile", "mcp", "environment", "timeout"}
MCP_KEYS = {"command", "tools"}
CONFIG_KEYS = {"bind", "port", "tokenFile", "programs"}


class ConfigError(Exception):
    """The config cannot be used, and the message says why."""


def _path(value: object, where: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} is not a path")
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        raise ConfigError(f"{where} is not absolute: {value}")
    return path


def _expand(part: str) -> str:
    """An argument with a leading `~`, or a `~` after the first `=`, expanded.

    A step has no shell, so `-Dcache=~/x` would otherwise reach cmake as a
    literal tilde.  The rule matches the one the MCP command gets.
    """
    if part.startswith("~"):
        return str(Path(os.path.expanduser(part)))
    flag, equal, value = part.partition("=")
    if equal and value.startswith("~"):
        return f"{flag}={os.path.expanduser(value)}"
    return part


def _resolves(command: str) -> bool:
    """Whether a step's command can be started: a file, or a name on RUN_PATH."""
    if command.startswith("~") or os.sep in command:
        path = Path(os.path.expanduser(command))
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(command, path=RUN_PATH) is not None


class Program:
    """One program the runner may start, and the MCP it may speak to."""

    def __init__(self, name: str, raw: object) -> None:
        self.name = name
        if not isinstance(raw, dict):
            raise ConfigError(f"programs.{name} is not an object")
        unknown = set(raw) - PROGRAM_KEYS
        if unknown:
            raise ConfigError(f"programs.{name} has the unknown key {sorted(unknown)[0]!r}")
        binary = raw.get("binary")
        steps = raw.get("steps")
        if binary is not None and steps is not None:
            raise ConfigError(f"programs.{name} has both 'binary' and 'steps'")
        if binary is None and steps is None:
            raise ConfigError(f"programs.{name} has neither 'binary' nor 'steps'")
        self.binary: Path | None = (_path(binary, f"programs.{name}.binary")
                                    if binary is not None else None)
        self.steps = self._steps(steps, f"programs.{name}.steps") if steps is not None else {}
        timeout = raw.get("timeout", DEFAULT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ConfigError(f"programs.{name}.timeout is not a positive number of seconds")
        self.timeout = float(min(timeout, MOST_TIMEOUT))
        self.cwd = _path(raw.get("cwd"), f"programs.{name}.cwd")
        root = _path(raw.get("dataRoot"), f"programs.{name}.dataRoot")
        self.data_root = Path(os.path.realpath(root))
        log_file = raw.get("logFile", "")
        if log_file and not isinstance(log_file, str):
            raise ConfigError(f"programs.{name}.logFile is not a string")
        self.log_file = self._inside(log_file, f"programs.{name}.logFile") if log_file else None
        environment = raw.get("environment", {})
        if not isinstance(environment, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items()
        ):
            raise ConfigError(f"programs.{name}.environment is not an object of strings")
        self.environment = dict(environment)
        mcp = raw.get("mcp")
        self.mcp_command: list[str] = []
        self.mcp_tools: list[str] | None = None
        if mcp is not None:
            if not isinstance(mcp, dict):
                raise ConfigError(f"programs.{name}.mcp is not an object")
            unknown = set(mcp) - MCP_KEYS
            if unknown:
                raise ConfigError(f"programs.{name}.mcp has the unknown key {sorted(unknown)[0]!r}")
            command = mcp.get("command")
            if (not isinstance(command, list) or not command
                    or any(not isinstance(part, str) or not part for part in command)):
                raise ConfigError(f"programs.{name}.mcp.command is not a list of strings")
            self.mcp_command = [_expand(part) for part in command]
            tools = mcp.get("tools")
            if tools is not None:
                if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
                    raise ConfigError(f"programs.{name}.mcp.tools is not a list of strings")
                self.mcp_tools = list(tools)

    def _inside(self, value: str, where: str) -> Path:
        """A path that must stay under the data root, the only place it may read."""
        path = Path(os.path.expanduser(value))
        if not path.is_absolute():
            path = self.data_root / path
        real = Path(os.path.realpath(path))
        if real != self.data_root and self.data_root not in real.parents:
            raise ConfigError(f"{where} is outside the data root: {value}")
        return real

    @staticmethod
    def _steps(raw: object, where: str) -> dict[str, list[str]]:
        """The fixed commands a step program may run, and nothing else."""
        if not isinstance(raw, dict) or not raw:
            raise ConfigError(f"{where} is not an object with one step")
        steps: dict[str, list[str]] = {}
        for name, argv in raw.items():
            if not isinstance(name, str) or not name.strip() or any(c in name for c in "\0\n\r"):
                raise ConfigError(f"{where} has a step name that is not a word")
            if (not isinstance(argv, list) or not argv
                    or any(not isinstance(part, str) or not part for part in argv)):
                raise ConfigError(f"{where}.{name} is not a list of strings")
            if any("\0" in part or "\n" in part or "\r" in part for part in argv):
                raise ConfigError(f"{where}.{name} holds a control character")
            steps[name] = [_expand(part) for part in argv]
        return steps

    @property
    def available(self) -> bool:
        if self.binary is not None:
            return self.binary.is_file() and os.access(self.binary, os.X_OK)
        return all(_resolves(argv[0]) for argv in self.steps.values())


class Config:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ConfigError(f"{path}: cannot be read ({error.strerror})") from error
        except json.JSONDecodeError as error:
            raise ConfigError(f"{path}: not JSON ({error.msg} at line {error.lineno})") from error
        if not isinstance(raw, dict):
            raise ConfigError(f"{path}: not a JSON object")
        unknown = set(raw) - CONFIG_KEYS
        if unknown:
            raise ConfigError(f"{path}: the unknown key {sorted(unknown)[0]!r}")
        bind = raw.get("bind", "127.0.0.1")
        if not isinstance(bind, str) or not bind:
            raise ConfigError(f"{path}: 'bind' is not an address")
        self.bind = bind
        port = raw.get("port", 4099)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError(f"{path}: 'port' is not a port")
        self.port = port
        self.token_file = _path(raw.get("tokenFile"), f"{path}: 'tokenFile'")
        programs = raw.get("programs")
        if not isinstance(programs, dict) or not programs:
            raise ConfigError(f"{path}: 'programs' is not an object with one program")
        self.programs = {name: Program(name, entry) for name, entry in programs.items()}
        try:
            self.token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ConfigError(f"{self.token_file}: cannot be read ({error.strerror})") from error
        if len(self.token) < 16:
            raise ConfigError(f"{self.token_file}: the token is too short")


def looks_like_path(value: str) -> bool:
    """Whether an argument names a path.  A Windows path has a backslash or a drive letter."""
    if value.startswith(("/", "~")) or "/" in value or os.sep in value:
        return True
    return len(value) > 1 and value[1] == ":"


def rooted(program: Program, value: str) -> bool:
    """Whether a path an argument names stays inside the program's data root."""
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        path = program.cwd / path
    real = os.path.realpath(path)
    return real == str(program.data_root) or real.startswith(str(program.data_root) + os.sep)


def check_args(program: Program, args: object) -> list[str]:
    """The argv to pass, or ValueError with the argument that may not pass."""
    if not isinstance(args, list) or not args:
        raise ValueError("args is not a non-empty list")
    if len(args) > MOST_ARGS:
        raise ValueError(f"args holds more than {MOST_ARGS} entries")
    checked: list[str] = []
    for index, arg in enumerate(args):
        if not isinstance(arg, str):
            raise ValueError(f"args[{index}] is not a string")
        if not arg or len(arg) > MOST_ARG:
            raise ValueError(f"args[{index}] is empty or longer than {MOST_ARG}")
        if "\0" in arg or "\n" in arg or "\r" in arg:
            raise ValueError(f"args[{index}] holds a control character")
        flag, equal, value = arg.partition("=")
        if equal:
            if looks_like_path(value) and not rooted(program, value):
                raise ValueError(f"args[{index}] names a path outside the data root: {value}")
            if flag == "--data" and os.path.realpath(os.path.expanduser(value)) != str(program.data_root):
                raise ValueError("--data must be the program's own data root")
        elif not flag.startswith("-"):
            if looks_like_path(arg) and not rooted(program, arg):
                raise ValueError(f"args[{index}] names a path outside the data root: {arg}")
        checked.append(arg)
    return checked


def tail_of(path: Path, tail: int) -> str:
    """The last `tail` lines of a file, read from its end."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        start = max(0, size - MOST_LOG)
        handle.seek(start)
        raw = handle.read(MOST_LOG)
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    return "\n".join(lines[-max(1, min(tail, 10000)):])


def inside(program: Program, value: object, where: str) -> Path:
    """A log path under the program's data root, symlinks resolved."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where} is not a path")
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        path = program.data_root / path
    real = Path(os.path.realpath(path))
    if real != program.data_root and program.data_root not in real.parents:
        raise ValueError(f"{where} is outside the data root: {value}")
    return real


class McpChild:
    """One long-lived MCP server over stdio, one request at a time."""

    def __init__(self, program: Program) -> None:
        self.program = program
        self.lock = threading.Lock()
        self.process: subprocess.Popen[bytes] | None = None
        self.next_id = 0
        self.tools: list[dict] | None = None

    def _start(self) -> None:
        self.process = subprocess.Popen(
            self.program.mcp_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(self.program.cwd),
            env=self._env(),
        )
        answer = self._exchange("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "vigo-host-runner", "version": VERSION},
        })
        if "error" in answer:
            raise RuntimeError(f"the MCP server refused initialize ({answer['error']})")
        self._notify("notifications/initialized", {})

    def _env(self) -> dict[str, str]:
        env = {
            "PATH": RUN_PATH,
            "HOME": str(Path.home()),
            "LANG": "C.UTF-8",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        }
        env.update(self.program.environment)
        return env

    def _write(self, body: dict) -> None:
        assert self.process and self.process.stdin
        self.process.stdin.write((json.dumps(body) + "\n").encode())
        self.process.stdin.flush()

    def _read(self, timeout: float) -> dict:
        assert self.process and self.process.stdout
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError("the MCP server did not answer in time")
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("the MCP server closed its output")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and "id" in message:
                return message
            # a notification or a request the server makes: nothing to do here

    def _exchange(self, method: str, params: dict, timeout: float = MCP_TIMEOUT) -> dict:
        self.next_id += 1
        self._write({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        return self._read(timeout)

    def _notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _ensure(self) -> None:
        if self.process is None or self.process.poll() is not None:
            self._stop()
            self._start()

    def _allowed(self) -> list[dict]:
        answer = self._exchange("tools/list", {})
        if "error" in answer:
            raise RuntimeError(f"the MCP server refused tools/list ({answer['error']})")
        listed = (answer.get("result") or {}).get("tools") or []
        tools = [tool for tool in listed if isinstance(tool, dict) and isinstance(tool.get("name"), str)]
        if self.program.mcp_tools is not None:
            keep = set(self.program.mcp_tools)
            tools = [tool for tool in tools if tool["name"] in keep]
        return tools

    def list_tools(self) -> list[dict]:
        with self.lock:
            self._ensure()
            if self.tools is None:
                self.tools = self._allowed()
            return list(self.tools)

    def call(self, name: str, arguments: dict) -> dict:
        with self.lock:
            self._ensure()
            if self.tools is None:
                self.tools = self._allowed()
            if not any(tool["name"] == name for tool in self.tools):
                raise ValueError(f"the MCP server does not offer {name!r}")
            answer = self._exchange("tools/call", {"name": name, "arguments": arguments})
            if "error" in answer:
                raise RuntimeError(str(answer["error"]))
            return answer.get("result") or {}

    def close(self) -> None:
        with self.lock:
            self._stop()
            self.tools = None

    def _stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


class Runner:
    """What the HTTP handlers share: the config and one MCP child per program."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.children = {name: McpChild(program) for name, program in config.programs.items()}

    def close(self) -> None:
        for child in self.children.values():
            child.close()

    def program(self, name: object) -> Program:
        if not isinstance(name, str) or name not in self.config.programs:
            raise ValueError(f"no program named {name!r}")
        return self.config.programs[name]

    def run(self, body: dict) -> dict:
        program = self.program(body.get("program"))
        if not program.available:
            missing = program.binary if program.binary is not None else "a step command"
            raise ValueError(f"the program {program.name} is missing: {missing}")
        step: str | None = None
        if program.steps:
            if body.get("args") is not None:
                raise ValueError(f"{program.name} runs a named step and takes no args")
            step = body.get("step")
            if not isinstance(step, str) or step not in program.steps:
                raise ValueError(f"{program.name} has no step {step!r}: "
                                 + ", ".join(sorted(program.steps)))
            command = list(program.steps[step])
        else:
            command = [str(program.binary), *check_args(program, body.get("args"))]
        timeout = body.get("timeout", body.get("timeout_seconds", program.timeout))
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout is not a positive number of seconds")
        timeout = min(float(timeout), float(MOST_TIMEOUT))
        started = time.monotonic()
        env = {
            "PATH": RUN_PATH,
            "HOME": str(Path.home()),
            "LANG": "C.UTF-8",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        }
        env.update(program.environment)
        timed_out = False
        try:
            completed = subprocess.run(
                command, cwd=str(program.cwd), env=env,
                capture_output=True, timeout=timeout,
            )
            code = completed.returncode
            out = completed.stdout
            err = completed.stderr
        except subprocess.TimeoutExpired as expired:
            timed_out = True
            code = None
            out = expired.stdout or b""
            err = expired.stderr or b""
        result: dict = {
            "program": program.name,
            "code": code,
            "timed_out": timed_out,
            "seconds": round(time.monotonic() - started, 3),
            "stdout": out[-MOST_OUTPUT:].decode("utf-8", "replace"),
            "stderr": err[-MOST_OUTPUT:].decode("utf-8", "replace"),
        }
        if step is not None:
            result["step"] = step
        if program.log_file and program.log_file.is_file():
            result["log"] = {
                "path": str(program.log_file),
                "tail": tail_of(program.log_file, DEFAULT_TAIL),
            }
        return result

    def log(self, program: Program, value: object, tail: object) -> str:
        path = inside(program, value, "path")
        if not path.is_file():
            raise FileNotFoundError(f"{value} is not a file")
        if isinstance(tail, bool) or not isinstance(tail, int):
            tail = DEFAULT_TAIL
        return tail_of(path, tail)

    def mcp(self, program: Program, request: dict) -> dict | None:
        method = request.get("method")
        ident = request.get("id")
        if not isinstance(method, str):
            return _error(ident, -32600, "not a JSON-RPC request")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        if method == "initialize":
            return _result(ident, {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "vigo-host-runner", "version": VERSION},
            })
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return _result(ident, {})
        if method == "tools/list":
            return _result(ident, {"tools": self._tools(program)})
        if method == "tools/call":
            return self._call(program, ident, params)
        return _error(ident, -32601, f"no method {method!r}")

    def _tools(self, program: Program) -> list[dict]:
        if program.steps:
            run = {
                "name": f"{program.name}_run",
                "description": (f"Run one step of {program.name} on the Mac ({', '.join(sorted(program.steps))}) "
                                "and return its exit code and output. A step takes no arguments."),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string", "enum": sorted(program.steps)},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MOST_TIMEOUT},
                    },
                    "required": ["step"],
                },
            }
        else:
            run = {
                "name": f"{program.name}_run",
                "description": (f"Run {program.name} on the Mac with these arguments and return its exit code, "
                                "output and the tail of its log. A path argument must stay under the program's "
                                "data root."),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "args": {"type": "array", "items": {"type": "string"}},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MOST_TIMEOUT},
                    },
                    "required": ["args"],
                },
            }
        tools = [run]
        # A program with an MCP of its own already offers its log tools; a
        # second one here would only give the model two names for one read.
        if not program.mcp_command:
            tools.append({
                "name": f"{program.name}_log",
                "description": f"Read the tail of a log under {program.name}'s data root.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "tail_lines": {"type": "integer", "minimum": 1, "maximum": 10000},
                    },
                    "required": ["path"],
                },
            })
        if program.mcp_command:
            tools.extend(self.children[program.name].list_tools())
        return tools

    def _call(self, program: Program, ident: object, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _error(ident, -32602, "tools/call needs a name and arguments")
        try:
            if name == f"{program.name}_run":
                ran = self.run({"program": program.name, **arguments})
                return _result(ident, _text(json.dumps(ran, indent=2)))
            if name == f"{program.name}_log":
                text = self.log(program, arguments.get("path"), arguments.get("tail_lines", DEFAULT_TAIL))
                return _result(ident, _text(text))
            if not program.mcp_command:
                return _error(ident, -32602, f"no tool {name!r}")
            result = self.children[program.name].call(name, arguments)
            return _result(ident, result)
        except (ValueError, FileNotFoundError, RuntimeError, OSError) as error:
            return _result(ident, _text(str(error), failed=True))
        except subprocess.SubprocessError as error:
            return _result(ident, _text(str(error), failed=True))


def _result(ident: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def _error(ident: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def _text(text: str, failed: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": failed}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    server: HttpServer

    def log_message(self, *args: object) -> None:  # the log is the runner's own
        pass

    # --- plumbing -----------------------------------------------------------
    def _send(self, status: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value).encode())

    def _fail(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _allowed(self) -> bool:
        given = (self.headers.get("Authorization") or "").encode()
        return hmac.compare_digest(given, f"Bearer {self.server.config.token}".encode())

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MOST_BODY:
            raise ValueError("the body is missing or too large")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"not JSON ({error.msg})") from error
        if not isinstance(value, dict):
            raise ValueError("the body is not a JSON object")
        return value

    def _program(self) -> Program:
        query = parse_qs(urlsplit(self.path).query)
        return self.server.runner.program((query.get("program") or [""])[0])

    # --- routes -------------------------------------------------------------
    def do_GET(self) -> None:
        if not self._allowed():
            return self._fail(401, "the token is missing or wrong")
        path = urlsplit(self.path).path
        if path == "/health":
            return self._json(200, {
                "ok": True,
                "version": VERSION,
                "programs": {name: program.available
                             for name, program in self.server.runner.config.programs.items()},
            })
        if path == "/log":
            query = parse_qs(urlsplit(self.path).query)
            try:
                program = self.server.runner.program((query.get("program") or [""])[0])
                text = self.server.runner.log(
                    program, (query.get("path") or [""])[0],
                    int((query.get("tail") or [str(DEFAULT_TAIL)])[0]),
                )
            except ValueError as error:
                return self._fail(400, str(error))
            except FileNotFoundError as error:
                return self._fail(404, str(error))
            return self._send(200, text.encode(), "text/plain; charset=utf-8")
        if path == "/mcp":
            self.send_response(405)
            self.send_header("Allow", "POST, DELETE")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._fail(404, "no route")

    def do_POST(self) -> None:
        if not self._allowed():
            return self._fail(401, "the token is missing or wrong")
        path = urlsplit(self.path).path
        if path == "/run":
            try:
                result = self.server.runner.run(self._body())
            except ValueError as error:
                return self._fail(400, str(error))
            except OSError as error:
                return self._fail(500, str(error))
            return self._json(200, result)
        if path == "/mcp":
            try:
                program = self._program()
                request = self._body()
                answer = self.server.runner.mcp(program, request)
            except ValueError as error:
                return self._fail(400, str(error))
            if answer is None:  # a notification has no answer
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._json(200, answer)
        self._fail(404, "no route")

    def do_DELETE(self) -> None:
        if not self._allowed():
            return self._fail(401, "the token is missing or wrong")
        if urlsplit(self.path).path == "/mcp":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._fail(404, "no route")


class HttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], runner: Runner) -> None:
        self.runner = runner
        self.config = runner.config
        super().__init__(address, Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a macOS-only program for Vigo's agent.")
    parser.add_argument("--config", default=str(Path.home() / ".config" / "vigo" / "host-runner.json"))
    parser.add_argument("--check", action="store_true", help="read the config, print it, and exit")
    args = parser.parse_args(argv)
    try:
        config = Config(Path(os.path.expanduser(args.config)))
    except ConfigError as error:
        print(f"host-runner: {error}", file=sys.stderr)
        return 2
    if args.check:
        print(json.dumps({
            "bind": config.bind,
            "port": config.port,
            "programs": {name: {"available": program.available, "mcp": bool(program.mcp_command)}
                         for name, program in config.programs.items()},
        }, indent=2))
        return 0
    runner = Runner(config)
    server = HttpServer((config.bind, config.port), runner)
    print(f"host-runner: {config.bind}:{config.port}, programs: "
          + ", ".join(f"{name}{'' if program.available else ' (missing)'}"
                      for name, program in config.programs.items()), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runner.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
