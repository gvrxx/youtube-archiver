#!/usr/bin/env python3
"""ytarchive - archive YouTube channels and playlists locally.

Tracks channels and playlists as *sources*, each with its own quality and
container settings, and keeps a local copy of every video in them. Designed to
be run repeatedly (hourly) from launchd; all state is idempotent, so repeated,
interrupted, or overlapping runs never re-download anything.

Stdlib only. Requires the `yt-dlp` and `ffmpeg` binaries on PATH.
"""

from __future__ import annotations

# Bump with every change worth noting, and add a matching CHANGELOG.md entry.
__version__ = "1.1.2"

import argparse
import csv
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import platforms as plat

APP_DIR = Path(__file__).resolve().parent

# All mutable state lives here. Kept beside the script for a normal install, and
# pointed at a mounted volume (/config) when running in a container so nothing
# is lost when the container is replaced.
DATA_DIR = Path(os.environ.get("YTARCHIVE_DATA") or APP_DIR).expanduser()
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
DATA_DIR = DATA_DIR.resolve()

CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "state.db"
ARCHIVE_PATH = DATA_DIR / "downloaded.txt"
PROGRESS_PATH = DATA_DIR / ".progress.json"
STOP_PATH = DATA_DIR / ".stop"
LOG_DIR = DATA_DIR / "logs"
LOCK_PATH = DATA_DIR / ".run.lock"

AGENT_LABEL = plat.AGENT_LABEL

CHANNEL_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"

CHANNEL_ID_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")
PLAYLIST_ID_RE = re.compile(r"(?:PL|OLAK5uy_|RDCLAK5uy_|UU|FL|LL|SP)[A-Za-z0-9_-]{10,}")
FILENAME_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
MEDIA_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus", ".flac", ".flv")
THUMB_EXTS = (".webp", ".jpg", ".jpeg", ".png")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# --------------------------------------------------------------------------
# quality / container presets
# --------------------------------------------------------------------------

# "original" keeps whatever YouTube actually stored - no height cap, no codec
# preference, so VP9/AV1/Opus survive untouched. Everything else caps height but
# still takes the best available streams at or below it.
QUALITY_PRESETS = {
    "original": "bv*+ba/b",
    "2160": "bv*[height<=?2160]+ba/b[height<=?2160]/bv*+ba/b",
    "1440": "bv*[height<=?1440]+ba/b[height<=?1440]/bv*+ba/b",
    "1080": "bv*[height<=?1080]+ba/b[height<=?1080]/bv*+ba/b",
    "720": "bv*[height<=?720]+ba/b[height<=?720]/bv*+ba/b",
    "480": "bv*[height<=?480]+ba/b[height<=?480]/bv*+ba/b",
    "360": "bv*[height<=?360]+ba/b[height<=?360]/bv*+ba/b",
    "audio": "ba/b",
}

QUALITY_LABELS = {
    "original": "Original (best available)",
    "2160": "4K · 2160p",
    "1440": "1440p",
    "1080": "1080p",
    "720": "720p",
    "480": "480p",
    "360": "360p",
    "audio": "Audio only",
}

# "original" here means: do not force a container. yt-dlp picks one that can
# hold the source streams without re-encoding (usually mkv or webm).
VIDEO_CONTAINERS = ["original", "mp4", "mkv"]
AUDIO_CONTAINERS = ["original", "m4a", "mp3", "opus", "flac"]

DEFAULT_CONFIG = {
    "download_dir": plat.default_download_dir(),
    # Filename only. Each source gets its own folder, named when it is added and
    # kept stable afterwards, so renaming a playlist on YouTube does not scatter
    # its videos across two directories.
    "output_template": "%(upload_date)s - %(title).150B [%(id)s].%(ext)s",
    "default_quality": "original",
    "default_container": "mkv",
    "write_thumbnail": True,
    "embed_thumbnail": True,
    "embed_metadata": True,
    "embed_chapters": True,
    "write_info_json": True,
    "write_subs": True,
    "write_auto_subs": False,
    "embed_subs": True,
    "sub_langs": "en.*",
    "sponsorblock_mark": [],
    "min_duration_seconds": 0,
    "max_duration_seconds": 0,
    "max_downloads_per_run": 0,
    "max_attempts": 5,
    # Seconds between videos. The wait is jittered by +/-30%, because a request
    # arriving on an exact interval is itself a signal of automation.
    "sleep_between_downloads": 3,
    # Seconds yt-dlp waits between its own HTTP requests within one video
    # (metadata, formats, fragments). 0 leaves it unthrottled.
    "sleep_requests": 0,
    # When YouTube starts refusing, every further attempt costs several
    # requests (one per player client) and makes the throttling worse. After
    # this many failures in a row the run stops and waits for the next one.
    # 0 disables the check and grinds through the whole queue regardless.
    "stop_after_consecutive_failures": 8,
    "deep_scan_hours": 12,
    "cookies_from_browser": None,
    "rate_limit": None,
    "concurrent_fragments": 4,
    # YouTube blocks its internal player clients unpredictably, and it varies
    # per video: a client can resolve formats fine yet have its media URLs
    # rejected with 403. Each is tried in turn until one actually transfers.
    # "" is yt-dlp's own default selection, which its maintainers keep pointed at
    # whatever YouTube currently serves. Pinning a specific client goes stale:
    # android_vr was the reliable choice in mid-2026 and only offered 360p by
    # October. Named clients remain as fallbacks for when the default is blocked.
    "player_client": "",
    "player_client_fallbacks": ["android_vr", "web_safari", "ios", "tv"],
    # Rotate ytarchive.log once it passes this size, keeping this many older
    # files. Total disk use is roughly log_max_mb * (log_keep + 1).
    "log_max_mb": 2,
    "log_keep": 5,
    # Run history rows to retain. Older ones are pruned after each run.
    "keep_run_history": 500,
    "web_port": 8800,
    # 127.0.0.1 keeps the interface off the network. A container has to bind
    # 0.0.0.0 to be reachable at all, which is why auth exists - see README.
    "web_host": "127.0.0.1",
    # Keep yt-dlp current automatically. YouTube breaks older builds within
    # weeks, and an unattended archiver otherwise stalls silently.
    # ytdlp_update_hours: how often to check (0 = never).
    # ytdlp_update_command: left null, the method is worked out from how yt-dlp
    #   was installed (Homebrew, winget, pip, standalone). Set it to override,
    #   e.g. ["brew", "upgrade", "yt-dlp"].
    # ytdlp_channel: "stable", or "nightly" for YouTube fixes days sooner.
    "ytdlp_update_hours": 24,
    "ytdlp_update_command": None,
    "ytdlp_channel": "stable",
    "yt_dlp_path": "yt-dlp",
    "extra_ytdlp_args": [],
}

DEFER_MARKERS = (
    "this live event will begin in", "premieres in", "premiere will begin",
    "this live stream recording is not available", "waiting for the live stream",
    "live event will begin",
)

PERMANENT_MARKERS = (
    "video unavailable", "private video", "this video has been removed",
    "removed by the uploader", "account associated with this video has been terminated",
    "video is not available", "video has been deleted",
)

CLIENT_ISSUE_MARKERS = (
    "403: forbidden", "requested format is not available",
    "unable to download video data", "nsig extraction failed",
    "failed to extract any player response", "unable to extract player",
    "sign in to confirm",
)


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

_quiet = False
_log_limits = None


def log_limits() -> "tuple[int, int]":
    """(max bytes, files to keep), read once per process.

    Resolved lazily because log() runs before any config is loaded, and it must
    never fail just because the config is missing or malformed.
    """
    global _log_limits
    if _log_limits is None:
        max_mb, keep = 2, 5
        try:
            cfg = load_config()
            max_mb = max(1, int(cfg["log_max_mb"]))
            keep = max(0, int(cfg["log_keep"]))
        except (OSError, ValueError, KeyError, SystemExit):
            pass
        _log_limits = (max_mb * 1024 * 1024, keep)
    return _log_limits


def rotate_log(path: Path) -> None:
    """Roll path -> path.1 -> path.2 ... discarding the oldest.

    Suffixes are built by appending to the full name rather than with
    with_suffix(), which would turn "ytarchive.log.1" into "ytarchive.log.1.2".
    """
    max_bytes, keep = log_limits()
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return

    try:
        if keep == 0:
            path.unlink(missing_ok=True)
            return
        oldest = path.parent / f"{path.name}.{keep}"
        oldest.unlink(missing_ok=True)
        for n in range(keep - 1, 0, -1):
            src = path.parent / f"{path.name}.{n}"
            if src.exists():
                src.replace(path.parent / f"{path.name}.{n + 1}")
        path.replace(path.parent / f"{path.name}.1")
    except OSError:
        pass          # a failed rotation must never stop the run


