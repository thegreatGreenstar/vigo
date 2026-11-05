import json
import os
import unittest
from pathlib import Path

from vigo import config as configuration
from tests.support import TempDir, base_settings, write_settings


class ConfigTest(unittest.TestCase):
    def test_a_good_file_loads(self):
        with TempDir() as directory:
            settings = configuration.load(write_settings(directory, base_settings()))
        self.assertEqual(settings.owner, "145512144145512144")
        self.assertEqual(settings.device, "macbook")
        self.assertEqual(settings.devices["macbook"].directory, "/tmp/project")
        self.assertEqual(str(settings.model), "opencode-go/deepseek-v4.1-flash")
        self.assertEqual(settings.timeout, 60)

    def test_unknown_key_stops_it(self):
        raw = base_settings()
        raw["surprise"] = 1
        with TempDir() as directory:
            path = write_settings(directory, raw)
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(path)
        self.assertIn("surprise", str(caught.exception))

    @unittest.skipIf(os.name == "nt", "Windows guards with ACLs, not these bits")
    def test_a_readable_file_stops_it(self):
        with TempDir() as directory:
            path = write_settings(directory, base_settings())
            os.chmod(path, 0o644)
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(path)
        self.assertIn("0600", str(caught.exception))

    def test_the_owner_cannot_be_listed(self):
        raw = base_settings()
        raw["users"] = [raw["owner"]]
        with TempDir() as directory:
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(write_settings(directory, raw))
        self.assertIn("owner", str(caught.exception))

    def test_two_devices_with_one_name(self):
        raw = base_settings(devices=[
            {"name": "macbook", "url": "http://127.0.0.1:1", "directory": "/a"},
            {"name": "macbook", "url": "http://127.0.0.1:2", "directory": "/b"},
        ])
        with TempDir() as directory:
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(write_settings(directory, raw))
        self.assertIn("twice", str(caught.exception))

    def test_this_device_must_be_listed(self):
        raw = base_settings(device="pc")
        with TempDir() as directory:
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(write_settings(directory, raw))
        self.assertIn("pc", str(caught.exception))

    def test_a_device_needs_an_http_url(self):
        raw = base_settings(devices=[
            {"name": "macbook", "url": "ftp://x", "directory": "/a"},
        ])
        with TempDir() as directory:
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(write_settings(directory, raw))
        self.assertIn("http", str(caught.exception))

    def test_a_directory_is_absolute(self):
        raw = base_settings(devices=[
            {"name": "macbook", "url": "http://127.0.0.1:1", "directory": "project"},
        ])
        with TempDir() as directory:
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(write_settings(directory, raw))
        self.assertIn("absolute", str(caught.exception))

    def test_a_window_out_of_range(self):
        raw = base_settings(activityWindow=999999)
        with TempDir() as directory:
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(write_settings(directory, raw))
        self.assertIn("activityWindow", str(caught.exception))

    def test_model_variant_must_be_text(self):
        raw = base_settings(model={"providerID": "p", "id": "m", "variant": 3})
        with TempDir() as directory:
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(write_settings(directory, raw))
        self.assertIn("variant", str(caught.exception))

    def test_no_token(self):
        raw = base_settings()
        raw["token"] = ""
        with TempDir() as directory:
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(write_settings(directory, raw))
        self.assertIn("empty", str(caught.exception))

    def test_not_json(self):
        with TempDir() as directory:
            path = Path(directory) / "vigo.json"
            path.write_text("{", encoding="utf-8")
            os.chmod(path, 0o600)
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(path)
        self.assertIn("not JSON", str(caught.exception))

    def test_a_device_may_name_its_own_password(self):
        raw = base_settings(devices=[
            {"name": "macbook", "url": "http://127.0.0.1:1",
             "directory": "/a", "password": "other"},
        ])
        with TempDir() as directory:
            settings = configuration.load(write_settings(directory, raw))
        self.assertEqual(settings.devices["macbook"].password, "other")

    def test_the_model_defaults(self):
        raw = base_settings()
        raw.pop("model")
        with TempDir() as directory:
            settings = configuration.load(write_settings(directory, raw))
        self.assertEqual(settings.model.id, "deepseek-v4.1-flash")
        self.assertEqual(settings.model.provider_id, "opencode-go")
        self.assertEqual(json.dumps(settings.model.as_ref()),
                         '{"providerID": "opencode-go", "id": "deepseek-v4.1-flash"}')

    # -- secrets out of the file ------------------------------------------

    def test_the_token_may_come_from_a_file(self):
        raw = base_settings()
        raw.pop("token")
        raw["tokenFile"] = "token.txt"
        with TempDir() as directory:
            secret = Path(directory) / "token.txt"
            secret.write_text("from-the-file\n", encoding="utf-8")
            os.chmod(secret, 0o600)
            settings = configuration.load(write_settings(directory, raw))
        self.assertEqual(settings.token, "from-the-file")

    def test_the_token_may_be_asked_of_a_command(self):
        raw = base_settings()
        raw.pop("token")
        raw["tokenCommand"] = "echo from-the-command"
        with TempDir() as directory:
            settings = configuration.load(write_settings(directory, raw))
        self.assertEqual(settings.token, "from-the-command")

    def test_two_ways_to_give_one_secret_is_refused(self):
        raw = base_settings()
        raw["tokenFile"] = "token.txt"
        with TempDir() as directory:
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(write_settings(directory, raw))
        self.assertIn("pick one", str(caught.exception))

    def test_a_missing_secret_file_names_it(self):
        raw = base_settings()
        raw.pop("token")
        raw["tokenFile"] = "gone.txt"
        with TempDir() as directory:
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(write_settings(directory, raw))
        self.assertIn("cannot be read", str(caught.exception))

    @unittest.skipIf(os.name == "nt", "Windows guards with ACLs, not these bits")
    def test_a_readable_secret_file_is_refused(self):
        raw = base_settings()
        raw.pop("token")
        raw["tokenFile"] = "token.txt"
        with TempDir() as directory:
            secret = Path(directory) / "token.txt"
            secret.write_text("x", encoding="utf-8")
            os.chmod(secret, 0o644)
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(write_settings(directory, raw))
        self.assertIn("0600", str(caught.exception))

    def test_a_command_that_fails_is_said_so(self):
        raw = base_settings()
        raw.pop("token")
        raw["tokenCommand"] = "vigo-no-such-command-exists"
        with TempDir() as directory:
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(write_settings(directory, raw))
        self.assertIn("failed", str(caught.exception))

    def test_a_placeholder_is_refused(self):
        raw = base_settings()
        raw["token"] = "PASTE-THE-BOT-TOKEN"
        with TempDir() as directory:
            path = write_settings(directory, raw)
            with self.assertRaises(configuration.ConfigError) as caught:
                configuration.load(path)
        self.assertIn("placeholder", str(caught.exception))

    def test_a_device_may_keep_its_own_password_in_a_file(self):
        with TempDir() as directory:
            secret = Path(directory) / "pc.pass"
            secret.write_text("pc-only-secret", encoding="utf-8")
            os.chmod(secret, 0o600)
            raw = base_settings(device="pc", devices=[
                {"name": "macbook", "url": "http://127.0.0.1:1", "directory": "/a",
                 "password": "mac-secret"},
                {"name": "pc", "url": "http://127.0.0.2:1", "directory": "/b",
                 "passwordFile": str(secret)},
            ])
            settings = configuration.load(write_settings(directory, raw))
        self.assertEqual(settings.devices["macbook"].password, "mac-secret")
        self.assertEqual(settings.devices["pc"].password, "pc-only-secret")

    def test_a_file_of_passwords_may_be_the_only_one(self):
        raw = base_settings()
        raw.pop("password")
        raw["devices"] = [
            {"name": "macbook", "url": "http://127.0.0.1:1", "directory": "/a",
             "password": "own"},
        ]
        with TempDir() as directory:
            settings = configuration.load(write_settings(directory, raw))
        self.assertEqual(settings.devices["macbook"].password, "own")
        self.assertEqual(settings.password, "")


if __name__ == "__main__":
    unittest.main()
