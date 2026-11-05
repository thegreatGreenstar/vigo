import subprocess
import tempfile
import unittest
from pathlib import Path

from vigo import __version__
from vigo.peer import PeerClient, PeerServer, update_self


def git(*args: str, cwd: Path) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
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


if __name__ == "__main__":
    unittest.main()
