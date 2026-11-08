import contextlib
import importlib.util
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ENTRYPOINT = Path(__file__).resolve().parent.parent / "deploy" / "container" / "vigo-entrypoint.py"

_ENV = threading.Lock()  # the environment is one per process, not one per case


@contextlib.contextmanager
def environ(**values):
    """Clear the process environment for a case, one case at a time.

    `mock.patch.dict` changes the environment the whole process shares, and
    `tests/parallel.py` runs cases on threads.  A lock keeps two cases that
    clear it from doing so over each other's assertions.
    """
    with _ENV:
        with mock.patch.dict(os.environ, values, clear=True):
            yield


def load_entrypoint():
    spec = importlib.util.spec_from_file_location("vigo_container_entrypoint", ENTRYPOINT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SecretTest(unittest.TestCase):
    def setUp(self):
        self.entry = load_entrypoint()

    def test_a_file_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_text("from-file\n", encoding="utf-8")
            with environ(SECRET="from-env"):
                self.assertEqual(
                    self.entry.read_secret(str(path), "SECRET", required=True),
                    "from-file",
                )

    def test_the_environment_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing"
            with environ(SECRET="from-env"):
                self.assertEqual(
                    self.entry.read_secret(str(path), "SECRET", required=True),
                    "from-env",
                )

    def test_a_missing_optional_secret_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing"
            with environ():
                self.assertEqual(self.entry.read_secret(str(path), "SECRET", required=False), "")

    def test_a_missing_required_secret_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing"
            with environ():
                with self.assertRaises(SystemExit):
                    self.entry.read_secret(str(path), "SECRET", required=True)


class EnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.entry = load_entrypoint()

    def test_the_opencode_password_and_gh_defaults(self):
        with environ():
            env = self.entry.environment("the-password", "", "")
        self.assertEqual(env["OPENCODE_PASSWORD"], "the-password")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("VIGO_HOST_RUNNER_TOKEN", env)
        self.assertEqual(env["GH_PROMPT_DISABLED"], "1")
        self.assertEqual(env["GH_CONFIG_DIR"], "/home/vigo/.config/gh")

    def test_a_gh_token_is_exported_when_there_is_one(self):
        with environ():
            env = self.entry.environment("the-password", "the-token", "")
        self.assertEqual(env["GH_TOKEN"], "the-token")

    def test_the_host_runner_token_is_exported_when_there_is_one(self):
        with environ():
            env = self.entry.environment("the-password", "", "the-runner-token")
        self.assertEqual(env["VIGO_HOST_RUNNER_TOKEN"], "the-runner-token")


class RoleTest(unittest.TestCase):
    def setUp(self):
        self.entry = load_entrypoint()

    def test_an_unknown_role_stops(self):
        with environ(VIGO_ROLE="other"):
            with self.assertRaises(SystemExit):
                self.entry.main()

    def test_the_daemon_becomes_vigo(self):
        with environ(VIGO_ROLE="daemon"):
            with mock.patch("os.execv", side_effect=SystemExit) as execv:
                with self.assertRaises(SystemExit):
                    self.entry.main()
        self.assertEqual(execv.call_args.args[1][:3], [execv.call_args.args[0], "-m", "vigo"])

    def test_the_agent_becomes_opencode(self):
        with environ(VIGO_ROLE="agent", VIGO_OPENCODE_PASSWORD="pw"):
            with mock.patch("os.execvpe") as execvpe:
                self.entry.main()
        self.assertEqual(execvpe.call_args.args[0], "opencode")
        self.assertIn("serve", execvpe.call_args.args[1])
