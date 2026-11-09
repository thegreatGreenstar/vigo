"""Say whether the local CI gate may run on this machine now.

`tests/ci-local.sh` starts the podman VM, and that heats a laptop.  GitHub
Actions is free now that the repository is public, so the local run is a
convenience.  It runs on a desktop, which has no battery, or on a laptop that is
on AC power with its battery at 80% or more.  Anything else is refused with the
reason, so a skipped gate is never mistaken for a green one.

Exit 0 means it may run, 3 means the machine said no, and 2 means the state
cannot be read.  Any other status is a fault in the check itself, which must
stop the gate rather than pass for a refusal.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

MINIMUM_PERCENT = 80
REFUSED = 3
SYSFS = pathlib.Path("/sys/class/power_supply")

type Power = tuple[bool, int] | None  # (on AC, battery percent), or a desktop


def parse_pmset(output: str) -> Power:
    """Read `pmset -g batt`; None means a desktop with no battery."""
    source = re.search(r"Now drawing from '([^']+)'", output)
    if source is None:
        raise ValueError("pmset did not name the power source")
    percent = re.search(r"(\d+)%", output)
    if percent is None:
        if source.group(1) == "AC Power" and "Battery" not in output:
            return None
        raise ValueError("pmset did not name the battery percentage")
    return source.group(1) == "AC Power", int(percent.group(1))


def _text(path: pathlib.Path) -> str:
    return path.read_text(encoding="ascii").strip()


def parse_sysfs(root: pathlib.Path = SYSFS) -> Power:
    """Read Linux's power supplies; None means a desktop with no battery."""
    if not root.is_dir():
        return None
    capacities: list[int] = []
    statuses: list[str] = []
    online: list[bool] = []
    for supply in sorted(root.iterdir()):
        kind = _text(supply / "type").lower()
        if kind == "battery":
            scope = supply / "scope"
            if scope.exists() and _text(scope).lower() != "system":
                continue  # a wireless mouse or a controller, not this machine
            present = supply / "present"
            if present.exists() and _text(present) == "0":
                continue
            capacities.append(int(_text(supply / "capacity")))
            statuses.append(_text(supply / "status").lower())
        else:
            state = supply / "online"
            if state.exists():
                online.append(_text(state) == "1")
    if not capacities:
        return None
    if online:
        ac = any(online)
    elif any(status in ("charging", "full", "not charging") for status in statuses):
        ac = True
    elif all(status == "discharging" for status in statuses):
        ac = False
    else:
        raise ValueError(f"no power supply says whether AC is connected ({statuses})")
    return ac, min(capacities)


def verdict(state: Power) -> tuple[bool, str]:
    """Whether the gate may run, and the sentence that says why."""
    if state is None:
        return True, "a desktop with no battery: always allowed"
    ac, percent = state
    if not ac:
        return False, f"on battery at {percent}%: the local gate needs AC power"
    if percent < MINIMUM_PERCENT:
        return False, f"on AC power at {percent}%: the local gate starts at {MINIMUM_PERCENT}%"
    return True, f"on AC power at {percent}%"


def probe() -> Power:
    if sys.platform == "darwin":
        try:
            done = subprocess.run(
                ["pmset", "-g", "batt"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except OSError as error:
            raise ValueError(f"cannot run pmset: {error}") from error
        if done.returncode != 0:
            detail = done.stderr.strip() or f"exit {done.returncode}"
            raise ValueError(f"pmset failed: {detail}")
        return parse_pmset(done.stdout)
    if sys.platform.startswith("linux"):
        return parse_sysfs()
    raise ValueError(f"no power check for {sys.platform}")


def main() -> int:
    try:
        state = probe()
    except (OSError, ValueError) as error:
        print(f"cannot read the power state: {error}", file=sys.stderr)
        return 2
    allowed, reason = verdict(state)
    print(reason)
    return 0 if allowed else REFUSED


if __name__ == "__main__":
    sys.exit(main())