def log(msg: str, *, err: bool = False) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    if not _quiet or err:
        print(line, file=sys.stderr if err else sys.stdout, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        target = LOG_DIR / "ytarchive.log"
        rotate_log(target)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def die(msg: str) -> None:
    log(f"error: {msg}", err=True)
    sys.exit(1)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def human(n) -> str:
    step = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if step < 1024 or unit == "TB":
            return f"{int(step)} B" if unit == "B" else f"{step:.1f} {unit}"
        step /= 1024
    return f"{step:.1f} TB"


def fmt_eta(seconds) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            die(f"{CONFIG_PATH} is not valid JSON: {exc}")
        cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
    return cfg


def save_config(cfg: dict) -> None:
    clean = {k: v for k, v in cfg.items() if k in DEFAULT_CONFIG}
    CONFIG_PATH.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")


def write_default_config() -> None:
    if not CONFIG_PATH.exists():
        save_config(dict(DEFAULT_CONFIG))


def download_root(cfg: dict) -> Path:
    # The environment wins so a container can be pointed at its mount without
    # having to rewrite a config file baked into the image.
    target = os.environ.get("YTARCHIVE_DOWNLOADS") or cfg["download_dir"]
    return Path(os.path.expanduser(target)).resolve()


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL DEFAULT 'channel',
    title          TEXT,
    url            TEXT,
    added_at       TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    last_check     TEXT,
    last_deep_scan TEXT,
    quality        TEXT NOT NULL DEFAULT 'original',
    container      TEXT NOT NULL DEFAULT 'mkv',
    limit_n        INTEGER NOT NULL DEFAULT 0,
    folder         TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    id            TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    title         TEXT,
    published     TEXT,
    status        TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    filepath      TEXT,
    filesize      INTEGER,
    first_seen    TEXT NOT NULL,
    downloaded_at TEXT,
    position      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_source ON videos(source_id);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    found       INTEGER DEFAULT 0,
    downloaded  INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    deferred    INTEGER DEFAULT 0,
    bytes       INTEGER DEFAULT 0
);
"""


def table_names(conn) -> "set[str]":
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def column_names(conn, table: str) -> "set[str]":
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def ensure_column(conn, table: str, name: str, decl: str) -> bool:
    """Add a column to an existing table if it is missing.

    CREATE TABLE IF NOT EXISTS silently skips tables that already exist, so new
    columns have to be added explicitly before the schema script runs - the
    indexes it creates would otherwise reference columns that do not exist yet.
    """
    if name in column_names(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    return True


VIDEOS_TABLE = """
CREATE TABLE videos (
    id            TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    title         TEXT,
    published     TEXT,
    status        TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    filepath      TEXT,
    filesize      INTEGER,
    first_seen    TEXT NOT NULL,
    downloaded_at TEXT,
    position      INTEGER DEFAULT 0
)
"""


def pre_migrate(conn) -> None:
    """Bring a pre-existing videos table up to the current shape.

    The old table declared `channel_id TEXT NOT NULL`. Merely adding a
    `source_id` column leaves that constraint in place, and because inserts use
    INSERT OR IGNORE (for primary-key dedup) every new row would be dropped
    silently rather than raising. The table has to be rebuilt.
    """
    if "videos" not in table_names(conn):
        return
    cols = column_names(conn, "videos")
    if "channel_id" not in cols:
        if ensure_column(conn, "videos", "position", "INTEGER DEFAULT 0"):
            conn.commit()
        return

    source_expr = "COALESCE(source_id, channel_id, '')" if "source_id" in cols \
        else "COALESCE(channel_id, '')"
    position = "position" if "position" in cols else "0"

    conn.executescript(f"""
        PRAGMA foreign_keys=off;
        BEGIN;
        ALTER TABLE videos RENAME TO videos_old;
        {VIDEOS_TABLE};
        INSERT OR IGNORE INTO videos
            (id, source_id, title, published, status, attempts, last_error,
             filepath, filesize, first_seen, downloaded_at, position)
        SELECT id, {source_expr}, title, published, status, attempts, last_error,
               filepath, filesize, first_seen, downloaded_at, {position}
        FROM videos_old;
        DROP TABLE videos_old;
        COMMIT;
        PRAGMA foreign_keys=on;
    """)
    kept = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    log(f"rebuilt the videos table for playlist support ({kept} row(s) kept)")


def post_migrate(conn) -> None:
    """Move rows from the channel-only schema into the new sources table."""
    # Height of the archived copy, and whether it fell short of the requested
    # quality and should be replaced when a better copy can be downloaded.
    ensure_column(conn, "videos", "height", "INTEGER")
    ensure_column(conn, "videos", "needs_upgrade", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "videos", "upgrade_checked", "TEXT")
    ensure_column(conn, "sources", "limit_n", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "sources", "folder", "TEXT")
    conn.commit()
    if "channels" not in table_names(conn):
        return
    if conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0:
        conn.execute(
            "INSERT OR IGNORE INTO sources "
            "(id, kind, title, url, added_at, enabled, last_check, quality, container) "
            "SELECT id, 'channel', title, url, added_at, enabled, last_check, "
            "'original', 'mkv' FROM channels")
        moved = conn.total_changes
        if moved:
            log("migrated existing channels into the new sources table")
    conn.execute("DROP TABLE channels")
    conn.commit()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    pre_migrate(conn)
    conn.executescript(SCHEMA)
    post_migrate(conn)
    return conn


# --------------------------------------------------------------------------
# source resolution
# --------------------------------------------------------------------------


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# Single videos added on their own all belong to one built-in source, whose
# files go straight into the download folder rather than a folder of their own.
SINGLES_ID = "videos"
SINGLES_FOLDER = "."
VIDEO_URL_RE = re.compile(
    r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")


def source_url(kind: str, sid: str) -> str:
    if kind == "playlist":
        return f"https://www.youtube.com/playlist?list={sid}"
    return f"https://www.youtube.com/channel/{sid}/videos"


def resolve_source(cfg: dict, target: str) -> dict:
    """Turn a handle / URL / ID into {id, kind, title, url}.

    Playlists win over channels when a URL carries both (a watch URL with a
    `list=` parameter is almost always someone sharing the playlist).
    """
    target = target.strip()

    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", target)
    if m:
        pid = m.group(1)
        return {"id": pid, "kind": "playlist", "url": source_url("playlist", pid),
                "title": playlist_title(cfg, pid) or pid}

    if PLAYLIST_ID_RE.fullmatch(target) and not CHANNEL_ID_RE.fullmatch(target):
        return {"id": target, "kind": "playlist", "url": source_url("playlist", target),
                "title": playlist_title(cfg, target) or target}

    m = VIDEO_URL_RE.search(target)
    if m:
        vid = m.group(1)
        url = f"https://www.youtube.com/watch?v={vid}"
        return {"id": vid, "kind": "video", "url": url,
                "title": ytdlp_field(cfg, url, "%(title)s") or vid}

    cid = None
    if CHANNEL_ID_RE.fullmatch(target):
        cid = target
    else:
        m = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", target)
        cid = m.group(1) if m else None

    if cid:
        return {"id": cid, "kind": "channel", "url": source_url("channel", cid),
                "title": channel_title_from_feed(cid) or cid}

    if target.startswith(("http://", "https://")):
        page = target
    elif target.startswith("@"):
        page = f"https://www.youtube.com/{target}"
    else:
        page = f"https://www.youtube.com/@{target}"

    html = ""
    try:
        html = http_get(page).decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        pass

    title = None
    if html:
        for pattern in (
            r'<meta itemprop="identifier" content="(UC[A-Za-z0-9_-]{22})"',
            r'"channelId":"(UC[A-Za-z0-9_-]{22})"',
            r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"',
            r'"externalId":"(UC[A-Za-z0-9_-]{22})"',
        ):
            m = re.search(pattern, html)
            if m:
                cid = m.group(1)
                break
        m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        if m:
            title = m.group(1)

    if not cid:
        cid = ytdlp_field(cfg, page, "%(channel_id)s")

    if not cid:
        raise ValueError(f"could not work out what {target!r} points to")

    return {"id": cid, "kind": "channel", "url": source_url("channel", cid),
            "title": title or channel_title_from_feed(cid) or cid}


def ytdlp_field(cfg: dict, url: str, template: str) -> "str | None":
    exe = shutil.which(cfg["yt_dlp_path"])
    if not exe:
        return None
    cmd = [exe, "--skip-download", "--playlist-items", "0", "--ignore-no-formats-error",
           "--flat-playlist", "--print", template, url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                             **plat.no_window_kwargs()).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    for line in out.splitlines():
        line = line.strip()
        if line and line != "NA":
            return line
    return None


def playlist_title(cfg: dict, pid: str) -> "str | None":
    return ytdlp_field(cfg, source_url("playlist", pid), "%(playlist_title,title)s")


def channel_title_from_feed(cid: str) -> "str | None":
    try:
        root = ET.fromstring(http_get(CHANNEL_FEED.format(cid), timeout=20))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ET.ParseError):
        return None
    node = root.find(f"{ATOM}title")
    return node.text if node is not None else None


# --------------------------------------------------------------------------
# listing videos in a source
# --------------------------------------------------------------------------


def fetch_channel_feed(cid: str) -> "tuple[str | None, list[dict]]":
    """Cheap check for a channel's latest ~15 uploads via its public RSS feed."""
    root = ET.fromstring(http_get(CHANNEL_FEED.format(cid), timeout=30))
    node = root.find(f"{ATOM}title")
    title = node.text if node is not None else None

    entries = []
    for entry in root.findall(f"{ATOM}entry"):
        vid = entry.find(f"{YT}videoId")
        if vid is None or not vid.text:
            continue
        t = entry.find(f"{ATOM}title")
        p = entry.find(f"{ATOM}published")
        entries.append({
            "id": vid.text,
            "title": (t.text if t is not None else "") or "",
            "published": (p.text if p is not None else "") or "",
        })
    return title, entries


def enumerate_source(cfg: dict, kind: str, sid: str,
                     limit: int = 0) -> "tuple[str | None, list[dict]]":
    """Full listing of a source via yt-dlp. Returns (title, entries) oldest-last.

    This is what makes "the whole playlist/channel" work: RSS only exposes the
    most recent handful of uploads.
    """
    exe = shutil.which(cfg["yt_dlp_path"])
    if not exe:
        die("yt-dlp not found on PATH")
    cmd = [exe, "--flat-playlist", "--ignore-errors", "--no-warnings",
           "--print", "%(id)s\t%(title)s\t%(playlist_title,channel)s"]
    if limit:
        cmd += ["--playlist-end", str(limit)]
    if cfg["sleep_requests"]:
        cmd += ["--sleep-requests", str(cfg["sleep_requests"])]
    cmd.append(source_url(kind, sid))

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                              **plat.no_window_kwargs())
    except subprocess.TimeoutExpired:
        log(f"  timed out listing {sid}", err=True)
        return None, []

    title, entries = None, []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        vid = parts[0].strip()
        if len(vid) != 11 or CHANNEL_ID_RE.fullmatch(vid):
            continue
        if len(parts) >= 3 and parts[2].strip() not in ("", "NA") and not title:
            # yt-dlp names a channel's uploads tab "<channel> - Videos".
            title = re.sub(r"\s*[-–]\s*Videos$", "", parts[2].strip())
        entries.append({"id": vid, "title": parts[1].strip(), "published": ""})
    return title, entries


def record_videos(conn, sid: str, entries: "list[dict]", status: str,
                  base_position: int = 0) -> int:
    """Insert entries not already known. Returns count of new rows.

    Entries arrive newest-first, so they are inserted in reverse: rowid order
    then matches chronological order for anything lacking a published date.
    """
    added, ts = 0, now()
    total = len(entries)
    for offset, e in enumerate(reversed(entries)):
        cur = conn.execute(
            "INSERT OR IGNORE INTO videos "
            "(id, source_id, title, published, status, first_seen, position) "
            "VALUES (?,?,?,?,?,?,?)",
            (e["id"], sid, e["title"], e["published"] or None, status, ts,
             base_position + total - offset),
        )
        added += cur.rowcount
    conn.commit()
    return added


# --------------------------------------------------------------------------
# adding / managing sources
# --------------------------------------------------------------------------


