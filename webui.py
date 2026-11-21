#!/usr/bin/env python3
"""Local web interface for ytarchive.

Serves a single-page UI on 127.0.0.1 only. Long operations (resolving and
enumerating a playlist, running downloads) happen in background threads or
subprocesses so the interface stays responsive; the page polls /api/state.
"""

from __future__ import annotations

import base64
import hmac
import json
import mimetypes
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import platforms as plat
import ytarchive as ya

UI_PATH = ya.APP_DIR / "ui.html"

# Optional HTTP Basic credentials, as "user:password". The interface can start,
# stop and reconfigure downloads, so it must not be reachable unauthenticated on
# a network - required whenever the bind address is not loopback.
AUTH = os.environ.get("YTARCHIVE_AUTH", "").strip()

# Progress of an in-flight "add", which can take a while for a large playlist.
_activity = {"busy": False, "message": "", "error": ""}
_activity_lock = threading.Lock()

# Interval of this process's own scheduler loop, when one is running.
_interval_minutes = 0

# yt-dlp version shown in the sidebar. The page polls about once a second, and
# asking yt-dlp for its version means starting a Python interpreter, so the
# answer is cached rather than recomputed per poll.
_ytdlp_cache = {"at": 0.0, "data": None}


_schedule_cache = {"at": 0.0, "data": None}


def schedule_summary() -> dict:
    """Whether a schedule exists and whether it still points at this folder.

    Cached because on Windows each check starts schtasks, and the page polls
    about once a second. A moved folder leaves the scheduler pointing at the old
    path - it fails in the background where nobody looks - so the interface is
    where that has to be visible.
    """
    if _schedule_cache["data"] is None or time.time() - _schedule_cache["at"] > 60:
        _schedule_cache.update(at=time.time(), data={
            "installed": ya.schedule_is_installed(),
            "stale": ya.schedule_problem(),
        })
    return _schedule_cache["data"]


def ytdlp_summary(cfg: dict) -> dict:
    if _ytdlp_cache["data"] is None or time.time() - _ytdlp_cache["at"] > 300:
        info = ya.ytdlp_update_info(cfg)
        info["installed"] = ya.ytdlp_version(cfg)
        _ytdlp_cache.update(at=time.time(), data=info)
    return _ytdlp_cache["data"]


def set_activity(**kw) -> None:
    with _activity_lock:
        _activity.update(kw)


def get_activity() -> dict:
    with _activity_lock:
        return dict(_activity)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def find_thumb(cfg: dict, video_id: str) -> "Path | None":
    """Locate a video's thumbnail.

    The database already records each video's path, and the thumbnail sits
    beside it under the same stem - so this is a lookup plus a couple of stats.
    Scanning the archive instead would mean walking every file for every
    thumbnail, which is O(archive) per request on a page showing hundreds.
    """
    row = ya.db().execute("SELECT filepath FROM videos WHERE id=?",
                          (video_id,)).fetchone()
    if row and row["filepath"]:
        media = Path(row["filepath"])
        for ext in ya.THUMB_EXTS:
            candidate = media.with_suffix(ext)
            if candidate.is_file():
                return candidate

    # Only fall back to searching when the path is unknown (e.g. adopted by
    # `scan` before filepath was recorded).
    root = ya.download_root(cfg)
    if not root.exists():
        return None
    for path in root.rglob(f"*[[]{video_id}[]]*"):
        if path.suffix.lower() in ya.THUMB_EXTS and path.is_file():
            return path
    return None


def tail_log(lines: int = 120) -> "list[str]":
    path = ya.LOG_DIR / "ytarchive.log"
    if not path.exists():
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, 64 * 1024)
            fh.seek(size - block)
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-lines:]


