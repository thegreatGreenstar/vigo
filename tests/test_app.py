import dataclasses
import subprocess
import tempfile
import unittest
from pathlib import Path

from vigo import __version__, commands
from vigo import ask as asking
from vigo.app import WAITING_MAX, App
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

    def make(self, directory: Path, checkout: Path | None = None, **overrides) -> App:
        settings = load_settings(
            directory,
            devices=[{"name": "macbook", "url": self.device.url, "directory": "/tmp/project"}],
            **overrides,
        )
        api = Discord("token", settings.app_id, base=self.discord.url)
        self.app = App(settings, api=api, state=State(directory / "state.json"), checkout=checkout)
        return self.app

    def said(self) -> list[str]:
        """Everything the bot posted: commands edit the deferred message."""
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

    def test_only_the_owner_upgrades(self):
        """Approval to ask is not approval to pull code onto the machines."""
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ALLOW, options=[
                {"name": "user", "type": 6, "value": {"id": "42", "username": "Alex"}},
            ])
            self.assertTrue(app.allow.may_ask("42"))
            interaction = self.use(app, commands.UPGRADE, who="42")
        answer = self.discord.answer_for(interaction)
        self.assertEqual(answer["data"].get("flags"), 64)
        self.assertIn("Only this bot's owner can use `/vigo upgrade`",
                      answer["data"].get("content", ""),
                      "an approved asker is refused, not deferred")

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

    def test_a_second_ask_in_the_channel_waits_its_turn(self):
        self.device.script = [("sleep", 1.0), ("idle", "the answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "first"}])
            self.assertTrue(self.device.wait_for_prompt())
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "second"}])
            self.assertTrue(any("Queued" in str(content) for content in self.said()))
            self.assertEqual([asked for _session, asked in self.device.prompts], ["first"],
                             "the second question waits for the first")
            self.assertTrue(self.discord.wait_for(
                lambda: len(self.device.prompts) == 2, 15))
        self.assertEqual([asked for _session, asked in self.device.prompts], ["first", "second"])

    def test_a_run_past_the_timeout_keeps_the_channel_busy(self):
        self.device.script = [("sleep", 2.0), ("idle", "the late answer")]
        with TempDir() as directory:
            app = self.make(directory)
            app.config = dataclasses.replace(app.config, timeout=1)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "slow"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("still running" in str(content) for content in self.said()), 10))
            self.assertIsNotNone(app.run_for(CHANNEL), "the run keeps the channel until it ends")
            shown = app._status(CHANNEL)
            self.assertIn("running for", shown)
            self.assertIn("still running on the device", shown)
            self.assertTrue(self.discord.wait_for(
                lambda: any("the late answer" in str(content) for content in self.said()), 15))
        self.assertIsNone(app.run_for(CHANNEL))

    def test_status_reports_a_run_that_belongs_to_another_daemon(self):
        """The device knows it is working; this daemon's map of runs cannot."""
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord DM")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord DM")
            self.device.started_turn(session)
            shown = app._status(CHANNEL)
            self.assertIsNone(app.run_for(CHANNEL), "no run here: another daemon has it")
        self.assertIn("still running on `macbook`", shown)
        self.assertIn("/vigo stop", shown)
        self.assertNotIn("idle", shown)

    def test_status_says_idle_when_the_device_finished(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord DM")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord DM")
            self.device.started_turn(session)
            self.device.finished_turn(session)
            shown = app._status(CHANNEL)
        self.assertIn("idle", shown)
        self.assertNotIn("still running", shown)

    def test_a_question_does_not_join_a_run_another_daemon_drives(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord DM")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord DM")
            self.device.started_turn(session)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "second"}])
            self.assertTrue(any("still working" in str(content) for content in self.said()))
        self.assertEqual(self.device.prompts, [],
                         "a second prompt would steer the agent that is working")

    def test_a_new_session_asks_while_the_old_one_works(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord DM")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord DM")
            self.device.started_turn(session)
            self.device.script = [("idle", "the answer")]
            self.use(app, commands.ASK, options=[
                {"name": "prompt", "type": 3, "value": "fresh"},
                {"name": "new", "type": 5, "value": True},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: any("the answer" in str(content) for content in self.said()), 10))
        self.assertNotEqual(self.device.prompts[0][0], session, "a fresh session, not the busy one")

    def test_status_names_the_next_up_and_the_questions_that_ran(self):
        with TempDir() as directory:
            app = self.make(directory)
            app.finished[CHANNEL] = ["old one", "old two", "old three", "old four"]
            app.waiting[CHANNEL] = [self.worker(app, "next one"), self.worker(app, "next two"),
                                    self.worker(app, "next three")]
            self.use(app, commands.STATUS)
            written = self.said()[-1]
        self.assertIn("**Next up** (3 waiting):", written)
        self.assertIn("1. “next one”", written)
        self.assertIn("3. “next three”", written)
        self.assertIn("**Finished:**", written)
        self.assertIn("“old four”", written)
        self.assertNotIn("“old one”", written, "only the last three that ran are named")

    def test_stop_drops_the_questions_that_wait(self):
        with TempDir() as directory:
            app = self.make(directory)
            app.waiting[CHANNEL] = [self.worker(app, "one"), self.worker(app, "two")]
            self.use(app, commands.STOP)
            written = self.said()[-1]
        self.assertIn("Dropped 2 queued questions.", written)
        self.assertEqual(app.waiting, {})

    def test_the_line_in_a_channel_is_bounded(self):
        with TempDir() as directory:
            app = self.make(directory)
            app.waiting[CHANNEL] = [self.worker(app, f"q{number}") for number in range(WAITING_MAX)]
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "one too many"}])
            written = self.said()[-1]
        self.assertIn("full", written)
        self.assertEqual(len(app.waiting[CHANNEL]), WAITING_MAX)

    def worker(self, app: App, prompt: str) -> asking.Ask:
        return asking.Ask(app, token="tok", channel_id=CHANNEL, user_id=OWNER,
                          device_name="macbook", prompt=prompt, new=False, title="test")

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

    def test_the_answer_stays_in_the_deferred_message(self):
        self.device.script = [("idle", "the answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertTrue(self.discord.wait_for(
                lambda: self.discord.originals.get("tok", {}).get("content") == "the answer", 10))
        original = self.discord.originals["tok"]
        self.assertFalse(original["deleted"], "the answer is not deleted after it is posted")
        self.assertEqual(self.discord.deleted, [], "nothing is deleted")
        self.assertEqual(self.discord.followups, [], "one message: the deferred response itself")
        self.assertEqual(len(self.discord.edits), 1, "the placeholder became the answer")
        self.assertEqual(original["content"].strip(), "the answer")

    def test_a_long_hidden_answer_stays_ephemeral(self):
        self.device.script = [("idle", "x" * 4000)]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK, options=[
                {"name": "prompt", "type": 3, "value": "hi"},
                {"name": "hidden", "type": 5, "value": True},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: len(self.discord.followups) == 2, 10))
        self.assertTrue(self.discord.originals["tok"]["ephemeral"])
        self.assertEqual({entry["flags"] for entry in self.discord.followups}, {64},
                         "the overflow is ephemeral too")
        self.assertEqual(self.discord.deleted, [])

    def test_an_expired_interaction_still_gets_the_answer(self):
        self.device.script = [("idle", "too late to edit")]
        with TempDir() as directory:
            app = self.make(directory)
            self.discord.uneditable.add("tok")  # the 15 minutes ran out
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("too late to edit" in message["content"]
                            for message in self.discord.messages.get(CHANNEL, [])), 10))
        self.assertEqual(self.discord.deleted, [])

    def test_a_hidden_answer_that_is_too_late_is_dropped(self):
        """The public fallback of an expired message must never run for a hidden ask."""
        self.device.script = [("idle", "nobody may see this")]
        with TempDir() as directory:
            app = self.make(directory)
            self.discord.uneditable.add("tok")  # the 15 minutes ran out
            self.use(app, commands.ASK, options=[
                {"name": "prompt", "type": 3, "value": "hi"},
                {"name": "hidden", "type": 5, "value": True},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: app.finished.get(CHANNEL) == ["hi"], 10), "the run ended")
        self.assertEqual(self.discord.messages.get(CHANNEL, []), [],
                         "a hidden answer is lost, never posted")
        self.assertEqual(self.discord.followups, [])

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

    def test_one_run_per_channel_and_the_rest_wait(self):
        self.device.script = [("sleep", 1.0), ("idle", "slow")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "first"}])
            self.assertTrue(self.device.wait_for_prompt())
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "second"}])
            self.assertEqual(list(app.runs), [CHANNEL], "one run per channel")
            self.assertEqual([worker.prompt for worker in app.waiting[CHANNEL]], ["second"])
            self.assertTrue(self.discord.wait_for(
                lambda: len(self.device.prompts) == 2, 15))

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
            other = PeerServer("127.0.0.1", 0, "rotten-device", "pw",
                               checkout=peer_checkout, restart=False,
                               version="0.9.0").start()
            self.addCleanup(other.stop)
            peer_url = f"http://127.0.0.1:{other.server.server_address[1]}"

            with TempDir() as directory:
                settings = load_settings(
                    directory,
                    device="fresh-device",
                    devices=[
                        {"name": "fresh-device", "url": "http://127.0.0.1:1",
                         "directory": "/a"},
                        {"name": "rotten-device", "url": "http://127.0.0.1:2",
                         "directory": "/b", "peer": peer_url},
                    ],
                )
                api = Discord("token", settings.app_id, base=self.discord.url)
                app = App(settings, api=api, state=State(directory / "state.json"))
                self.app = app
                self.use(app, commands.UPGRADE, options=[
                    {"name": "device", "type": 3, "value": "rotten-device"},
                ])
                self.assertTrue(self.discord.wait_for(
                    lambda: any("rotten-device" in str(content)
                                for content in self.said()), 30))
                said = self.said()[-1]
            self.assertIn("pulled", said)
            self.assertNotIn("fresh-device", said)
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

    def test_devices_show_the_vigo_version_and_latest_commit(self):
        with tempfile.TemporaryDirectory() as work:
            checkout = self._checkout(Path(work))
            one = self._commit(checkout, "one")
            with TempDir() as directory:
                app = self.make(directory, checkout=checkout)
                app._build = "abc1234"
                pick = app.router.pick(fresh=True)
                written = app._devices(pick, detailed=True)
        self.assertIn(f"vigo {__version__} (abc1234)", written)
        self.assertIn(f"latest commit: {one} one", written)
        self.assertIn("this daemon runs abc1234; restart it to run", written)

    def test_devices_read_the_latest_commit_again_after_a_commit(self):
        """The daemon keeps running through a commit, so the line must follow the checkout."""
        with tempfile.TemporaryDirectory() as work:
            checkout = self._checkout(Path(work))
            one = self._commit(checkout, "one")
            with TempDir() as directory:
                app = self.make(directory, checkout=checkout)
                app._build = one
                pick = app.router.pick(fresh=True)
                before = app._devices(pick, detailed=True)
                two = self._commit(checkout, "two")
                after = app._devices(pick, detailed=True)
        self.assertIn(f"latest commit: {one} one", before)
        self.assertNotIn("restart it", before)
        self.assertIn(f"latest commit: {two} two", after)
        self.assertIn(f"this daemon runs {one}; restart it to run {two}", after)

    def _checkout(self, root: Path) -> Path:
        """A git checkout to read commits from."""
        checkout = root / "checkout"
        self._git("init", "-q", str(checkout), cwd=root)
        self._git("config", "user.email", "t@example.com", cwd=checkout)
        self._git("config", "user.name", "t", cwd=checkout)
        return checkout

    def _commit(self, checkout: Path, subject: str) -> str:
        """Commit one file named after the subject, and answer the short hash."""
        (checkout / f"{subject}.txt").write_text(subject, encoding="utf-8")
        self._git("add", ".", cwd=checkout)
        self._git("commit", "-qm", subject, cwd=checkout)
        return self._git("rev-parse", "--short", "HEAD", cwd=checkout).strip()

    @staticmethod
    def _git(*args, cwd):
        done = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True)
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