def add_source(cfg: dict, conn, target: str, quality: str, container: str,
               limit: int = 0, only_new: bool = False) -> dict:
    """Subscribe to a channel or playlist and queue its contents.

    Returns a summary dict; raises ValueError if the target cannot be resolved.
    """
    info = resolve_source(cfg, target)
    if info["kind"] == "video":
        return add_single_video(cfg, conn, info, quality, container)
    sid, kind = info["id"], info["kind"]

    existing = conn.execute("SELECT id FROM sources WHERE id=?", (sid,)).fetchone()
    conn.execute(
        "INSERT INTO sources (id, kind, title, url, added_at, enabled, quality, "
        "container, limit_n, folder) VALUES (?,?,?,?,?,1,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET title=excluded.title, enabled=1, "
        "quality=excluded.quality, container=excluded.container",
        (sid, kind, info["title"], info["url"], now(), quality, container,
         limit, safe_folder(info["title"])),
    )
    conn.commit()

    if existing:
        return dict(info, added=False, queued=0,
                    message=f"already tracked - settings updated to "
                            f"{QUALITY_LABELS.get(quality, quality)} / {container}")

    title, entries = enumerate_source(cfg, kind, sid, limit)
    if title:
        # Name the folder from the real title now that it is known, so videos
        # land somewhere recognisable rather than under a bare ID.
        conn.execute("UPDATE sources SET title=?, folder=? WHERE id=?",
                     (title, safe_folder(title), sid))
    conn.execute("UPDATE sources SET last_deep_scan=? WHERE id=?", (now(), sid))
    conn.commit()

    # Adopt anything the folder says is already downloaded, before queueing.
    # This is what makes removing and re-adding a source cheap, and what lets an
    # archive folder moved from another machine be recognised on sight.
    folder = safe_folder(title or info["title"])
    adopted = adopt_existing(conn, cfg, sid, folder,
                             {e["id"]: e["title"] for e in entries})

    if only_new:
        # Watch for future additions without pulling the back catalogue.
        record_videos(conn, sid, entries, "skipped")
        queued = 0
    else:
        queued = record_videos(conn, sid, entries, "pending")

    note = f" ({adopted} already on disk)" if adopted else ""
    return dict(info, title=title or info["title"], added=True, queued=queued,
                total=len(entries), adopted=adopted,
                message=f"queued {queued} of {len(entries)} video(s){note}")


def add_single_video(cfg: dict, conn, info: dict, quality: str,
                     container: str) -> dict:
    """Queue one video into the download folder itself, not a source folder."""
    vid = info["id"]
    conn.execute(
        "INSERT INTO sources (id, kind, title, url, added_at, enabled, quality, "
        "container, folder) VALUES (?,'videos','Single videos','',?,1,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET enabled=1, quality=excluded.quality, "
        "container=excluded.container",
        (SINGLES_ID, now(), quality, container, SINGLES_FOLDER))
    conn.commit()
    title = info["title"]
    known = conn.execute(
        "SELECT v.status, s.title FROM videos v LEFT JOIN sources s ON s.id=v.source_id "
        "WHERE v.id=?", (vid,)).fetchone()
    base = dict(info, title=title, kind="video", total=1, adopted=0)
    if known:
        where = known["title"] or "another source"
        return dict(base, added=False, queued=0,
                    message=f"already archived ({where})" if known["status"] == "done"
                    else f"already queued ({where})")
    if adopt_existing(conn, cfg, SINGLES_ID, SINGLES_FOLDER, {vid: title}):
        return dict(base, added=True, queued=0, adopted=1, message="already on disk")
    record_videos(conn, SINGLES_ID, [{"id": vid, "title": title, "published": ""}],
                  "pending")
    return dict(base, added=True, queued=1, message="queued")


def cmd_add(args) -> None:
    cfg = load_config()
    conn = db()
    quality = args.quality or cfg["default_quality"]
    container = args.container or cfg["default_container"]

    if quality not in QUALITY_PRESETS:
        die(f"unknown quality {quality!r}. Choose from: {', '.join(QUALITY_PRESETS)}")

    for target in args.targets:
        try:
            res = add_source(cfg, conn, target, quality, container,
                             limit=args.limit, only_new=args.only_new)
        except (ValueError, urllib.error.URLError, OSError) as exc:
            log(f"skip {target!r}: {exc}", err=True)
            continue
        log(f"{res['kind']}: {res['title']} ({res['id']}) - {res['message']}")


def cmd_import(args) -> None:
    path = Path(os.path.expanduser(args.path))
    if not path.exists():
        die(f"no such file: {path}")

    targets = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if not row:
                continue
            m = CHANNEL_ID_RE.search(row[0].strip())
            if m:
                targets.append(m.group(0))

    if not targets:
        die(f"found no channel IDs in {path} (expected Takeout's subscriptions.csv)")

    log(f"importing {len(targets)} channel(s) from {path.name}")
    args.targets = targets
    cmd_add(args)


def cmd_list(args) -> None:
    conn = db()
    rows = conn.execute(
        "SELECT s.*, "
        " (SELECT COUNT(*) FROM videos v WHERE v.source_id=s.id AND v.status='done') done, "
        " (SELECT COUNT(*) FROM videos v WHERE v.source_id=s.id "
        "  AND v.status IN ('pending','deferred')) pending "
        "FROM sources s ORDER BY s.kind, s.title COLLATE NOCASE").fetchall()
    if not rows:
        print("Nothing tracked yet.  ./ytarchive.py add <playlist-or-channel-url>")
        return
    print(f"{'TITLE':<38} {'KIND':<9} {'QUALITY':<9} {'FMT':<9} {'DONE':>5} {'LEFT':>5}")
    for r in rows:
        mark = "" if r["enabled"] else "  (paused)"
        print(f"{(r['title'] or '')[:36]:<38} {r['kind']:<9} {r['quality']:<9} "
              f"{r['container']:<9} {r['done']:>5} {r['pending']:>5}{mark}")


def cmd_remove(args) -> None:
    conn = db()
    for target in args.targets:
        row = conn.execute(
            "SELECT id, title FROM sources WHERE id=? OR title=? COLLATE NOCASE",
            (target, target)).fetchone()
        if not row:
            log(f"not tracked: {target}")
            continue
        if args.purge_state:
            conn.execute("DELETE FROM videos WHERE source_id=?", (row["id"],))
        conn.execute("DELETE FROM sources WHERE id=?", (row["id"],))
        conn.commit()
        log(f"removed: {row['title']} ({row['id']})")


def cmd_pause(args) -> None:
    conn = db()
    flag = 0 if args.paused else 1
    for target in args.targets:
        cur = conn.execute(
            "UPDATE sources SET enabled=? WHERE id=? OR title=? COLLATE NOCASE",
            (flag, target, target))
        conn.commit()
        verb = "paused" if args.paused else "resumed"
        log(f"{verb}: {target}" if cur.rowcount else f"not tracked: {target}")


def cmd_quality(args) -> None:
    """Change quality/container for an existing source."""
    if args.quality and args.quality not in QUALITY_PRESETS:
        die(f"unknown quality {args.quality!r}. Choose: {', '.join(QUALITY_PRESETS)}")
    conn = db()
    row = conn.execute("SELECT * FROM sources WHERE id=? OR title=? COLLATE NOCASE",
                       (args.target, args.target)).fetchone()
    if not row:
        die(f"not tracked: {args.target}")
    quality = args.quality or row["quality"]
    container = args.container or row["container"]
    conn.execute("UPDATE sources SET quality=?, container=? WHERE id=?",
                 (quality, container, row["id"]))
    conn.commit()
    log(f"{row['title']}: now {QUALITY_LABELS.get(quality, quality)} / {container}")
    if args.redownload:
        queued, removed, freed = requeue_source(conn, row["id"])
        log(f"  deleted {removed} existing file(s), freeing {human(freed)}")
        log(f"  requeued {queued} video(s) for re-download at the new setting")


def requeue_source(conn, source_id: str) -> "tuple[int, int, int]":
    """Re-download everything in a source at its current settings.

    The existing files have to go first. A quality change usually keeps the same
    container, so the new download would land on the identical filename - and
    yt-dlp, told never to overwrite, would find it already there and report
    success without fetching anything.

    Returns (requeued, files_removed, bytes_freed).
    """
    rows = conn.execute(
        "SELECT id, filepath, filesize FROM videos WHERE source_id=?",
        (source_id,)).fetchall()

    removed = freed = 0
    for r in rows:
        if not r["filepath"]:
            continue
        if Path(r["filepath"]).exists():
            freed += r["filesize"] or 0
            removed += 1
        discard_file(r["filepath"])

    queued = conn.execute(
        "UPDATE videos SET status='pending', attempts=0, last_error=NULL, "
        "filepath=NULL, filesize=NULL, downloaded_at=NULL WHERE source_id=?",
        (source_id,)).rowcount
    conn.commit()
    archive_remove([r["id"] for r in rows])
    return queued, removed, freed


# --------------------------------------------------------------------------
# dedup archive
# --------------------------------------------------------------------------


MANIFEST_NAME = "ytarchive.json"


def source_dir(cfg: dict, folder: str) -> Path:
    """The directory a source's videos live in.

    An accented letter can be stored as one code point or as a base letter plus
    a combining mark - same text, different bytes. Copying an archive between
    macOS and Linux (notably over SMB) can switch between the two, and a
    byte-exact Linux filesystem then sees a different name. Without this lookup
    the source would get a lookalike twin folder and everything would download
    into it again.
    """
    root = download_root(cfg)
    if folder == SINGLES_FOLDER:
        return root
    exact = root / safe_folder(folder)
    if exact.exists():
        return exact
    want = unicodedata.normalize("NFC", exact.name)
    try:
        for child in root.iterdir():
            if child.is_dir() and unicodedata.normalize("NFC", child.name) == want:
                return child
    except OSError:
        pass
    return exact


def video_id_from_name(name: str) -> "str | None":
    """The video ID in a filename. The last bracketed match wins, since the
    template puts the ID at the end and a title may contain brackets too."""
    found = FILENAME_ID_RE.findall(name)
    return found[-1] if found else None


def media_by_id(folder: Path) -> "dict[str, Path]":
    """Media files in a folder, keyed by the video ID in their name."""
    out = {}
    try:
        for path in folder.iterdir():
            if path.is_file() and path.suffix.lower() in MEDIA_EXTS:
                vid = video_id_from_name(path.name)
                if vid:
                    out[vid] = path
    except OSError:
        pass
    return out


def manifest_path(cfg: dict, folder: "str | None") -> "Path | None":
    if not folder:
        return None
    return source_dir(cfg, folder) / MANIFEST_NAME


