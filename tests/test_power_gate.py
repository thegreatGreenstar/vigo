"""The local gate runs on a desktop, or on a laptop on AC at 80% or more.

`tests/ci-local.sh` starts the podman VM, which heats a laptop, so it asks
`tests/power_gate.py` first.  These tests cover the two readings of the machine,
the verdict, and the script's refusal to start a suite when the verdict is no.
"""

from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest

from tests import power_gate

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "ci-local.sh"

PMSET_AC = """Now drawing from 'AC Power'
 -InternalBattery-0 (id=1234567)\t96%; charged; 0:00 remaining present: true
"""
PMSET_BATTERY = """Now drawing from 'Battery Power'
 -InternalBattery-0 (id=1234567)\t100%; discharging; 4:12 remaining present: true
"""


def supply(root: pathlib.Path, name: str, **files: str | int) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    for key, value in files.items():
        (directory / key).write_text(str(value), encoding="ascii")


class PMSet(unittest.TestCase):
    def test_ac_power_reads_the_percentage(self) -> None:
        self.assertEqual(power_gate.parse_pmset(PMSET_AC), (True, 96))

    def test_battery_power_is_not_ac(self) -> None:
        self.assertEqual(power_gate.parse_pmset(PMSET_BATTERY), (False, 100))

    def test_a_desktop_has_no_battery(self) -> None:
        self.assertIsNone(power_gate.parse_pmset("Now drawing from 'AC Power'\n"))

    def test_a_missing_source_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            power_gate.parse_pmset(" -InternalBattery-0\t96%; charged\n")

    def test_battery_power_without_a_percentage_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            power_gate.parse_pmset("Now drawing from 'Battery Power'\n")


class SYSFS(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)

    def test_a_charging_battery_on_ac(self) -> None:
        supply(self.root, "BAT0", type="Battery", scope="System",
               capacity=85, status="Charging", present=1)
        supply(self.root, "AC", type="Mains", online=1)
        self.assertEqual(power_gate.parse_sysfs(self.root), (True, 85))

    def test_a_discharging_battery_is_not_ac(self) -> None:
        supply(self.root, "BAT0", type="Battery", scope="System",
               capacity=85, status="Discharging", present=1)
        supply(self.root, "AC", type="Mains", online=0)
        self.assertEqual(power_gate.parse_sysfs(self.root), (False, 85))

    def test_no_battery_is_a_desktop(self) -> None:
        supply(self.root, "AC", type="Mains", online=1)
        self.assertIsNone(power_gate.parse_sysfs(self.root))

    def test_a_peripheral_battery_does_not_make_a_desktop_a_laptop(self) -> None:
        supply(self.root, "hidpp_battery_0", type="Battery", scope="Device",
               capacity=40, status="Discharging", present=1)
        supply(self.root, "AC", type="Mains", online=1)
        self.assertIsNone(power_gate.parse_sysfs(self.root))

    def test_two_batteries_take_the_lowest(self) -> None:
        supply(self.root, "BAT0", type="Battery", scope="System",
               capacity=95, status="Charging", present=1)
        supply(self.root, "BAT1", type="Battery", scope="System",
               capacity=72, status="Charging", present=1)
        self.assertEqual(power_gate.parse_sysfs(self.root), (True, 72))

    def test_ac_comes_from_the_battery_status_when_there_is_no_ac_node(self) -> None:
        supply(self.root, "BAT0", type="Battery", scope="System",
               capacity=85, status="Full", present=1)
        self.assertEqual(power_gate.parse_sysfs(self.root), (True, 85))

    def test_an_unknown_status_is_an_error(self) -> None:
        supply(self.root, "BAT0", type="Battery", scope="System",
               capacity=85, status="Unknown", present=1)
        with self.assertRaises(ValueError):
            power_gate.parse_sysfs(self.root)


class Verdict(unittest.TestCase):
    def test_a_desktop_always_runs(self) -> None:
        allowed, reason = power_gate.verdict(None)
        self.assertTrue(allowed)
        self.assertIn("desktop", reason)

    def test_the_battery_may_be_exactly_eighty_percent(self) -> None:
        self.assertTrue(power_gate.verdict((True, 80))[0])

    def test_below_eighty_percent_is_refused(self) -> None:
        allowed, reason = power_gate.verdict((True, 79))
        self.assertFalse(allowed)
        self.assertIn("80", reason)

    def test_battery_power_is_refused_however_full(self) -> None:
        allowed, reason = power_gate.verdict((False, 100))
        self.assertFalse(allowed)
        self.assertIn("AC", reason)


@unittest.skipIf(sys.platform == "win32", "the gate is a POSIX shell script")
class Script(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = pathlib.Path(temporary.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.podman = self.dir / "podman.log"
        self._stub("python3", """#!/bin/sh
if [ "${1:-}" = "tests/power_gate.py" ]; then
    printf '%s\\n' "$POWER_TEXT"
    exit "$POWER_EXIT"
fi
exit 0
""")
        for version in ("python3.12", "python3.13", "python3.14"):
            self._stub(version, """#!/bin/sh
case "$*" in
    *unittest*) echo "Ran 1 test in 0.000s" ;;
esac
exit 0
""")
        self._stub("podman", """#!/bin/sh
[ "$1" = info ] && exit 1
printf '%s\\n' "$*" >>"$PODMAN_LOG"
exit 0
""")

    def _stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def _run(self, exit_code: int, text: str) -> subprocess.CompletedProcess[str]:
        environ = dict(os.environ)
        # The parallel runner shares os.environ with tests that clear it, so a
        # missing PATH here is a race in the runner and not a refusal to run.
        environ["PATH"] = f"{self.bin}:{environ.get('PATH', '')}"
        environ["POWER_EXIT"] = str(exit_code)
        environ["POWER_TEXT"] = text
        environ["PODMAN_LOG"] = str(self.podman)
        return subprocess.run(
            ["sh", str(SCRIPT)],
            cwd=ROOT,
            env=environ,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_a_refused_laptop_starts_no_suite(self) -> None:
        result = self._run(1, "on battery at 100%: the local gate needs AC power")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("skipped:", result.stdout)
        self.assertFalse(self.podman.exists(), "podman ran after the gate refused")

    def test_an_unreadable_state_fails_instead_of_skipping(self) -> None:
        result = self._run(2, "cannot read the power state: no power supply")
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read the power state", result.stdout)
        self.assertNotIn("skipped:", result.stdout)
        self.assertFalse(self.podman.exists(), "podman ran after the probe failed")

    def test_an_allowed_machine_runs_the_gate(self) -> None:
        result = self._run(0, "on AC power at 96%")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("power: on AC power at 96%", result.stdout)
        self.assertNotIn("skipped:", result.stdout)
        self.assertIn("host · Python 3.12: OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
