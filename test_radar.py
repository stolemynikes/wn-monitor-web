#!/usr/bin/env python3
"""Tests for the parts that have actually broken.

    python test_radar.py            # or: python -m unittest test_radar -v

stdlib unittest on purpose: no dependency to install, so it runs anywhere the
radar itself runs. Every case here corresponds to a real defect — the comments
say which, because a test whose reason is forgotten is a test nobody dares
delete.
"""

import json
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import monitor
import profile_tools
import web


class TempProject(unittest.TestCase):
    """Point the modules at a scratch directory, not the real install."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self._saved = {
            "profile": profile_tools.PROFILE_DIR,
            "lock": profile_tools.CONFIG_LOCK,
            "seen": profile_tools.SEEN_PATH,
            "web_cfg": web.CONFIG_PATH,
            "mon_cfg": monitor.CONFIG_PATH,
        }
        profile_tools.PROFILE_DIR = self.dir / "whatnot-profile"
        profile_tools.CONFIG_LOCK = self.dir / ".config.lock"
        profile_tools.SEEN_PATH = self.dir / ".session_seen"
        web.CONFIG_PATH = monitor.CONFIG_PATH = self.dir / "config.json"
        profile_tools._SIZE_CACHE["value"] = None

    def tearDown(self):
        profile_tools.PROFILE_DIR = self._saved["profile"]
        profile_tools.CONFIG_LOCK = self._saved["lock"]
        profile_tools.SEEN_PATH = self._saved["seen"]
        web.CONFIG_PATH = self._saved["web_cfg"]
        monitor.CONFIG_PATH = self._saved["mon_cfg"]

    def write_config(self, **extra):
        cfg = {"notifier": "bark", "bark_key": "k", "blacklist": []}
        cfg.update(extra)
        web.CONFIG_PATH.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg


class SellerParsing(TempProject):
    """Bulk blocking: a pasted list must survive whatever shape it arrives in."""

    def test_separators_and_at_prefix(self):
        self.assertEqual(
            web.parse_sellers("sellername1, sellername2, etc3,"),
            ["sellername1", "sellername2", "etc3"])
        self.assertEqual(
            web.parse_sellers("@a  @b\n@c;d"), ["a", "b", "c", "d"])

    def test_case_folded_and_deduped(self):
        self.assertEqual(web.parse_sellers("DuPe, dupe, DUPE"), ["dupe"])

    def test_junk_yields_nothing(self):
        self.assertEqual(web.parse_sellers("  ,,, "), [])
        self.assertEqual(web.parse_sellers(""), [])

    def test_absurdly_long_token_dropped(self):
        self.assertEqual(web.parse_sellers("x" * 200), [])


class GiveawayIdFallback(TempProject):
    """Two id-less giveaways on one stream used to collapse into one
    notification, so the second was silently never sent."""

    def ev(self, **kw):
        base = {"product_id": "", "title": "", "ends_at_ms": None}
        base.update(kw)
        return base

    def test_product_id_preferred(self):
        self.assertEqual(monitor.fallback_gid("s1", self.ev(product_id="p9")),
                         "ws:s1:p9")

    def test_different_prizes_get_different_keys(self):
        a = monitor.fallback_gid("s1", self.ev(title="Booster box",
                                               ends_at_ms=1000))
        b = monitor.fallback_gid("s1", self.ev(title="Sticker",
                                               ends_at_ms=2000))
        self.assertNotEqual(a, b)

    def test_same_giveaway_redelivered_dedupes(self):
        ev = self.ev(title="Booster box", ends_at_ms=1000)
        self.assertEqual(monitor.fallback_gid("s1", ev),
                         monitor.fallback_gid("s1", dict(ev)))

    def test_same_prize_on_two_streams_differs(self):
        ev = self.ev(title="Booster box", ends_at_ms=1000)
        self.assertNotEqual(monitor.fallback_gid("s1", ev),
                            monitor.fallback_gid("s2", ev))


class SessionState(TempProject):
    """The login badge said "no profile" for a good session, and reported
    logged-out whenever Windows had the cookie DB locked."""

    def cookie_db(self, rel, hosts=("www.whatnot.com",)):
        path = profile_tools.PROFILE_DIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE cookies (host_key TEXT)")
        con.executemany("INSERT INTO cookies VALUES (?)", [(h,) for h in hosts])
        con.commit()
        con.close()

    def test_no_profile_at_all(self):
        st = profile_tools.session_state()
        self.assertFalse(st["logged_in"])
        self.assertIn("no profile yet", st["detail"])

    def test_finds_chrome_network_layout(self):
        self.cookie_db("Default/Network/Cookies")
        self.assertTrue(profile_tools.session_state()["logged_in"])

    def test_finds_chromium_layout(self):
        self.cookie_db("Default/Cookies")
        self.assertTrue(profile_tools.session_state()["logged_in"])

    def test_profile_without_cookies_is_distinguished(self):
        profile_tools.PROFILE_DIR.mkdir(parents=True)
        st = profile_tools.session_state()
        self.assertFalse(st["logged_in"])
        self.assertIn("login didn't finish", st["detail"])

    def test_locked_db_keeps_the_last_known_count(self):
        self.cookie_db("Default/Cookies", ["a.whatnot.com", "b.whatnot.com"])
        self.assertEqual(profile_tools.session_state()["cookie_count"], 2)
        real = profile_tools._count_whatnot_cookies
        profile_tools._count_whatnot_cookies = lambda db: (_ for _ in ()).throw(
            PermissionError(13, "locked"))
        try:
            st = profile_tools.session_state()
        finally:
            profile_tools._count_whatnot_cookies = real
        self.assertTrue(st["logged_in"], "a locked file is not a logout")
        self.assertEqual(st["cookie_count"], 2)
        self.assertTrue(st["locked"])

    def test_directory_where_the_cookie_file_should_be(self):
        # Opening a directory raises PermissionError on Windows, which used to
        # be indistinguishable from a lock.
        (profile_tools.PROFILE_DIR / "Default" / "Cookies").mkdir(parents=True)
        self.assertIsNone(profile_tools.cookie_db())


class Deletion(TempProject):
    """A locked file used to abort the delete with a 500, and "fresh profile"
    reported success having removed nothing."""

    def test_delete_reports_what_would_not_go(self):
        prof = profile_tools.PROFILE_DIR
        (prof / "Default").mkdir(parents=True)
        (prof / "Default" / "Cookies").write_bytes(b"x" * 100)
        real = pathlib.Path.unlink

        def picky(self, **kw):
            if self.name == "Cookies":
                raise PermissionError(32, "in use")
            return real(self, **kw)

        pathlib.Path.unlink = picky
        try:
            freed, stuck = profile_tools.clear_site_data()
        finally:
            pathlib.Path.unlink = real
        self.assertEqual(len(stuck), 1)
        self.assertTrue((prof / "Default" / "Cookies").exists())

    def test_reset_counts_survivors(self):
        (profile_tools.PROFILE_DIR / "Default").mkdir(parents=True)
        (profile_tools.PROFILE_DIR / "Default" / "f").write_bytes(b"x")
        real = shutil.rmtree
        shutil.rmtree = lambda p, **kw: None      # deletes nothing, silently
        try:
            _freed, left = profile_tools.reset_profile()
        finally:
            shutil.rmtree = real
        self.assertEqual(left, 1, "a reset that removed nothing must say so")

    def test_size_survives_files_vanishing_mid_walk(self):
        d = profile_tools.PROFILE_DIR / "Default" / "Cache"
        d.mkdir(parents=True)
        for i in range(20):
            (d / str(i)).write_bytes(b"x" * 10)
        real = pathlib.Path.stat

        def vanishing(self, **kw):
            if self.name == "7":
                raise FileNotFoundError(2, "gone")
            return real(self, **kw)

        pathlib.Path.stat = vanishing
        try:
            profile_tools._size(profile_tools.PROFILE_DIR)   # must not raise
        finally:
            pathlib.Path.stat = real


class ConfigLocking(TempProject):
    """The panel and the monitor both rewrite config.json; without the lock
    one update is silently lost."""

    def test_concurrent_updates_all_survive(self):
        self.write_config(blacklist=[])
        errors = []

        def add(name):
            try:
                for _ in range(10):
                    with profile_tools.config_lock():
                        cfg = json.loads(
                            web.CONFIG_PATH.read_text(encoding="utf-8"))
                        cfg["blacklist"] = cfg["blacklist"] + [name]
                        web.CONFIG_PATH.write_text(
                            json.dumps(cfg), encoding="utf-8")
            except Exception as exc:            # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(f"s{i}",))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        final = json.loads(web.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(len(final["blacklist"]), 40,
                         "every write should survive")

    def test_stale_lock_is_broken_rather_than_deadlocking(self):
        profile_tools.CONFIG_LOCK.write_text("")     # orphan from a killed run
        with profile_tools.config_lock(timeout=0.2):
            pass
        self.assertFalse(profile_tools.CONFIG_LOCK.exists())


class Readiness(TempProject):
    """Start is gated on these, so they must not drift from the checklist."""

    def test_missing_everything(self):
        self.write_config(bark_key="", discovery={}, sellers=[])
        r = web.readiness()
        self.assertFalse(r["ready"])
        self.assertEqual(len(r["missing"]), 3)

    def test_ready_when_all_present(self):
        self.write_config(bark_key="abc", sellers=["x"],
                          discovery={"sources": [{"feedId": "f"}]})
        (profile_tools.PROFILE_DIR / "Default").mkdir(parents=True)
        con = sqlite3.connect(profile_tools.PROFILE_DIR / "Default" / "Cookies")
        con.execute("CREATE TABLE cookies (host_key TEXT)")
        con.execute("INSERT INTO cookies VALUES ('www.whatnot.com')")
        con.commit()
        con.close()
        self.assertTrue(web.readiness()["ready"])


class SshCommands(TempProject):
    """Shortcuts needs the bare command; a terminal needs the ssh wrapper. The
    two are not interchangeable, and Windows paths need cmd.exe quoting."""

    def test_shortcuts_script_has_no_ssh_prefix(self):
        info = web.api_ssh_info()
        for action, script in info["scripts"].items():
            self.assertFalse(script.startswith("ssh "))
            self.assertTrue(script.endswith(f" {action}"))

    def test_terminal_command_wraps_the_script(self):
        info = web.api_ssh_info()
        self.assertTrue(info["commands"]["start"].startswith("ssh "))

    def test_dash_leading_username_is_flagged(self):
        import getpass
        real = getpass.getuser
        getpass.getuser = lambda: "--"
        try:
            self.assertTrue(web.api_ssh_info()["user_warning"])
        finally:
            getpass.getuser = real


class AuditLog(TempProject):
    """A 🎁 in the title crashed the audit write on Windows, and the caller
    reported it as a failed send for a push that had already gone out."""

    def test_emoji_title_is_written(self):
        monitor.SEND_LOG_PATH = self.dir / "notifications.log"
        monitor.audit_send("bark", "max", "\U0001f381 Giveaway", "OK")
        line = monitor.SEND_LOG_PATH.read_text(encoding="utf-8")
        self.assertIn("\U0001f381", line)

    def test_unwritable_log_does_not_raise(self):
        monitor.SEND_LOG_PATH = self.dir / "nope" / "notifications.log"
        monitor.audit_send("bark", "max", "title", "OK")   # must not raise


class StopPathsActuallyStop(unittest.TestCase):
    """A challenge must end the run, not just the helper that spotted it.

    Shipped bug: announce_challenge() was followed by `return` inside
    process_watchers(), a nested helper — so the run loop carried on,
    re-detected the same challenge two seconds later, and pushed the
    notification again, indefinitely. Source-level checks, because cmd_run is
    a 700-line closure over a live browser and cannot be called in a test;
    they catch the specific mistake of a stop path that does not set the flag.
    """

    @classmethod
    def setUpClass(cls):
        import ast
        cls.ast = ast
        source = pathlib.Path(__file__).with_name("monitor.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        cls.cmd_run = next(n for n in ast.walk(tree)
                           if isinstance(n, ast.FunctionDef)
                           and n.name == "cmd_run")
        cls.helpers = {n.name: n for n in ast.walk(cls.cmd_run)
                       if isinstance(n, ast.FunctionDef) and n is not cls.cmd_run}

    def test_announce_sets_the_shutdown_flag(self):
        fn = self.helpers["announce_challenge"]
        sets_flag = any(
            isinstance(n, self.ast.Assign)
            and any(isinstance(t, self.ast.Subscript)
                    and getattr(t.value, "id", "") == "stop" for t in n.targets)
            for n in self.ast.walk(fn))
        self.assertTrue(sets_flag,
                        "announce_challenge must set stop['requested']; a bare "
                        "return from a nested helper does not end the run loop")

    def test_announce_is_guarded_against_repeating(self):
        fn = self.helpers["announce_challenge"]
        names = {n.id for n in self.ast.walk(fn) if isinstance(n, self.ast.Name)}
        self.assertIn("announced_challenge", names,
                      "one challenge must produce one notification, not one "
                      "per loop tick")

    def test_run_loop_honours_the_flag(self):
        loops = [n for n in self.ast.walk(self.cmd_run)
                 if isinstance(n, self.ast.While)]
        honoured = any("stop" in self.ast.dump(n.test) for n in loops)
        self.assertTrue(honoured, "the main loop must exit on stop['requested']")


def _playwright_available():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_playwright_available(), "needs a Playwright browser")
class ChallengeDetection(unittest.TestCase):
    """A Turnstile widget sat on every stream tab undetected: it leaves
    Whatnot's own title in place and lives in a cross-origin iframe."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(headless=True)
        cls.page = cls._browser.new_page()

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def marker(self, html):
        self.page.set_content(html)
        return monitor.challenge_marker(self.page)

    def test_full_interstitial(self):
        self.assertTrue(self.marker("<title>Just a moment...</title><body>x"))

    def test_interstitial_with_generic_title(self):
        self.assertTrue(
            self.marker("<title>whatnot</title><body><div id='challenge-running'>x</div>"))

    def test_visible_turnstile_widget(self):
        self.assertTrue(self.marker(
            "<title>Whatnot | Live</title><body>"
            "<iframe src='https://challenges.cloudflare.com/x'"
            " style='width:300px;height:65px'></iframe>"))

    def test_challenge_text(self):
        self.assertTrue(self.marker(
            "<title>Whatnot</title><body><p>Verify you are human</p>"))

    def test_normal_page_is_clean(self):
        self.assertEqual(self.marker(
            "<title>Whatnot | Live</title><body><h1>Pokemon break</h1>"), "")

    def test_cf_protected_page_without_a_challenge_is_clean(self):
        # challenge-platform scripts are on EVERY Cloudflare-protected page.
        # Matching them would stop the radar permanently.
        self.assertEqual(self.marker(
            "<title>Whatnot | Live</title><body>"
            "<script src='/cdn-cgi/challenge-platform/h/b/scripts/x.js'></script>"), "")

    def test_hidden_turnstile_is_clean(self):
        # A zero-size widget is a passive check that resolves itself.
        self.assertEqual(self.marker(
            "<title>Whatnot | Live</title><body>"
            "<iframe src='https://challenges.cloudflare.com/x'"
            " style='width:0;height:0'></iframe>"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