def read_manifest(path: "Path | None") -> dict:
    if not path or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def manifest_record(cfg: dict, folder: str, source: dict, video_id: str,
                    title: str, result: dict) -> None:
    """Note a downloaded video in the manifest kept alongside the videos.

    This travels with the folder, so re-adding a removed playlist - or moving
    the archive to another machine - can recognise what is already there
    without depending on the app's own database.
    """
    path = manifest_path(cfg, folder)
    if not path:
        return
    data = read_manifest(path)
    data.setdefault("source", {})
    data["source"].update({k: source.get(k) for k in ("id", "kind", "title", "url")})
    data["updated"] = now()
    videos = data.setdefault("videos", {})
    filepath = result.get("filepath") or ""
    videos[video_id] = {
        "title": title,
        "file": Path(filepath).name if filepath else "",
        "quality": source.get("quality"),
        "size": result.get("filesize") or 0,
        "downloaded_at": now(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log(f"  could not update {MANIFEST_NAME}: {exc}", err=True)


def adopt_existing(conn, cfg: dict, source_id: str, folder: str,
                   listed: "dict[str, str] | None" = None) -> int:
    """Mark videos already present in a source's folder as archived.

    Matching is by video ID - the bracketed suffix on every file - never by
    filename. IDs are plain ASCII, so this survives a copy that re-encodes
    accented letters, and even a renamed title.

    Candidates are the folder's manifest entries plus whatever YouTube lists
    for the source right now, so a folder with a missing or stale manifest is
    still recognised. A manifest entry whose file is gone is not adopted: it
    downloads again rather than being silently counted as archived.
    """
    base = source_dir(cfg, folder)
    on_disk = media_by_id(base)
    if not on_disk:
        return 0
    manifest = read_manifest(base / MANIFEST_NAME).get("videos") or {}
    listed = listed or {}

    adopted, ids = 0, []
    for vid in set(manifest) | set(listed):
        target = on_disk.get(vid)
        if target is None:
            continue
        meta = manifest.get(vid) or {}
        try:
            size = target.stat().st_size
        except OSError:
            size = 0
        conn.execute(
            "INSERT INTO videos (id, source_id, title, status, first_seen, "
            "filepath, filesize, downloaded_at) VALUES (?,?,?,'done',?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status='done', filepath=excluded.filepath, "
            "filesize=excluded.filesize, source_id=excluded.source_id",
            (vid, source_id, meta.get("title") or listed.get(vid) or vid, now(),
             str(target), size, meta.get("downloaded_at") or now()))
        ids.append(vid)
        adopted += 1
    conn.commit()
    archive_add(ids)
    return adopted


def archive_ids() -> "set[str]":
    if not ARCHIVE_PATH.exists():
        return set()
    out = set()
    for line in ARCHIVE_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2:
            out.add(parts[1])
    return out


def archive_add(video_ids) -> int:
    have = archive_ids()
    new = [v for v in video_ids if v and v not in have]
    if new:
        with open(ARCHIVE_PATH, "a", encoding="utf-8") as fh:
            for vid in new:
                fh.write(f"youtube {vid}\n")
    return len(new)


def archive_remove(video_ids) -> int:
    if not ARCHIVE_PATH.exists():
        return 0
    drop = set(video_ids)
    kept, removed = [], 0
    for line in ARCHIVE_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] in drop:
            removed += 1
            continue
        if line.strip():
            kept.append(line)
    ARCHIVE_PATH.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return removed


# --------------------------------------------------------------------------
# progress display + publishing
# --------------------------------------------------------------------------

PROG = "@@YTAPROG@@"
PROGRESS_TEMPLATE = (
    "download:" + PROG + "%(progress.status)s|%(progress.downloaded_bytes)s|"
    "%(progress.total_bytes,progress.total_bytes_estimate)s|%(progress.speed)s|"
    "%(progress.eta)s|%(info.format_id)s"
)

PP_STAGES = (
    ("[Merger]", "merging video + audio"),
    ("[EmbedThumbnail]", "embedding thumbnail"),
    ("[Metadata]", "writing metadata"),
    ("[EmbedSubtitle]", "embedding subtitles"),
    ("[SubtitlesConvertor]", "converting subtitles"),
    ("[ThumbnailsConvertor]", "converting thumbnail"),
    ("[ExtractAudio]", "extracting audio"),
    ("[SponsorBlock]", "fetching SponsorBlock data"),
    ("[ModifyChapters]", "writing chapters"),
    ("[VideoRemuxer]", "remuxing"),
    ("[VideoConvertor]", "converting"),
)


def _num(tok: str):
    tok = tok.strip()
    if not tok or tok in ("NA", "None"):
        return None
    try:
        return float(tok)
    except ValueError:
        return None


def publish_progress(data: "dict | None") -> None:
    """Write current activity where the web UI (a separate process) can read it."""
    try:
        if data is None:
            PROGRESS_PATH.unlink(missing_ok=True)
            return
        data = dict(data, pid=os.getpid(), updated=time.time())
        tmp = PROGRESS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(PROGRESS_PATH)
    except OSError:
        pass


def read_progress() -> "dict | None":
    try:
        data = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - data.get("updated", 0) > 90:
        return None
    pid = data.get("pid")
    if pid and not plat.pid_alive(pid):
        return None
    return data


class ProgressBar:
    """In-place terminal progress bar that also publishes state for the web UI.

    Disables its own drawing when stdout is not a TTY, so scheduled background
    runs write clean logs instead of thousands of lines - but keeps publishing,
    so the UI still shows live progress for launchd-triggered runs.
    """

    FILL, PARTIALS, EMPTY = "█", " ▏▎▍▌▋▊▉", "░"

    def __init__(self, enabled: bool = True, width: int = 24, meta: "dict | None" = None):
        # Windows consoles need VT processing switched on before the in-place
        # redraw escapes mean anything.
        self.enabled = bool(enabled) and sys.stdout.isatty() and plat.enable_ansi()
        self.width = width
        self.meta = meta or {}
        self.dirty = False
        self.formats = []
        self.last_draw = 0.0
        self.last_publish = 0.0
        self.stage = ""
        self.last_speed = None
        self.last_eta = None
        self.percent = 0.0

    def _bar(self, frac: float) -> str:
        frac = min(max(frac, 0.0), 1.0)
        full, rem = divmod(int(round(frac * self.width * 8)), 8)
        full = min(full, self.width)
        out = self.FILL * full
        if full < self.width:
            out += (self.PARTIALS[rem] + self.EMPTY * (self.width - full - 1)) if rem \
                else self.EMPTY * (self.width - full)
        return out[: self.width]

    def _write(self, text: str) -> None:
        cols = shutil.get_terminal_size((100, 24)).columns
        sys.stdout.write("\r" + text[: cols - 1] + "\033[K")
        sys.stdout.flush()
        self.dirty = True

    def update(self, status, done, total, speed, eta, fmt) -> None:
        if fmt and fmt not in self.formats:
            self.formats.append(fmt)
            self.last_speed = self.last_eta = None   # stale for the new stream

        if speed:
            self.last_speed = speed
        if eta:
            self.last_eta = eta
        speed, eta = speed or self.last_speed, eta or self.last_eta

        frac = (done / total) if (done is not None and total) else 0.0
        self.percent = frac * 100
        tick = time.monotonic()

        if tick - self.last_publish >= 0.4:
            self.last_publish = tick
            publish_progress(dict(self.meta, stage="downloading", percent=self.percent,
                                  downloaded=done or 0, total=total or 0,
                                  speed=speed or 0, eta=eta,
                                  part=len(self.formats)))

        if not self.enabled:
            return
        if status == "downloading" and tick - self.last_draw < 0.08:
            return                      # cap redraws at ~12/s
        self.last_draw = tick

        rate = f"{human(int(speed))}/s" if speed else "--"
        part = f"  [part {len(self.formats)}]" if len(self.formats) > 1 else ""
        self._write(
            f"  {self.percent:5.1f}% ▕{self._bar(frac)}▏ "
            f"{human(int(done)) if done is not None else '?'}/"
            f"{human(int(total)) if total else '?'}  {rate}  ETA {fmt_eta(eta)}{part}")

    def set_stage(self, label: str) -> None:
        if label == self.stage:
            return
        self.stage = label
        publish_progress(dict(self.meta, stage=label, percent=100.0))
        if self.enabled:
            self._write(f"  100.0% ▕{self.FILL * self.width}▏  {label}…")

    def clear(self) -> None:
        if self.enabled and self.dirty:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        self.dirty = False


# --------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------


safe_folder = plat.safe_folder


def build_ytdlp_cmd(cfg: dict, video_id: str, printfile: Path, quality: str,
                    container: str, client: "str | None" = None,
                    folder: "str | None" = None,
                    staging: "Path | None" = None) -> "list[str]":
    exe = shutil.which(cfg["yt_dlp_path"]) or cfg["yt_dlp_path"]
    root = download_root(cfg)
    template = cfg["output_template"]
    if folder:
        # The source's own folder decides the directory, so drop any directory
        # component the template carries (older configs kept one there).
        template = PurePosixPath(template).name
        root = source_dir(cfg, folder)
    if staging:
        # An upgrade downloads beside the archive, not into it, so the copy
        # already kept is only replaced once the new one is complete.
        template = PurePosixPath(template).name
        root = Path(staging)
    audio_only = quality == "audio"

    cmd = [
        exe, "--no-playlist", "--no-overwrites", "--continue", "--newline",
        "--no-warnings", "--ignore-config",
        "--retries", "5", "--fragment-retries", "10",
        "--concurrent-fragments", str(cfg["concurrent_fragments"]),
        "-f", QUALITY_PRESETS.get(quality, QUALITY_PRESETS["original"]),
        "-o", str(root / template),
        "--print-to-file", "after_move:filepath", str(printfile),
        # Recorded so the result can be checked against the requested quality:
        # a client that is reachable but only offers a low format must not be
        # allowed to pass off 360p as a successful 1080p download.
        "--print-to-file", "after_move:%(height)s", str(printfile) + ".height",
        # Independent of state.db: yt-dlp itself refuses anything already
        # recorded here, so a lost or rolled-back database cannot re-download.
        *([] if staging else ["--download-archive", str(ARCHIVE_PATH)]),
        "--progress", "--progress-template", PROGRESS_TEMPLATE,
    ]

    if audio_only:
        cmd.append("--extract-audio")
        cmd += ["--audio-quality", "0"]
        if container in AUDIO_CONTAINERS and container != "original":
            cmd += ["--audio-format", container]
    elif container in ("mp4", "mkv"):
        cmd += ["--merge-output-format", container]
    # container == "original": no flag at all, so yt-dlp keeps the source
    # streams in whatever container can hold them without re-encoding.

    if cfg["write_thumbnail"]:
        cmd.append("--write-thumbnail")
    if cfg["embed_thumbnail"]:
        cmd.append("--embed-thumbnail")
    if cfg["embed_metadata"]:
        cmd.append("--embed-metadata")
    if cfg["embed_chapters"] and not audio_only:
        cmd.append("--embed-chapters")
    if cfg["write_info_json"]:
        cmd.append("--write-info-json")
    if not audio_only:
        if cfg["write_subs"]:
            cmd.append("--write-subs")
        if cfg["write_auto_subs"]:
            cmd.append("--write-auto-subs")
        if cfg["embed_subs"]:
            cmd.append("--embed-subs")
        if cfg["write_subs"] or cfg["write_auto_subs"]:
            cmd += ["--sub-langs", cfg["sub_langs"]]
    for category in cfg["sponsorblock_mark"] or []:
        cmd += ["--sponsorblock-mark", category]
    if client:
        cmd += ["--extractor-args", f"youtube:player_client={client}"]
    if cfg["cookies_from_browser"]:
        cmd += ["--cookies-from-browser", cfg["cookies_from_browser"]]
    if cfg["rate_limit"]:
        cmd += ["--limit-rate", str(cfg["rate_limit"])]
    if cfg["sleep_requests"]:
        cmd += ["--sleep-requests", str(cfg["sleep_requests"])]

    filters = []
    if cfg["min_duration_seconds"]:
        filters.append(f"duration >= {int(cfg['min_duration_seconds'])}")
    if cfg["max_duration_seconds"]:
        filters.append(f"duration <= {int(cfg['max_duration_seconds'])}")
    if filters:
        cmd += ["--match-filter", " & ".join(filters)]

    cmd += plat.filename_args()
    cmd += [str(a) for a in cfg["extra_ytdlp_args"]]
    cmd.append(f"https://www.youtube.com/watch?v={video_id}")
    return cmd


def classify_failure(output: str) -> str:
    low = output.lower()
    if any(m in low for m in DEFER_MARKERS):
        return "deferred"
    if any(m in low for m in PERMANENT_MARKERS):
        return "permanent"
    return "retry"


def last_error_line(output: str) -> str:
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if "ERROR" in ln or "error" in ln:
            return ln[:500]
    return lines[-1][:500] if lines else ""


def find_existing_file(cfg: dict, video_id: str) -> "str | None":
    root = download_root(cfg)
    if not root.exists():
        return None
    for path in root.rglob(f"*[[]{video_id}[]]*"):
        if path.suffix.lower() in MEDIA_EXTS and path.is_file():
            return str(path)
    return None


def discard_file(path: "str | None") -> None:
    """Remove a file this run just downloaded and then rejected.

    Only ever called on a path produced by the attempt being discarded, and its
    sidecars (subtitles, thumbnail, metadata) go with it.
    """
    if not path:
        return
    try:
        target = Path(path)
        if not target.exists():
            return
        stem = target.with_suffix("").name
        # Sidecars are always "<stem>.<something>" (.webp, .info.json, .en.vtt).
        # Matching "<stem>*" instead would also sweep up a different video whose
        # title merely begins with this one's full name.
        for sibling in target.parent.glob(f"{glob_escape(stem)}.*"):
            if sibling.is_file():
                sibling.unlink(missing_ok=True)
    except OSError as exc:
        log(f"  could not clean up {path}: {exc}", err=True)


def glob_escape(text: str) -> str:
    """Escape glob metacharacters so titles with [] match literally."""
    return re.sub(r"([\[\]*?])", r"[\1]", text)


def client_chain(cfg: dict) -> "list[str]":
    chain, seen = [], set()
    for c in [cfg["player_client"]] + list(cfg["player_client_fallbacks"] or []):
        c = (c or "").strip()
        if c not in seen:
            seen.add(c)
            chain.append(c)
    return chain


def download_video(cfg: dict, video_id: str, quality: str, container: str,
                   show_progress: bool = True, meta: "dict | None" = None,
                   folder: "str | None" = None) -> dict:
    """Download one video, falling back through player clients as needed.

    YouTube blocks clients unpredictably and per-video, so a single 403 is not
    evidence the video is unavailable. Only once every client has failed the
    same way is this reported as a real failure.
    """
    chain = client_chain(cfg)
    result = {"status": "failed", "error": "no player client configured"}
    target = int(quality) if quality.isdigit() else 0
    idx = 0

    while idx < len(chain):
        client = chain[idx]
        result = attempt_download(cfg, video_id, quality, container, client,
                                  show_progress, meta, folder)

        if result["status"] == "done":
            got = result.get("height") or 0
            if not (target and got and got < target * 0.9):
                if idx > 0:
                    where = f" at {got}p" if got else ""
                    log(f"  succeeded with player client '{client or 'default'}'{where}")
                return result

            # This client is reachable but only serves a low format. Before
            # accepting it, ask the remaining clients what they can actually
            # offer - a metadata check, so nothing is transferred twice unless
            # a better source genuinely exists.
            better = client_offering(cfg, video_id, quality, chain[idx + 1:], target)
            if better is None:
                # Keep it rather than have nothing - often the full-quality
                # client was only briefly blocked. Marked so later runs replace
                # it once a better copy has actually been downloaded.
                log(f"  {got}p was the best any client offered (wanted {target}p) - "
                    f"keeping it and will try to upgrade later.", err=True)
                return dict(result, short=True)

            log(f"  '{client or 'default'}' only offered {got}p; "
                f"'{better or 'default'}' has {target}p - fetching that instead")
            # Discard this copy and its archive entry, or yt-dlp would refuse
            # to fetch the same video again.
            discard_file(result.get("filepath"))
            archive_remove([video_id])
            idx = chain.index(better)
            continue

        if not result.get("client_issue"):
            return result
        if idx + 1 < len(chain):
            log(f"  '{client or 'default'}' failed ({result['error'][:90]}) "
                f"- trying '{chain[idx + 1] or 'default'}'")
        idx += 1

    return result


def client_offering(cfg: dict, video_id: str, quality: str,
                    clients: "list[str]", target: int) -> "str | None":
    """First client that can serve `target` height, or None.

    Metadata only - no media is transferred, so this is cheap enough to run
    when a download comes back below the requested quality.
    """
    exe = shutil.which(cfg["yt_dlp_path"])
    if not exe:
        return None
    for client in clients:
        cmd = [exe, "--ignore-config", "--no-warnings", "--simulate",
               "--no-playlist", "-f", QUALITY_PRESETS.get(quality, "bv*+ba/b"),
               "--print", "%(height)s"]
        if client:
            cmd += ["--extractor-args", f"youtube:player_client={client}"]
        if cfg["cookies_from_browser"]:
            cmd += ["--cookies-from-browser", cfg["cookies_from_browser"]]
        cmd.append(f"https://www.youtube.com/watch?v={video_id}")
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                                 **plat.no_window_kwargs()).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        for line in out.splitlines():
            try:
                if int(line.strip()) >= target * 0.9:
                    return client
            except ValueError:
                continue
    return None


