"""The agent a task or a goal runs under.

A task is a session of its own, and its rule lets it hand parts of the work to
subagents.  OpenCode runs a `subagent` agent only in a child session through
its `subagent` tool, and a subagent cannot start another subagent, so the
worker must be a primary (or `all`) agent.  `build` is the coding one.
"""

from __future__ import annotations

import unittest

from vigo.opencode_api import OpenCode
from tests.fake_opencode import FakeOpenCode


class WorkerAgentTest(unittest.TestCase):
    def setUp(self):
        self.device = FakeOpenCode("macbook")
        self.addCleanup(self.device.close)

    def worker(self) -> str:
        return OpenCode(self.device.url, self.device.password, timeout=2.0).worker_agent()

    def test_the_coding_agent_runs_a_task(self):
        self.assertEqual(self.worker(), "build")

    def test_a_subagent_never_runs_a_task(self):
        self.device.agents = [
            {"id": "general", "mode": "subagent", "name": "General"},
            {"id": "explore", "mode": "subagent", "name": "Explore"},
        ]
        self.assertEqual(self.worker(), "",
                         "no primary is named: the device's default primary runs it")

    def test_another_primary_is_the_fallback(self):
        self.device.agents = [
            {"id": "reviewer", "mode": "primary", "name": "Reviewer"},
            {"id": "general", "mode": "subagent", "name": "General"},
        ]
        self.assertEqual(self.worker(), "reviewer")

    def test_an_agent_that_runs_both_ways_is_a_primary(self):
        self.device.agents = [{"id": "writer", "mode": "all", "name": "Writer"}]
        self.assertEqual(self.worker(), "writer")


if __name__ == "__main__":  # pragma: no cover - `python -m unittest` finds it
    unittest.main()
