import subprocess
import tempfile
import unittest
from pathlib import Path

from vigo import __version__, commands
from vigo.app import App
from vigo.discord_api import Discord
from vigo.peer import PeerServer
from vigo.state import State
from tests.fake_discord import FakeDiscord
from tests.fake_opencode import FakeOpenCode
from tests.support import TempDir, load_settings

OWNER = "145512144145512144"
STRANGER = "888888888888888888"
CHANNEL = "chan_1"


class AppTest(unittest.TestCase):
    def setUp(self):
        self.discord = FakeDiscord()
        self.device = FakeOpenCode("macbook")
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.shutdown()
        self.device.close()
        self.discord.close()

    def make(self, directory: Path, **overrides) -> App:
        settings = load_settings(
            directory,
            devices=[{"name": "macbook", "url": self.device.url, "directory": "/tmp/project"}],
            **overrides,
        )
        api = Discord("token", settings.app_id, base=self.discord.url)
        self.app = App(settings, api=api, state=State(directory / "state.json"))
        return self.app

    def said(self) -> list[str]:
        """Everything the bot posted: a command edits, an answer follows up."""
        return [entry["content"] for entry in self.discord.edits + self.discord.followups]

    def use(self, app: App, sub: str, options: list | None = None,
            who: str = OWNER, token: str = "tok", interaction: str = ""):
        interaction = interaction or self.discord.next_id("int")
        payload = {
            "id": interaction,
            "token": token,
            "type": 2,
            "channel_id": CHANNEL,
            "channel": {"name": "general"},
            "member": {"user": {"id": who, "username": "someone"}},
            "data": {"name": "vigo", "options": [{"name": sub, "type": 1, "options": options or []}]},
        }
        app.handle_interaction(payload)
        return interaction

    # -- access -----------------------------------------------------------

    def test_help_answers_anyone_alone(self):
        with TempDir() as directory:
            app = self.make(directory)
            interaction = self.use(app, commands.HELP, who=STRANGER)
        answer = self.discord.answer_for(interaction)
        self.assertEqual(answer["type"], 4)
        self.assertEqual(answer["data"]["flags"], 64)
        self.assertIn("Vigo", answer["data"]["content"])
        self.assertIn("/vigo ask", answer["data"]["content"])

    def test_strangers_never_see_the_owners_id(self):
        with TempDir() as directory:
            app = self.make(directory)
            for sub in (commands.ASK, commands.HELP, commands.DEVICES, commands.ALLOW):
                interaction = self.use(app, sub, who=STRANGER,
                                       options=[{"name": "prompt", "type": 3, "value": "hi"}])
                answer = self.discord.answer_for(interaction)["data"]["content"]
                self.assertNotIn(OWNER, answer, f"{sub} must not name the owner")
                self.assertNotIn("<@", answer)

    def test_help_honours_hidden(self):
        with TempDir() as directory:
            app = self.make(directory)
            public = self.use(app, commands.HELP)
            self.assertNotIn("flags", self.discord.answer_for(public)["data"])
            quiet = self.use(app, commands.HELP,
                             options=[{"name": "hidden", "type": 5, "value": True}])
            self.assertEqual(self.discord.answer_for(quiet)["data"]["flags"], 64)
            stranger = self.use(app, commands.HELP, who=STRANGER)
            self.assertEqual(self.discord.answer_for(stranger)["data"]["flags"], 64,
                             "a stranger's help is never public")

    def test_a_stranger_is_told_no_and_nothing_runs(self):
        with TempDir() as directory:
            app = self.make(directory)
            interaction = self.use(app, commands.ASK,
                                   options=[{"name": "prompt", "type": 3, "value": "hi"}],
                                   who=STRANGER)
        answer = self.discord.answer_for(interaction)
        self.assertEqual(answer["data"]["flags"], 64)
        self.assertIn("approved", answer["data"]["content"])
        self.assertEqual(self.device.prompts, [])

    def test_only_the_owner_allows(self):
        with TempDir() as directory:
            app = self.make(directory)
            interaction = self.use(app, commands.ALLOW, who=STRANGER,
                                   options=[{"name": "user", "type": 6,
                                             "value": {"id": "42", "username": "Alex"}}])
        answer = self.discord.answer_for(interaction)
        self.assertIn("Only", answer["data"]["content"])
        self.assertFalse(app.allow.may_ask("42"))

    def test_the_owner_allows_and_denies(self):
        with TempDir() as directory:
            app = self.make(directory)
            interaction = self.use(app, commands.ALLOW, options=[
                {"name": "user", "type": 6, "value": {"id": "42", "username": "Alex"}},
                {"name": "for", "type": 3, "value": "2h"},
            ])
            self.assertEqual(self.discord.answer_for(interaction)["data"]["flags"], 64,
                             "someone else's name is never public")
            self.assertTrue(self.discord.wait_for(lambda: app.allow.may_ask("42")))
            self.use(app, commands.DENY, options=[
                {"name": "user", "type": 6, "value": {"id": "42", "username": "Alex"}},
            ])
            self.assertTrue(self.discord.wait_for(lambda: not app.allow.may_ask("42")))

    def test_a_listed_person_may_ask(self):
        listed = "777777777777777777"
        with TempDir() as directory:
            app = self.make(directory, users=[listed])
            self.device.script = [("delta", "hello"), ("idle", "hello")]
            self.use(app, commands.ASK, who=listed,
                     options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("hello" in content for content in self.said())))

    # -- asking -----------------------------------------------------------

    def test_an_ask_is_public_and_ends_with_the_answer(self):
        self.device.script = [("delta", "hello "), ("delta", "world"), ("idle", "hello world")]
        with TempDir() as directory:
            app = self.make(directory)
            interaction = self.use(app, commands.ASK,
                                   options=[{"name": "prompt", "type": 3, "value": "say hi"}])
            deferred = self.discord.answer_for(interaction)
            self.assertEqual(deferred["type"], 5)
            self.assertNotIn("flags", deferred["data"])
            self.assertTrue(self.discord.wait_for(
                lambda: any("hello world" in content for content in self.said()), 10))
        final = next(content for content in self.said() if "hello world" in content)
        self.assertNotIn("asked by", final)
        self.assertNotIn("macbook", final, "the answer carries no footer")
        self.assertEqual(final.strip(), "hello world")
        self.assertEqual(self.device.prompts[0][1], "say hi")

    def test_quick_answers_once_and_starts_no_session(self):
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.QUICK,
                      options=[{"name": "prompt", "type": 3, "value": "what is a pointer"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("one-off answer" in str(content)
                            for content in self.said()), 10))
        self.assertEqual(self.device.generated, ["what is a pointer"])
        self.assertEqual(self.device.sessions, {}, "a one-off is not a session")
        self.assertEqual(self.device.prompts, [])

    def test_quick_can_be_hidden(self):
        with TempDir() as directory:
            app = self.make(directory)
            interaction = self.use(app, commands.QUICK, options=[
                {"name": "prompt", "type": 3, "value": "hi"},
                {"name": "hidden", "type": 5, "value": True},
            ])
            self.assertEqual(self.discord.answer_for(interaction)["data"]["flags"], 64)

    def test_quick_says_when_no_device_answers(self):
        self.device.close()
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.QUICK,
                      options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("no device" in str(content) for content in self.said())))
        self.assertEqual(self.device.generated, [])

    def test_a_quick_answer_is_not_preceded_by_progress(self):
        self.device.script = [("idle", "just the answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("just the answer" in str(content)
                            for content in self.said()), 10))
        self.assertEqual(len(self.said()), 1,
                         "Discord's own 'thinking' is enough; only the answer is sent")
        self.assertEqual(self.said()[0].strip(), "just the answer")

    def test_the_answer_is_its_own_message(self):
        self.device.script = [("idle", "the answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("the answer" in str(entry["content"])
                            for entry in self.discord.followups), 10))
        self.assertEqual(self.discord.edits, [], "the placeholder is not edited")
        self.assertEqual(len(self.discord.deleted), 1, "the placeholder is removed")
        self.assertEqual(self.discord.followups[0]["content"].strip(), "the answer")

    def test_hidden_asks_are_ephemeral(self):
        self.device.script = [("idle", "quiet")]
        with TempDir() as directory:
            app = self.make(directory)
            interaction = self.use(app, commands.ASK, options=[
                {"name": "prompt", "type": 3, "value": "hi"},
                {"name": "hidden", "type": 5, "value": True},
            ])
            deferred = self.discord.answer_for(interaction)
            self.assertEqual(deferred["data"]["flags"], 64)
            self.assertTrue(self.discord.wait_for(
                lambda: any("quiet" in content for content in self.said()), 10))

    def test_no_device_answers(self):
        self.device.close()
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("no device" in str(content) for content in self.said())))
        self.assertEqual(self.device.prompts, [])

    def test_one_run_per_channel(self):
        self.device.script = [("sleep", 1.5), ("idle", "slow")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "first"}])
            self.assertTrue(self.discord.wait_for(lambda: bool(self.device.prompts)))
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "second"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("already running" in str(content) for content in self.said())))
            run = app.run_for(CHANNEL)
            if run:
                run.stop.set()

    def test_upgrade_updates_the_device_it_names(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            origin = root / "origin.git"
            self._git("init", "--bare", "-q", str(origin), cwd=root)
            seed = root / "seed"
            self._git("clone", "-q", str(origin), str(seed), cwd=root)
            self._git("config", "user.email", "t@example.com", cwd=seed)
            self._git("config", "user.name", "t", cwd=seed)
            (seed / "one.txt").write_text("one\n", encoding="utf-8")
            self._git("add", ".", cwd=seed)
            self._git("commit", "-qm", "one", cwd=seed)
            self._git("push", "-q", "origin", "HEAD:main", cwd=seed)
            peer_checkout = root / "peer"
            self._git("clone", "-q", "-b", "main", str(origin), str(peer_checkout), cwd=root)
            (seed / "two.txt").write_text("two\n", encoding="utf-8")
            self._git("add", ".", cwd=seed)
            self._git("commit", "-qm", "two", cwd=seed)
            self._git("push", "-q", "origin", "HEAD:main", cwd=seed)

            # the other daemon, on its own checkout, old build, no restart
            other = PeerServer("127.0.0.1", 0, "pc", "pw",
                               checkout=peer_checkout, restart=False,
                               version="0.9.0").start()
            self.addCleanup(other.stop)
            peer_url = f"http://127.0.0.1:{other.server.server_address[1]}"

            with TempDir() as directory:
                settings = load_settings(
                    directory,
                    device="macbook",
                    devices=[
                        {"name": "macbook", "url": "http://127.0.0.1:1",
                         "directory": "/a"},
                        {"name": "pc", "url": "http://127.0.0.1:2",
                         "directory": "/b", "peer": peer_url},
                    ],
                )
                api = Discord("token", settings.app_id, base=self.discord.url)
                app = App(settings, api=api, state=State(directory / "state.json"))
                self.app = app
                self.use(app, commands.UPGRADE, options=[
                    {"name": "device", "type": 3, "value": "pc"},
                ])
                self.assertTrue(self.discord.wait_for(
                    lambda: any("pc" in str(content)
                                for content in self.said()), 30))
                said = self.said()[-1]
            self.assertIn("pulled", said)
            self.assertNotIn("macbook", said)
            self.assertTrue((peer_checkout / "two.txt").exists(), "the other checkout moved")

    def test_builds_decide_who_is_behind(self):
        with TempDir() as directory:
            app = self.make(directory)
            app._build = "aaa1111"
            self.assertTrue(app._same_build({"build": "aaa1111", "version": "0.9.0"}))
            self.assertFalse(app._same_build({"build": "bbb2222", "version": __version__}))
            # an older peer reports no build; the version is all it can tell
            self.assertTrue(app._same_build({"version": __version__}))
            self.assertFalse(app._same_build({"version": "0.1.0"}))

    @staticmethod
    def _git(*args, cwd):
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if done.returncode != 0:
            raise AssertionError(done.stderr)
        return done.stdout

    def test_the_device_option_offers_the_names(self):
        with TempDir() as directory:
            app = self.make(directory)
            interaction = self.discord.next_id("int")
            app.handle_interaction({
                "id": interaction,
                "token": "tok",
                "type": 4,
                "member": {"user": {"id": OWNER}},
                "data": {"name": "vigo", "options": [{"name": "ask", "type": 1, "options": [
                    {"name": "device", "type": 3, "value": "mac", "focused": True},
                ]}]},
            })
            answer = self.discord.answer_for(interaction)
        self.assertEqual(answer["type"], 8)
        self.assertEqual([choice["value"] for choice in answer["data"]["choices"]], ["macbook"])

    def test_autocomplete_offers_nothing_to_a_stranger(self):
        with TempDir() as directory:
            app = self.make(directory)
            interaction = self.discord.next_id("int")
            app.handle_interaction({
                "id": interaction,
                "token": "tok",
                "type": 4,
                "member": {"user": {"id": STRANGER}},
                "data": {"name": "vigo", "options": [{"name": "ask", "type": 1, "options": [
                    {"name": "device", "type": 3, "value": "", "focused": True},
                ]}]},
            })
            answer = self.discord.answer_for(interaction)
        self.assertEqual(answer["data"]["choices"], [])

    def test_the_devices_listing_is_hidden_and_detailed(self):
        with TempDir() as directory:
            app = self.make(directory)
            interaction = self.use(app, commands.DEVICES)
            answer = self.discord.answer_for(interaction)
            self.assertTrue(self.discord.wait_for(
                lambda: any("macbook" in str(content) for content in self.said())))
            shown = self.said()[-1]
        self.assertEqual(answer["data"]["flags"], 64, "devices is never shown to a channel")
        self.assertIn("/tmp/project", shown, "only the owner sees the machine")

    def test_devices_has_no_way_to_be_shown(self):
        with TempDir() as directory:
            app = self.make(directory)
            app.register_commands()
        command = next(c for c in self.discord.global_commands
                       if c["name"] == "vigo")
        devices = next(o for o in command["options"] if o["name"] == "devices")
        self.assertFalse(devices.get("options"), "no option, so no hidden:false")

    def test_devices_are_the_owners_alone(self):
        listed = "777777777777777777"
        with TempDir() as directory:
            app = self.make(directory, users=[listed])
            interaction = self.use(app, commands.DEVICES, who=listed)
            answer = self.discord.answer_for(interaction)
        self.assertEqual(answer["data"]["flags"], 64)
        self.assertIn("Only", answer["data"]["content"])
        self.assertEqual(self.discord.edits, [], "nothing reached the channel")

    def test_a_hidden_devices_listing_may_name_the_machine(self):
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.DEVICES,
                      options=[{"name": "hidden", "type": 5, "value": True}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("/tmp/project" in str(content) for content in self.said())))
        self.assertIn("macbook", self.said()[-1])

    def test_no_progress_message_names_a_device(self):
        self.device.script = [("delta", "thinking hard"), ("sleep", 0.4), ("idle", "done")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("done" in str(content) for content in self.said()), 10))
        for content in self.said():
            self.assertNotIn("macbook", str(content),
                             "the device never appears in a channel message")

    def test_status_is_the_owners_alone_and_always_hidden(self):
        listed = "777777777777777777"
        with TempDir() as directory:
            app = self.make(directory, users=[listed])
            refused = self.use(app, commands.STATUS, who=listed)
            self.assertEqual(self.discord.answer_for(refused)["data"]["flags"], 64)
            self.assertIn("Only", self.discord.answer_for(refused)["data"]["content"])
            mine = self.use(app, commands.STATUS)
            self.assertEqual(self.discord.answer_for(mine)["data"]["flags"], 64,
                             "status is never shown to a channel")

    def test_an_answer_is_public_unless_hidden(self):
        with TempDir() as directory:
            app = self.make(directory)
            interaction = self.use(app, commands.HELP)
            self.assertNotIn("flags", self.discord.answer_for(interaction)["data"])
            interaction = self.use(app, commands.HELP,
                                   options=[{"name": "hidden", "type": 5, "value": True}])
            self.assertEqual(self.discord.answer_for(interaction)["data"]["flags"], 64)

    # -- registration -----------------------------------------------------

    def test_commands_are_registered_once(self):
        with TempDir() as directory:
            app = self.make(directory)
            app.register_commands()
            app.register_commands()
        self.assertEqual(self.discord.global_command_puts, 1)
        names = [command["name"] for command in self.discord.global_commands]
        self.assertEqual(names, ["vigo"])
        command = self.discord.global_commands[0]
        self.assertEqual(command["integration_types"], [0, 1])
        self.assertEqual(command["contexts"], [0, 1, 2])
        subcommands = [option["name"] for option in command["options"]]
        self.assertEqual(subcommands, ["ask", "quick", "upgrade", "stop", "status", "devices",
                                       "allow", "deny", "allowed", "help"])
        always_private = {"devices", "status", "upgrade", "allow", "deny", "allowed"}
        for option in command["options"]:
            names = [inner["name"] for inner in (option.get("options") or [])]
            if option["name"] in always_private:
                self.assertNotIn("hidden", names, f"{option['name']} cannot be shown")
            else:
                self.assertIn("hidden", names, f"{option['name']} takes hidden")

    def test_a_user_install_keeps_to_dms(self):
        with TempDir() as directory:
            app = self.make(directory, install="user")
            app.register_commands()
        command = self.discord.global_commands[0]
        self.assertEqual(command["integration_types"], [1])
        self.assertEqual(command["contexts"], [1, 2])

    def test_another_command_is_kept(self):
        with TempDir() as directory:
            app = self.make(directory)
            self.discord.global_commands = [
                {"id": "x", "name": "other", "description": "theirs", "type": 1}
            ]
            app.register_commands()
        names = [command["name"] for command in self.discord.global_commands]
        self.assertEqual(names, ["other", "vigo"])


if __name__ == "__main__":
    unittest.main()