def attempt_download(cfg: dict, video_id: str, quality: str, container: str,
                     client: str, show_progress: bool = True,
                     meta: "dict | None" = None, folder: "str | None" = None,
                     staging: "Path | None" = None) -> dict:
    """One attempt with a specific player client. Streams output so progress is live."""
    printfile = DATA_DIR / f".lastpath.{os.getpid()}"
    printfile.unlink(missing_ok=True)
    cmd = build_ytdlp_cmd(cfg, video_id, printfile, quality, container, client, folder,
                          staging)
    bar = ProgressBar(enabled=show_progress, meta=meta)
    tail: "list[str]" = []
    stopped = False

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, errors="replace",
                                **plat.no_window_kwargs())
    except FileNotFoundError:
        return {"status": "failed", "error": "yt-dlp not found on PATH", "permanent": True}
    except OSError as exc:
        return {"status": "failed", "error": str(exc), "permanent": False}

    try:
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line.startswith(PROG):
                fields = line[len(PROG):].split("|")
                if len(fields) >= 6:
                    bar.update(fields[0].strip(), _num(fields[1]), _num(fields[2]),
                               _num(fields[3]), _num(fields[4]), fields[5].strip())
                if STOP_PATH.exists():
                    stopped = True
                    proc.terminate()
                    break
                continue
            if line.strip():
                tail.append(line)
                if len(tail) > 400:
                    del tail[:100]
            for token, label in PP_STAGES:
                if line.startswith(token):
                    bar.set_stage(label)
                    break
    finally:
        proc.stdout.close()
        proc.wait()
        bar.clear()

    if stopped:
        return {"status": "stopped", "error": "stopped by request"}

    combined = "\n".join(tail)
    low = combined.lower()

    paths = []
    if printfile.exists():
        paths = [ln.strip() for ln in
                 printfile.read_text(encoding="utf-8").splitlines() if ln.strip()]
        printfile.unlink(missing_ok=True)

    if proc.returncode != 0:
        kind = classify_failure(combined)
        err = last_error_line(combined) or f"yt-dlp exited {proc.returncode}"
        if kind == "deferred":
            return {"status": "deferred", "error": err}
        return {"status": "failed", "error": err, "permanent": kind == "permanent",
                # A blocked client is not a broken video: let the caller retry.
                "client_issue": kind != "permanent"
                                and any(m in low for m in CLIENT_ISSUE_MARKERS)}

    if not paths:
        # Exit 0 but nothing moved into place: either yt-dlp already had it
        # (archive hit or file on disk), or a --match-filter rejected it.
        # These must not be conflated - the first is success, the second is not.
        if "recorded in the archive" in low or "has already been downloaded" in low:
            existing = find_existing_file(cfg, video_id)
            size = 0
            if existing:
                try:
                    size = Path(existing).stat().st_size
                except OSError:
                    pass
            return {"status": "done", "filepath": existing, "filesize": size,
                    "already": True}
        if "does not pass filter" in low:
            return {"status": "skipped", "error": "excluded by duration filter"}
        return {"status": "skipped", "error": "nothing downloaded"}

    media = None
    for p in paths:
        if Path(p).suffix.lower() in MEDIA_EXTS:
            media = p
    media = media or paths[-1]
    size = 0
    try:
        size = Path(media).stat().st_size
    except OSError:
        pass

    height = 0
    heightfile = Path(str(printfile) + ".height")
    if heightfile.exists():
        for line in heightfile.read_text(encoding="utf-8").splitlines():
            try:
                height = max(height, int(line.strip()))
            except ValueError:
                continue
        heightfile.unlink(missing_ok=True)

    return {"status": "done", "filepath": media, "filesize": size, "height": height}


# --------------------------------------------------------------------------
# the run loop
# --------------------------------------------------------------------------


YTDLP_VENV = Path("/opt/yt-dlp")


def ytdlp_update_hours(cfg: dict) -> int:
    raw = os.environ.get("YTARCHIVE_YTDLP_UPDATE_HOURS")
    try:
        return int(raw) if raw not in (None, "") else int(cfg["ytdlp_update_hours"] or 0)
    except (TypeError, ValueError):
        return 0


def ytdlp_channel(cfg: dict) -> str:
    return (os.environ.get("YTARCHIVE_YTDLP_CHANNEL") or cfg["ytdlp_channel"]
            or "stable").strip().lower()


def ytdlp_update_plan(cfg: dict) -> "tuple[list[list[str]], Path, str] | None":
    """(commands, stamp file, method) for refreshing yt-dlp, or None.

    Order of preference:
    - ytdlp_update_command from config.json, if set - one command, or a list.
    - The Docker image's own virtualenv, which the unprivileged app user owns
      so it can be upgraded while the container keeps running.
    - Otherwise whatever matches how yt-dlp was installed on this machine:
      Homebrew, winget, pip, or a standalone build that updates itself.
    """
    custom = cfg.get("ytdlp_update_command")
    if custom:
        cmds = custom if isinstance(custom[0], list) else [custom]
        return ([[str(c) for c in cmd] for cmd in cmds],
                DATA_DIR / ".ytdlp-update.json", "config.json")
    pip = YTDLP_VENV / "bin" / "pip"
    if pip.exists():
        cmd = [str(pip), "install", "--upgrade", "--no-cache-dir",
               "--disable-pip-version-check", "--quiet"]
        if ytdlp_channel(cfg) == "nightly":
            cmd.append("--pre")
        # The stamp lives with the installation it describes: replacing the
        # container resets both together, so a fresh container always checks.
        return ([cmd + ["yt-dlp[default,deno]"]],
                YTDLP_VENV / ".ytarchive-update.json", "container")
    exe = shutil.which(cfg["yt_dlp_path"], path=plat.with_tool_path()["PATH"])
    if not exe:
        return None
    return (plat.ytdlp_update_commands(exe, ytdlp_channel(cfg)),
            DATA_DIR / ".ytdlp-update.json", plat.ytdlp_install_kind(exe))


