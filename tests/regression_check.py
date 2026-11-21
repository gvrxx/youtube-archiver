#!/usr/bin/env python3
"""Regression checks for bugs that reached a real install.

Offline: uses temporary state and download folders, and never contacts YouTube
or touches an existing archive. Standard library only.

    python tests/regression_check.py
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_temp = tempfile.TemporaryDirectory(prefix="yta-regression-")
os.environ["YTARCHIVE_DATA"] = _temp.name
os.environ["YTARCHIVE_DOWNLOADS"] = str(Path(_temp.name) / "downloads")
import ytarchive as ya  # noqa: E402


class ScheduleWarning(unittest.TestCase):
    """`status` and the interface flag a schedule that can no longer run."""

    def stale_for(self, target):
        with mock.patch.object(ya.plat, "scheduled_script_path", return_value=target):
            return ya.stale_schedule_target()

    def test_schedule_pointing_here_is_not_reported(self):
        self.assertIsNone(self.stale_for(str(ya.APP_DIR / "ytarchive.py")))

    @unittest.skipUnless((ya.APP_DIR / "YTARCHIVE.PY").exists(),
                         "needs a filesystem that ignores capitalisation")
    def test_folder_renamed_by_capitalisation_only_is_not_reported(self):
        # The folder was renamed from CLAUDE to Claude. macOS and Windows treat
        # both spellings as the same folder, so the schedule kept working - but
        # comparing paths as text called it broken.
        self.assertIsNone(self.stale_for(str(ya.APP_DIR / "YTARCHIVE.PY")))

    def test_schedule_pointing_at_a_vanished_folder_is_reported(self):
        gone = "/nowhere/ytarchive/ytarchive.py"
        self.assertEqual(self.stale_for(gone), gone)


class UpgradeShortCopies(unittest.TestCase):
    """A copy below the requested quality is kept, then replaced once a
    better one has actually been downloaded."""

    def setUp(self):
        for path in (ya.DB_PATH, ya.ARCHIVE_PATH, ya.CONFIG_PATH):
            path.unlink(missing_ok=True)
        self.root = ya.download_root(ya.load_config())
        shutil.rmtree(self.root, ignore_errors=True)
        (self.root / "S").mkdir(parents=True)
        self.conn = ya.db()
        self.conn.execute("INSERT INTO sources(id,kind,title,url,added_at,quality,container,folder)"
                          " VALUES('S','playlist','S','u',?,'720','mkv','S')", (ya.now(),))
        self.conn.commit()
        self.cfg = dict(ya.load_config(), player_client="",
                        player_client_fallbacks=["android_vr"])

    def tearDown(self):
        self.conn.close()

    def low_copy(self, vid="aBcDeFgHiJk", flagged=True):
        media = self.root / "S" / f"Video [{vid}].mp4"
        media.write_bytes(b"360p copy")
        (self.root / "S" / f"Video [{vid}].en.vtt").write_bytes(b"subs")
        self.conn.execute(
            "INSERT INTO videos(id,source_id,title,status,first_seen,filepath,filesize,"
            "height,needs_upgrade) VALUES(?,?,?,?,?,?,?,?,?)",
            (vid, "S", "Video", "done", ya.now(), str(media), 9, 360, int(flagged)))
        self.conn.commit()
        return media

    def run_once(self, **patches):
        from types import SimpleNamespace
        args = SimpleNamespace(dry_run=False, deep=False, limit=None, no_progress=True)
        with mock.patch.object(ya, "enumerate_source", return_value=(None, [])), \
             mock.patch.object(ya, "update_ytdlp", return_value=False), \
             mock.patch.object(ya.shutil, "which", return_value="/fake/yt-dlp"), \
             mock.patch.object(ya.time, "sleep"), \
             mock.patch.object(ya, "client_offering", patches.get("offering", mock.Mock(return_value=None))) as off, \
             mock.patch.object(ya, "attempt_download", patches.get("attempt", mock.Mock())) as att:
            ya.cmd_run(args)
        return off, att

    def row(self, vid="aBcDeFgHiJk"):
        return self.conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()

    def staged_download(self, height):
        """Fake yt-dlp: writes a finished file where it was told to and reports it."""
        def attempt(cfg, video_id, quality, container, client, *a, staging=None, **k):
            out = Path(staging) / f"Video [{video_id}].mkv"
            out.write_bytes(b"better copy")
            (Path(staging) / f"Video [{video_id}].en.vtt").write_bytes(b"new subs")
            return {"status": "done", "filepath": str(out), "filesize": 11, "height": height}
        return mock.Mock(side_effect=attempt)

    # -- keeping and flagging --------------------------------------------------

    def test_short_copy_is_kept_and_flagged_when_better_client_was_blocked(self):
        media = self.root / "S" / "Short [aBcDeFgHiJk].mp4"
        media.write_bytes(b"x")
        with mock.patch.object(ya, "attempt_download", side_effect=[
                {"status": "failed", "error": "HTTP Error 403", "client_issue": True},
                {"status": "done", "filepath": str(media), "height": 360}]), \
             mock.patch.object(ya, "client_offering", return_value=None):
            result = ya.download_video(self.cfg, "aBcDeFgHiJk", "720", "mkv")
        self.assertEqual((result["status"], result.get("short")), ("done", True))
        self.assertTrue(media.exists())

    def test_copy_at_the_requested_quality_is_not_flagged(self):
        with mock.patch.object(ya, "attempt_download",
                               return_value={"status": "done", "filepath": "x", "height": 720}):
            result = ya.download_video(self.cfg, "aBcDeFgHiJk", "720", "mkv")
        self.assertFalse(result.get("short"))

    def test_run_records_height_and_flags_short_copy(self):
        self.conn.execute("INSERT INTO videos(id,source_id,title,status,first_seen)"
                          " VALUES('aBcDeFgHiJk','S','V','pending',?)", (ya.now(),))
        self.conn.commit()
        short = {"status": "done", "filepath": "x", "filesize": 1, "height": 360, "short": True}
        with mock.patch.object(ya, "download_video", return_value=short):
            self.run_once()
        r = self.row()
        self.assertEqual((r["height"], r["needs_upgrade"]), (360, 1))

    # -- upgrading -------------------------------------------------------------

    def test_better_copy_replaces_the_short_one(self):
        old = self.low_copy()
        self.run_once(offering=mock.Mock(return_value="android_vr"),
                      attempt=self.staged_download(720))
        new = self.root / "S" / "Video [aBcDeFgHiJk].mkv"
        self.assertTrue(new.exists())
        self.assertFalse(old.exists())
        self.assertEqual((self.root / "S" / "Video [aBcDeFgHiJk].en.vtt").read_bytes(), b"new subs")
        r = self.row()
        self.assertEqual((r["filepath"], r["height"], r["needs_upgrade"]), (str(new), 720, 0))

    def test_short_copy_is_kept_when_no_better_version_is_reachable(self):
        old = self.low_copy()
        _, att = self.run_once(offering=mock.Mock(return_value=None))
        self.assertTrue(old.exists())
        self.assertEqual(att.call_count, 0)
        self.assertEqual(self.row()["needs_upgrade"], 1)

    def test_upgrade_that_is_not_actually_better_keeps_the_original(self):
        old = self.low_copy()
        self.run_once(offering=mock.Mock(return_value="android_vr"),
                      attempt=self.staged_download(360))
        self.assertTrue(old.exists())
        self.assertFalse((self.root / "S" / "Video [aBcDeFgHiJk].mkv").exists())

    def test_failed_upgrade_download_keeps_the_original(self):
        old = self.low_copy()
        self.run_once(offering=mock.Mock(return_value="android_vr"),
                      attempt=mock.Mock(return_value={"status": "failed", "error": "403"}))
        self.assertTrue(old.exists())
        self.assertEqual(self.row()["needs_upgrade"], 1)

    def test_each_short_copy_is_retried_at_most_once_a_day(self):
        self.low_copy()
        self.run_once(offering=mock.Mock(return_value=None))
        off, _ = self.run_once(offering=mock.Mock(return_value=None))
        self.assertEqual(off.call_count, 0)

    def test_upgrades_respect_the_per_run_download_limit(self):
        for n in range(5):
            self.low_copy(f"vid{n:08d}")
        Path(ya.CONFIG_PATH).write_text('{"max_downloads_per_run": 3}')
        off, _ = self.run_once(offering=mock.Mock(return_value=None))
        self.assertEqual(off.call_count, 3)


class SingleVideos(unittest.TestCase):
    """A video added on its own downloads into the main folder; playlists and
    channels keep a folder each."""

    def setUp(self):
        for path in (ya.DB_PATH, ya.ARCHIVE_PATH, ya.CONFIG_PATH):
            path.unlink(missing_ok=True)
        self.cfg = ya.load_config()
        self.root = ya.download_root(self.cfg)
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True)
        self.conn = ya.db()

    def tearDown(self):
        self.conn.close()

    def add(self, url):
        with mock.patch.object(ya, "ytdlp_field", return_value="A Video"):
            return ya.add_source(self.cfg, self.conn, url, "720", "mkv")

    def test_video_links_are_recognised_as_single_videos(self):
        for url in ("https://www.youtube.com/watch?v=aBcDeFgHiJk",
                    "https://youtu.be/aBcDeFgHiJk?si=x",
                    "https://www.youtube.com/shorts/aBcDeFgHiJk",
                    "https://m.youtube.com/watch?feature=share&v=aBcDeFgHiJk"):
            with mock.patch.object(ya, "ytdlp_field", return_value="A Video"):
                info = ya.resolve_source(self.cfg, url)
            self.assertEqual((info["kind"], info["id"]), ("video", "aBcDeFgHiJk"), url)

    def test_a_video_shared_from_a_playlist_still_adds_the_playlist(self):
        with mock.patch.object(ya, "playlist_title", return_value="P"):
            info = ya.resolve_source(
                self.cfg, "https://www.youtube.com/watch?v=aBcDeFgHiJk&list=PLabcdefghijklmnop")
        self.assertEqual(info["kind"], "playlist")

    def test_single_video_is_queued_and_downloads_into_the_main_folder(self):
        res = self.add("https://youtu.be/aBcDeFgHiJk")
        self.assertEqual(res["queued"], 1)
        row = self.conn.execute("SELECT v.status, s.folder FROM videos v "
                                "JOIN sources s ON s.id=v.source_id").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(ya.source_dir(self.cfg, row["folder"]), self.root)
        cmd = ya.build_ytdlp_cmd(self.cfg, "aBcDeFgHiJk", self.root / "p", "720", "mkv",
                                 folder=row["folder"])
        out = Path(cmd[cmd.index("-o") + 1])
        self.assertEqual(out.parent, self.root)

    def test_playlists_still_get_their_own_folder(self):
        self.assertEqual(ya.source_dir(self.cfg, "My List"), self.root / "My List")

    def test_adding_the_same_video_twice_does_not_queue_it_again(self):
        self.add("https://youtu.be/aBcDeFgHiJk")
        self.assertEqual(self.add("https://youtu.be/aBcDeFgHiJk")["queued"], 0)

    def test_video_already_in_the_main_folder_is_not_downloaded_again(self):
        (self.root / "A Video [aBcDeFgHiJk].mkv").write_bytes(b"x")
        res = self.add("https://youtu.be/aBcDeFgHiJk")
        self.assertEqual((res["queued"], res["adopted"]), (0, 1))


class Login(unittest.TestCase):
    """The web interface's login, as set in YTARCHIVE_AUTH."""

    def allowed(self, auth, user, password):
        import base64
        import webui
        handler = webui.Handler.__new__(webui.Handler)
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        handler.headers = {"Authorization": "Basic " + token}
        with mock.patch.object(webui, "AUTH", auth):
            return handler.authorised()

    def test_user_and_password_must_both_match(self):
        self.assertTrue(self.allowed("admin:pw", "admin", "pw"))
        self.assertFalse(self.allowed("admin:pw", "other", "pw"))
        self.assertFalse(self.allowed("admin:pw", "admin", "nope"))

    def test_password_only_login_accepts_any_username(self):
        # Set as just a password, no username could ever log in.
        self.assertTrue(self.allowed("pw", "anything", "pw"))
        self.assertFalse(self.allowed("pw", "anything", "nope"))


class Versioning(unittest.TestCase):
    """Every release has a version, and the changelog says what changed in it."""

    def test_changelog_describes_the_current_version(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## {ya.__version__} ", changelog)

    def test_version_flag_prints_the_version(self):
        import subprocess
        out = subprocess.run([sys.executable, str(ROOT / "ytarchive.py"), "--version"],
                             capture_output=True, text=True).stdout
        self.assertIn(ya.__version__, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
