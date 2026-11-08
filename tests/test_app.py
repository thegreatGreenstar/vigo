import dataclasses
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from vigo import __version__, commands
from vigo import ask as asking
from vigo.app import REQUEST_BUTTON, WAITING_MAX, App, _attached
from vigo.discord_api import Discord
from vigo.peer import PeerServer, build_of
from vigo.state import State
from tests.fake_discord import FakeDiscord
from tests.fake_opencode import FakeOpenCode
from tests.support import TempDir, load_settings, question_of

OWNER = "145512144145512144"
STRANGER = "888888888888888888"
GUEST = "424242424242424242"
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

    def posted(self) -> list[str]:
        """Everything the bot said anywhere, including the channel itself.

        An answer is a followup when the interaction still lives and a plain
        message when it has expired, and a real connection can drop between the
        two; a test that asserts on the answer asks this, not one route.
        """
        return self.said() + [message["content"]
                              for message in self.discord.messages.get(CHANNEL, [])]

    def use(self, app: App, sub: str, options: list | None = None,
            who: str = OWNER, token: str = "tok", interaction: str = "",
            resolved: dict | None = None):
        interaction = interaction or self.discord.next_id("int")
        payload = {
            "id": interaction,
            "token": token,
            "type": 2,
            "channel_id": CHANNEL,
            "channel": {"name": "general"},
            "member": {"user": {"id": who, "username": "someone"}},
            "data": {"name": "vigo", "options": [{"name": sub, "type": 1, "options": options or []}],
                     **({"resolved": resolved} if resolved else {})},
        }
        app.handle_interaction(payload)
        return interaction

    def asked(self) -> list[str]:
        """The questions the device got, without the audience line they carry."""
        return [question_of(question) for _session, question in self.device.prompts]

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
        self.assertIn("Only the owner of this Discord application can use `/vigo upgrade`",
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

    def test_allow_refuses_the_owner_and_the_bot(self):
        """The picker offers everyone; only someone else can be allowed."""
        with TempDir() as directory:
            app = self.make(directory)
            cases = ((app.config.owner, "always ask"), (self.discord.bot_id, "this bot"))
            for sub in (commands.ALLOW, commands.DENY):
                for who, expected in cases:
                    self.use(app, sub, options=[
                        {"name": "user", "type": 6, "value": {"id": who, "username": "x"}},
                    ])
                    self.assertIn(expected, self.discord.edits[-1]["content"])
        self.assertFalse(app.allow.may_ask(self.discord.bot_id))
        self.assertTrue(app.allow.may_ask(app.config.owner), "the owner needs no entry")

    def test_a_listed_person_may_ask(self):
        listed = "777777777777777777"
        with TempDir() as directory:
            app = self.make(directory, users=[listed])
            self.device.script = [("delta", "hello"), ("idle", "hello")]
            self.use(app, commands.ASK, who=listed,
                     options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("hello" in content for content in self.said())))

    def test_another_application_owner_has_the_owner_commands(self):
        """Vigo2's owner runs Vigo2's queue. A guest of Vigo2 never does."""
        other = "999999999999999999"
        with TempDir() as directory:
            app = self.make(directory, owner=other, users=[GUEST])
            owned = self.use(app, commands.QUEUE, who=other)
            refused = self.use(app, commands.QUEUE, who=GUEST)
        self.assertEqual(self.discord.answer_for(owned)["type"], 5,
                         "the owner's command is acknowledged, not refused")
        answer = self.discord.answer_for(refused)["data"]
        self.assertEqual(answer["flags"], 64)
        self.assertIn("Only the owner of this Discord application", answer["content"])

    # -- asking the owner for access ---------------------------------------

    def click(self, app: App, custom: str, who: str = STRANGER,
              name: str = "Alex") -> str:
        interaction = self.discord.next_id("int")
        app.handle_interaction({
            "id": interaction,
            "token": f"tok_{interaction}",
            "type": 3,
            "channel_id": "dm",
            "user": {"id": who, "username": name.lower().replace(" ", ""),
                     "global_name": name},
            "data": {"custom_id": custom},
        })
        return interaction

    def owner_cards(self) -> list[dict]:
        return [message for message in self.discord.messages.get(f"dm_{OWNER}", [])
                if "asks for access" in message["content"]]

    def test_a_stranger_is_offered_the_request_button(self):
        with TempDir() as directory:
            app = self.make(directory)
            interaction = self.use(app, commands.ASK, who=STRANGER,
                                   options=[{"name": "prompt", "type": 3, "value": "hi"}])
        answer = self.discord.answer_for(interaction)["data"]
        self.assertEqual(answer["flags"], 64)
        self.assertIn("approved", answer["content"])
        buttons = answer["components"][0]["components"]
        self.assertEqual([button["custom_id"] for button in buttons], [REQUEST_BUTTON])
        self.assertEqual(buttons[0]["label"], "Ask for access")

    def test_help_offers_the_button_and_an_approved_person_does_not_need_it(self):
        with TempDir() as directory:
            app = self.make(directory)
            stranger = self.use(app, commands.HELP, who=STRANGER)
            app.allow.allow(GUEST, "Guest", None)
            guest = self.use(app, commands.HELP, who=GUEST)
        self.assertIn("components", self.discord.answer_for(stranger)["data"])
        self.assertNotIn("components", self.discord.answer_for(guest)["data"])

    def test_requests_can_be_turned_off(self):
        with TempDir() as directory:
            app = self.make(directory, requests=False)
            interaction = self.use(app, commands.ASK, who=STRANGER,
                                   options=[{"name": "prompt", "type": 3, "value": "hi"}])
        answer = self.discord.answer_for(interaction)["data"]
        self.assertNotIn("components", answer)
        self.assertIn("allow", answer["content"].lower())

    def test_a_request_reaches_the_owner_and_an_allow_lets_them_ask(self):
        with TempDir() as directory:
            app = self.make(directory)
            asked = self.click(app, REQUEST_BUTTON)
            self.assertIn("with the owner",
                          self.discord.originals[f"tok_{asked}"]["content"])
            cards = self.owner_cards()
            self.assertEqual(len(cards), 1, "one card, not one per daemon")
            card = cards[0]
            self.assertIn("Alex", card["content"])
            self.assertNotIn(STRANGER, card["content"], "the id stays out of the text")
            labels = [button["label"] for button in card["components"][0]["components"]]
            self.assertEqual(labels, ["Allow 1 day", "Allow for good", "Deny"])

            allow_button = card["components"][0]["components"][0]
            answered = self.click(app, allow_button["custom_id"], who=OWNER)
            self.assertTrue(self.discord.wait_for(lambda: app.allow.may_ask(STRANGER)))
            self.assertTrue(app.allow.label(STRANGER).startswith("allowed until"))
            self.assertIn("allowed", self.discord.originals[f"tok_{answered}"]["content"])
            told = [message["content"] for message in
                    self.discord.messages.get(f"dm_{STRANGER}", [])]
            self.assertTrue(any("allowed you to ask" in text for text in told))

    def test_the_owner_can_allow_for_good(self):
        with TempDir() as directory:
            app = self.make(directory)
            self.click(app, REQUEST_BUTTON)
            card = self.owner_cards()[0]
            permanent = card["components"][0]["components"][1]
            self.assertEqual(permanent["label"], "Allow for good")
            self.click(app, permanent["custom_id"], who=OWNER)
            self.assertTrue(self.discord.wait_for(lambda: app.allow.may_ask(STRANGER)))
            self.assertEqual(app.allow.label(STRANGER), "allowed for good")

    def test_the_owner_can_deny_and_a_second_request_waits(self):
        with TempDir() as directory:
            app = self.make(directory)
            self.click(app, REQUEST_BUTTON)
            card = self.owner_cards()[0]
            deny = card["components"][0]["components"][2]
            self.click(app, deny["custom_id"], who=OWNER)
            self.assertFalse(app.allow.may_ask(STRANGER))
            told = [message["content"] for message in
                    self.discord.messages.get(f"dm_{STRANGER}", [])]
            self.assertTrue(any("did not allow" in text for text in told))

            self.click(app, REQUEST_BUTTON)
            self.assertEqual(len(self.owner_cards()), 1, "the denial buys an hour of quiet")
            self.assertIn("asked recently", self.discord.edits[-1]["content"])

    def test_a_display_name_cannot_carry_markdown_or_a_link(self):
        with TempDir() as directory:
            app = self.make(directory)
            self.click(app, REQUEST_BUTTON, name="[x](https://evil) *@everyone*")
            card = self.owner_cards()[0]
        self.assertIn("asks for access", card["content"])
        for bad in ("[", "]", "(", ")", "@", "://"):
            self.assertNotIn(bad, card["content"], f"a name must not introduce {bad}")
        self.assertNotIn("*", card["content"].split("**")[1])

    def test_only_the_owner_answers_a_request_card(self):
        with TempDir() as directory:
            app = self.make(directory)
            self.click(app, REQUEST_BUTTON)
            card = self.owner_cards()[0]
            allow = card["components"][0]["components"][0]
            interaction = self.click(app, allow["custom_id"], who=STRANGER)
        answer = self.discord.answer_for(interaction)["data"]
        self.assertEqual(answer["flags"], 64)
        self.assertIn("Only the bot's owner", answer["content"])
        self.assertFalse(app.allow.may_ask(STRANGER))

    def test_one_daemon_sends_the_card(self):
        """Discord accepts one acknowledgement; the other daemon stays quiet."""
        with TempDir() as directory:
            app = self.make(directory)
            interaction = self.discord.next_id("int")
            payload = {
                "id": interaction, "token": "tok_once", "type": 3,
                "channel_id": "dm", "user": {"id": STRANGER, "username": "alex"},
                "data": {"custom_id": REQUEST_BUTTON},
            }
            app.handle_interaction(payload)
            app.handle_interaction(payload)
        self.assertEqual(len(self.owner_cards()), 1)

    def test_a_pending_request_is_not_sent_twice(self):
        with TempDir() as directory:
            app = self.make(directory)
            self.click(app, REQUEST_BUTTON)
            self.click(app, REQUEST_BUTTON)
        self.assertEqual(len(self.owner_cards()), 1)
        self.assertIn("already with the owner", self.discord.edits[-1]["content"])

    def test_the_owner_is_told_when_the_requester_dm_is_closed(self):
        with TempDir() as directory:
            app = self.make(directory)
            self.click(app, REQUEST_BUTTON)
            card = self.owner_cards()[0]
            allow = card["components"][0]["components"][0]
            self.discord.no_dm.add(STRANGER)
            answered = self.click(app, allow["custom_id"], who=OWNER)
        self.assertIn("could not reach their DM",
                      self.discord.originals[f"tok_{answered}"]["content"])

    def test_a_closed_owner_dm_is_said_and_the_request_can_be_tried_again(self):
        with TempDir() as directory:
            app = self.make(directory, stateChannel="999999999999999999")
            self.discord.no_dm.add(OWNER)
            asked = self.click(app, REQUEST_BUTTON)
            self.assertIn("could not reach",
                          self.discord.originals[f"tok_{asked}"]["content"])
            self.assertEqual(app.state.request_state(STRANGER), "")
            self.discord.no_dm.discard(OWNER)
            self.click(app, REQUEST_BUTTON)
        self.assertEqual(len(self.owner_cards()), 1)

    # -- the guest share of the plan ---------------------------------------

    def test_an_approved_persons_session_carries_the_mark(self):
        """The device records whose session it is, for the plan's accounting."""
        self.device.script = [("idle", "done")]
        with TempDir() as directory:
            app = self.make(directory, users=[GUEST])
            self.use(app, commands.ASK, who=GUEST, options=[
                {"name": "prompt", "type": 3, "value": "what is a pointer"},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: any("done" in content for content in self.said()), 10))
        session = self.device.prompts[0][0]
        self.assertEqual(self.device.sessions[session]["metadata"]["vigoGuest"], GUEST)
        self.assertEqual(self.device.sessions[session]["metadata"][asking.CHANNEL_KEY],
                         f"{CHANNEL}:{GUEST}")

    def test_a_session_from_before_the_mark_is_marked_when_it_is_used(self):
        """A guest session made by an older vigo is marked on its next ask."""
        session = self.device.new_session(
            metadata={asking.CHANNEL_KEY: f"{CHANNEL}:{GUEST}"})
        self.device.script = [("idle", "done")]
        with TempDir() as directory:
            app = self.make(directory, users=[GUEST])
            self.use(app, commands.ASK, who=GUEST, options=[
                {"name": "prompt", "type": 3, "value": "what is a pointer"},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: any("done" in content for content in self.said()), 10))
        self.assertEqual(len(self.device.prompts), 1)
        self.assertEqual(self.device.prompts[0][0], session, "the old session is reused")
        self.assertEqual(self.device.sessions[session]["metadata"]["vigoGuest"], GUEST)

    def test_the_share_refuses_a_guest_before_the_device_is_asked(self):
        self.device.meters = {"monthly": 90.0}
        used = self.device.new_session(metadata={"vigoGuest": GUEST})
        self.device.add_message(used, "assistant", tokens={
            "input": 1_000_000, "output": 0, "reasoning": 0})
        with TempDir() as directory:
            app = self.make(
                directory, users=[GUEST],
                guests={"hourly": 0, "daily": 0,
                        "perUserHourly": 0, "perUserDaily": 0},
            )
            self.use(app, commands.ASK, who=GUEST, options=[
                {"name": "prompt", "type": 3, "value": "what is a pointer"},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: any("share of the owner's plan" in content
                            for content in self.said()), 10))
        self.assertEqual(self.device.prompts, [], "a refusal costs the plan nothing")

    def test_a_guest_below_the_share_still_asks(self):
        self.device.meters = {"monthly": 5.0}
        self.device.script = [("idle", "the answer")]
        with TempDir() as directory:
            app = self.make(directory, users=[GUEST])
            self.use(app, commands.ASK, who=GUEST, options=[
                {"name": "prompt", "type": 3, "value": "what is a pointer"},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: any("the answer" in content for content in self.said()), 10))
        self.assertEqual(len(self.device.prompts), 1)

    def test_the_owner_is_never_capped_by_the_guest_share(self):
        self.device.meters = {"monthly": 100.0}
        self.device.limited_meters = ("rolling",)
        self.device.script = [("idle", "the owner's answer")]
        with TempDir() as directory:
            app = self.make(directory, users=[GUEST])
            self.use(app, commands.ASK, options=[
                {"name": "prompt", "type": 3, "value": "what is a pointer"},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: any("the owner's answer" in content for content in self.said()), 10))
        self.assertEqual(len(self.device.prompts), 1)

    def test_the_status_names_what_guests_have_used(self):
        with TempDir() as directory:
            app = self.make(directory, users=[GUEST])
            self.use(app, commands.STATUS)
            self.assertTrue(self.discord.wait_for(
                lambda: any("**Guests**" in content for content in self.said()), 10))
        said = next(content for content in self.said() if "**Guests**" in content)
        self.assertIn("33.3%", said)
        self.assertIn("today", said)

    def test_an_approved_persons_one_off_is_refused(self):
        """A one-off leaves no session, so its use of the plan cannot be counted."""
        self.device.answer = "a " * 1200
        with TempDir() as directory:
            app = self.make(directory, users=[GUEST])
            self.use(app, commands.QUICK, who=GUEST, options=[
                {"name": "prompt", "type": 3, "value": "say hi"},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: any("is the owner's" in content for content in self.said()), 10))
        self.assertEqual(self.device.generated, [], "nothing was sent to the device")
        self.assertEqual(self.discord.messages.get(CHANNEL, []), [])

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
        self.assertTrue(final.startswith("hello world"),
                        "the answer leads; a task names the session it ran in after it")
        self.assertEqual(self.asked()[0], "say hi")

    def test_an_older_daemon_leaves_the_use_to_a_newer_peer(self):
        peer_url = self._peer(built=2000)
        with TempDir() as directory:
            app = self._two_devices(directory, peer_url)
            app._built = 1000
            interaction = self.use(app, commands.ASK,
                                   options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertIsNone(self.discord.answer_for(interaction),
                              "the older daemon does not acknowledge the use")
            self.assertEqual(self.device.prompts, [],
                             "and no device turn is spent on it")

    def test_a_newer_daemon_answers_beside_an_older_peer(self):
        peer_url = self._peer(built=1000)
        with TempDir() as directory:
            app = self._two_devices(directory, peer_url)
            app._built = 2000
            interaction = self.use(app, commands.ASK,
                                   options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertIsNotNone(self.discord.answer_for(interaction),
                                 "the newer daemon acknowledges it")
            self.assertTrue(self.device.wait_for_prompt())

    def _peer(self, built: int) -> str:
        """A peer daemon that answers with the commit time a test names."""
        server = PeerServer("127.0.0.1", 0, "workstation", "pw", built=built).start()
        self.addCleanup(server.stop)
        return f"http://127.0.0.1:{server.server.server_address[1]}"

    def _two_devices(self, directory: Path, peer_url: str) -> App:
        settings = load_settings(
            directory,
            devices=[
                {"name": "macbook", "url": self.device.url, "directory": "/tmp/project"},
                {"name": "workstation", "url": "http://127.0.0.1:1",
                 "directory": "/tmp/other", "peer": peer_url},
            ],
        )
        api = Discord("token", settings.app_id, base=self.discord.url)
        self.app = App(settings, api=api, state=State(directory / "state.json"))
        return self.app

    def test_the_device_is_told_who_reads_the_answer(self):
        self.device.script = [("idle", "fine")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "where is the config"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("fine" in content for content in self.said()), 10))
        line = self.device.prompts[0][1].split("\n\n", 1)[0]
        self.assertIn("the owner asked this", line)
        self.assertIn("everyone in #general sees the answer", line)
        self.assertIn("Keep credentials", line)

    def test_a_hidden_answer_is_told_it_is_the_askers_alone(self):
        listed = "444444444444444444"
        self.device.script = [("idle", "fine")]
        with TempDir() as directory:
            app = self.make(directory, users=[listed])
            self.use(app, commands.ASK, who=listed, options=[
                {"name": "prompt", "type": 3, "value": "where is the config"},
                {"name": "hidden", "type": 5, "value": True},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: any("fine" in content for content in self.said()), 10))
        line = self.device.prompts[0][1].split("\n\n", 1)[0]
        self.assertIn("someone the owner allowed asked this", line)
        self.assertIn("only the asker sees the answer", line)
        self.assertNotIn("Keep credentials", line, "the asker may see what they asked about")

    def test_a_public_answer_has_its_secret_removed(self):
        self.device.script = [("idle", "use sk-proj-abcdefghijklmnopqrstuvwxyz0123 for it")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "which key"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("for it" in content for content in self.said()), 10))
        answer = next(content for content in self.said() if "for it" in content)
        self.assertNotIn("sk-proj-abcdefghijklmnopqrstuvwxyz0123", answer)
        self.assertIn("[redacted: API key]", answer)
        self.assertIn("was removed from this answer", answer)

    def test_a_hidden_answer_keeps_its_secret(self):
        self.device.script = [("idle", "use sk-proj-abcdefghijklmnopqrstuvwxyz0123 for it")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK, options=[
                {"name": "prompt", "type": 3, "value": "which key"},
                {"name": "hidden", "type": 5, "value": True},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: any("for it" in content for content in self.said()), 10))
        answer = next(content for content in self.said() if "for it" in content)
        self.assertIn("sk-proj-abcdefghijklmnopqrstuvwxyz0123", answer,
                      "the asker alone reads it")
        self.assertNotIn("[redacted", answer)

    def test_a_file_attached_to_an_ask_reaches_the_agent(self):
        """A log is read on the device: the question says where it is and how."""
        self.device.script = [("idle", "it did not boot")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "why does it crash?"},
                              {"name": "file", "type": 11, "value": "att_1"}],
                     resolved={"attachments": {"att_1": {
                         "id": "att_1", "filename": "crash.log", "size": 4096,
                         "url": "https://cdn.discordapp.com/attachments/1/2/crash.log?ex=signed",
                     }}})
            self.assertTrue(self.discord.wait_for(
                lambda: any("it did not boot" in str(content)
                            for content in self.said()), 10))
        told = self.device.prompts[0][1]
        self.assertIn("why does it crash?", told, "the person's words stay first")
        self.assertIn("“crash.log”, 4 KB", told)
        self.assertIn("https://cdn.discordapp.com/attachments/1/2/crash.log?ex=signed", told)
        self.assertIn("curl -L -o /tmp/vigo-attached-att_1", told)
        self.assertIn("Never `cat` a file whole", told, "a log is grepped, not printed")

    def test_an_image_attached_to_an_ask_is_shown_to_the_model(self):
        """An image is not a log: it goes to the read tool, not to grep."""
        self.device.script = [("idle", "the button is cut off")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "what is wrong?"},
                              {"name": "file", "type": 11, "value": "att_2"}],
                     resolved={"attachments": {"att_2": {
                         "id": "att_2", "filename": "screen.png", "size": 2048,
                         "content_type": "image/png",
                         "url": "https://cdn.discordapp.com/attachments/1/2/screen.png?ex=signed",
                     }}})
            self.assertTrue(self.discord.wait_for(
                lambda: any("cut off" in str(content) for content in self.said()), 10))
        told = self.device.prompts[0][1]
        self.assertIn("“screen.png”, 2 KB", told)
        self.assertIn("curl -L -o /tmp/vigo-attached-att_2", told)
        self.assertIn("read tool", told)
        self.assertIn("PNG, JPEG, GIF or WebP", told)
        self.assertNotIn("grep -n", told, "an image is not grepped")
        self.assertNotIn("Never `cat`", told)

    def test_the_name_decides_how_a_file_without_a_content_type_is_read(self):
        """Discord does not always send a content type; the extension still says."""
        told = _attached("look", {
            "id": "att_3", "filename": "screenshot.webp",
            "url": "https://cdn.discordapp.com/attachments/1/2/screenshot.webp",
        })
        self.assertIn("read tool", told)
        self.assertNotIn("grep -n", told)

    def test_a_pdf_and_an_archive_get_the_tool_their_format_needs(self):
        pdf = _attached("read", {
            "id": "att_4", "filename": "report.pdf", "url": "https://x/y.pdf",
        })
        self.assertIn("read tool", pdf)
        self.assertIn("PDF", pdf)
        archive = _attached("read", {
            "id": "att_5", "filename": "build.zip", "content_type": "application/zip",
            "url": "https://x/y.zip",
        })
        self.assertIn("`file`", archive)
        self.assertIn("`unzip -l`", archive)
        self.assertNotIn("read tool", archive)
        self.assertNotIn("grep -n", archive)

    def test_a_file_discord_did_not_send_is_named_instead_of_lost(self):
        """The field can arrive without its attachment: say so, do not ask blind."""
        self.device.script = [("idle", "no file was there")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "read this"},
                              {"name": "file", "type": 11, "value": "att_lost"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("did not send that file" in str(content)
                            for content in self.said()), 10))
        self.assertEqual(self.device.prompts, [], "a lost file does not spend the plan")

    def test_a_file_with_no_name_is_still_offered(self):
        """Discord always names it; a device that loses the name is not a reason to drop it."""
        name, options = commands.flatten({
            "options": [{"name": "ask", "type": 1, "options": [
                {"name": "prompt", "type": 3, "value": "read it"},
                {"name": "file", "type": 11, "value": "att_1"},
            ]}],
            "resolved": {"attachments": {"att_1": {"url": "https://x/y"}}},
        })
        self.assertEqual(name, "ask")
        self.assertEqual(options["file"]["url"], "https://x/y")
        self.assertIsNone(options["file"]["filename"])
        self.assertIn("“a file”", _attached("read it", options["file"]))

    def test_a_second_ask_in_the_channel_waits_its_turn(self):
        finishing = threading.Event()
        self.addCleanup(finishing.set)
        self.device.script = [("until", finishing), ("idle", "the answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "first"}])
            self.assertTrue(self.device.wait_for_prompt())
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "second"}])
            self.assertTrue(any("Queued" in str(content) for content in self.said()))
            self.assertEqual(self.asked(), ["first"],
                             "the second question waits for the first")
            finishing.set()
            self.assertTrue(self.discord.wait_for(
                lambda: len(self.device.prompts) == 2, 15))
        self.assertEqual(self.asked(), ["first", "second"])

    def test_a_queued_question_says_it_still_runs_in_its_own_line(self):
        """Past `timeout` the note ends the queued line, and no message arrives beside it."""
        first, second = threading.Event(), threading.Event()
        self.addCleanup(first.set)
        self.addCleanup(second.set)
        self.device.script = [("until", first), ("idle", "the first answer")]
        with TempDir() as directory:
            app = self.make(directory)
            app.config = dataclasses.replace(app.config, timeout=0.2)
            self.use(app, commands.ASK, token="tok_first",
                     options=[{"name": "prompt", "type": 3, "value": "first"}])
            self.assertTrue(self.device.wait_for_prompt())
            self.use(app, commands.ASK, token="tok_second",
                     options=[{"name": "prompt", "type": 3, "value": "second"}])
            queued = self.discord.originals["tok_second"]["content"]
            self.assertTrue(queued.startswith("Queued behind 1 question"), queued)
            self.device.script = [("until", second), ("idle", "the second answer")]
            first.set()
            note = "Still running on the device. `/vigo stop` ends it."
            self.assertTrue(self.discord.wait_for(
                lambda: self.discord.originals["tok_second"]["content"]
                == f"{queued}\n\n{note}", 10),
                "the queued line grows the note at its end")
            second.set()
            self.assertTrue(self.discord.wait_for(
                lambda: any("the second answer" in said for said in self.said()), 15))
        self.assertFalse([said for said in self.discord.followups
                          if note in str(said.get("content") or "")],
                         "the note is not a message of its own")

    def test_a_note_keeps_the_line_under_discords_limit(self):
        """A line at the limit gives way to the note, never the other way round."""
        line = "q" * 2000
        note = "Still running on the device. `/vigo stop` ends it."
        grown = asking._grown(line, note)
        self.assertLessEqual(len(grown), 2000)
        self.assertTrue(grown.endswith(note), "the note is whole")
        self.assertIn("…", grown, "the cut line says it was cut")

    def test_a_note_without_a_line_is_still_said(self):
        """The acknowledgement never landed, so there is no line to grow."""
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.device.script = [("until", gate), ("idle", "the late answer")]
        with TempDir() as directory:
            app = self.make(directory)
            app.config = dataclasses.replace(app.config, timeout=0.2)
            self.discord.uneditable.add("tok")  # the deferred message is gone
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "hi"}])
            note = "Still running on the device. `/vigo stop` ends it."
            self.assertTrue(self.discord.wait_for(
                lambda: any(content == note for content in self.posted()), 10),
                "the note goes out alone when there is no line to edit")

    def test_a_note_edits_the_line_after_the_interaction_token_expires(self):
        """Discord stops taking the token after 15 minutes; the line is still there.

        A question that waits its turn spends that time in the queue, so the
        run that starts later can only edit its message by the channel's id.
        The note belongs on that line, not in a message beside it.
        """
        first, second = threading.Event(), threading.Event()
        self.addCleanup(first.set)
        self.addCleanup(second.set)
        self.device.script = [("until", first), ("idle", "the first answer")]
        with TempDir() as directory:
            app = self.make(directory)
            app.config = dataclasses.replace(app.config, timeout=0.2)
            self.use(app, commands.ASK, token="tok_first",
                     options=[{"name": "prompt", "type": 3, "value": "first"}])
            self.assertTrue(self.device.wait_for_prompt())
            self.use(app, commands.ASK, token="tok_second",
                     options=[{"name": "prompt", "type": 3, "value": "second"}])
            queued = self.discord.originals["tok_second"]["content"]
            self.assertTrue(queued.startswith("Queued behind 1 question"), queued)
            # The queue outlives the interaction: Discord takes no edit on it.
            self.discord.uneditable.add("tok_second")
            self.device.script = [("until", second), ("idle", "the second answer")]
            first.set()
            note = "Still running on the device. `/vigo stop` ends it."
            self.assertTrue(self.discord.wait_for(
                lambda: self.discord.originals["tok_second"]["content"]
                == f"{queued}\n\n{note}", 10),
                "the queued line grows the note at its end")
            second.set()
            self.assertTrue(self.discord.wait_for(
                lambda: any("the second answer" in said for said in self.posted()), 15))
        self.assertFalse([said for said in self.discord.followups
                          if note in str(said.get("content") or "")],
                         "the note is not a message of its own")
        self.assertFalse([message for message in self.discord.messages.get(CHANNEL, [])
                          if note in message["content"]],
                         "the note is not a channel message either")

    def test_a_run_past_the_timeout_keeps_the_channel_busy(self):
        finishing = threading.Event()
        self.addCleanup(finishing.set)
        self.device.script = [("until", finishing), ("idle", "the late answer")]
        with TempDir() as directory:
            app = self.make(directory)
            app.config = dataclasses.replace(app.config, timeout=0.2)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "slow"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("running on the device" in str(content)
                            for content in self.said()), 10))
            self.assertIsNotNone(app.run_for(CHANNEL), "the run keeps the channel until it ends")
            shown = app._status(CHANNEL)
            self.assertIn("running for", shown)
            self.assertIn("still running on the device", shown)
            finishing.set()
            self.assertTrue(self.discord.wait_for(
                lambda: any("the late answer" in str(content) for content in self.said()), 15))
            # The answer is in the channel before the run lets go: releasing the
            # claim and unregistering happen just after it is posted.
            self.assertTrue(self.discord.wait_for(
                lambda: app.run_for(CHANNEL) is None, 10),
                "the run lets the channel go once it answers")

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

    def test_status_does_not_call_a_lost_turn_running(self):
        """The stamp a device left behind is not a turn, and must not grow."""
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord DM")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord DM")
            self.device.started_turn(session)
            self.device.lost_turn(session)
            shown = app._status(CHANNEL)
        self.assertIn("idle", shown)
        self.assertNotIn("still running", shown)

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

    def test_a_task_starts_while_the_device_answers_a_question(self):
        """A task runs in a session of its own, so a busy conversation is no reason to refuse."""
        self.device.script = [("idle", "the work is done")]
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord DM")
            self.device.started_turn(session)
            app.state.remember_session(CHANNEL, "macbook", session, "Discord DM")
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "fix the failing test"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("the work is done" in str(content) for content in self.said()), 10))
            task = app.state.session_for(CHANNEL)["session"]
        self.assertNotEqual(task, session, "the task got a session of its own")
        self.assertNotIn("still working", " ".join(self.said()))

    def test_a_question_does_not_join_a_run_another_daemon_drives(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord DM")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord DM")
            self.device.started_turn(session)
            self.use(app, commands.ASK, options=[
                {"name": "prompt", "type": 3, "value": "why is the second one red?"},
            ])
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

    # -- goals ------------------------------------------------------------

    def goals(self):
        """What the device was told, in order: (name, text) per command."""
        return [(name, text) for _session, name, text in self.device.commands]

    def test_the_ask_command_has_no_goal_option(self):
        """The wording decides, so Discord offers no switch that says which it is."""
        ask = next(option for option in commands.spec()[0]["options"]
                   if option["name"] == commands.ASK)
        self.assertEqual([inner["name"] for inner in ask["options"]],
                         ["prompt", "file", "device", "hidden", "new"])

    def test_the_device_option_names_the_devices_in_the_settings(self):
        """No alias is written in the code: the settings say which devices exist."""
        spec = commands.spec(devices=["alpha", "beta"])
        ask = next(option for option in spec[0]["options"]
                   if option["name"] == commands.ASK)
        device = next(inner for inner in ask["options"] if inner["name"] == "device")
        self.assertEqual(device["description"],
                         "alpha, beta; empty picks where you last worked.")
        self.assertNotIn("macbook", str(spec))

    def test_a_long_device_list_is_counted_rather_than_named(self):
        devices = ["a" * 30, "b" * 30, "c" * 30]
        spec = commands.spec(devices=devices)
        ask = next(option for option in spec[0]["options"]
                   if option["name"] == commands.ASK)
        device = next(inner for inner in ask["options"] if inner["name"] == "device")
        self.assertLessEqual(len(device["description"]), 100)
        self.assertIn("3 devices", device["description"])

    def test_a_task_ask_gives_the_worker_a_goal(self):
        self.device.script = [("idle", "done")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3,
                                "value": "fix the failing test"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("done" in str(content) for content in self.said()), 10))
            session = app.state.session_for(CHANNEL)["session"]
        self.assertEqual([(kind, question_of(text)) for kind, text in self.goals()],
                         [("goal", "fix the failing test")])
        self.assertEqual(question_of(self.device.goals[session]["objective"]),
                         "fix the failing test")
        self.assertEqual(self.device.sessions[session]["agent"], "build",
                         "the work runs under a primary agent, which can delegate subagents")

    def test_a_question_ask_only_asks(self):
        self.device.script = [("idle", "because the table moved")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why is CI red?"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("because" in str(content) for content in self.said()), 10))
        self.assertEqual(self.goals(), [], "a question is answered, not chased")
        self.assertEqual(self.asked(),
                         ["why is CI red?"])

    def test_the_goal_command_sets_it_in_a_session_of_its_own(self):
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.GOAL, options=[
                {"name": "objective", "type": 3, "value": "keep the build green"},
            ])
            written = self.said()[-1]
            session = app.state.session_for(CHANNEL)["session"]
        self.assertEqual(self.goals(), [("goal", "keep the build green")])
        self.assertEqual(self.device.goals[session]["objective"], "keep the build green")
        self.assertEqual(self.device.sessions[session]["agent"], "build",
                         "the work runs under a primary agent, which can delegate subagents")
        self.assertIn("session of its own", written)

    def test_a_second_objective_steers_the_goal_the_channel_has(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord #general")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord #general")
            self.device.goals[session] = {"objective": "ship it", "status": "active"}
            self.use(app, commands.GOAL, options=[
                {"name": "objective", "type": 3, "value": "keep the build green"},
            ])
            written = self.said()[-1]
            goal = app.state.session_for(CHANNEL)["session"]
        self.assertEqual(goal, session, "a channel with a goal keeps its session")
        self.assertEqual(len(self.device.sessions), 1, "a second objective starts no session")
        self.assertEqual(self.device.goals[session]["objective"], "keep the build green")
        self.assertIn("goal is set", written)

    def test_the_goal_command_controls_the_goal_the_channel_has(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord #general")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord #general")
            self.device.goals[session] = {"objective": "ship it", "status": "active"}
            self.use(app, commands.GOAL, options=[
                {"name": "objective", "type": 3, "value": "pause"},
            ])
            written = self.said()[-1]
        self.assertEqual(self.goals(), [("goal", "pause")])
        self.assertEqual(self.device.goals[session]["status"], "paused")
        self.assertEqual(len(self.device.sessions), 1, "a control keeps the session")
        self.assertIn("paused", written)

    def test_a_goal_to_control_needs_one_in_the_channel(self):
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.GOAL, options=[
                {"name": "objective", "type": 3, "value": "clear"},
            ])
            written = self.said()[-1]
        self.assertEqual(self.device.commands, [], "nothing to clear, nothing sent")
        self.assertIn("no goal", written.lower())

    def test_a_goal_waits_while_a_question_runs(self):
        with TempDir() as directory:
            app = self.make(directory)
            app.register_run(CHANNEL, asking.Run(session="ses_x", device="macbook",
                                                 started=0.0))
            try:
                self.use(app, commands.GOAL, options=[
                    {"name": "objective", "type": 3, "value": "ship it"},
                ])
                written = self.said()[-1]
            finally:
                app.unregister_run(CHANNEL)
        self.assertEqual(self.device.commands, [])
        self.assertIn("question is running", written)
        self.assertIn("`/vigo goal pause`", written)

    def test_status_names_the_goal_the_device_holds(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord #general")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord #general")
            self.device.goals[session] = {
                "objective": "keep the build green",
                "status": "paused",
                "reason": "the server restarted",
            }
            shown = app._status(CHANNEL)
        self.assertIn("Goal", shown)
        self.assertIn("paused", shown)
        self.assertIn("keep the build green", shown)
        self.assertIn("the server restarted", shown)

    def test_status_says_nothing_about_a_goal_the_device_does_not_have(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord #general")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord #general")
            shown = app._status(CHANNEL)
        self.assertNotIn("Goal", shown)

    # -- a question the device never starts -------------------------------

    def test_a_question_the_device_never_starts_ends_the_run(self):
        """The drain that owned the session is stuck: say so, and unblock."""
        self.device.hold_next = 1
        with TempDir() as directory:
            app = self.make(directory)
            app.config = dataclasses.replace(app.config, timeout=0.2)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why is CI red?"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("never started" in str(content) for content in self.said()), 10))
            self.assertTrue(self.discord.wait_for(
                lambda: app.run_for(CHANNEL) is None, 5))
        written = self.said()[-1]
        self.assertIn("restart", written.lower())

    def test_a_device_that_stamps_itself_busy_and_took_nothing_ends_the_run(self):
        """The second shape: a stale busy stamp over a question it never read.

        The real device did this twice in one morning: the drain marked the
        session busy, never wrote the question, and never stepped.  The stamp
        is not evidence of work, so the run must not wait on it.  The session
        already has an answer in it, which is what makes the stamp the only
        thing that could be mistaken for progress.
        """
        self.device.script = [("idle", "the first answer")]
        with TempDir() as directory:
            app = self.make(directory)
            app.config = dataclasses.replace(app.config, timeout=0.2)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why is CI red?"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("the first answer" in str(content) for content in self.said()), 10))
            self.assertTrue(self.discord.wait_for(
                lambda: app.run_for(CHANNEL) is None, 5))
            self.device.hold_next = 1
            self.device.hold_busy = True
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "and the second?"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("never started" in str(content) for content in self.said()), 15))
            self.assertTrue(self.discord.wait_for(
                lambda: app.run_for(CHANNEL) is None, 5))
        self.assertIn("restart", self.said()[-1].lower())

    def test_a_question_the_device_wrote_but_never_started_ends_the_run(self):
        """The third shape: the question is on the session, and no turn reads it.

        The device wrote it and its drain never started a turn.  No idle will
        ever come, so the run must end and say the device is stuck.
        """
        self.device.write_only = True
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why is CI red?"}])
            self.assertTrue(self.device.wait_for_prompt())
            session = app.run_for(CHANNEL).session
            self.assertTrue(self.discord.wait_for(
                lambda: any(message.get("type") == "user"
                            for message in self.device.messages.get(session, [])), 2))
            # Past the grace a starting turn gets, and still no execution.  The
            # stream ends too, so the run reads the session instead of events.
            with self.device.lock:
                for message in self.device.messages.get(session, []):
                    if message.get("type") == "user":
                        message["time"]["created"] -= 20_000
            self.device.cut_events()
            self.assertTrue(self.discord.wait_for(
                lambda: any("never started" in str(content) for content in self.said()), 10))
            self.assertTrue(self.discord.wait_for(
                lambda: app.run_for(CHANNEL) is None, 5))
        self.assertIn("restart", self.said()[-1].lower())

    def test_a_turn_the_device_stops_running_ends_the_run(self):
        """A restart of the device tears a turn down without writing its idle.

        The event stream ends with it, so the run polls a session that will
        never say it finished.  The device's own list of running sessions says
        no turn runs there, and the channel must not wait for one that is not
        coming.
        """
        self.device.script = [("cut",), ("lost", "half of an answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why is CI red?"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("half of an answer" in str(content) for content in self.posted()), 15))
            self.assertTrue(self.discord.wait_for(
                lambda: app.run_for(CHANNEL) is None, 5))
        self.assertIn("stopped this turn", self.said()[-1])

    def test_a_question_runs_when_the_device_left_a_busy_stamp_behind(self):
        """A turn torn down leaves the session looking busy for ever.

        The device's list says nothing runs there.  A question must not wait
        for the turn that is gone: it claims the channel, asks, and the device
        starts a new turn on the same session.
        """
        with TempDir() as directory:
            app = self.make(directory)
            app.config = dataclasses.replace(app.config, timeout=0.5)
            session = self.device.new_session("Discord #general")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord #general")
            self.device.started_turn(session)
            self.device.lost_turn(session)
            self.device.script = [("idle", "the answer after the restart")]
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why is CI red?"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("the answer after the restart" in str(content)
                            for content in self.posted()), 10))
        self.assertEqual(self.asked(), ["why is CI red?"])

    def test_the_next_question_runs_when_a_run_ends_without_an_answer(self):
        self.device.hold_next = 1
        self.device.script = [("idle", "the second answer")]
        with TempDir() as directory:
            app = self.make(directory)
            app.config = dataclasses.replace(app.config, timeout=0.2)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why is CI red?"}])
            self.assertTrue(self.device.wait_for_prompt())
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why is the build slow?"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("the second answer" in str(content) for content in self.said()), 20))
        self.assertEqual(self.asked(),
                         ["why is CI red?", "why is the build slow?"])

    def test_an_answer_the_channel_already_has_is_not_posted_again(self):
        """Two runs can watch one device turn and both post the same answer."""
        self.device.script = [("idle", "an identical answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.discord.add_message(CHANNEL, "an identical answer", bot=True)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why is CI red?"}])
            self.assertTrue(self.discord.wait_for(
                lambda: app.run_for(CHANNEL) is None, 10))
        # A followup is a channel message in Discord; the fake keeps the two
        # apart, so count both.
        posted = [message for message in self.discord.messages[CHANNEL]
                  if message["content"].strip() == "an identical answer"]
        posted += [entry for entry in self.discord.followups
                   if (entry.get("content") or "").strip() == "an identical answer"]
        self.assertEqual(len(posted), 1, "the channel already had it")

    def test_an_answer_is_posted_when_the_channel_does_not_have_it(self):
        self.device.script = [("idle", "a fresh answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why is CI red?"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("a fresh answer" in str(content) for content in self.posted()), 10))

    def test_a_run_that_ends_does_not_take_the_next_ones_place(self):
        """The run that ends gives up its own place, never the next one's.

        A run unregisters from its own thread and again from its `finally`.
        Taking whatever is registered there starts the question behind it while
        it is still running, which is how one device turn came to be answered
        twice in the same channel.
        """
        with TempDir() as directory:
            app = self.make(directory)
            first = asking.Run(session="ses_1", device="macbook", started=0.0, prompt="first")
            second = asking.Run(session="ses_2", device="macbook", started=0.0, prompt="second")
            app.register_run(CHANNEL, first)
            app.unregister_run(CHANNEL, first)
            app.register_run(CHANNEL, second)
            app.unregister_run(CHANNEL, first)  # the same run, ending a second time
            self.assertIs(app.run_for(CHANNEL), second, "the next run keeps the channel")
            self.assertEqual(app.finished[CHANNEL], ["first"])

    def test_a_queued_question_waits_while_another_daemon_is_asking(self):
        """One turn at a time on the session, not one per daemon.

        The question is queued while this daemon is busy, and by the time its
        turn comes the device is busy with a turn this daemon does not own — the
        shape that used to put two questions into one device turn.
        """
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.device.stay_busy = True
        self.device.script = [("until", gate), ("idle", "the first answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why first?"}])
            self.assertTrue(self.device.wait_for_prompt())
            self.device.script = [("idle", "the second answer")]
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why second?"}])
            gate.set()
            self.assertTrue(self.discord.wait_for(
                lambda: any("the first answer" in str(content) for content in self.posted()), 10))
            # One prompt so far, and a second must not follow while the device
            # still looks busy: `wait_for` polls, so this is a bounded look.
            self.assertFalse(self.discord.wait_for(
                lambda: len(self.device.prompts) > 1, 0.3),
                "the device still looks busy: nothing is sent into it")
            session = app.state.session_for(CHANNEL)["session"]
            self.device.finished_turn(session)
            self.assertTrue(self.discord.wait_for(
                lambda: any("the second answer" in str(content) for content in self.posted()), 10))
        self.assertEqual(self.asked(),
                         ["why first?", "why second?"], "no turn was shared")

    def test_a_question_gives_up_when_the_device_stays_busy(self):
        with TempDir() as directory:
            app = self.make(directory)
            app.config = dataclasses.replace(app.config, timeout=0.2)
            gate = threading.Event()
            self.addCleanup(gate.set)
            self.device.stay_busy = True
            self.device.script = [("until", gate), ("idle", "the first answer")]
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why first?"}])
            self.assertTrue(self.device.wait_for_prompt())
            self.device.script = [("idle", "the second answer")]
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why second?"}])
            gate.set()
            self.assertTrue(self.discord.wait_for(
                lambda: any("did not run" in str(content) for content in self.said()), 10))
            self.assertTrue(self.discord.wait_for(
                lambda: app.run_for(CHANNEL) is None, 5))
        self.assertEqual(self.asked(), ["why first?"],
                         "nothing was sent into another turn")

    def test_the_second_question_holds_the_channel_while_it_works(self):
        """A gated turn, not a sleeping one: the queue's own bookkeeping."""
        finishing = threading.Event()
        self.addCleanup(finishing.set)
        self.device.script = [("until", finishing), ("idle", "the first answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why first?"}])
            self.assertTrue(self.device.wait_for_prompt())
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why second?"}])
            self.assertTrue(any("Queued" in str(content) for content in self.said()))
            self.assertEqual(len(self.device.prompts), 1, "one turn at a time")
            finishing.set()
            self.assertTrue(self.discord.wait_for(
                lambda: any("the first answer" in str(content) for content in self.said()), 10))

    def test_status_names_the_next_up_and_the_questions_that_ran(self):
        with TempDir() as directory:
            app = self.make(directory)
            app.finished[CHANNEL] = ["old one", "old two", "old three", "old four"]
            app.waiting[CHANNEL] = [self.worker(app, f"next {number}") for number in range(1, 6)]
            self.use(app, commands.STATUS)
            written = self.said()[-1]
        self.assertIn("**Next up** (5 waiting):", written)
        for number in range(1, 6):
            self.assertIn(f"{number}. “next {number}”", written, "every task is named, not a sample")
        self.assertIn("**Finished:**", written)
        self.assertIn("“old four”", written)
        self.assertNotIn("“old one”", written, "only the last three that ran are named")

    def test_queue_names_every_channel_that_runs_or_waits(self):
        with TempDir() as directory:
            app = self.make(directory)
            app.state.remember_session(CHANNEL, "macbook", "ses_1", "Discord #general")
            app.waiting[CHANNEL] = [self.worker(app, "the waiting task"),
                                    self.worker(app, "the task behind it")]
            app.finished[CHANNEL] = ["the one that ran"]
            self.use(app, commands.QUEUE)
            written = self.said()[-1]
        self.assertIn("“Discord #general”", written)
        self.assertIn("2 waiting", written)
        self.assertIn("1. “the waiting task”", written)
        self.assertIn("2. “the task behind it”", written)
        self.assertNotIn("the one that ran", written, "that left the queue already")

    def test_the_queue_lists_the_run_in_slot_zero(self):
        """The task at work is in the list, and the tasks behind it start at 1."""
        with TempDir() as directory:
            app = self.make(directory)
            app.state.remember_session(CHANNEL, "macbook", "ses_1", "Discord #general")
            app.register_run(CHANNEL, asking.Run(session="ses_1", device="macbook",
                                                 started=time.monotonic(),
                                                 prompt="the task that runs"))
            app.waiting[CHANNEL] = [self.worker(app, "the waiting task")]
            self.use(app, commands.QUEUE)
            written = self.said()[-1]
        self.assertIn("0. “the task that runs”", written)
        self.assertIn("1. “the waiting task”", written)
        self.assertLess(written.index("0. “"), written.index("1. “"))

    def test_a_queue_that_only_runs_is_not_empty(self):
        """A run with nothing behind it is still the queue's one task."""
        with TempDir() as directory:
            app = self.make(directory)
            app.state.remember_session(CHANNEL, "macbook", "ses_1", "Discord #general")
            app.register_run(CHANNEL, asking.Run(session="ses_1", device="macbook",
                                                 started=time.monotonic(),
                                                 prompt="the task that runs"))
            self.use(app, commands.QUEUE)
            written = self.said()[-1]
        self.assertIn("0. “the task that runs”", written)
        self.assertIn("nothing waits", written)

    def test_the_queue_reads_a_run_no_daemon_holds(self):
        """The device is working, and no daemon's memory has the line.

        A run outlives the daemon that started it, and `/vigo goal` opens a turn
        with no run at all.  `/vigo status` reads the device's own record, so
        the queue must read it too instead of saying nothing runs.
        """
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session(
                "Discord #general", metadata={asking.CHANNEL_KEY: CHANNEL})
            self.device.started_turn(session)
            self.device.goals[session] = {
                "id": "goal_1", "objective": "sort out the failing build",
                "status": "active", "rounds": 0, "maxRounds": 50,
            }
            self.use(app, commands.QUEUE)
            written = self.said()[-1]
        self.assertIsNone(app.run_for(CHANNEL), "no daemon holds the line")
        self.assertIn("“Discord #general”: running", written)
        self.assertIn("0. “sort out the failing build”", written)

    def test_a_run_a_peer_reports_is_not_listed_twice(self):
        """The device is busy on a session the peer's line already holds.

        Both daemons read the devices, so the one that does not hold the line
        would derive a second row for the same channel without this.
        """
        other = PeerServer("127.0.0.1", 0, "workstation", "pw", queue=lambda: [{
            "channel": CHANNEL, "title": "Discord #general",
            "running": 12.0, "prompt": "their copy", "waiting": [],
        }])
        other.start()
        self.addCleanup(other.stop)
        peer_url = f"http://127.0.0.1:{other.server.server_address[1]}"
        with TempDir() as directory:
            settings = load_settings(
                directory,
                device="macbook",
                password="pw",
                devices=[
                    {"name": "macbook", "url": self.device.url, "directory": "/tmp/project"},
                    {"name": "workstation", "url": "http://127.0.0.1:2", "directory": "/b",
                     "peer": peer_url},
                ],
            )
            api = Discord("token", settings.app_id, base=self.discord.url)
            app = App(settings, api=api, state=State(directory / "state.json"))
            self.app = app
            session = self.device.new_session(
                "Discord #general", metadata={asking.CHANNEL_KEY: CHANNEL})
            self.device.started_turn(session)
            self.device.goals[session] = {
                "id": "goal_1", "objective": "sort out the failing build",
                "status": "active", "rounds": 0, "maxRounds": 50,
            }
            self.use(app, commands.QUEUE)
            written = self.said()[-1]
        self.assertEqual(written.count("0. "), 1, "the channel is named once")
        self.assertIn("their copy", written)
        self.assertNotIn("sort out the failing build", written,
                         "the row the peer already reports is not read again")

    def test_the_public_queue_redacts_the_run_in_slot_zero(self):
        with TempDir() as directory:
            app = self.make(directory)
            app.state.remember_session(CHANNEL, "macbook", "ses_1", "Discord #general")
            app.register_run(CHANNEL, asking.Run(session="ses_1", device="macbook",
                                                 started=time.monotonic(),
                                                 prompt="read /Users/alice/notes.txt"))
            app.waiting[CHANNEL] = [self.worker(app, "fix the failing test")]
            self.use(app, commands.QUEUE,
                     options=[{"name": "public", "type": 5, "value": True}])
            written = self.said()[-1]
        self.assertIn("0. “redacted task”", written)
        self.assertIn("1. “fix the failing test”", written)
        self.assertNotIn("/Users/alice", written)

    def test_queue_asks_the_other_daemon_and_names_a_silent_one(self):
        other = PeerServer("127.0.0.1", 0, "workstation", "pw", queue=lambda: [{
            "channel": "other_chan", "title": "Discord DM",
            "running": 12.0, "waiting": ["their task"],
        }])
        other.start()
        self.addCleanup(other.stop)
        peer_url = f"http://127.0.0.1:{other.server.server_address[1]}"
        with TempDir() as directory:
            settings = load_settings(
                directory,
                device="macbook",
                password="pw",
                devices=[
                    {"name": "macbook", "url": "http://127.0.0.1:1", "directory": "/a"},
                    {"name": "workstation", "url": "http://127.0.0.1:2", "directory": "/b",
                     "peer": peer_url},
                    {"name": "silent-device", "url": "http://127.0.0.1:3", "directory": "/c",
                     "peer": "http://127.0.0.1:1"},
                ],
            )
            api = Discord("token", settings.app_id, base=self.discord.url)
            app = App(settings, api=api, state=State(directory / "state.json"))
            self.app = app
            self.use(app, commands.QUEUE)
            written = self.said()[-1]
        self.assertIn("“Discord DM”: running 12s · 1 waiting", written)
        self.assertIn("0. (the task that runs)", written,
                      "a peer that reports no text still has the item at work")
        self.assertIn("1. “their task”", written)
        self.assertIn("does not answer on its peer port", written)

    def test_the_public_queue_names_no_channel_task_or_device(self):
        other = PeerServer("127.0.0.1", 0, "workstation", "pw", queue=lambda: [{
            "channel": "other_chan", "title": "Discord DM: Alice",
            "running": 12.0, "waiting": ["their task", "open /Users/alice/secret.txt"],
        }])
        other.start()
        self.addCleanup(other.stop)
        peer_url = f"http://127.0.0.1:{other.server.server_address[1]}"
        with TempDir() as directory:
            settings = load_settings(
                directory,
                device="macbook",
                password="pw",
                devices=[
                    {"name": "macbook", "url": "http://127.0.0.1:1", "directory": "/a"},
                    {"name": "workstation", "url": "http://127.0.0.1:2", "directory": "/b",
                     "peer": peer_url},
                    {"name": "silent-device", "url": "http://127.0.0.1:3", "directory": "/c",
                     "peer": "http://127.0.0.1:1"},
                ],
            )
            api = Discord("token", settings.app_id, base=self.discord.url)
            app = App(settings, api=api, state=State(directory / "state.json"))
            self.app = app
            app.state.remember_session(CHANNEL, "macbook", "ses_1", "Discord #general")
            app.waiting[CHANNEL] = [self.worker(app, "fix the failing test"),
                                    self.worker(app, "read /Users/alice/notes.txt")]
            self.use(app, commands.QUEUE,
                     options=[{"name": "public", "type": 5, "value": True}])
            written = self.said()[-1]
            ephemeral = self.discord.originals["tok"]["ephemeral"]
        self.assertFalse(ephemeral, "the channel reads the public queue")
        self.assertIn("1. “fix the failing test”", written, "a general task keeps its words")
        self.assertEqual(written.count("“redacted task”"), 2,
                         "each task that names somebody keeps its place and loses its text")
        self.assertNotIn("/Users/alice", written)
        self.assertNotIn("Discord", written, "no channel is named")
        for name in ("macbook", "workstation", "silent-device"):
            self.assertNotIn(name, written, f"{name} is not named")
        self.assertIn("running 12s · 2 waiting", written)
        self.assertIn("⚠️ another daemon does not answer on its peer port", written)

    def test_the_queue_says_how_many_agents_work_on_a_task(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord DM")
            self.device.started_turn(session)
            for title in ("reading the queue code", "checking the tests"):
                self.device.started_turn(self.device.new_session(title, parent=session))
            app.register_run(CHANNEL, asking.Run(session=session, device="macbook",
                                                 started=time.monotonic(), prompt="fix it"))
            self.use(app, commands.QUEUE)
            written = self.said()[-1]
        self.assertIn("· 3 agents", written, "the task's own agent and its two subagents")

    def test_a_subagent_that_finished_is_not_counted(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord DM")
            self.device.started_turn(session)
            done = self.device.new_session("finished work", parent=session)
            self.device.started_turn(done)
            self.device.finished_turn(done)
            self.device.started_turn(self.device.new_session("still working", parent=session))
            app.register_run(CHANNEL, asking.Run(session=session, device="macbook",
                                                 started=time.monotonic(), prompt="fix it"))
            self.use(app, commands.QUEUE)
            written = self.said()[-1]
        self.assertIn("· 2 agents", written, "the task's agent and one subagent, not the one that ended")

    def test_the_public_queue_keeps_the_agent_count(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord DM")
            self.device.started_turn(session)
            self.device.started_turn(self.device.new_session("checking the tests", parent=session))
            app.register_run(CHANNEL, asking.Run(session=session, device="macbook",
                                                 started=time.monotonic(), prompt="fix it"))
            self.use(app, commands.QUEUE,
                     options=[{"name": "public", "type": 5, "value": True}])
            written = self.said()[-1]
        self.assertIn("· 2 agents", written)
        self.assertNotIn("checking the tests", written, "a subagent is not named in public")

    def test_status_names_the_subagents_at_work(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord DM")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord DM")
            for title in ("reading the queue code", "checking the tests"):
                self.device.started_turn(self.device.new_session(title, parent=session))
            finished = self.device.new_session("finished work", parent=session)
            self.device.started_turn(finished)
            self.device.finished_turn(finished)
            self.use(app, commands.STATUS)
            written = self.said()[-1]
        self.assertIn("2 subagents at work", written)
        self.assertIn("“reading the queue code”", written)
        self.assertIn("“checking the tests”", written)
        self.assertNotIn("finished work", written, "a subagent that ended is not at work")

    def test_one_subagent_is_said_in_the_singular(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord DM")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord DM")
            self.device.started_turn(self.device.new_session("reading the queue code", parent=session))
            self.use(app, commands.STATUS)
            written = self.said()[-1]
        self.assertIn("1 subagent at work", written)

    def test_the_queue_is_private_unless_it_is_asked_to_be_public(self):
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.QUEUE, token="quiet")
            self.assertTrue(self.discord.originals["quiet"]["ephemeral"])
            self.use(app, commands.QUEUE, token="open",
                     options=[{"name": "public", "type": 5, "value": True}])
            self.assertFalse(self.discord.originals["open"]["ephemeral"])
            self.use(app, commands.QUEUE, token="off",
                     options=[{"name": "public", "type": 5, "value": False}])
            self.assertTrue(self.discord.originals["off"]["ephemeral"])

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

    def test_a_question_says_what_vigo_is(self):
        """The model has no other way to know it is Vigo."""
        self.device.script = [("idle", "done")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3,
                                "value": "explain how Vigo works"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("done" in str(content) for content in self.said()), 10))
        self.assertIn(asking.SELF, self.device.prompts[0][1])

    def test_quick_answers_once_and_starts_no_session(self):
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.QUICK,
                      options=[{"name": "prompt", "type": 3, "value": "what is a pointer"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("one-off answer" in str(content)
                            for content in self.said()), 10))
        self.assertEqual(len(self.device.generated), 1,
                         "the question is the only thing the device is asked")
        self.assertTrue(self.device.generated[0].startswith(asking.SELF),
                        "a one-off is told what Vigo is too")
        self.assertTrue(self.device.generated[0].endswith("what is a pointer"))
        self.assertEqual(self.device.sessions, {}, "a one-off is not a session")
        self.assertEqual(self.device.prompts, [])
        self.assertEqual([entry["content"] for entry in self.discord.edits],
                         ["one-off answer"],
                         "the thinking message becomes the answer, with no got-it line")
        self.assertEqual(self.discord.followups, [], "the answer is the one message")

    def test_a_quick_answer_is_told_to_stay_brief(self):
        """A one-off reaches the channel whole, so it is told the same limit."""
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.QUICK,
                      options=[{"name": "prompt", "type": 3, "value": "what is a pointer"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("one-off answer" in str(content)
                            for content in self.said()), 10))
        asked = self.device.generated[0]
        self.assertIn(asking.BRIEF, asked)
        self.assertIn("two short paragraphs at most", asked)
        self.assertTrue(asked.endswith("what is a pointer"),
                        "the question stays the last thing the device reads")
        self.assertNotIn(asking.STAYS, asked,
                         "a one-off keeps no session, so it promises none")

    def test_a_long_quick_answer_arrives_whole(self):
        self.device.answer = "x" * 4000
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.QUICK,
                      options=[{"name": "prompt", "type": 3, "value": "what is a pointer"}])
            self.assertTrue(self.discord.wait_for(
                lambda: sum(len(entry["content"])
                            for entry in self.discord.edits + self.discord.followups) >= 4000,
                10))
        parts = [entry["content"] for entry in self.discord.edits]
        parts += [entry["content"] for entry in self.discord.followups]
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), "x" * 4000, "the one answer arrives whole")

    def test_a_runaway_quick_answer_says_it_stopped(self):
        self.device.answer = "word " * 5000
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.QUICK,
                      options=[{"name": "prompt", "type": 3, "value": "what is a pointer"}])
            self.assertTrue(self.discord.wait_for(
                lambda: len(self.discord.followups) >= asking.ANSWER_PARTS - 1, 10))
        parts = [entry["content"] for entry in self.discord.edits]
        parts += [entry["content"] for entry in self.discord.followups]
        self.assertEqual(len(parts), asking.ANSWER_PARTS)
        self.assertIn("the answer was longer than this", parts[-1],
                      "the person is told it stopped, and that there is no session")

    def test_quick_can_be_hidden(self):
        with TempDir() as directory:
            app = self.make(directory)
            interaction = self.use(app, commands.QUICK, options=[
                {"name": "prompt", "type": 3, "value": "hi"},
                {"name": "hidden", "type": 5, "value": True},
            ])
            self.assertEqual(self.discord.answer_for(interaction)["data"]["flags"], 64)
            self.assertTrue(self.discord.wait_for(lambda: len(self.discord.edits) == 1, 10))
        self.assertEqual(self.discord.edits[0]["content"], "one-off answer",
                         "the ephemeral message becomes the answer")
        self.assertEqual(self.discord.followups, [], "nothing of it reaches the channel")

    def test_quick_says_when_no_device_answers(self):
        self.device.close()
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.QUICK,
                      options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("No device" in str(content) for content in self.said())))
        self.assertEqual(self.device.generated, [])

    def test_a_failed_run_names_the_limit(self):
        self.device.script = [("fail", "429 Too Many Requests from the provider")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "why is CI red"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("rate limited" in str(content) for content in self.said()), 10))
        posted = next(content for content in self.said() if "rate limited" in content)
        self.assertIn("ask again in a minute", posted)
        self.assertNotIn("429", posted, "the device's own words stay in the log")

    def test_a_run_that_produced_nothing_says_so(self):
        self.device.script = []
        with TempDir() as directory:
            app = self.make(directory)
            worker = self.worker(app, "why is CI red")
            worker._final("", "succeeded", 0.0, {}, steps=0)
            self.assertTrue(self.discord.wait_for(
                lambda: any("no text" in str(content) for content in self.said()), 5))
        self.assertTrue(any("returned no text" in content for content in self.said()))

    def test_the_ask_stops_at_ten_agents(self):
        with TempDir() as directory:
            app = self.make(directory)
            for _ in range(10):
                self.device.started_turn(self.device.new_session("a subagent"))
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "one more"}])
            self.assertTrue(any("already runs 10 agents" in str(content)
                                for content in self.said()))
        self.assertEqual(self.device.prompts, [], "no prompt went to a full device")

    def test_a_task_is_told_subagents_are_a_ceiling_and_optional(self):
        self.device.script = [("idle", "fine")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "fix the failing test"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("fine" in content for content in self.said()), 10))
        goals = [goal.get("objective") or "" for goal in self.device.goals.values()]
        rule = next(goal for goal in goals if "subagents" in goal)
        self.assertIn("at most 3 at a time", rule)
        self.assertIn("use fewer when fewer are enough", rule)
        self.assertIn("none when none is", rule)
        self.assertIn("starts with fresh context", rule)
        self.assertIn("cannot start another subagent", rule)
        self.assertIn("exact files, the exact result", rule)
        self.assertIn("read every report", rule)

    def test_an_answer_that_names_github_work_leads_with_it(self):
        self.device.script = [
            ("idle", "the fix is right.\n\nOpened https://github.com/scarletnx/ScarletNX/pull/1243."),
        ]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "open the pull request"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("the fix is right" in str(content)
                            for content in self.said()), 10))
        self.assertTrue(
            self.discord.followups[-1]["content"].startswith(
                "GitHub: [PR #1243](https://github.com/scarletnx/ScarletNX/pull/1243)"),
            "the pull request names itself before the prose about it")

    def test_a_github_report_is_told_the_size_to_keep(self):
        """The manner is short, so the rule carries the size and not only the shape."""
        self.device.script = [("idle", "fine")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "open the pull request"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("fine" in content for content in self.said()), 10))
        rule = next(goal for goal in (entry.get("objective") or ""
                                      for entry in self.device.goals.values())
                    if "GitHub" in goal)
        self.assertIn("about fifty words", rule)
        self.assertIn("a list of four facts is the long one", rule)

    def test_a_github_sign_off_is_vigo_and_not_the_model(self):
        """The agent speaks as Vigo, whatever model runs that turn."""
        self.device.script = [("idle", "fine")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "open the pull request"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("fine" in content for content in self.said()), 10))
        rule = next(goal for goal in (entry.get("objective") or ""
                                      for entry in self.device.goals.values())
                    if "GitHub" in goal)
        self.assertIn('"From Vigo (an agent)"', rule)
        self.assertIn("A pull request or an issue ends with it", rule)
        self.assertIn("A comment starts with it", rule)
        self.assertIn("Never sign it with the name of the model", rule)

    def test_a_question_is_told_subagents_are_a_ceiling_and_optional(self):
        self.device.script = [("idle", "fine")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "why is CI red"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("fine" in content for content in self.said()), 10))
        self.assertIn("at most 3 at a time", self.device.prompts[0][1])
        self.assertIn("none when none is", self.device.prompts[0][1])

    def test_the_device_is_told_to_keep_the_answer_to_two_paragraphs(self):
        self.device.script = [("idle", "short")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "why is CI red"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("short" in content for content in self.said()), 10))
        question = self.device.prompts[0][1]
        self.assertIn("two short paragraphs at most", question)
        self.assertIn("leave the rest out", question)
        self.assertIn("only as long as the answer needs", question)

    def test_status_names_the_agents_running(self):
        with TempDir() as directory:
            app = self.make(directory)
            session = self.device.new_session("Discord #general")
            app.state.remember_session(CHANNEL, "macbook", session, "Discord #general")
            for _ in range(3):
                self.device.started_turn(self.device.new_session("a subagent"))
            shown = app._status(CHANNEL)
        self.assertIn("3 agents are running on your devices", shown)

    def test_a_task_runs_in_a_session_of_its_own(self):
        """The conversation answers questions; work happens where the person can open it."""
        self.device.script = [("idle", "answered")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "why is CI red"}])
            self.assertTrue(self.discord.wait_for(lambda: len(self.device.prompts) == 1, 10))
            conversation = app.state.session_for(CHANNEL)["session"]
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "fix the failing test"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("its own OpenCode session" in str(content)
                            for content in self.said()), 10))
            task = app.state.session_for(CHANNEL)["session"]
            answer = next(content for content in self.said()
                          if "its own OpenCode session" in content)
        self.assertNotEqual(task, conversation, "the task does not run in the conversation")
        self.assertEqual(self.device.sessions[task]["agent"], "build",
                         "a primary agent: a subagent agent cannot start subagents")
        self.assertIn("fix the failing test", self.device.sessions[task]["title"])
        self.assertEqual({session for session, _name, _text in self.device.commands}, {task},
                         "the goal went to the task's own session")
        self.assertIn(self.device.sessions[task]["title"], answer)

    def test_a_task_title_with_a_path_stays_out_of_the_answer(self):
        """The session keeps the words; the channel reads only that it exists."""
        self.device.script = [("idle", "done")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK, options=[
                {"name": "prompt", "type": 3, "value": "fix /Users/alice/secret/thing"},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: any("its own OpenCode session" in str(content)
                            or "an OpenCode session of its own" in str(content)
                            for content in self.said()), 10))
            answer = next(str(content) for content in self.said()
                          if "OpenCode session" in str(content))
        self.assertNotIn("/Users/alice", answer)
        self.assertIn("an OpenCode session of its own", answer)

    def test_a_question_stays_in_the_conversation(self):
        self.device.script = [("idle", "answered")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "why is CI red"}])
            self.assertTrue(self.discord.wait_for(lambda: len(self.device.prompts) == 1, 10))
            session = app.state.session_for(CHANNEL)["session"]
        self.assertEqual(len(self.device.sessions), 1, "a question makes no worker")
        self.assertEqual(self.device.sessions[session]["agent"], "build")
        self.assertNotIn("its own OpenCode session", self.said()[-1])

    def test_the_ack_describes_the_work_rather_than_quoting_it(self):
        """It must not read the question back, and it must fit the work."""
        self.device.script = [("idle", "done")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK, options=[
                {"name": "prompt", "type": 3, "value": "improve the message received response"},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: any("done" in str(content) for content in self.said()), 10))
            self.assertTrue(self.discord.wait_for(
                lambda: app.run_for(CHANNEL) is None, 10),
                "the first question ends before the next one is asked")
            request = self.discord.edits[0]["content"]
            self.device.description = "tidying the reply wording"
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "why is CI red?"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("tidying the reply wording" in str(content)
                            for content in self.discord.edits), 10))
            question = self.discord.edits[-1]["content"]
        self.assertEqual(request, "Got it, fixing the thing. I'll follow up here.")
        self.assertNotIn("improve the message received response", request)
        self.assertEqual(question, "Got it, tidying the reply wording. I'll follow up here.",
                         "the phrasing follows the work, not a fixed template")

    def test_the_ack_line_names_no_secret_or_path(self):
        """The phrase is posted in the channel, so it gets the answer's check."""
        self.device.script = [("idle", "done")]
        with TempDir() as directory:
            app = self.make(directory)
            self.device.description = "reading " + "M" * 25 + ".GaBcDe." + "a" * 27
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "look at this"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("done" in str(content) for content in self.said()), 10))
            acks = [entry["content"] for entry in self.discord.edits
                    if str(entry["content"]).startswith("Got it")]
            self.assertTrue(acks)
            self.assertEqual(acks[0], "Got it. I'll follow up here.")

    def test_an_ask_is_acknowledged_first_and_answered_after(self):
        self.device.script = [("idle", "just the answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("just the answer" in str(content)
                            for content in self.said()), 10))
        self.assertEqual(self.discord.edits[0]["content"],
                         "Got it, fixing the thing. I'll follow up here.",
                         "the deferred message says it arrived")
        self.assertTrue(self.discord.followups[-1]["content"].startswith("just the answer"))
        self.assertIn("just the answer", self.said()[-1])

    def test_the_answer_is_a_followup_and_nothing_is_deleted(self):
        self.device.script = [("idle", "the answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any(str(entry.get("content") or "").startswith("the answer")
                            for entry in self.discord.followups), 10))
        original = self.discord.originals["tok"]
        self.assertFalse(original["deleted"], "the got-it line is not deleted")
        self.assertEqual(self.discord.deleted, [], "nothing is deleted")
        self.assertEqual(original["content"],
                         "Got it, fixing the thing. I'll follow up here.")
        self.assertTrue(self.discord.followups[-1]["content"].startswith("the answer"))
        self.assertEqual(len(self.discord.edits), 1, "the placeholder became the got-it line")

    def test_a_short_answer_keeps_all_its_paragraphs(self):
        """The device is asked for two paragraphs; the code does not cut a third."""
        self.device.script = [
            ("idle", "first paragraph\n\nsecond paragraph\n\nthird paragraph"),
        ]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "hi"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("first paragraph" in str(content)
                            for content in self.said()), 10))
        posted = self.discord.followups[-1]["content"]
        self.assertIn("second paragraph", posted)
        self.assertIn("third paragraph", posted)

    def test_a_long_answer_arrives_in_more_than_one_message(self):
        """An answer Discord cannot hold in one message is sent in several."""
        self.device.script = [("idle", "x" * 4000)]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why is CI red?"}])
            self.assertTrue(self.discord.wait_for(
                lambda: "".join(entry["content"] for entry in self.discord.followups)
                == "x" * 4000, 10))
        parts = [entry["content"] for entry in self.discord.followups]
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 2000)
        self.assertEqual("".join(parts), "x" * 4000, "no part of the answer is lost")

    def test_a_runaway_answer_says_the_rest_is_in_the_session(self):
        self.device.script = [("idle", "x" * 20000)]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "why is CI red?"}])
            self.assertTrue(self.discord.wait_for(
                lambda: len(self.discord.followups) >= asking.ANSWER_PARTS, 10))
        parts = [entry["content"] for entry in self.discord.followups]
        self.assertEqual(len(parts), asking.ANSWER_PARTS, "a runaway answer is capped")
        self.assertIn("the rest of this answer is in the session", parts[-1])
        for part in parts:
            self.assertLessEqual(len(part), 2000)

    def test_a_long_hidden_answer_is_several_ephemeral_messages(self):
        self.device.script = [("idle", "x" * 4000)]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK, options=[
                {"name": "prompt", "type": 3, "value": "why is CI red?"},
                {"name": "hidden", "type": 5, "value": True},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: "".join(entry["content"] for entry in self.discord.followups)
                == "x" * 4000, 10))
        self.assertTrue(self.discord.originals["tok"]["ephemeral"])
        for entry in self.discord.followups:
            self.assertEqual(entry["flags"], 64, "every part stays ephemeral")
            self.assertLessEqual(len(entry["content"]), 2000)
        self.assertEqual("".join(entry["content"] for entry in self.discord.followups),
                         "x" * 4000, "the asker sees the whole answer")
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

    def test_a_goal_that_is_met_says_so(self):
        gate = threading.Event()
        self.device.script = [("until", gate), ("idle", "the work is done")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "fix the failing test"}])
            self.assertTrue(self.discord.wait_for(lambda: bool(self.device.goals), 10))
            for held in self.device.goals.values():
                held["status"] = "complete"
            gate.set()
            self.assertTrue(self.discord.wait_for(
                lambda: any("the goal is met" in str(content)
                            for content in self.said()), 10))
        self.assertIn("the work is done", self.said()[-1])

    def test_a_goal_still_running_says_so(self):
        self.device.script = [("idle", "one round")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "fix the failing test"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("one round" in str(content) for content in self.said()), 10))
        self.assertIn("the goal is still active", self.said()[-1])

    def test_a_hidden_answer_that_is_too_late_goes_to_the_owner_dm(self):
        """A hidden answer has no channel to fall back to, so the owner's DM is it."""
        self.device.script = [("idle", "nobody may see this")]
        with TempDir() as directory:
            app = self.make(directory)
            self.discord.uneditable.add("tok")  # the 15 minutes ran out
            self.use(app, commands.ASK, options=[
                {"name": "prompt", "type": 3, "value": "hi"},
                {"name": "hidden", "type": 5, "value": True},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: bool(self.discord.dm_channels), 10), "the owner's DM was opened")
            self.assertTrue(self.discord.wait_for(
                lambda: app.finished.get(CHANNEL) == ["hi"], 10), "the run ended")
            dm = self.discord.messages.get(self.discord.dm_channels[OWNER], [])
        self.assertTrue(any("nobody may see this" in message["content"] for message in dm),
                        "the person who asked still gets the answer")
        self.assertTrue(any("the answer to" in message["content"] for message in dm),
                        "it names the question it belongs to")
        self.assertEqual(self.discord.messages.get(CHANNEL, []), [],
                         "a hidden answer is never posted in the channel")
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
                lambda: any("No device" in str(content) for content in self.said())))
        self.assertEqual(self.device.prompts, [])

    def test_one_run_per_channel_and_the_rest_wait(self):
        finishing = threading.Event()
        self.addCleanup(finishing.set)
        self.device.script = [("until", finishing), ("idle", "slow")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "first"}])
            self.assertTrue(self.device.wait_for_prompt())
            self.use(app, commands.ASK,
                      options=[{"name": "prompt", "type": 3, "value": "second"}])
            self.assertEqual(list(app.runs), [CHANNEL], "one run per channel")
            self.assertEqual([worker.prompt for worker in app.waiting[CHANNEL]], ["second"])
            finishing.set()
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
            was = build_of(peer_checkout)
            (seed / "two.txt").write_text("two\n", encoding="utf-8")
            self._git("add", ".", cwd=seed)
            self._git("commit", "-qm", "two", cwd=seed)
            self._git("push", "-q", "origin", "HEAD:main", cwd=seed)

            # the other daemon, on its own checkout, old build, no restart
            other = PeerServer("127.0.0.1", 0, "workstation", "pw",
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
                        {"name": "workstation", "url": "http://127.0.0.1:2",
                         "directory": "/b", "peer": peer_url},
                    ],
                )
                api = Discord("token", settings.app_id, base=self.discord.url)
                app = App(settings, api=api, state=State(directory / "state.json"))
                self.app = app
                self.use(app, commands.UPGRADE, options=[
                    {"name": "device", "type": 3, "value": "workstation"},
                ])
                self.assertTrue(self.discord.wait_for(
                    lambda: any("workstation" in str(content)
                                for content in self.said()), 30))
                said = self.said()[-1]
            self.assertIn("pulled", said)
            self.assertNotIn("macbook", said)
            self.assertTrue((peer_checkout / "two.txt").exists(), "the other checkout moved")
            dm = self.discord.messages.get(self.discord.dm_channels[settings.owner], [])
        self.assertTrue(dm, "the owner is told, not only the channel")
        told = dm[0]["content"]
        self.assertIn("workstation", told, "the alias it moved")
        self.assertIn(f"{was} → {build_of(peer_checkout)}", told,
                      "what it went from and to")

    def test_upgrade_with_no_device_pulls_this_one_when_the_peer_is_off(self):
        """An empty device list means every device, this one included.

        The Mac is the daemon that answers the command.  With the PC off, an
        upgrade used to try only the PC, say it was not answering, and leave
        this checkout behind.  The local pull must still be scheduled.
        """
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
            checkout = root / "local"
            self._git("clone", "-q", "-b", "main", str(origin), str(checkout), cwd=root)
            (seed / "two.txt").write_text("two\n", encoding="utf-8")
            self._git("add", ".", cwd=seed)
            self._git("commit", "-qm", "two", cwd=seed)
            self._git("push", "-q", "origin", "HEAD:main", cwd=seed)

            with TempDir() as directory:
                settings = load_settings(
                    directory,
                    device="macbook",
                    devices=[
                        {"name": "macbook", "url": "http://127.0.0.1:1",
                         "directory": "/a"},
                        {"name": "workstation", "url": "http://127.0.0.1:2",
                         "directory": "/b", "peer": "http://127.0.0.1:1"},
                    ],
                )
                api = Discord("token", settings.app_id, base=self.discord.url)
                app = App(settings, api=api, state=State(directory / "state.json"),
                          checkout=checkout)
                self.app = app
                self.use(app, commands.UPGRADE)
                self.assertTrue(self.discord.wait_for(
                    lambda: any("pulls after this answer" in str(content)
                                for content in self.said()), 10))
                said = self.said()[-1]
                self.assertTrue(self.discord.wait_for(
                    lambda: (checkout / "two.txt").exists(), 15),
                    "the local checkout is pulled, not just the peer")
        self.assertIn("macbook", said)
        self.assertIn("workstation", said)
        self.assertIn("not answering", said)

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
        self.assertIn("this daemon runs abc1234. Restart it to run", written)

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
        self.assertIn(f"this daemon runs {one}. Restart it to run {two}", after)

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
        self.device.script = [("delta", "thinking hard"), ("idle", "done")]
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
            for sub in (commands.STATUS, commands.QUEUE):
                refused = self.use(app, sub, who=listed)
                self.assertEqual(self.discord.answer_for(refused)["data"]["flags"], 64)
                self.assertIn("Only", self.discord.answer_for(refused)["data"]["content"])
                mine = self.use(app, sub)
                self.assertEqual(self.discord.answer_for(mine)["data"]["flags"], 64,
                                 f"{sub} is never shown to a channel")

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
        self.assertEqual(subcommands, ["ask", "goal", "quick", "upgrade", "stop", "status",
                                       "queue", "devices", "allow", "deny", "allowed", "help"])
        always_private = {"devices", "status", "queue", "upgrade", "allow", "deny", "allowed"}
        for option in command["options"]:
            names = [inner["name"] for inner in (option.get("options") or [])]
            if option["name"] in always_private:
                self.assertNotIn("hidden", names, f"{option['name']} cannot be shown")
            else:
                self.assertIn("hidden", names, f"{option['name']} takes hidden")
        queue = next(option for option in command["options"] if option["name"] == "queue")
        self.assertEqual([inner["name"] for inner in queue["options"]], ["public"],
                         "the queue's only option is the public form")

    def test_a_user_install_works_in_servers_too(self):
        """A user install is visible in every server, not only DMs.

        Discord's contexts for a USER_INSTALL command are GUILD, BOT_DM and
        PRIVATE_CHANNEL.  Leaving GUILD out hides `/vigo` from the command
        picker of every server the person is in, which is the surface a user
        install exists for.
        """
        with TempDir() as directory:
            app = self.make(directory, install="user")
            app.register_commands()
        command = self.discord.global_commands[0]
        self.assertEqual(command["integration_types"], [1])
        self.assertEqual(command["contexts"], [0, 1, 2])

    def test_another_command_is_kept(self):
        with TempDir() as directory:
            app = self.make(directory)
            self.discord.global_commands = [
                {"id": "x", "name": "other", "description": "theirs", "type": 1}
            ]
            app.register_commands()
        names = [command["name"] for command in self.discord.global_commands]
        self.assertEqual(names, ["other", "vigo"])


    def test_an_approved_asker_runs_in_a_session_of_their_own(self):
        """The owner's conversation is not the approved person's to read."""
        self.device.script = [("idle", "the first answer")]
        with TempDir() as directory:
            app = self.make(directory, users=[GUEST])
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "what is a pointer"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("the first answer" in content for content in self.said()), 10))
            self.device.script = [("idle", "the second answer")]
            self.use(app, commands.ASK, who=GUEST,
                     options=[{"name": "prompt", "type": 3, "value": "what is a heap"}])
            self.assertTrue(self.discord.wait_for(
                lambda: any("the second answer" in content for content in self.said()), 10))
        sessions = [session for session, _text in self.device.prompts]
        self.assertEqual(len(sessions), 2)
        self.assertNotEqual(sessions[0], sessions[1],
                            "a shared session would carry one asker's history to the other")
        keys = [self.device.sessions[session]["metadata"].get(asking.CHANNEL_KEY)
                for session in sessions]
        self.assertIn(CHANNEL, keys)
        self.assertIn(f"{CHANNEL}:{GUEST}", keys)

    def test_an_approved_asker_is_answered_privately(self):
        """Nothing about the owner's machine is posted where the channel reads."""
        self.device.script = [("idle", "the config lives in /home/you/.config")]
        with TempDir() as directory:
            app = self.make(directory, users=[GUEST])
            interaction = self.use(app, commands.ASK, who=GUEST, options=[
                {"name": "prompt", "type": 3, "value": "where is the config"},
            ])
            deferred = self.discord.answer_for(interaction)
            self.assertEqual(deferred["data"]["flags"], 64,
                             "the question's own line is private too")
            self.assertTrue(self.discord.wait_for(
                lambda: any("the config lives" in entry["content"]
                            for entry in self.discord.followups), 10))
        answers = [entry for entry in self.discord.followups
                   if "the config lives" in entry["content"]]
        self.assertEqual(answers[0]["flags"], 64, "the answer is ephemeral")
        self.assertEqual(self.discord.messages.get(CHANNEL, []), [],
                         "an approved person's answer never lands in the channel")

    def test_an_approved_ask_is_a_question_not_a_goal(self):
        """No goal, no subagent: one turn, then the work stops."""
        self.device.script = [("idle", "done")]
        with TempDir() as directory:
            app = self.make(directory, users=[GUEST])
            self.use(app, commands.ASK, who=GUEST, options=[
                {"name": "prompt", "type": 3, "value": "fix the tests"},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: any("done" in content for content in self.said()), 10))
        self.assertEqual(self.device.goals, {}, "an approved ask sets no goal")
        self.assertEqual(self.device.commands, [], "no `/goal` is sent for it")
        session = self.device.prompts[0][0]
        self.assertEqual(self.device.sessions[session]["agent"], "build",
                         "the work must not run under the subagent agent")

    def test_an_approved_asker_cannot_set_a_goal(self):
        with TempDir() as directory:
            app = self.make(directory, users=[GUEST])
            interaction = self.use(app, commands.GOAL, who=GUEST, options=[
                {"name": "objective", "type": 3, "value": "rewrite the parser"},
            ])
        answer = self.discord.answer_for(interaction)
        self.assertEqual(answer["data"]["flags"], 64)
        self.assertIn("Only the owner of this Discord application", answer["data"]["content"])
        self.assertEqual(self.device.prompts, [])
        self.assertEqual(self.device.goals, {})

    def test_an_approved_asker_cannot_choose_the_device(self):
        with TempDir() as directory:
            app = self.make(directory, users=[GUEST])
            interaction = self.use(app, commands.ASK, who=GUEST, options=[
                {"name": "prompt", "type": 3, "value": "what is a pointer"},
                {"name": "device", "type": 3, "value": "macbook"},
            ])
        answer = self.discord.answer_for(interaction)
        self.assertEqual(answer["data"]["flags"], 64)
        self.assertIn("owner chooses the device", answer["data"]["content"])
        self.assertEqual(self.device.prompts, [])

    def test_an_approved_asker_has_one_question_in_flight(self):
        gate = threading.Event()
        self.device.script = [("until", gate), ("idle", "finished")]
        with TempDir() as directory:
            app = self.make(directory, users=[GUEST])
            self.use(app, commands.ASK, who=GUEST, options=[
                {"name": "prompt", "type": 3, "value": "what is a pointer"},
            ])
            self.assertTrue(self.device.wait_for_prompt(10))
            self.use(app, commands.ASK, who=GUEST, options=[
                {"name": "prompt", "type": 3, "value": "and a heap"},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: any("already have a question in flight" in entry["content"]
                            for entry in self.discord.edits), 10))
            self.assertEqual(len(self.device.prompts), 1,
                             "the second question is not sent to the device")
            gate.set()
            self.assertTrue(self.discord.wait_for(
                lambda: any("finished" in content for content in self.said()), 10))

    def test_the_help_for_an_approved_asker_says_what_they_may_do(self):
        with TempDir() as directory:
            app = self.make(directory, users=[GUEST])
            interaction = self.use(app, commands.HELP, who=GUEST)
        answer = self.discord.answer_for(interaction)
        self.assertEqual(answer["data"]["flags"], 64)
        text = answer["data"]["content"]
        self.assertIn("private to you", text)
        self.assertIn("belong to the owner", text)
        self.assertNotIn("/vigo status", text)
        self.assertNotIn("/vigo devices", text)

    def test_only_the_owner_reads_the_device_state(self):
        with TempDir() as directory:
            app = self.make(directory, users=[GUEST])
            for sub in (commands.STATUS, commands.DEVICES, commands.QUEUE,
                        commands.ALLOW, commands.DENY, commands.ALLOWED, commands.STOP):
                interaction = self.use(app, sub, who=GUEST)
                answer = self.discord.answer_for(interaction)
                self.assertEqual(answer["data"]["flags"], 64)
                self.assertIn("Only the owner of this Discord application", answer["data"]["content"],
                              f"{sub} must not answer an approved person")

    def test_an_allow_made_now_lets_the_next_question_run(self):
        self.device.script = [("idle", "the answer")]
        with TempDir() as directory:
            app = self.make(directory)
            self.use(app, commands.ALLOW, options=[
                {"name": "user", "type": 6, "value": {"id": GUEST, "username": "Guest"}},
            ])
            self.assertTrue(self.discord.wait_for(lambda: app.allow.may_ask(GUEST)))
            self.use(app, commands.ASK, who=GUEST, options=[
                {"name": "prompt", "type": 3, "value": "what is a heap"},
            ])
            self.assertTrue(self.discord.wait_for(
                lambda: any("the answer" in content for content in self.said()), 10))
            self.assertEqual(len(self.device.prompts), 1)

    def test_an_allow_that_ran_out_refuses_the_next_question(self):
        with TempDir() as directory:
            app = self.make(directory)
            app.allow.allow(GUEST, "Guest", 0)
            interaction = self.use(app, commands.ASK, who=GUEST, options=[
                {"name": "prompt", "type": 3, "value": "what is a heap"},
            ])
        answer = self.discord.answer_for(interaction)
        self.assertIn("approved", answer["data"]["content"])
        self.assertEqual(self.device.prompts, [])

    def test_a_deny_stops_a_question_that_waits_its_turn(self):
        """Access taken back while the question waits is read again before it runs."""
        gate = threading.Event()
        self.device.script = [("until", gate), ("idle", "owner answer")]
        with TempDir() as directory:
            app = self.make(directory)
            app.allow.allow(GUEST, "Guest", None)
            self.use(app, commands.ASK,
                     options=[{"name": "prompt", "type": 3, "value": "what is a pointer"}])
            self.assertTrue(self.device.wait_for_prompt(10))
            self.use(app, commands.ASK, who=GUEST, options=[
                {"name": "prompt", "type": 3, "value": "what is a heap"},
            ])
            _done, _said = app.allow.deny(GUEST)
            gate.set()
            self.assertTrue(self.discord.wait_for(
                lambda: any("owner answer" in content for content in self.said()), 10))
            self.assertTrue(self.discord.wait_for(
                lambda: any("Your access ended" in content for content in self.said()), 10))
            self.assertTrue(self.discord.wait_for(
                lambda: any("Your access ended" in content for content in self.said()), 10))
        self.assertEqual(len(self.device.prompts), 1,
                         "the denied question never reached the device")

if __name__ == "__main__":
    unittest.main()