def ytdlp_version(cfg: dict) -> "str | None":
    exe = shutil.which(cfg["yt_dlp_path"], path=plat.with_tool_path()["PATH"])
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=60, **plat.no_window_kwargs()).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def js_runtime() -> "str | None":
    """Path to deno, which yt-dlp needs to answer YouTube's JavaScript checks."""
    return shutil.which("deno", path=plat.with_tool_path()["PATH"])


def ytdlp_update_info(cfg: dict) -> dict:
    plan = ytdlp_update_plan(cfg)
    stamp = {}
    if plan and plan[1].exists():
        try:
            stamp = json.loads(plan[1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            stamp = {}
    return {"configured": bool(plan), "hours": ytdlp_update_hours(cfg),
            "channel": ytdlp_channel(cfg), "method": plan[2] if plan else None,
            "checked_at": stamp.get("checked_at"), "version": stamp.get("version")}


def update_ytdlp(cfg: dict, force: bool = False) -> bool:
    """Upgrade yt-dlp if it is due (or forced). Returns True if a check ran.

    Called with the run lock held, so files are never swapped out from under an
    active download. A failed update is logged and left for the next run; the
    installed version keeps working in the meantime.
    """
    plan = ytdlp_update_plan(cfg)
    if not plan:
        if force:
            log("yt-dlp not found, so there is nothing to update. Install: "
                f"{plat.install_hint('yt-dlp')}", err=True)
        return False
    cmds, stamp, method = plan
    hours = ytdlp_update_hours(cfg)
    if not force:
        if hours <= 0:
            return False
        last = ytdlp_update_info(cfg).get("checked_at")
        if last:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
                if age < timedelta(hours=hours):
                    return False
            except ValueError:
                pass

    before = ytdlp_version(cfg)
    log(f"checking for a yt-dlp update via {method} (have {before or 'none'})")
    publish_progress({"stage": "updating", "title": "Updating yt-dlp"})
    ok, detail = False, ""
    try:
        for cmd in cmds:
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                                     env=plat.with_tool_path(), errors="replace",
                                     **plat.no_window_kwargs())
            except (OSError, subprocess.SubprocessError) as exc:
                ok, detail = False, str(exc)
                continue
            output = (res.stdout or "") + (res.stderr or "")
            current = any(m in output.lower() for m in plat.ALREADY_CURRENT)
            ok = res.returncode == 0 or current
            lines = [ln for ln in output.strip().splitlines() if ln.strip()]
            detail = lines[-1].strip() if lines else f"exit code {res.returncode}"
    finally:
        publish_progress(None)

    if not ok:
        # e.g. offline, or a package manager refusing - the next run retries.
        log(f"  yt-dlp update failed: {detail}", err=True)
        return True

    after = ytdlp_version(cfg)
    try:
        stamp.write_text(json.dumps({"checked_at": now(), "version": after,
                                     "previous": before, "method": method,
                                     "channel": ytdlp_channel(cfg)}), encoding="utf-8")
    except OSError as exc:
        log(f"  could not record the update time: {exc}", err=True)
    if before and after and before != after:
        log(f"  yt-dlp updated {before} -> {after}")
    else:
        log(f"  yt-dlp {after} is current")
    return True


def cmd_update_ytdlp(args) -> None:
    cfg = load_config()
    if ytdlp_update_plan(cfg) is None:
        die(f"yt-dlp not found. Install: {plat.install_hint('yt-dlp')}")
    lock = acquire_lock()
    if lock is None:
        die("a run is in progress; try again when it finishes")
    try:
        update_ytdlp(cfg, force=True)
    finally:
        lock.release()


UPGRADE_DIR = DATA_DIR / ".upgrade"


def upgrade_short_copies(conn, cfg: dict, budget: int, show_progress: bool = True) -> int:
    """Try to replace archived copies that fell short of the requested quality.

    The kept copy is only deleted after a better one has fully downloaded, so a
    failed attempt never loses anything. Each copy is tried at most once a day:
    a cheap metadata check first, and a download only when a client can serve
    more. Returns the number of copies upgraded.
    """
    if budget <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT v.*, s.quality, s.container, s.folder AS source_folder, "
        "s.title AS source_title FROM videos v JOIN sources s ON s.id = v.source_id "
        "WHERE v.status='done' AND v.needs_upgrade=1 AND s.enabled=1 "
        "AND (v.upgrade_checked IS NULL OR v.upgrade_checked < ?) "
        "ORDER BY v.upgrade_checked IS NOT NULL, v.upgrade_checked LIMIT ?",
        (cutoff, budget)).fetchall()

    upgraded = 0
    for v in rows:
        quality = v["quality"]
        target = int(quality) if quality.isdigit() else 0
        old_height = v["height"] or 0
        if not target or old_height >= target * 0.9:
            conn.execute("UPDATE videos SET needs_upgrade=0 WHERE id=?", (v["id"],))
            conn.commit()
            continue
        conn.execute("UPDATE videos SET upgrade_checked=? WHERE id=?", (now(), v["id"]))
        conn.commit()

        client = client_offering(cfg, v["id"], quality, client_chain(cfg), target)
        if client is None:
            continue

        staging = UPGRADE_DIR / v["id"]
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            log(f"upgrading {(v['title'] or v['id'])[:60]} from {old_height}p")
            res = attempt_download(cfg, v["id"], quality, v["container"], client,
                                   show_progress, {"video_id": v["id"],
                                                   "title": v["title"] or v["id"],
                                                   "source": v["source_title"],
                                                   "quality": quality},
                                   staging=staging)
            got = res.get("height") or 0
            if res.get("status") != "done" or not res.get("filepath") or got <= old_height:
                log(f"  no better copy this time ({res.get('error') or f'{got}p'}); "
                    f"keeping the {old_height}p one")
                continue

            # The new copy is complete: only now remove the old one and its
            # sidecars, then move the new files into the archive folder.
            dest = source_dir(cfg, v["source_folder"] or v["source_title"])
            dest.mkdir(parents=True, exist_ok=True)
            if v["filepath"]:
                discard_file(v["filepath"])
            for item in staging.iterdir():
                shutil.move(str(item), str(dest / item.name))
            new_path = dest / Path(res["filepath"]).name
            conn.execute(
                "UPDATE videos SET filepath=?, filesize=?, height=?, needs_upgrade=?, "
                "downloaded_at=? WHERE id=?",
                (str(new_path), new_path.stat().st_size, got,
                 int(got < target * 0.9), now(), v["id"]))
            conn.commit()
            archive_add([v["id"]])
            manifest_record(cfg, v["source_folder"] or v["source_title"],
                            {"id": v["source_id"], "kind": "", "title": v["source_title"],
                             "url": "", "quality": quality},
                            v["id"], v["title"] or v["id"],
                            dict(res, filepath=str(new_path)))
            log(f"  upgraded {old_height}p -> {got}p, removed the old copy")
            upgraded += 1
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return upgraded


def acquire_lock():
    lock = plat.RunLock(LOCK_PATH)
    return lock if lock.acquire() else None


def is_running() -> bool:
    """True if another run currently holds the lock."""
    return plat.RunLock.is_held(LOCK_PATH)


def needs_deep_scan(row, hours: int) -> bool:
    if row["kind"] == "playlist":
        return True          # playlist RSS is unreliable; always enumerate
    if not row["last_deep_scan"]:
        return True
    try:
        last = datetime.fromisoformat(row["last_deep_scan"])
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last >= timedelta(hours=hours)


