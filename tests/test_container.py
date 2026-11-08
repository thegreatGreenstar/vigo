"""The container kit keeps the boundary it claims: one project tree, no home.

Deployment is not Python, but the two properties that make the pod worth
running are text: the projects are the only host data the agent sees, and the
Discord token never reaches it.  A later edit that mounts the home, moves the
token into the agent or lets a published port listen on every interface would
pass every other test, so this one reads the files.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTAINER = ROOT / "deploy" / "container"


def read(name: str) -> str:
    return (CONTAINER / name).read_text(encoding="utf-8")


class Image(unittest.TestCase):
    def test_opencode_is_pinned_by_version_and_checksum(self) -> None:
        text = read("Containerfile")
        self.assertIsNotNone(
            re.search(r"^ARG OPENCODE_VERSION=(\d+\.\d+\.\d+)$", text, re.MULTILINE),
            "the image names no OpenCode version",
        )
        for arch in ("ARM64", "X64"):
            self.assertIsNotNone(
                re.search(
                    rf"^ARG OPENCODE_SHA256_{arch}=([0-9a-f]{{64}})$",
                    text,
                    re.MULTILINE,
                ),
                f"no SHA-256 for {arch}",
            )
        self.assertIn("releases/download/v${OPENCODE_VERSION}", text)
        self.assertIn("sha256sum -c -", text)

    def test_the_agent_config_names_no_mcp_server(self) -> None:
        """An npx server fetches and runs a package at run time, in the pod."""
        self.assertNotIn("mcp", read("opencode.json").lower())

    def test_the_image_copies_no_path_from_the_host(self) -> None:
        self.assertNotIn("COPY /Users", read("Containerfile"))


class Pod(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read("run.sh")
        after_agent = self.text.split("--name vigo-opencode", 1)[1]
        self.agent, self.daemon = after_agent.split("--name vigo ", 1)

    def test_the_agent_sees_the_projects_and_no_home(self) -> None:
        self.assertIn('--volume "$projects:$projects:rw"', self.agent)
        self.assertNotRegex(self.text, r"--volume\s+\"?\$home\"?:")

    def test_the_daemon_holds_the_token_and_the_agent_does_not(self) -> None:
        self.assertIn("target=VIGO_TOKEN", self.daemon)
        self.assertNotIn("VIGO_TOKEN", self.agent)

    def test_both_containers_drop_what_they_do_not_need(self) -> None:
        for block in (self.agent, self.daemon):
            for flag in (
                "--cap-drop=all",
                "--security-opt=no-new-privileges",
                "--read-only",
                "--pids-limit=",
                "--memory=",
            ):
                self.assertIn(flag, block)
        self.assertNotIn("--network=host", self.text)
        self.assertNotIn("--privileged", self.text)

    def test_the_published_ports_are_named_not_every_interface(self) -> None:
        self.assertIn("bind=${VIGO_PEER_BIND:-127.0.0.1}", self.text)
        self.assertIn("$bind:$opencode_port:$opencode_port", self.text)
        self.assertIn("$bind:$peer_port:$peer_port", self.text)

    def test_the_one_shared_secret_is_the_agent_key(self) -> None:
        for block in (self.agent, self.daemon):
            self.assertIn("target=vigo-ssh-key,mode=0400", block)


class Services(unittest.TestCase):
    def test_the_unit_runs_the_installed_kit(self) -> None:
        plist = read("dev.vigo.container.plist.in")
        self.assertIn("<string>__KIT__/run.sh</string>", plist)
        self.assertNotIn("Projects/vigo", plist)
        self.assertIn(
            "ExecStart=%h/.local/libexec/vigo-container/run.sh",
            read("vigo-container.service"),
        )

    def test_the_key_is_the_one_secret_and_the_host_keys_are_pinned(self) -> None:
        ssh = read("ssh_config")
        self.assertIn("IdentityFile /run/secrets/vigo-ssh-key", ssh)
        self.assertIn("StrictHostKeyChecking yes", ssh)
        self.assertIn("BatchMode yes", ssh)


if __name__ == "__main__":
    unittest.main()
