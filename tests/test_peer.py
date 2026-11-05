import subprocess
import tempfile
import unittest
from pathlib import Path

from vigo import __version__
from vigo.peer import PeerClient, PeerServer, _launchd_workdir, _why, build_of, update_self


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


class RefusalTest(unittest.TestCase):
    """A refused pull says why, not the last line of git's advice."""

    def test_a_refused_pull_reports_the_cause_and_no_local_path(self):
        with tempfile.TemporaryDirectory() as directory:
            here = Path(directory)
            git("init", "-q", "-b", "main", str(here), cwd=here)
            git("config", "user.email", "t@example.com", cwd=here)
            git("config", "user.name", "t", cwd=here)
            (here / "file.txt").write_text("one\n", encoding="utf-8")
            git("add", ".", cwd=here)
            git("commit", "-qm", "one", cwd=here)
            git("remote", "add", "origin", str(here / "gone.git"), cwd=here)

            told = update_self(checkout=here, restart=False)

        self.assertFalse(told["ok"])
        # git ends with "Please make sure you have the correct access rights /
        # and the repository exists.", which names nothing; the fatal line does.
        self.assertIn("Could not read from remote repository", told["said"])
        self.assertNotIn("and the repository exists", told["said"])
        self.assertNotIn(directory, told["said"], "a local path is not said to Discord")


class WhyTest(unittest.TestCase):
    def test_the_summary_fatal_line_wins(self):
        self.assertEqual(
            _why("fatal: '/x/gone.git' does not appear to be a git repository\n"
                 "fatal: Could not read from remote repository.\n"
                 "\n"
                 "Please make sure you have the correct access rights\n"
                 "and the repository exists."),
            "fatal: Could not read from remote repository.",
        )

    def test_advice_alone_falls_back_to_its_last_line(self):
        self.assertEqual(_why("error: Your local changes would be overwritten\nAborting\n"),
                         "Aborting")

    def test_no_output_says_nothing(self):
        self.assertEqual(_why(""), "")


class SupervisorTest(unittest.TestCase):
    """Only the job that runs this checkout is restarted."""

    PRINTED = """gui/501/dev.vigo.bot = {
\tactive count = 1
\tpath = /Users/you/Library/LaunchAgents/dev.vigo.bot.plist
\tstate = running

\tprogram = /opt/homebrew/bin/python3.14
\targuments = {
\t\t/opt/homebrew/bin/python3.14
\t\t-m
\t\tvigo
\t}

\tworking directory = /Users/you/Projects/vigo
}
"""

    def test_it_reads_the_jobs_working_directory(self):
        self.assertEqual(_launchd_workdir(self.PRINTED), "/Users/you/Projects/vigo")

    def test_a_job_without_one_is_not_this_checkout(self):
        self.assertEqual(_launchd_workdir("state = running\n"), "")

    def test_a_checkout_no_supervisor_runs_is_not_restarted(self):
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

            told = update_self(checkout=here)  # restart is on by default

        self.assertTrue(told["ok"])
        self.assertFalse(told["restarted"], "no supervisor runs this clone")


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
