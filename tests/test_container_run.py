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
INSTALL = ROOT / "deploy" / "container" / "install.sh"


@unittest.skipIf(sys.platform == "win32", "the launcher is a POSIX shell script")
class LauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.state = self.dir / "state"
        self.state.mkdir()
        # The daemon runs a private checkout, not the project copy.
        checkout = self.dir / ".local/share/vigo/checkout"
        (checkout / ".git").mkdir(parents=True)
        subprocess.run(["git", "-C", str(checkout), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(checkout), "remote", "add", "origin",
             "git@example.invalid:fake/repo.git"],
            check=True,
        )
        (self.dir / "Projects").mkdir()
        (self.dir / ".config/vigo").mkdir(parents=True)
        (self.dir / ".config/vigo/vigo.json").write_text("{}\n", encoding="utf-8")
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
            'container) [ -f "$STATE/agent-exists" ] && exit 0 || exit 1 ;;\n'
            'image) if [ "$2" = inspect ]; then cat "$STATE/image-id" 2>/dev/null; fi ;;\n'
            'inspect) case "$*" in\n'
            '         *Image*) cat "$STATE/agent-image" 2>/dev/null ;;\n'
            '         *Running*) cat "$STATE/agent-running" 2>/dev/null ;;\n'
            '         esac ;;\n'
            'rm) case "$*" in\n'
            '    *vigo-opencode*) rm -f "$STATE/agent-exists" "$STATE/agent-running" ;;\n'
            '    esac ;;\n'
            'run) case "$*" in\n'
            '     *"--name vigo-opencode"*)\n'
            '         touch "$STATE/agent-run"\n'
            '         touch "$STATE/agent-exists"; echo true > "$STATE/agent-running"\n'
            '         cp "$STATE/image-id" "$STATE/agent-image" 2>/dev/null\n'
            '         ;;\n'
            '     *) echo run ;;\n'
            '     esac ;;\n'
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

    def test_keeps_the_agent_a_daemon_restart_left_running(self) -> None:
        """An upgrade ends the daemon, and the agent keeps the turn in flight.

        Replacing the agent on the next start would cancel the turn the daemon
        carried over the restart, which is what wedged a channel.
        """
        (self.state / "agent-exists").touch()
        (self.state / "agent-running").write_text("true\n", encoding="utf-8")
        (self.state / "image-id").write_text("sha256:same\n", encoding="utf-8")
        (self.state / "agent-image").write_text("sha256:same\n", encoding="utf-8")

        done = self._run()

        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("run", done.stdout, "the daemon starts")
        self.assertFalse((self.state / "agent-run").exists(),
                         "the agent the restart left is not replaced")

    def test_replaces_the_agent_when_its_image_changed(self) -> None:
        (self.state / "agent-exists").touch()
        (self.state / "agent-running").write_text("true\n", encoding="utf-8")
        (self.state / "image-id").write_text("sha256:new\n", encoding="utf-8")
        (self.state / "agent-image").write_text("sha256:old\n", encoding="utf-8")

        done = self._run()

        self.assertEqual(0, done.returncode, done.stderr)
        self.assertTrue((self.state / "agent-run").exists(),
                        "an agent on an older image is replaced")
        self.assertEqual("sha256:new\n",
                         (self.state / "agent-image").read_text(encoding="utf-8"))


@unittest.skipUnless(sys.platform == "darwin", "the plist is a macOS launchd file")
class InstallTest(unittest.TestCase):
    """The macOS install keeps the address the peer port is published on."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self._stub("uname", "echo Darwin\n")
        self._stub("launchctl", "exit 0\n")
        self._stub("podman", "exit 0\n")
        self._stub("openssl", "echo deadbeef\n")
        checkout = self.home / ".local/share/vigo/checkout"
        (checkout / ".git").mkdir(parents=True)
        subprocess.run(["git", "-C", str(checkout), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(checkout), "remote", "add", "origin",
             "git@example.invalid:fake/repo.git"],
            check=True,
        )
        (self.home / "source").mkdir()
        self.plist = self.home / "Library/LaunchAgents/dev.vigo.container.plist"

    def _stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def _run(self, bind: str = "") -> subprocess.CompletedProcess[str]:
        environ = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "VIGO_SOURCE": str(self.home / "source"),
            "VIGO_INSTALL_DIRTY": "1",
        }
        if bind:
            environ["VIGO_BIND_ADDRESS"] = bind
        return subprocess.run(
            ["sh", str(INSTALL)],
            env=environ,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _bind(self) -> str:
        done = subprocess.run(
            ["plutil", "-extract", "EnvironmentVariables.VIGO_BIND_ADDRESS",
             "raw", "-o", "-", str(self.plist)],
            capture_output=True, text=True,
        )
        return done.stdout.strip() if done.returncode == 0 else ""

    def test_it_writes_the_peer_bind_and_keeps_it_on_a_reinstall(self) -> None:
        done = self._run(bind="100.64.0.5")

        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual("100.64.0.5", self._bind())

        done = self._run()

        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual("100.64.0.5", self._bind(),
                         "a reinstall keeps the address the machine uses")

    def test_no_address_is_set_unless_one_is_asked_for(self) -> None:
        done = self._run()

        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual("", self._bind(), "the launcher defaults to loopback")