def build_state() -> dict:
    cfg = ya.load_config()
    conn = ya.db()

    sources = []
    for r in conn.execute(
        "SELECT s.*, "
        " (SELECT COUNT(*) FROM videos v WHERE v.source_id=s.id AND v.status='done') done, "
        " (SELECT COUNT(*) FROM videos v WHERE v.source_id=s.id "
        "   AND v.status IN ('pending','deferred')) pending, "
        " (SELECT COUNT(*) FROM videos v WHERE v.source_id=s.id AND v.status='failed') failed, "
        " (SELECT COALESCE(SUM(filesize),0) FROM videos v WHERE v.source_id=s.id "
        "   AND v.status='done') bytes "
        "FROM sources s ORDER BY s.title COLLATE NOCASE"
    ):
        sources.append(dict(r))

    counts = {r["status"]: r["n"] for r in
              conn.execute("SELECT status, COUNT(*) n FROM videos GROUP BY status")}
    total_bytes = conn.execute(
        "SELECT COALESCE(SUM(filesize),0) s FROM videos WHERE status='done'").fetchone()["s"]

    queue = [dict(r) for r in conn.execute(
        "SELECT v.id, v.title, v.status, v.attempts, v.last_error, s.title source_title, "
        "s.quality FROM videos v JOIN sources s ON s.id=v.source_id "
        "WHERE v.status IN ('pending','deferred','failed') "
        "AND (? = 0 OR v.attempts < ?) AND s.enabled=1 "
        "ORDER BY v.status='failed', "
        "v.published IS NULL, v.published ASC, v.position ASC, v.rowid ASC "
        "LIMIT 60", (cfg["max_attempts"], cfg["max_attempts"]))]

    failures = [dict(r) for r in conn.execute(
        "SELECT v.id, v.title, v.attempts, v.last_error, v.status, s.title source_title "
        "FROM videos v LEFT JOIN sources s ON s.id=v.source_id "
        "WHERE v.status IN ('failed','dead') ORDER BY v.rowid DESC LIMIT 40")]

    last = conn.execute(
        "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()

    free = 0
    try:
        root = ya.download_root(cfg)
        target = root if root.exists() else root.parent
        free = shutil.disk_usage(target).free
    except OSError:
        pass

    return {
        "sources": sources,
        "counts": counts,
        "queue": queue,
        "failures": failures,
        "totals": {
            "archived": counts.get("done", 0),
            "bytes": total_bytes,
            "pending": counts.get("pending", 0) + counts.get("deferred", 0),
            "failed": counts.get("failed", 0),
            "free": free,
        },
        "progress": ya.read_progress(),
        "running": ya.is_running(),
        "activity": get_activity(),
        "last_run": dict(last) if last else None,
        "scheduled": schedule_summary()["installed"],
        "schedule_stale": schedule_summary()["stale"],
        "in_container": plat.IN_CONTAINER,
        "version": ya.__version__,
        "interval_minutes": _interval_minutes,
        "ytdlp": ytdlp_summary(cfg),
        "config": {
            "download_dir": str(ya.download_root(cfg)),
            "default_quality": cfg["default_quality"],
            "default_container": cfg["default_container"],
        },
        "qualities": [{"value": k, "label": v} for k, v in ya.QUALITY_LABELS.items()],
        "video_containers": ya.VIDEO_CONTAINERS,
        "audio_containers": ya.AUDIO_CONTAINERS,
        "log": tail_log(),
    }


def library(source_id: str = "", query: str = "", limit: int = 300) -> list:
    conn = ya.db()
    sql = ("SELECT v.id, v.title, v.filesize, v.filepath, v.downloaded_at, "
           "s.title source_title, s.quality FROM videos v "
           "LEFT JOIN sources s ON s.id=v.source_id WHERE v.status='done'")
    params = []
    if source_id:
        sql += " AND v.source_id=?"
        params.append(source_id)
    if query:
        sql += " AND v.title LIKE ?"
        params.append(f"%{query}%")
    sql += " ORDER BY v.downloaded_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def spawn_run(extra=None) -> None:
    """Start a download run as a detached child so the UI never blocks on it."""
    cmd = [sys.executable, str(ya.APP_DIR / "ytarchive.py"), "run", "--no-progress"]
    cmd += extra or []
    plat.spawn_detached(cmd, str(ya.APP_DIR))


def do_add(payload: dict) -> None:
    """Resolve and enumerate a source, then queue its contents. Runs in a thread."""
    cfg = ya.load_config()
    try:
        set_activity(busy=True, message="Looking up source…", error="")
        conn = ya.db()
        res = ya.add_source(
            cfg, conn, payload["url"],
            payload.get("quality") or cfg["default_quality"],
            payload.get("container") or cfg["default_container"],
            limit=int(payload.get("limit") or 0),
            only_new=bool(payload.get("only_new")),
        )
        set_activity(busy=False, message=f"{res['title']}: {res['message']}", error="")
        ya.log(f"[ui] added {res['kind']} {res['title']} - {res['message']}")
        if payload.get("start") and not ya.is_running():
            spawn_run()
    except Exception as exc:                       # surfaced in the UI banner
        set_activity(busy=False, message="", error=str(exc))
        ya.log(f"[ui] add failed: {exc}", err=True)


# --------------------------------------------------------------------------
# request handling
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "ytarchive"

    def log_message(self, fmt, *args):
        pass                                        # keep the console clean

    # -- auth -------------------------------------------------------------

    def authorised(self) -> bool:
        if not AUTH:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            given = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        # compare_digest avoids leaking the password through timing
        return hmac.compare_digest(given, AUTH)

    def require_auth(self) -> bool:
        """Returns True when the request may proceed."""
        if self.authorised():
            return True
        body = b"authentication required"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ytarchive"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return False

    # -- plumbing ---------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str, extra=None) -> None:
        extra = extra or {}
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Default to no-store for live data; a caller may override for static
        # content, and sending both would leave the browser with two conflicting
        # Cache-Control headers.
        if not any(k.lower() == "cache-control" for k in extra):
            self.send_header("Cache-Control", "no-store")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        if not self.require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            try:
                body = UI_PATH.read_bytes()
            except OSError:
                self._send(500, b"ui.html is missing", "text/plain")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return

        if path == "/api/state":
            self._json(build_state())
            return

        if path == "/api/library":
            self._json(library(params.get("source", [""])[0],
                               params.get("q", [""])[0]))
            return

        if path == "/api/resolve":
            url = params.get("url", [""])[0].strip()
            if not url:
                self._json({"error": "no url"}, 400)
                return
            try:
                info = ya.resolve_source(ya.load_config(), url)
                self._json(info)
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return

        if path.startswith("/thumb/"):
            vid = path[len("/thumb/"):]
            if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                self._send(400, b"bad id", "text/plain")
                return
            thumb = find_thumb(ya.load_config(), vid)
            if not thumb:
                self._send(404, b"no thumbnail", "text/plain")
                return
            ctype = mimetypes.guess_type(thumb.name)[0] or "image/webp"
            try:
                # Thumbnails never change once written, so let the browser keep
                # them rather than refetching on every poll of the library.
                self._send(200, thumb.read_bytes(), ctype,
                           {"Cache-Control": "public, max-age=86400"})
            except OSError:
                self._send(404, b"unreadable", "text/plain")
            return

        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self.require_auth():
            return
        path = urllib.parse.urlparse(self.path).path
        data = self._body()

        if path == "/api/sources":
            url = (data.get("url") or "").strip()
            if not url:
                self._json({"error": "no url given"}, 400)
                return
            if get_activity().get("busy"):
                self._json({"error": "already adding something"}, 409)
                return
            threading.Thread(target=do_add, args=(data,), daemon=True).start()
            self._json({"ok": True})
            return

        if path == "/api/source/update":
            sid = data.get("id")
            conn = ya.db()
            row = conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
            if not row:
                self._json({"error": "unknown source"}, 404)
                return
            quality = data.get("quality") or row["quality"]
            container = data.get("container") or row["container"]
            enabled = int(data.get("enabled", row["enabled"]))
            conn.execute("UPDATE sources SET quality=?, container=?, enabled=? WHERE id=?",
                         (quality, container, enabled, sid))
            conn.commit()
            if data.get("redownload"):
                queued, removed, freed = ya.requeue_source(conn, sid)
                ya.log(f"[ui] {row['title']}: deleted {removed} file(s) "
                       f"({ya.human(freed)}), requeued {queued} at "
                       f"{quality}/{container}")
            self._json({"ok": True})
            return

        if path == "/api/source/remove":
            conn = ya.db()
            sid = data.get("id")
            if data.get("purge"):
                conn.execute("DELETE FROM videos WHERE source_id=?", (sid,))
            conn.execute("DELETE FROM sources WHERE id=?", (sid,))
            conn.commit()
            self._json({"ok": True})
            return

        if path == "/api/run":
            if ya.is_running():
                self._json({"error": "a run is already in progress"}, 409)
                return
            spawn_run(["--deep"] if data.get("deep") else None)
            self._json({"ok": True})
            return

        if path == "/api/stop":
            ya.STOP_PATH.touch()
            self._json({"ok": True})
            return

        if path == "/api/retry":
            conn = ya.db()
            vid = data.get("id")
            if vid:
                rows = [vid]
                conn.execute("UPDATE videos SET status='pending', attempts=0, "
                             "last_error=NULL WHERE id=?", (vid,))
            else:
                rows = [r["id"] for r in
                        conn.execute("SELECT id FROM videos WHERE status='failed'")]
                conn.execute("UPDATE videos SET status='pending', attempts=0, "
                             "last_error=NULL WHERE status='failed'")
            conn.commit()
            ya.archive_remove(rows)
            self._json({"ok": True, "requeued": len(rows)})
            return

        if path == "/api/reveal":
            root = ya.download_root(ya.load_config())
            target = data.get("path") or str(root)
            # Only ever open something inside the archive folder. normcase keeps
            # this correct on Windows, where paths are case-insensitive.
            here = os.path.normcase(os.path.abspath(target))
            if not here.startswith(os.path.normcase(str(root))):
                self._json({"error": "outside the archive"}, 400)
                return
            plat.reveal(target, select=bool(data.get("reveal")))
            self._json({"ok": True})
            return

        if path == "/api/schedule":
            action = data.get("action")
            if action not in ("install", "uninstall"):
                self._json({"error": "bad action"}, 400)
                return
            args = type("A", (), {"action": action,
                                  "interval": int(data.get("interval") or 60)})()
            try:
                ya.cmd_schedule(args)
            except SystemExit as exc:
                self._json({"error": str(exc)}, 500)
                return
            self._json({"ok": True})
            return

        self._send(404, b"not found", "text/plain")


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse DNS lookup on start-up.

    HTTPServer.server_bind() resolves the host's fully-qualified name to fill in
    server_name, which nothing here uses. That lookup can stall for tens of
    seconds when DNS is slow or the machine is offline, so the interface took
    that long to open.
    """

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def scheduler_loop(interval_minutes: int, stop_event: threading.Event) -> None:
    """Periodic checks from inside this process.

    Used where no host scheduler is available (containers) or when the user
    would rather keep one process running than register a system task. Each
    check is a child process, so it reuses the ordinary run path - including the
    lock that stops two runs overlapping - and gets reaped rather than lingering.
    """
    first_delay = 30
    if stop_event.wait(first_delay):
        return
    while True:
        if not ya.is_running():
            ya.log(f"[scheduler] starting check (every {interval_minutes} min)")
            cmd = [sys.executable, str(ya.APP_DIR / "ytarchive.py"),
                   "run", "--no-progress"]
            try:
                subprocess.run(cmd, cwd=str(ya.APP_DIR), env=plat.with_tool_path(),
                               **plat.no_window_kwargs())
            except OSError as exc:
                ya.log(f"[scheduler] could not start a check: {exc}", err=True)
        if stop_event.wait(interval_minutes * 60):
            return


def serve(port: int = 8800, host: str = "127.0.0.1", interval_minutes: int = 0,
          open_browser: bool = True) -> None:
    if not UI_PATH.exists():
        ya.die(f"ui.html not found next to ytarchive.py ({UI_PATH})")

    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not AUTH:
        ya.log(f"WARNING: bound to {host} with no password. Anyone who can reach "
               f"this port can add sources and start downloads. Set "
               f"YTARCHIVE_AUTH=user:password to require a login.", err=True)

    for attempt in range(20):
        try:
            httpd = Server((host, port + attempt), Handler)
            break
        except OSError as exc:
            last = exc
            continue
    else:
        ya.die(f"could not bind {host} near port {port}: {last}")

    bound = httpd.server_address[1]
    shown = "127.0.0.1" if loopback else host
    url = f"http://{shown}:{bound}"
    ya.log(f"web interface on {url}  (ctrl-c to stop)")
    if AUTH:
        ya.log("  login required (YTARCHIVE_AUTH is set)")

    global _interval_minutes
    stop_event = threading.Event()
    if interval_minutes > 0:
        _interval_minutes = interval_minutes
        ya.log(f"  checking every {interval_minutes} minute(s) in this process")
        threading.Thread(target=scheduler_loop, args=(interval_minutes, stop_event),
                         daemon=True).start()

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        ya.log("web interface stopped")
    finally:
        stop_event.set()
        httpd.shutdown()