def cmd_run(args) -> None:
    cfg = load_config()
    lock = acquire_lock()
    if lock is None:
        log("another run is already in progress; exiting")
        return
    STOP_PATH.unlink(missing_ok=True)
    if sys.stdout.isatty():
        warn_if_schedule_stale()

    conn = None
    try:
        if not args.dry_run:
            update_ytdlp(cfg)

        if not shutil.which(cfg["yt_dlp_path"]):
            die(f"yt-dlp not found (looked for {cfg['yt_dlp_path']!r}). "
                f"Install: {plat.install_hint('yt-dlp')}")

        root = download_root(cfg)
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".write-test"
            probe.touch()
            probe.unlink()
        except OSError as exc:
            die(f"download dir {root} is not writable: {exc}")

        conn = db()
        run_id = conn.execute("INSERT INTO runs (started_at) VALUES (?)",
                              (now(),)).lastrowid
        conn.commit()

        sources = conn.execute(
            "SELECT * FROM sources WHERE enabled=1 ORDER BY title COLLATE NOCASE").fetchall()
        if not sources:
            log("nothing tracked - add a playlist or channel first")
            return

        # 1. refresh every source
        found = 0
        for n, src in enumerate(sources):
            if STOP_PATH.exists():
                break
            if src["kind"] == "videos":
                continue        # single videos have nothing to re-check
            # Every source listing is a burst of requests to YouTube. With many
            # playlists and channels, checking them back to back is exactly the
            # pattern that gets an IP bot-checked, so space them out when
            # throttling is on.
            if n and cfg["sleep_requests"]:
                time.sleep(cfg["sleep_requests"] * random.uniform(2, 4))
            deep = args.deep or needs_deep_scan(src, cfg["deep_scan_hours"])
            publish_progress({"stage": "checking", "title": src["title"] or src["id"],
                              "source": src["title"]})
            try:
                if deep:
                    # Honour the limit chosen when the source was added, or a
                    # re-check would quietly pull in the whole back catalogue.
                    title, entries = enumerate_source(cfg, src["kind"], src["id"],
                                                      src["limit_n"] or 0)
                    conn.execute("UPDATE sources SET last_deep_scan=? WHERE id=?",
                                 (now(), src["id"]))
                else:
                    title, entries = fetch_channel_feed(src["id"])
            except (urllib.error.HTTPError, urllib.error.URLError, ET.ParseError,
                    OSError, subprocess.SubprocessError) as exc:
                log(f"could not refresh {src['title']}: {exc}", err=True)
                continue

            # Apply the source's limit to whichever path produced the entries -
            # the RSS feed returns its own fixed number regardless of what was
            # asked for, so clamping only inside the deep scan is not enough.
            if src["limit_n"]:
                entries = entries[: src["limit_n"]]

            new = record_videos(conn, src["id"], entries, "pending")
            conn.execute("UPDATE sources SET last_check=?, title=COALESCE(?, title) "
                         "WHERE id=?", (now(), title, src["id"]))
            conn.commit()
            found += new
            if new:
                log(f"{new} new in {title or src['title']}"
                    f"{' (full scan)' if deep else ''}")

        log(f"checked {len(sources)} source(s), {found} new video(s)")

        # 2. work the queue, oldest first, using each source's own settings
        limit = args.limit if args.limit is not None else cfg["max_downloads_per_run"]
        # 'failed' belongs here: a failure is usually YouTube rate-limiting or a
        # blocked client, both of which clear on their own, so every later run
        # should try again. Videos that can never succeed are marked 'dead'
        # instead and stay out. max_attempts of 0 means keep trying forever.
        sql = ("SELECT v.*, s.quality, s.container, s.title AS source_title, "
               "s.folder AS source_folder "
               "FROM videos v JOIN sources s ON s.id = v.source_id "
               "WHERE v.status IN ('pending','deferred','failed') "
               "AND (? = 0 OR v.attempts < ?) AND s.enabled = 1 "
               "ORDER BY v.status='failed', "        # fresh work before retries
               "v.published IS NULL, v.published ASC, v.position ASC, v.rowid ASC")
        params = [cfg["max_attempts"], cfg["max_attempts"]]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        queue = conn.execute(sql, params).fetchall()

        if not queue:
            log("queue empty")

        downloaded = failed = deferred = 0
        total_bytes = 0
        streak = 0
        give_up_after = cfg["stop_after_consecutive_failures"]
        for i, v in enumerate(queue):
            if STOP_PATH.exists():
                log("stop requested - halting")
                break
            label = (v["title"] or v["id"])[:70]
            if args.dry_run:
                log(f"would download: {label} [{v['id']}] "
                    f"({v['quality']}/{v['container']})")
                continue

            log(f"downloading ({i + 1}/{len(queue)}): {label}")
            meta = {"video_id": v["id"], "title": v["title"] or v["id"],
                    "source": v["source_title"], "index": i + 1,
                    "queue_len": len(queue), "quality": v["quality"]}
            res = download_video(cfg, v["id"], v["quality"], v["container"],
                                 show_progress=not args.no_progress, meta=meta,
                                 folder=v["source_folder"] or v["source_title"])
            status = res["status"]

            if status == "stopped":
                log("stop requested - halting")
                break

            streak = streak + 1 if status == "failed" else 0
            if status == "done":
                conn.execute(
                    "UPDATE videos SET status='done', filepath=?, filesize=?, "
                    "downloaded_at=?, last_error=NULL, height=?, needs_upgrade=? WHERE id=?",
                    (res.get("filepath"), res.get("filesize", 0), now(),
                     res.get("height") or None, int(bool(res.get("short"))), v["id"]))
                archive_add([v["id"]])
                manifest_record(cfg, v["source_folder"] or v["source_title"],
                                {"id": v["source_id"], "kind": "",
                                 "title": v["source_title"], "url": "",
                                 "quality": v["quality"]},
                                v["id"], v["title"] or v["id"], res)
                if res.get("already"):
                    log("  already on disk - marked archived, not re-downloaded")
                else:
                    downloaded += 1
                    total_bytes += res.get("filesize", 0) or 0
                    log(f"  saved {human(res.get('filesize', 0))} -> {res.get('filepath')}")
            elif status == "skipped":
                conn.execute("UPDATE videos SET status='skipped', last_error=? WHERE id=?",
                             (res.get("error"), v["id"]))
                log(f"  skipped: {res.get('error')}")
            elif status == "deferred":
                conn.execute("UPDATE videos SET status='deferred', last_error=? WHERE id=?",
                             (res.get("error"), v["id"]))
                deferred += 1
                log("  not out yet - will retry next run")
            else:
                attempts = v["attempts"] + 1
                if res.get("permanent"):
                    # Deleted, private or terminated: retrying cannot help, so
                    # it is parked rather than retried on every future run.
                    conn.execute("UPDATE videos SET status='dead', attempts=?, "
                                 "last_error=? WHERE id=?",
                                 (attempts, res.get("error"), v["id"]))
                    log(f"  unavailable, will not retry: {res.get('error')}", err=True)
                else:
                    conn.execute("UPDATE videos SET status='failed', attempts=?, "
                                 "last_error=? WHERE id=?",
                                 (attempts, res.get("error"), v["id"]))
                    cap = cfg["max_attempts"]
                    tail = (f"attempt {attempts}, will retry" if not cap or attempts < cap
                            else "giving up")
                    log(f"  failed ({tail}): {res.get('error')}", err=True)
                failed += 1
            conn.commit()

            if give_up_after and streak >= give_up_after:
                log(f"{streak} failures in a row - stopping this run so the rate "
                    f"limit can clear. The remaining {len(queue) - i - 1} stay "
                    f"queued for the next check.", err=True)
                break

            base = cfg["sleep_between_downloads"]
            if i + 1 < len(queue) and base:
                # Jitter the gap so requests do not land on an exact cadence.
                pause = base * random.uniform(0.7, 1.3)
                log(f"  waiting {pause:.0f}s before the next one")
                time.sleep(pause)

        if not args.dry_run and not STOP_PATH.exists() \
                and not (give_up_after and streak >= give_up_after):
            budget = (limit - len(queue)) if limit else 3
            downloaded += upgrade_short_copies(conn, cfg, budget, not args.no_progress)

        conn.execute("UPDATE runs SET finished_at=?, found=?, downloaded=?, failed=?, "
                     "deferred=?, bytes=? WHERE id=?",
                     (now(), found, downloaded, failed, deferred, total_bytes, run_id))
        # Trim run history, or an hourly schedule accumulates rows forever.
        keep = cfg["keep_run_history"]
        if keep:
            conn.execute("DELETE FROM runs WHERE id NOT IN "
                         "(SELECT id FROM runs ORDER BY id DESC LIMIT ?)", (keep,))
        conn.commit()

        remaining = conn.execute(
            "SELECT COUNT(*) c FROM videos v JOIN sources s ON s.id=v.source_id "
            "WHERE v.status IN ('pending','deferred','failed') "
            "AND (? = 0 OR v.attempts < ?) AND s.enabled=1",
            (cfg["max_attempts"], cfg["max_attempts"])).fetchone()["c"]
        log(f"done: {downloaded} downloaded ({human(total_bytes)}), {failed} failed, "
            f"{deferred} deferred, {remaining} still queued")
    finally:
        # Windows keeps an open database file locked; close it explicitly.
        if conn is not None:
            conn.close()
        publish_progress(None)
        STOP_PATH.unlink(missing_ok=True)
        if lock is not None:
            lock.release()


def cmd_stop(args) -> None:
    if not is_running():
        log("no run in progress")
        return
    STOP_PATH.touch()
    log("stop requested - the current download will be halted")


