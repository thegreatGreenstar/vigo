"""One ruff pin and one Python matrix, named everywhere and equal everywhere.

The Linux half of CI moved to a codespace, so the same two facts now live in
four files: the image, the runner, `tests/ci-local.sh` and the workflow.  A pin
that moves in one of them and not the others is green here and red there, and
nothing else would say so.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUFF_PATTERNS = {
    ".github/workflows/tests.yml": re.compile(r"ruff==(\d+\.\d+\.\d+)"),
    ".devcontainer/Dockerfile": re.compile(r"^ARG RUFF_VERSION=(\d+\.\d+\.\d+)$", re.MULTILINE),
    "tests/in-codespace.sh": re.compile(r"^ruff_pin=(\d+\.\d+\.\d+)$", re.MULTILINE),
}
PYTHONS = ("3.12", "3.13", "3.14")


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class Pins(unittest.TestCase):
    def test_every_file_names_the_same_ruff(self) -> None:
        found = {}
        for name, pattern in RUFF_PATTERNS.items():
            matches = pattern.findall(read(name))
            self.assertTrue(matches, f"{name} names no ruff pin")
            self.assertEqual(len(set(matches)), 1, f"{name} names two: {matches}")
            found[name] = matches[0]
        self.assertEqual(len(set(found.values())), 1, f"the pins disagree: {found}")

    def test_every_file_names_the_same_pythons(self) -> None:
        workflow = read(".github/workflows/tests.yml")
        matrix = re.search(r'python: \[([^\]]*)\]', workflow)
        self.assertIsNotNone(matrix, "the workflow has no language matrix")
        self.assertEqual(
            re.findall(r'"([\d.]+)"', matrix.group(1)), list(PYTHONS),
        )
        image = read(".devcontainer/Dockerfile")
        self.assertEqual(
            [version for version in PYTHONS if f"python{version}" not in image], [],
        )
        runner = read("tests/in-codespace.sh")
        self.assertIn(f'versions=${{*:-"{" ".join(PYTHONS)}"}}', runner)

    def test_the_linux_matrix_is_not_in_the_workflow(self) -> None:
        """A runner covers macOS and Windows; a codespace covers Linux."""
        workflow = read(".github/workflows/tests.yml")
        operating_systems = re.findall(r"^\s+os: (\[[^\]]*\])", workflow, re.MULTILINE)
        self.assertEqual(len(operating_systems), 2, "quick and test each name their OSes")
        for listed in operating_systems:
            self.assertEqual(listed, "[macos-latest, windows-latest]")
        self.assertNotIn("ubuntu", " ".join(operating_systems))
        # The two platform-free jobs stay on a runner: they gate a pull request
        # without anyone making a codespace.
        self.assertEqual(workflow.count("runs-on: ubuntu-latest"), 2)


if __name__ == "__main__":
    unittest.main()
