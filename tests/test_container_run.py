"""The container launcher starts the podman machine it needs, and says so.

On macOS the machine is a VM and the Homebrew package does not start it at
login, so a unit that only waits for podman never starts after a login.  The
launcher starts it once, then fails loudly if it still cannot answer.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "container" / "run.sh"


@unittest.skipIf(sys.platform == "win32", "the launcher is a POSIX shell script")
class LauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.state = self.dir / "state"
        self.state.mkdir()
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self._stub("uname", 'echo Darwin\n')
        self._stub(
            "podman",
            'case "$1" in\n'
            'info) [ -f "$STATE/up" ] && exit 0 || exit 1 ;;\n'
            'machine) echo "machine start" >> "$STATE/calls";\n'
            '         [ -f "$STATE/fail" ] && exit 1;\n'
            '         touch "$STATE/up"; exit 0 ;;\n'
            'run) echo run ;;\n'
            'esac\n'
            'exit 0\n',
        )

    def _stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def _run(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["sh", str(SCRIPT)],
            env={"HOME": str(self.dir), "PATH": f"{self.bin}:/usr/bin:/bin", "STATE": str(self.state)},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_starts_the_machine_when_it_is_not_answering(self) -> None:
        done = self._run()

        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("not answering; starting it", done.stderr)
        self.assertIn("run", done.stdout)
        self.assertEqual("machine start\n", (self.state / "calls").read_text())

    def test_leaves_a_running_machine_alone(self) -> None:
        (self.state / "up").touch()

        done = self._run()

        self.assertEqual(0, done.returncode, done.stderr)
        self.assertNotIn("starting it", done.stderr)
        self.assertIn("run", done.stdout)
        self.assertFalse((self.state / "calls").exists())

    def test_stops_when_the_machine_will_not_start(self) -> None:
        (self.state / "fail").touch()

        done = self._run()

        self.assertEqual(2, done.returncode)
        self.assertIn("podman is not answering", done.stderr)
        self.assertNotIn("run", done.stdout)
