"""The Developer Portal check, without Discord or a network."""

import contextlib
import io
import unittest

from vigo import config as configuration
from vigo import portal
from vigo.__main__ import _resolve_owner, _invite
from vigo.discord_api import Discord
from tests.fake_discord import FakeDiscord
from tests.support import TempDir, base_settings, load_settings, write_settings

APP = "111111111111111111"
OWNER = "145512144145512144"


def application(*, public: bool = True, user: bool = True, guild: bool = False,
                scopes: list[str] | None = None,
                owner: str | None = OWNER, app_id: str = APP) -> dict:
    config: dict = {}
    if guild:
        config["0"] = {"oauth2_install_params": {"scopes": ["bot", "applications.commands"],
                                                 "permissions": "68608"}}
    if user:
        config["1"] = {"oauth2_install_params": {
            "scopes": scopes if scopes is not None else ["applications.commands"],
            "permissions": "0",
        }}
    body: dict = {"id": app_id, "bot_public": public, "integration_types_config": config}
    if owner:
        body["owner"] = {"id": owner, "username": "owner"}
    return body


class PortalTest(unittest.TestCase):
    def report(self, *, install: str = "user", **overrides) -> tuple[str, bool]:
        with TempDir() as directory:
            settings = load_settings(directory, install=install)
            return portal.report(settings, application(**overrides))

    def test_a_user_install_that_is_public_is_ready(self):
        text, ready = self.report()
        self.assertTrue(ready)
        self.assertIn(portal.user_install_link(APP), text)
        self.assertIn("integration_type=1", text)
        self.assertIn("applications.commands", text)
        self.assertIn("Ready", text)

    def test_a_private_app_is_not_ready(self):
        text, ready = self.report(public=False)
        self.assertFalse(ready)
        self.assertIn("Public Bot", text)
        self.assertIn(portal.pages(APP)["bot"], text)

    def test_user_install_off_is_not_ready(self):
        text, ready = self.report(user=False)
        self.assertFalse(ready)
        self.assertIn("Turn on User Install", text)
        self.assertIn(portal.pages(APP)["installation"], text)

    def test_a_missing_command_scope_is_not_ready(self):
        text, ready = self.report(scopes=["identify"])
        self.assertFalse(ready)
        self.assertIn("applications.commands scope", text)

    def test_the_settings_must_match_the_contexts_the_app_offers(self):
        text, ready = self.report(install="both")
        self.assertFalse(ready)
        self.assertIn('"install": "user"', text)

    def test_a_guild_install_can_be_checked_too(self):
        text, ready = self.report(install="both", guild=True)
        self.assertTrue(ready)
        self.assertIn("Guild Install       on", text)

    def test_a_guild_only_setup_cannot_serve_a_user_install(self):
        text, ready = self.report(install="guild", guild=True)
        self.assertFalse(ready)
        self.assertIn("only a guild install", text)

    def test_the_report_never_carries_the_token(self):
        text, _ready = self.report()
        self.assertNotIn("test-token", text)

    def test_the_guild_link_is_a_separate_one(self):
        link = portal.guild_install_link(APP)
        self.assertIn("scope=bot+applications.commands", link)
        self.assertNotIn("integration_type=1", link)

    def test_the_application_is_read_with_the_token(self):
        fake = FakeDiscord()
        fake.application = application()
        try:
            got = Discord("token", APP, base=fake.url).application()
        finally:
            fake.close()
        self.assertEqual(got["id"], APP)
        self.assertTrue(got["bot_public"])

    def test_the_cli_prints_the_link_and_exits_zero_when_ready(self):
        with TempDir() as directory:
            settings = load_settings(directory, install="user")
            fake = FakeDiscord()
            fake.application = application()
            try:
                api = Discord("token", APP, base=fake.url)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = _invite(settings, api=api)
            finally:
                fake.close()
        self.assertEqual(code, 0)
        self.assertIn(portal.user_install_link(APP), out.getvalue())
        self.assertIn("Ready", out.getvalue())

    def test_the_cli_exits_non_zero_while_the_portal_is_unfinished(self):
        with TempDir() as directory:
            settings = load_settings(directory, install="user")
            fake = FakeDiscord()
            fake.application = application(public=False)
            try:
                api = Discord("token", APP, base=fake.url)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = _invite(settings, api=api)
            finally:
                fake.close()
        self.assertEqual(code, 1)
        self.assertIn("Public Bot", out.getvalue())

    # -- the owner is the application's owner ------------------------------

    def resolve(self, application_body: dict, *, with_owner: bool = False):
        with TempDir() as directory:
            if with_owner:
                settings = load_settings(directory)
            else:
                raw = base_settings()
                raw.pop("owner")
                settings = configuration.load(write_settings(directory, raw))
            fake = FakeDiscord()
            fake.application = application_body
            try:
                api = Discord("token", APP, base=fake.url)
                return _resolve_owner(settings, api=api)
            finally:
                fake.close()

    def test_a_missing_owner_is_the_application_owner(self):
        got = self.resolve(application())
        self.assertIsNotNone(got)
        self.assertEqual(got.owner, OWNER)

    def test_a_configured_owner_that_matches_is_kept(self):
        got = self.resolve(application(), with_owner=True)
        self.assertIsNotNone(got)
        self.assertEqual(got.owner, OWNER)

    def test_a_configured_owner_that_differs_stops_the_daemon(self):
        got = self.resolve(application(owner="999999999999999999"), with_owner=True)
        self.assertIsNone(got, "a typed id must not take over somebody else's app")

    def test_an_application_with_no_named_owner_stops_the_daemon(self):
        self.assertIsNone(self.resolve(application(owner=None)))

    def test_a_token_for_another_application_stops_the_daemon(self):
        self.assertIsNone(self.resolve(application(app_id="999999999999999999")))

    def test_the_report_refuses_a_token_that_is_another_application(self):
        with TempDir() as directory:
            settings = load_settings(directory)
            text, ready = portal.report(settings, application(app_id="999999999999999999"))
        self.assertFalse(ready)
        self.assertIn("appID", text)

    def test_the_report_names_the_owner_and_refuses_a_mismatch(self):
        text, ready = self.report()
        self.assertTrue(ready)
        self.assertIn("Owner", text)
        self.assertIn(OWNER, text)
        with TempDir() as directory:
            settings = load_settings(directory)
            text, ready = portal.report(settings, application(owner="999999999999999999"))
        self.assertFalse(ready)
        self.assertIn("belongs to", text)

    def test_a_report_with_no_application_owner_is_not_ready(self):
        with TempDir() as directory:
            settings = load_settings(directory)
            text, ready = portal.report(settings, application(owner=None))
        self.assertFalse(ready)
        self.assertIn("did not name", text)

    def test_a_ready_report_says_what_requests_off_means(self):
        with TempDir() as directory:
            settings = load_settings(directory, install="user", requests=False)
            text, ready = portal.report(settings, application())
        self.assertTrue(ready)
        self.assertIn("Requests are off", text)
        self.assertNotIn("can ask for access", text)


if __name__ == "__main__":
    unittest.main()
