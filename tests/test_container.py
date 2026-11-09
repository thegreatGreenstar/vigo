"""The pod keeps the boundary it claims: the agent has no Discord token.

Deployment is not Python, but the properties that make the split worth running
are text.  A later edit that mounts the token in the agent, puts the projects in
the daemon, or publishes on every interface would pass every other test.
"""

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTAINER = ROOT / "deploy" / "container"


def read(name: str) -> str:
    return (CONTAINER / name).read_text(encoding="utf-8")


def blocks() -> tuple[str, str]:
    text = read("run.sh")
    after_agent = text.split("--name vigo-opencode", 1)[1]
    agent, daemon = after_agent.split("--name vigo ", 1)
    return agent, daemon


class Image(unittest.TestCase):
    def test_opencode_is_pinned_by_version_and_checksum(self) -> None:
        text = read("Containerfile.agent")
        self.assertIn("ARG OPENCODE_VERSION=2.2.6", text)
        for arch in ("ARM64", "X64"):
            self.assertRegex(text, rf"ARG OPENCODE_SHA256_{arch}=[0-9a-f]{{64}}")
        self.assertIn("releases/download/v${OPENCODE_VERSION}", text)
        self.assertIn("sha256sum -c -", text)

    def test_gh_is_pinned_by_version_and_checksum(self) -> None:
        text = read("Containerfile.agent")
        self.assertIn("ARG GH_VERSION=2.103.3", text)
        for arch in ("ARM64", "AMD64"):
            self.assertRegex(text, rf"ARG GH_SHA256_{arch}=[0-9a-f]{{64}}")
        self.assertIn("gh_${GH_VERSION}_linux_${arch}.tar.gz", text)

    def test_the_image_copies_no_path_from_the_host(self) -> None:
        for name in ("Containerfile", "Containerfile.agent"):
            self.assertNotIn("COPY /Users", read(name))
            self.assertNotIn("COPY /home", read(name))

    def test_the_agent_has_the_tools_scarletnx_needs(self) -> None:
        agent = read("Containerfile.agent")
        for tool in (
            "clang", "llvm", "cmake", "ninja", "nasm",
            "sdl3", "wayland", "libdecor",
            "vulkan-icd-loader", "mesa", "vulkan-validation-layers",
            "spirv-tools", "nodejs", "npm", "gdb", "renderdoc",
            "qt6-base", "ffmpeg", "boost",
        ):
            self.assertIn(tool, agent, f"the agent image has no {tool}")

    def test_the_daemon_stays_minimal(self) -> None:
        daemon = read("Containerfile")
        for tool in ("clang", "cmake", "nodejs", "vulkan-validationlayers", "opencode"):
            self.assertNotIn(tool, daemon)

    def test_pacman_can_run_without_a_landlock_sandbox(self) -> None:
        # pacman 7.1's sandbox needs Landlock, which the podman machine on macOS
        # refuses; without the switch the build stops before it fetches anything.
        self.assertIn("DisableSandbox", read("Containerfile.agent"))


