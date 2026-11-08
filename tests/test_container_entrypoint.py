import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENTRYPOINT = Path(__file__).resolve().parent.parent / "deploy" / "container" / "vigo-entrypoint.py"


def load_entrypoint():
    spec = importlib.util.spec_from_file_location("vigo_container_entrypoint", ENTRYPOINT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SecretTest(unittest.TestCase):
    def setUp(self):
        self.entry = load_entrypoint()

    def test_a_missing_optional_secret_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "no-such-secret"
            self.assertEqual(self.entry.read_secret(str(path), required=False), "")

    def test_a_missing_required_secret_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "no-such-secret"
            with self.assertRaises(SystemExit):
                self.entry.read_secret(str(path), required=True)

    def test_an_empty_required_secret_stops(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as handle:
            handle.write("\n")
            handle.flush()
            with self.assertRaises(SystemExit):
                self.entry.read_secret(handle.name, required=True)


class EnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.entry = load_entrypoint()

    def test_the_opencode_password_and_gh_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            env = self.entry.environment("the-password", "")
        self.assertEqual(env["OPENCODE_PASSWORD"], "the-password")
        self.assertNotIn("GH_TOKEN", env)
        self.assertEqual(env["GH_PROMPT_DISABLED"], "1")
        self.assertEqual(env["GH_CONFIG_DIR"], "/home/vigo/.config/gh")

    def test_a_gh_token_is_exported_when_there_is_one(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            env = self.entry.environment("the-password", "the-token")
        self.assertEqual(env["GH_TOKEN"], "the-token")
