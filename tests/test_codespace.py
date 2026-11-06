"""`tests/ci-codespace.sh` drives a codespace, without a codespace.

The script is the Linux gate, so its order matters: a tree that never reaches
the codespace, or a codespace that outlives a failed run, wastes quota and
hides the failure.  `gh` is stubbed on PATH here, so the test needs no network
and no token.  The tree it copies is this checkout, which is why the test
always passes `--base`: the script would otherwise ask `origin`.
"""

import os
import pathlib
import stat
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "ci-codespace.sh"

# One `gh` for every subcommand the script uses.  The subcommand is `$1 $2`,
# except codespace's third word which names what to do with it.
GH = """#!/bin/sh
printf '%s\\n' "$*" >>"$GH_LOG"
case "$1 $2" in
    "auth status") exit 0 ;;
    "codespace list") exit "${GH_LIST_EXIT:-0}" ;;
    "codespace create")
        echo "progress on stderr" >&2
        echo "vigo-ci-abc123"
        exit 0 ;;
    "codespace view") echo "Available" ;;
    "codespace cp") cp "$5" "$GH_COPY_DIR/tree.tgz"; exit 0 ;;
    "codespace ssh")
        echo "Linux · Python 3.12: OK (Ran 42 tests in 1.500s)"
        exit "${GH_SSH_EXIT:-0}" ;;
    "codespace stop") exit 0 ;;
    "codespace delete") exit 0 ;;
    *) echo "stub gh: unsupported: $*" >&2; exit 2 ;;
esac
"""


class Codespace(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.stub = pathlib.Path(self.directory.name)
        self.log = self.stub / "gh.log"
        self.log.touch()
        (self.stub / "copy").mkdir()
        gh = self.stub / "gh"
        gh.write_text(GH, encoding="utf-8")
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def run_script(self, *arguments: str, **environment: str) -> subprocess.CompletedProcess:
        environ = dict(os.environ)
        environ["PATH"] = f"{self.stub}:{environ['PATH']}"
        environ["GH_LOG"] = str(self.log)
        environ["GH_COPY_DIR"] = str(self.stub / "copy")
        environ.update(environment)
        return subprocess.run(
            ["sh", str(SCRIPT), "--base", "main", *arguments],
            cwd=ROOT, env=environ, capture_output=True, text=True, timeout=180,
        )

    def calls(self) -> list[str]:
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def subcommands(self) -> list[str]:
        """The verb of each call: `status`, `list`, `create`, `delete`…"""
        return [call.split()[1] for call in self.calls()]

    def test_a_good_run_creates_copies_runs_and_deletes(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(
            self.subcommands(), ["status", "list", "create", "view", "cp", "ssh", "delete"],
        )
        self.assertIn("Linux · Python 3.12: OK", result.stdout)
        self.assertIn("no secrets: OK", result.stdout)
        self.assertIn("codespace vigo-ci-abc123: OK", result.stdout)
        self.assertIn("macOS and Windows", result.stdout)
        self.assertTrue(any("--no-git" in call for call in self.calls()))

    def test_the_codespace_is_deleted_when_the_run_fails(self) -> None:
        result = self.run_script(GH_SSH_EXIT="3")
        self.assertEqual(result.returncode, 3)
        self.assertIn("deleted vigo-ci-abc123", result.stdout)
        self.assertIn("FAILED (exit 3)", result.stdout)
        self.assertEqual(self.subcommands()[-1], "delete")

    def test_keep_stops_it_instead_and_says_how_to_delete_it(self) -> None:
        result = self.run_script("--keep")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.subcommands()[-1], "stop")
        self.assertNotIn("delete", self.subcommands())
        self.assertIn("gh codespace delete -c vigo-ci-abc123", result.stdout)

    def test_a_token_without_the_scope_stops_before_anything_is_created(self) -> None:
        result = self.run_script(GH_LIST_EXIT="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("gh auth refresh -h github.com -s codespace", result.stdout + result.stderr)
        self.assertEqual(self.subcommands(), ["status", "list"])

    def test_the_tree_that_is_copied_has_no_settings_file(self) -> None:
        """`.gitignore` keeps the token out, and the copy follows `.gitignore`."""
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        copy = self.stub / "copy" / "tree.tgz"
        self.assertTrue(copy.exists(), "nothing was copied to the codespace")
        listed = subprocess.run(
            ["tar", "-tzf", str(copy)], capture_output=True, text=True, timeout=60,
        ).stdout.splitlines()
        self.assertIn("tests/in-codespace.sh", listed)
        self.assertNotIn(".git", listed)
        self.assertEqual([name for name in listed if name.startswith(".git/")], [])
        for absent in ("vigo.json", ".state/state.json", ".venv/", "__pycache__/"):
            self.assertNotIn(absent, listed)


if __name__ == "__main__":
    unittest.main()