class Pod(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read("run.sh")
        self.agent, self.daemon = blocks()

    def test_the_agent_sees_the_projects_and_its_own_home(self) -> None:
        self.assertIn('src=$projects,dst=$projects', self.agent)
        self.assertIn('src=$agent_home,dst=/home/vigo', self.agent)
        self.assertNotIn('src=$home,dst=$home', self.text)
        self.assertNotIn('src=$HOME,dst=$HOME', self.text)

    def test_the_daemon_runs_a_private_checkout(self) -> None:
        self.assertIn("checkout=${VIGO_CHECKOUT:-$home/.local/share/vigo/checkout}", self.text)
        self.assertIn("src=$checkout,dst=$checkout", self.daemon)
        self.assertNotIn("src=$checkout,dst=$checkout", self.agent)
        self.assertIn("src=$config/ssh,dst=/home/vigo/.ssh,ro", self.daemon)
        self.assertNotIn("src=$agent_home/.ssh", self.daemon)

    def test_the_daemon_holds_the_token_and_the_agent_does_not(self) -> None:
        self.assertIn("dst=/run/secrets/vigo-token", self.daemon)
        self.assertNotIn("vigo-token", self.agent)
        self.assertNotIn("VIGO_TOKEN", self.agent)
        self.assertNotIn('src=$projects', self.daemon)

    def test_the_peer_password_is_daemon_only(self) -> None:
        self.assertIn("dst=/run/secrets/peer-password", self.daemon)
        self.assertNotIn("peer-password", self.agent)

    def test_the_agent_holds_gh_and_the_daemon_does_not(self) -> None:
        self.assertIn("dst=/run/secrets/gh-token", self.agent)
        self.assertIn("target=gh-token", self.agent)
        self.assertNotIn("gh-token", self.daemon)
        self.assertNotIn("GH_TOKEN", self.daemon)

    def test_the_host_runner_token_reaches_the_agent_only(self) -> None:
        # The runner is the one door from the agent to a program only the Mac
        # can run; the daemon has no reason to hold its token.
        self.assertIn("target=host-runner-token", self.agent)
        self.assertIn("dst=/run/secrets/host-runner-token", self.agent)
        self.assertIn("VIGO_HOST_RUNNER_URL", self.agent)
        self.assertIn("VIGO_HOST_RUNNER_TOKEN_FILE", self.agent)
        self.assertNotIn("host-runner-token", self.daemon)
        self.assertNotIn("VIGO_HOST_RUNNER", self.daemon)

    def test_both_containers_drop_what_they_do_not_need(self) -> None:
        for block in (self.agent, self.daemon):
            for flag in (
                "--cap-drop=all",
                "--security-opt=no-new-privileges",
                "--read-only",
                "--pids-limit=",
                "--cpus=",
                "--memory=",
                "--ulimit core=0",
            ):
                self.assertIn(flag, block)
        self.assertNotIn("--network=host", self.text)
        self.assertNotIn("--privileged", self.text)

    def test_the_published_ports_are_named_not_every_interface(self) -> None:
        self.assertIn('case "$bind" in', self.text)
        self.assertIn("'0.0.0.0'", self.text)
        self.assertIn('"$bind:$opencode_port:$opencode_port"', self.text)
        self.assertIn('"$bind:$peer_port:$peer_port"', self.text)

    def test_linux_uses_the_tpm_agent_and_macos_the_one_key(self) -> None:
        self.assertIn('src=$agent_dir,dst=/run/vigo,ro', self.agent)
        self.assertIn('src=$agent_dir,dst=/run/vigo,ro', self.daemon)
        # macOS has no agent socket inside the pod, so both containers need the
        # one key: the agent commits, the daemon pulls.
        self.assertIn("target=vigo-ssh-key", self.agent)
        self.assertIn("target=vigo-ssh-key", self.daemon)

    def test_the_agent_config_is_read_only_and_may_hold_mcp(self) -> None:
        self.assertIn(
            "src=$agent_home/.config/opencode,dst=/home/vigo/.config/opencode,ro",
            self.agent,
        )

    def test_the_daemon_mounts_its_settings_once(self) -> None:
        # podman refuses a destination twice.  The macOS branch used to repeat
        # the settings and the peer password, so the daemon never started there.
        self.assertEqual(self.daemon.count("dst=/config/vigo.json"), 1)
        self.assertEqual(self.daemon.count("dst=/config/peer.password"), 1)

    def test_agent_data_cannot_contain_protected_paths(self) -> None:
        self.assertIn("VIGO_AGENT_DATA must not be the home directory", self.text)
        self.assertIn("contains the protected", self.text)

    def test_gpu_wayland_keys_and_extra_data_are_opt_in(self) -> None:
        for name in ("VIGO_AGENT_GPU", "VIGO_AGENT_WAYLAND", "VIGO_AGENT_KEYS", "VIGO_AGENT_DATA"):
            self.assertIn(name, self.text)

    def test_gpu_is_the_render_node_only(self) -> None:
        # card* carries modesetting and EDID; a headless Vulkan driver needs
        # only the render node, and the narrower device is the boundary.
        self.assertIn("--device /dev/dri/renderD128", self.text)
        self.assertNotIn("--device /dev/dri ", self.text)


class Images(unittest.TestCase):
    def test_the_two_images_are_built_and_used(self) -> None:
        build = (ROOT / "deploy" / "build-container.sh").read_text(encoding="utf-8")
        self.assertIn("Containerfile.agent", build)
        self.assertIn("cachyos/cachyos", build)
        run = read("run.sh")
        self.assertIn("agent_image=", run)
        self.assertIn('podman run "$@" "$agent_image"', run)

    def test_the_arm64_base_has_an_arm64_image(self) -> None:
        # docker.io/library/archlinux carries no arm64 manifest, so it cannot
        # start on the podman machine of an Apple silicon Mac.  Arch Linux ARM
        # is the arm64 build of the same distribution.
        build = (ROOT / "deploy" / "build-container.sh").read_text(encoding="utf-8")
        self.assertNotIn("library/archlinux", build)
        self.assertIn("archlinuxarm", build)


class Services(unittest.TestCase):
    def test_the_unit_runs_the_installed_kit(self) -> None:
        self.assertIn(
            "ExecStart=%h/.local/libexec/vigo-container/run.sh",
            read("vigo-container.service"),
        )
        plist = read("dev.vigo.container.plist.in")
        self.assertIn("<string>__KIT__/run.sh</string>", plist)
        self.assertNotIn("Projects/vigo", plist)

    def test_the_installer_installs_the_host_runner(self) -> None:
        installer = read("install.sh")
        for line in (
            "host-runner.py",
            "dev.vigo.host-runner.plist.in",
            "host-runner.example.json",
            "host-runner-token",
            "mcp?program={name}",
        ):
            self.assertIn(line, installer, f"the installer does not mention {line}")

    def test_the_host_runner_example_is_a_program_table(self) -> None:
        import json
        example = json.loads((ROOT / "deploy" / "host-runner.example.json").read_text(encoding="utf-8"))
        program = example["programs"]["scarletnx"]
        self.assertEqual(program["binary"], "/Applications/ScarletNX.app/Contents/MacOS/scarletnx")
        self.assertEqual(program["dataRoot"], "~/Library/Application Support/scarletnx")
        self.assertIn("read_log", program["mcp"]["tools"])
        self.assertNotIn("edenprobe_run", program["mcp"]["tools"])

    def test_the_example_has_a_build_door(self) -> None:
        import json
        example = json.loads((ROOT / "deploy" / "host-runner.example.json").read_text(encoding="utf-8"))
        build = example["programs"]["scarletnx-build"]
        self.assertNotIn("binary", build)
        self.assertIn("configure", build["steps"])
        self.assertIn("build", build["steps"])
        # The built copy is outside the folder the agent's container mounts,
        # so the agent cannot replace the binary the runner starts.
        self.assertNotIn("~/Projects", example["programs"]["scarletnx-built"]["binary"])

    def test_the_ssh_clients_use_the_right_key(self) -> None:
        mac = read("ssh_config")
        self.assertIn("IdentityFile /run/secrets/vigo-ssh-key", mac)
        self.assertIn("StrictHostKeyChecking yes", mac)
        linux = read("ssh_config.linux")
        self.assertIn("IdentityAgent /run/vigo/ssh-agent.sock", linux)
        self.assertIn("UserKnownHostsFile /home/vigo/.ssh/known_hosts", linux)

    def test_the_agent_signs_its_commits(self) -> None:
        gitconfig = read("gitconfig")
        self.assertIn("signingkey = /run/secrets/vigo-ssh-key", gitconfig)
        self.assertIn("gpgsign = true", gitconfig)
