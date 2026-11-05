import subprocess
import tempfile
import unittest
from pathlib import Path

from vigo import __version__
from vigo.peer import PeerClient, PeerServer, build_of, update_self


def git(*args: str, cwd: Path) -> str:
    done = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


class PeerTest(unittest.TestCase):
    def setUp(self):
        self.server = PeerServer("127.0.0.1", 0, "macbook", "pw").start()
        self.url = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def tearDown(self):
        self.server.stop()

    def test_it_says_which_daemon_and_which_build(self):
        info = PeerClient(self.url, "pw").info()
        self.assertEqual(info["device"], "macbook")
        self.assertEqual(info["version"], __version__)

    def test_the_password_is_checked(self):
        with self.assertRaises(OSError):
            PeerClient(self.url, "wrong").info()

    def test_an_unknown_route_is_refused(self):
        with self.assertRaises(OSError):
            PeerClient(self.url, "pw")._call("GET", "/vigo/secrets")

    def test_it_reports_the_build_it_was_given(self):
        server = PeerServer("127.0.0.1", 0, "macbook", "pw", build="abc1234").start()
        self.addCleanup(server.stop)
        info = PeerClient(f"http://127.0.0.1:{server.server.server_address[1]}", "pw").info()
        self.assertEqual(info["build"], "abc1234")
        self.assertEqual(info["version"], __version__)


class UpdateTest(unittest.TestCase):
    def test_a_checkout_is_pulled_and_never_forcefully(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin.git"
            git("init", "--bare", "-q", str(origin), cwd=root)
            seed = root / "seed"
            git("clone", "-q", str(origin), str(seed), cwd=root)
            git("config", "user.email", "t@example.com", cwd=seed)
            git("config", "user.name", "t", cwd=seed)
            (seed / "file.txt").write_text("one\n", encoding="utf-8")
            git("add", ".", cwd=seed)
            git("commit", "-qm", "one", cwd=seed)
            git("push", "-q", "origin", "HEAD:main", cwd=seed)

            here = root / "here"
            git("clone", "-q", "-b", "main", str(origin), str(here), cwd=root)

            (seed / "file.txt").write_text("two\n", encoding="utf-8")
            git("commit", "-qam", "two", cwd=seed)
            git("push", "-q", "origin", "HEAD:main", cwd=seed)

            told = update_self(checkout=here, restart=False)
            self.assertTrue(told["ok"])
            self.assertEqual((here / "file.txt").read_text(encoding="utf-8"), "two\n")
            self.assertFalse(told["restarted"])


class BuildTest(unittest.TestCase):
    def test_it_reads_the_checkout_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git("init", "-q", str(root), cwd=root)
            git("config", "user.email", "t@example.com", cwd=root)
            git("config", "user.name", "t", cwd=root)
            (root / "one.txt").write_text("one\n", encoding="utf-8")
            git("add", ".", cwd=root)
            git("commit", "-qm", "one", cwd=root)
            self.assertEqual(build_of(root), git("rev-parse", "--short", "HEAD", cwd=root))

    def test_a_directory_that_is_not_git_has_no_build(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(build_of(Path(directory)), "")


if __name__ == "__main__":
    unittest.main()


class PeerPasswordTest(unittest.TestCase):
    """The peer server checks the password a peer will send: this device's own."""

    def test_it_uses_this_devices_password_not_the_fallback(self):
        from vigo.app import App
        from vigo.config import Device, Config

        settings = Config(
            token="t", app_id="1", owner="2", device="macbook", password="",
            devices={
                "macbook": Device("macbook", "http://127.0.0.1:1", "/a", "mine"),
                "pc": Device("pc", "http://127.0.0.1:2", "/b", "theirs"),
            },
        )
        app = App(settings, api=None, state=None)
        self.assertEqual(app.peer_password(), "mine")

    def test_a_shared_password_is_still_used(self):
        from vigo.app import App
        from vigo.config import Device, Config

        settings = Config(
            token="t", app_id="1", owner="2", device="macbook", password="shared",
            devices={
                "macbook": Device("macbook", "http://127.0.0.1:1", "/a", "shared"),
            },
        )
        app = App(settings, api=None, state=None)
        self.assertEqual(app.peer_password(), "shared")