def cmd_status(args) -> None:
    cfg = load_config()
    conn = db()
    counts = {r["status"]: r["n"] for r in
              conn.execute("SELECT status, COUNT(*) n FROM videos GROUP BY status")}
    nsrc = conn.execute(
        "SELECT COUNT(*) c, SUM(kind='playlist') p FROM sources WHERE enabled=1").fetchone()
    size = conn.execute(
        "SELECT COALESCE(SUM(filesize),0) s FROM videos WHERE status='done'").fetchone()["s"]
    last = conn.execute(
        "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()

    print(f"Version         : {__version__}")
    print(f"Tracking        : {nsrc['c']} source(s) "
          f"({nsrc['p'] or 0} playlist, {nsrc['c'] - (nsrc['p'] or 0)} channel)")
    print(f"Archived        : {counts.get('done', 0)} videos, {human(size)}")
    print(f"Queued          : {counts.get('pending', 0)} pending, "
          f"{counts.get('deferred', 0)} awaiting release")
    print(f"Failed          : {counts.get('failed', 0)} (will retry)")
    print(f"Unavailable     : {counts.get('dead', 0)} (deleted/private)")
    print(f"Download dir    : {download_root(cfg)}")
    print(f"Last run        : {last['finished_at'] if last else 'never'}")
    problem = schedule_problem()
    state = "not installed"
    if schedule_is_installed():
        state = "installed" if not problem else f"BROKEN - it {problem}"
    print(f"Recurring check : {state}")
    print(f"Currently       : {'running' if is_running() else 'idle'}")
    info = ytdlp_update_info(cfg)
    auto = (f"every {info['hours']}h via {info['method']}"
            if info["configured"] and info["hours"] > 0 else "off")
    checked = info["checked_at"] or "never"
    print(f"yt-dlp          : {ytdlp_version(cfg) or 'NOT FOUND'} "
          f"(auto-update {auto}; last checked {checked})")
    deno = js_runtime()
    print(f"JS runtime      : {'deno' if deno else 'NOT FOUND - install: ' + plat.deno_hint()}")

    if args.failures:
        rows = conn.execute(
            "SELECT id, title, attempts, last_error FROM videos WHERE status='failed' "
            "ORDER BY rowid DESC LIMIT 25").fetchall()
        if rows:
            print("\nRecent failures:")
            for r in rows:
                print(f"  [{r['id']}] {(r['title'] or '')[:60]}")
                print(f"      attempts={r['attempts']}  {r['last_error']}")


def cmd_retry(args) -> None:
    conn = db()
    if args.video_ids:
        marks = ",".join("?" * len(args.video_ids))
        rows = conn.execute(f"SELECT id FROM videos WHERE id IN ({marks})",
                            args.video_ids).fetchall()
        cur = conn.execute(f"UPDATE videos SET status='pending', attempts=0, "
                           f"last_error=NULL WHERE id IN ({marks})", args.video_ids)
    else:
        # 'dead' videos (deleted, private, terminated) are only revived when
        # asked for explicitly, since retrying them cannot normally help.
        statuses = "('failed','dead')" if args.include_dead else "('failed')"
        rows = conn.execute(
            f"SELECT id FROM videos WHERE status IN {statuses}").fetchall()
        cur = conn.execute(
            f"UPDATE videos SET status='pending', attempts=0, last_error=NULL "
            f"WHERE status IN {statuses}")
    conn.commit()
    archive_remove([r["id"] for r in rows])   # or yt-dlp would refuse
    log(f"requeued {cur.rowcount} video(s)")


def cmd_scan(args) -> None:
    """Rebuild archive state from files already on disk.

    Recovers from a lost or rolled-back state.db, and adopts videos downloaded
    by hand, so neither situation causes a re-download.
    """
    cfg = load_config()
    root = download_root(cfg)
    if not root.exists():
        die(f"download dir does not exist: {root}")

    found = {}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in MEDIA_EXTS:
            vid = video_id_from_name(path.name)
            if vid:
                found[vid] = path

    if not found:
        log(f"no archived videos found under {root}")
        return

    conn = db()
    adopted = 0
    for vid, path in found.items():
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        row = conn.execute("SELECT status FROM videos WHERE id=?", (vid,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO videos (id, source_id, title, status, first_seen, "
                "filepath, filesize, downloaded_at) VALUES (?,?,?,'done',?,?,?,?)",
                (vid, "", path.stem, now(), str(path), size, now()))
            adopted += 1
        elif row["status"] != "done":
            conn.execute("UPDATE videos SET status='done', filepath=?, filesize=?, "
                         "downloaded_at=COALESCE(downloaded_at,?) WHERE id=?",
                         (str(path), size, now(), vid))
            adopted += 1
    conn.commit()
    added = archive_add(found.keys())
    log(f"scanned {root}: {len(found)} file(s) on disk, {adopted} newly marked "
        f"archived, {added} added to {ARCHIVE_PATH.name}")

    written = 0
    for src in conn.execute("SELECT * FROM sources").fetchall():
        if rebuild_manifest(conn, cfg, src):
            written += 1
    if written:
        log(f"wrote {MANIFEST_NAME} into {written} source folder(s)")


def rebuild_manifest(conn, cfg: dict, source) -> bool:
    """Regenerate a source's folder manifest from what the database holds."""
    folder = source["folder"] or source["title"]
    path = manifest_path(cfg, folder)
    if not path or not path.parent.exists():
        return False

    rows = conn.execute(
        "SELECT id, title, filepath, filesize, downloaded_at FROM videos "
        "WHERE source_id=? AND status='done'", (source["id"],)).fetchall()
    if not rows:
        return False

    data = {
        "source": {k: source[k] for k in ("id", "kind", "title", "url")},
        "updated": now(),
        "videos": {
            r["id"]: {
                "title": r["title"],
                "file": Path(r["filepath"]).name if r["filepath"] else "",
                "quality": source["quality"],
                "size": r["filesize"] or 0,
                "downloaded_at": r["downloaded_at"],
            } for r in rows
        },
    }
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# scheduling (launchd)
# --------------------------------------------------------------------------

def protected_dir_warnings(cfg: dict) -> "list[str]":
    """Folders the OS shields from background tasks (macOS TCC)."""
    protected = plat.protected_dirs()
    if not protected:
        return []
    out = []
    home = Path.home()
    for label, path in (("the app itself", APP_DIR),
                        ("the download dir", download_root(cfg))):
        try:
            rel = path.relative_to(home)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in protected:
            out.append(f"{label} lives in ~/{rel.parts[0]}")
    return out


def schedule_is_installed() -> bool:
    return plat.schedule_installed(APP_DIR)


def stale_schedule_target() -> "str | None":
    """The old path a schedule still points at after the app folder moved.

    Returns None when there is no schedule, or when it correctly points here.
    """
    target = plat.scheduled_script_path(APP_DIR)
    if not target:
        return None
    try:
        # Compared as files, not as text: macOS and Windows ignore capitalisation,
        # so a folder renamed from CLAUDE to Claude is still the same folder.
        if os.path.samefile(target, APP_DIR / "ytarchive.py"):
            return None
    except OSError:
        pass
    return target


def schedule_problem() -> "str | None":
    """Why the installed schedule cannot work, or None if it can.

    Both failures happen silently in the background: the scheduler cannot even
    start the program, so nothing it would log ever gets written.
    """
    target = stale_schedule_target()
    if target:
        return f"points at {target}, but the folder has moved"
    exe = plat.scheduled_interpreter(APP_DIR)
    if exe and os.path.isabs(exe) and not os.path.exists(exe):
        return (f"uses a Python that no longer exists ({exe}) - "
                f"probably replaced by an upgrade")
    return None


def warn_if_schedule_stale() -> None:
    problem = schedule_problem()
    if problem:
        log(f"WARNING: the scheduled check is broken: it {problem}. Background "
            f"runs are doing nothing. Fix with: "
            f"{Path(sys.executable).name} ytarchive.py schedule install", err=True)


def cmd_schedule(args) -> None:
    cfg = load_config()

    if args.action == "status":
        if not schedule_is_installed():
            print("Recurring check: not installed")
            return
        print("Recurring check: installed")
        print(plat.schedule_describe(APP_DIR))
        return

    if args.action == "uninstall":
        log(plat.schedule_uninstall(APP_DIR))
        return

    try:
        summary = plat.schedule_install(APP_DIR, args.interval, LOG_DIR)
    except RuntimeError as exc:
        die(f"could not install the recurring check: {exc}")

    log(f"scheduled: {summary}")
    for w in protected_dir_warnings(cfg):
        log(f"  WARNING: {w}. macOS may block a background task from reading it. "
            f"If scheduled runs log permission errors, move it outside "
            f"Desktop/Documents/Downloads (e.g. ~/ytarchive).")


def cmd_serve(args) -> None:
    import threading
    import webui
    cfg = load_config()
    warn_if_schedule_stale()
    if not js_runtime() and not plat.IN_CONTAINER:
        log("WARNING: deno is not installed. yt-dlp needs it to get past YouTube's "
            f"JavaScript checks, and downloads will fail without it. Install: "
            f"{plat.deno_hint()}", err=True)

    def update_on_start() -> None:
        # Opening the interface is "running the app" too, so check for a
        # yt-dlp update here as well - in the background, so the page opens at
        # once. Under the run lock, like any download; if a run already holds
        # it, that run does its own check.
        lock = acquire_lock()
        if lock is None:
            return
        try:
            if update_ytdlp(cfg):
                webui._ytdlp_cache["data"] = None     # show the new version
        except Exception as exc:                      # never take the UI down
            log(f"yt-dlp update check failed: {exc}", err=True)
        finally:
            lock.release()

    threading.Thread(target=update_on_start, daemon=True).start()
    port = args.port or int(os.environ.get("YTARCHIVE_PORT") or cfg["web_port"])
    host = args.host or os.environ.get("YTARCHIVE_HOST") or cfg["web_host"]
    interval = args.interval
    if interval is None:
        interval = int(os.environ.get("YTARCHIVE_INTERVAL") or 0)
    webui.serve(port, host=host, interval_minutes=interval,
                open_browser=not args.no_open and host in ("127.0.0.1", "localhost"))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ytarchive",
        description="Archive YouTube channels and playlists locally.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  ./ytarchive.py ui                                    open the web interface
  ./ytarchive.py add "https://youtube.com/playlist?list=PLxxxx" --quality original
  ./ytarchive.py add @sentdex --quality 1080 --container mp4
  ./ytarchive.py quality "Some Playlist" --quality original --redownload
  ./ytarchive.py schedule install                      check every hour
""")
    p.add_argument("-q", "--quiet", action="store_true", help="log to file only")
    p.add_argument("--version", action="version", version=f"ytarchive {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    qual = list(QUALITY_PRESETS)
    conts = sorted(set(VIDEO_CONTAINERS + AUDIO_CONTAINERS))

    a = sub.add_parser("add", help="track a playlist or channel, or download one video")
    a.add_argument("targets", nargs="+", help="playlist URL, channel URL, @handle, ID, or a video URL")
    a.add_argument("--quality", choices=qual, help="default: config default_quality")
    a.add_argument("--container", choices=conts, help="default: config default_container")
    a.add_argument("--limit", type=int, default=0, help="only the N most recent videos")
    a.add_argument("--only-new", action="store_true",
                   help="skip the back catalogue; archive future additions only")
    a.set_defaults(func=cmd_add)

    i = sub.add_parser("import", help="import Google Takeout subscriptions.csv")
    i.add_argument("path")
    i.add_argument("--quality", choices=qual)
    i.add_argument("--container", choices=conts)
    i.add_argument("--limit", type=int, default=0)
    i.add_argument("--only-new", action="store_true")
    i.set_defaults(func=cmd_import)

    l = sub.add_parser("list", help="show tracked playlists and channels")
    l.set_defaults(func=cmd_list)

    r = sub.add_parser("remove", help="stop tracking a source")
    r.add_argument("targets", nargs="+")
    r.add_argument("--purge-state", action="store_true",
                   help="also forget which of its videos were archived")
    r.set_defaults(func=cmd_remove)

    pa = sub.add_parser("pause", help="stop checking a source without removing it")
    pa.add_argument("targets", nargs="+")
    pa.set_defaults(func=cmd_pause, paused=True)

    rs = sub.add_parser("resume", help="resume a paused source")
    rs.add_argument("targets", nargs="+")
    rs.set_defaults(func=cmd_pause, paused=False)

    qu = sub.add_parser("quality", help="change quality/format for a source")
    qu.add_argument("target")
    qu.add_argument("--quality", choices=qual)
    qu.add_argument("--container", choices=conts)
    qu.add_argument("--redownload", action="store_true",
                    help="also requeue everything already archived from it")
    qu.set_defaults(func=cmd_quality)

    ru = sub.add_parser("run", help="check all sources and download what is missing")
    ru.add_argument("--limit", type=int, default=None, help="max downloads this run")
    ru.add_argument("--dry-run", action="store_true")
    ru.add_argument("--deep", action="store_true",
                    help="force a full re-listing of every source")
    ru.add_argument("--no-progress", action="store_true")
    ru.set_defaults(func=cmd_run)

    up = sub.add_parser("update-ytdlp", help="update yt-dlp now")
    up.set_defaults(func=cmd_update_ytdlp)

    sp = sub.add_parser("stop", help="halt the run in progress")
    sp.set_defaults(func=cmd_stop)

    sn = sub.add_parser("scan", help="adopt videos already on disk")
    sn.set_defaults(func=cmd_scan)

    st = sub.add_parser("status", help="show archive statistics")
    st.add_argument("--failures", action="store_true")
    st.set_defaults(func=cmd_status)

    rt = sub.add_parser("retry", help="requeue failed downloads")
    rt.add_argument("video_ids", nargs="*")
    rt.add_argument("--include-dead", action="store_true",
                    help="also retry videos marked unavailable (deleted/private)")
    rt.set_defaults(func=cmd_retry)

    sc = sub.add_parser("schedule", help="manage the recurring background check")
    sc.add_argument("action", choices=["install", "uninstall", "status"])
    sc.add_argument("--interval", type=int, default=60, help="minutes (default 60)")
    sc.set_defaults(func=cmd_schedule)

    for name in ("ui", "serve"):
        sv = sub.add_parser(name, help="open the web interface")
        sv.add_argument("--port", type=int, default=None)
        sv.add_argument("--host", default=None,
                        help="bind address (default 127.0.0.1; 0.0.0.0 exposes "
                             "it to your network - set YTARCHIVE_AUTH too)")
        sv.add_argument("--interval", type=int, default=None,
                        help="also check every N minutes in-process, instead of "
                             "installing a system scheduler")
        sv.add_argument("--no-open", action="store_true", help="do not open a browser")
        sv.set_defaults(func=cmd_serve)

    return p


def main() -> None:
    global _quiet
    args = build_parser().parse_args()
    _quiet = args.quiet
    write_default_config()
    try:
        args.func(args)
    except KeyboardInterrupt:
        publish_progress(None)
        log("interrupted", err=True)
        sys.exit(130)


if __name__ == "__main__":
    main()
