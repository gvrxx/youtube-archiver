# ytarchive

Keeps local copies of YouTube playlists and channels. Point it at a source, pick
a quality, and it archives everything in it — then re-checks on a schedule and
picks up anything new.

Runs on **Windows**, **macOS** and **Linux**, or as a **Docker container** for
Unraid. No API key, no Google login, no Python packages to install.

Everything the program needs lives in one folder. Move it wherever you like —
see [Moving the folder](#moving-the-folder).

---

## Contents

- [Install on Windows](#install-on-windows)
- [Install on macOS](#install-on-macos)
- [First run](#first-run)
- [Adding playlists and channels](#adding-playlists-and-channels)
- [Quality and format](#quality-and-format)
- [Running automatically](#running-automatically)
- [Moving the folder](#moving-the-folder)
- [How it avoids re-downloading](#how-it-avoids-re-downloading)
- [Command reference](#command-reference)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Docker and Unraid](DOCKER.md)

---

## Install on Windows

You need three things: Python, yt-dlp, and ffmpeg.

**1. Open PowerShell** (Start menu → type `powershell` → Enter).

**2. Install everything:**

```powershell
winget install Python.Python.3.12 yt-dlp.yt-dlp Gyan.FFmpeg DenoLand.Deno
```

**3. Close PowerShell and open a new one.** This matters — PATH changes don't
reach windows that are already open, and skipping it is the most common reason
the next step "fails".

**4. Check all four are visible:**

```powershell
py --version; yt-dlp --version; ffmpeg -version; deno --version
```

Four version numbers means you're ready. deno matters: yt-dlp needs it to get
past YouTube's JavaScript checks, and without it downloads fail. If any says "not recognized", see
[yt-dlp not found](#yt-dlp-or-ffmpeg-not-found).

**5. Put the ytarchive folder wherever you want it** — `C:\Users\you\ytarchive`
is a good choice. Avoid OneDrive-synced folders: OneDrive will try to upload
your entire video archive.

**6. Start it:** double-click **`start.bat`** in the folder. Or from
PowerShell:

```powershell
cd C:\Users\you\ytarchive
py ytarchive.py ui
```

> On Windows every command below uses `py` instead of `python3`.

### No winget?

On older Windows, install Python from [python.org](https://www.python.org/downloads/)
— **tick "Add python.exe to PATH"** on the first screen — then:

```powershell
pip install -U "yt-dlp[default,deno]"
```

The `[default,deno]` part matters: it brings deno, which yt-dlp needs for
YouTube's JavaScript checks. A plain `pip install yt-dlp` leaves it out.

ffmpeg has no installer: download the "release full" build from
[gyan.dev](https://www.gyan.dev/ffmpeg/builds/), unzip it, and either add its
`bin` folder to PATH or drop `ffmpeg.exe` next to `ytarchive.py`.

---

## Install on macOS

**1. Open Terminal** (Cmd+Space → "Terminal").

**2. Install Homebrew** if you don't have it:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

**3. Install the two tools** (macOS already includes Python 3):

```bash
brew install yt-dlp ffmpeg
```

**4. Check they work:**

```bash
python3 --version && yt-dlp --version && ffmpeg -version
```

**5. Start it:** double-click **`start.command`** in Finder. Or from Terminal:

```bash
cd ~/ytarchive
python3 ytarchive.py ui
```

If macOS says the file "can't be opened because it is from an unidentified
developer" (it can, for a folder downloaded as a zip), right-click it → **Open**
→ **Open** once; after that a double-click works.

> **Where you put the folder matters on macOS.** Avoid `~/Desktop`,
> `~/Documents` and `~/Downloads` — macOS blocks background tasks from reading
> those, which can stop scheduled checks from working. `~/ytarchive` is safest.

---

## Keeping yt-dlp updated

**This happens automatically.** YouTube changes things every few weeks and
breaks older yt-dlp builds; when that happens nothing crashes, the app just
stops archiving — easy to miss for weeks. So whenever ytarchive runs — when you
open the interface, and before every scheduled check — it looks for a yt-dlp
update if the last check was more than a day ago.

It works out how yt-dlp was installed and updates it the same way:

| Installed with | It runs |
| --- | --- |
| Homebrew (macOS) | `brew update`, then `brew upgrade yt-dlp` |
| winget (Windows) | `winget upgrade --id yt-dlp.yt-dlp` |
| pip | that Python's `pip install -U "yt-dlp[default,deno]"` |
| standalone download | `yt-dlp -U` |
| Docker image | its own built-in updater — see [DOCKER.md](DOCKER.md) |

The page doesn't wait for it — the check runs in the background, and the top
of the interface shows *Updating yt-dlp…* while it does. It never swaps files
out mid-download, and if an update fails (offline, say) the current version
keeps working and the next run tries again.

Check what you have, and when it was last checked:

```bash
python3 ytarchive.py status
```

Update right now: `python3 ytarchive.py update-ytdlp`.

To change the interval, set `ytdlp_update_hours` in `config.json` (`0` turns
it off). To use a different method, set `ytdlp_update_command`, e.g.
`["brew", "upgrade", "yt-dlp"]`.

---

## First run

```
python3 ytarchive.py ui        (macOS)
py ytarchive.py ui             (Windows)
```

Your browser opens <http://127.0.0.1:8800>. Leave the terminal window open —
closing it stops the interface. `Ctrl+C` stops it deliberately.

On first start it creates `config.json`, `state.db` and `logs/` next to the
program. Videos go to `~/Movies/YouTubeArchive` (macOS) or
`C:\Users\you\Videos\YouTubeArchive` (Windows) — change `download_dir` in
`config.json` to put them elsewhere, including an external drive or NAS.

---

## Adding playlists and channels

Click **Add source**, or from the command line:

```bash
python3 ytarchive.py add "https://www.youtube.com/playlist?list=PL…"
python3 ytarchive.py add @sentdex --quality 1080
```

Everything in the source is archived, and every later check picks up additions.

| You paste | What you get |
| --- | --- |
| `youtube.com/playlist?list=PL…` | that playlist |
| `youtube.com/watch?v=…&list=PL…` | the **playlist**, not just that one video |
| `youtube.com/@handle` or `@handle` | that channel's uploads |
| `youtube.com/channel/UC…` | that channel's uploads |

**Before adding a big channel**, consider `--limit`. Without it, a channel with
800 uploads queues 800 downloads:

| Flag | Effect |
| --- | --- |
| `--limit N` | Only the N most recent, on **every** check — not just the first |
| `--only-new` | Skip the back catalogue; archive what appears from now on |

### Importing your subscriptions

Your subscription list is private, so nothing can read it without a Google
login. Export it once from [Google Takeout](https://takeout.google.com/):
deselect everything, choose **YouTube**, then under "All YouTube data included"
pick only **subscriptions**. Then:

```bash
python3 ytarchive.py import ~/Downloads/subscriptions.csv --limit 5
```

Use a limit here. Importing 200 channels unlimited queues tens of thousands of
videos.

---

## Quality and format

Set per source, changeable at any time — from the dropdowns in **Sources**, or:

```bash
python3 ytarchive.py quality "My Playlist" --quality 720
```

New settings apply to everything **not yet downloaded**, including future
uploads. Add `--redownload` to also replace what you already have (this deletes
the existing files first, then fetches them again at the new setting).

| `--quality` | |
| --- | --- |
| `original` | Best available. No cap, nothing re-encoded. |
| `2160` `1440` `1080` `720` `480` `360` | Best streams at or below that height |
| `audio` | Audio only |

| `--container` | |
| --- | --- |
| `original` | No forced container; keeps source streams as-is |
| `mkv` | Matroska — holds any codec, never re-encodes. Best for archiving. |
| `mp4` | Plays everywhere; occasionally needs a remux |
| `m4a` `mp3` `opus` `flac` | Audio-only targets |

**A note on file sizes:** the container does *not* determine size — the
resolution and codec do. Converting MKV to MP4 changes the wrapper, not the
bytes. To use less space, lower the quality instead.

---

## Running automatically

Nothing runs in the background until you ask:

```bash
python3 ytarchive.py schedule install
```

That registers a **Scheduled Task** on Windows, a **launchd agent** on macOS, or
a **systemd timer** on Linux. It checks every 60 minutes (`--interval N` to
change), needs no admin rights or stored password, and keeps working after you
close the interface. On Windows it runs while you're logged in.

```bash
python3 ytarchive.py schedule status
python3 ytarchive.py schedule uninstall
```

Prefer keeping one window open instead? `python3 ytarchive.py ui --interval 60`
serves the interface and checks on the same schedule, in one process.

---

## Moving the folder

The whole program — settings, database, download record, logs — lives in its own
folder. Move or copy it anywhere and it keeps working. Your downloaded videos
live separately (`download_dir`) and are unaffected.

**One thing to redo after moving:** if you had a schedule installed, the
scheduler stored the old path and will silently do nothing. Re-register it:

```bash
cd /new/location/ytarchive
python3 ytarchive.py schedule install
```

`status` warns you when this has happened, showing the stale path.

To move it to another computer, copy the folder across, install the tools for
that platform, and run `schedule install` there. If you also move your videos,
update `download_dir` and run `python3 ytarchive.py scan`.

---

## How it avoids re-downloading

Four independent guards, so restarts, moves, lost databases and re-added
playlists never cause a second download:

1. **`state.db`** — archived videos are never queued.
2. **`downloaded.txt`** — a yt-dlp download archive; yt-dlp itself refuses
   anything listed, even if the database is gone.
3. **The filename** — each carries its video ID in brackets, so a file already
   on disk is recognised.
4. **`ytarchive.json` in each source folder** — a manifest listing every video
   downloaded from that source, updated as it goes.

That fourth one is what makes **removing and re-adding a playlist free**. The
record lives with the videos, not with the program, so re-adding a source reads
it and adopts everything already present:

```
playlist: Road Trips — queued 59 of 95 video(s) (36 already on disk)
```

It also means you can copy an archive folder to another machine and have a fresh
install recognise it immediately. Only entries whose file is genuinely still
there are adopted — delete a video and it downloads again.

If you lose `state.db` entirely, or have videos downloaded by hand:

```bash
python3 ytarchive.py scan
```

That rebuilds everything from the filenames and writes fresh manifests.

---

## Command reference

Prefix with `python3` on macOS, `py` on Windows.

| Command | |
| --- | --- |
| `ui` | Web interface. `--port`, `--host`, `--interval` |
| `add <url…>` | Track a playlist or channel (own folder each), or download a single video (main folder) |
| `import <csv>` | Bulk-import a Takeout export |
| `list` | Tracked sources and their settings |
| `quality <source>` | Change quality/format. `--redownload` to replace existing files |
| `remove <source>` | Stop tracking. `--purge-state` also forgets its history |
| `pause` / `resume <source>` | Stop or restart checking a source |
| `run` | Check now. `--dry-run`, `--limit N`, `--deep` |
| `stop` | Halt the run in progress |
| `scan` | Adopt videos already on disk |
| `status` | Statistics. `--failures` lists errors |
| `update-ytdlp` | Update yt-dlp now |
| `retry [ids…]` | Requeue failures. `--include-dead` for unavailable ones |
| `schedule install\|uninstall\|status` | The recurring check |

---

## Configuration

`config.json`, created on first run. Restart after editing.

| Key | Default | |
| --- | --- | --- |
| `download_dir` | `~/Movies/…` (mac), `~/Videos/…` (Win) | Where videos go |
| `default_quality` | `original` | For newly added sources |
| `default_container` | `mkv` | |
| `max_downloads_per_run` | `0` | 0 = no cap. Set a number to spread a backlog out |
| `max_attempts` | `5` | 0 = retry forever |
| `sleep_between_downloads` | `3` | Seconds between videos, jittered ±30% |
| `sleep_requests` | `0` | Seconds between yt-dlp's own requests |
| `stop_after_consecutive_failures` | `8` | Back off when refused. 0 disables |
| `rate_limit` | `null` | e.g. `"3M"` to cap speed |
| `concurrent_fragments` | `4` | Parallel connections per video |
| `deep_scan_hours` | `12` | How often channels get a full re-listing |
| `min_duration_seconds` | `0` | `61` skips most Shorts |
| `write_auto_subs` | `false` | Auto-captions: large and slow |
| `cookies_from_browser` | `null` | `"chrome"`, `"firefox"`, `"safari"` |
| `log_max_mb` / `log_keep` | `2` / `5` | Log rotation |
| `ytdlp_update_hours` | `24` | Check for a yt-dlp update this often. 0 = off |
| `ytdlp_update_command` | `null` | Override the detected update method |
| `ytdlp_channel` | `stable` | `nightly` gets YouTube fixes sooner (Docker image only) |
| `player_client` + `_fallbacks` | yt-dlp default + list | See troubleshooting |

### Going easy on YouTube

Downloading hundreds of videos quickly gets your IP flagged. If you hit bot
checks, these settings trade speed for staying unblocked:

```json
"max_downloads_per_run": 3,
"sleep_between_downloads": 120,
"sleep_requests": 2,
"concurrent_fragments": 1,
"rate_limit": "3M",
"stop_after_consecutive_failures": 3
```

With an hourly schedule that's ~3 videos an hour — slow, but it keeps working.

---

## Troubleshooting

### yt-dlp or ffmpeg "not found"

Open a **new** terminal and check: `yt-dlp --version`.

If it works there but ytarchive disagrees, give it the full path in
`config.json`:

```json
"yt_dlp_path": "C:\\Users\\you\\AppData\\Local\\Microsoft\\WindowsApps\\yt-dlp.exe"
```

(Double backslashes — it's JSON.) On macOS: `"/opt/homebrew/bin/yt-dlp"`.

Find the path with `where yt-dlp` (Windows) or `which yt-dlp` (macOS).

### "Sign in to confirm you're not a bot"

YouTube has flagged your IP for automated access. Either wait — these usually
clear within a day, and the app backs off on its own — or set
`cookies_from_browser` to `"chrome"`, `"firefox"` or `"safari"`.

Two warnings: **close that browser first** (Chrome locks its cookie database),
and **use a throwaway Google account**, not your main one. Heavy automated use
can get an account restricted.

Also apply the [go easy](#going-easy-on-youtube) settings, or it will happen again.

### `HTTP Error 403` or `Requested format is not available`

Handled automatically. YouTube blocks its internal player clients
unpredictably and per-video, so each download tries `player_client`, then walks
`player_client_fallbacks` until one works. If a client offers less than the
quality you asked for, the others are checked before it is accepted — so a
blocked client can't quietly hand you 360p.

If everything fails, update yt-dlp, then `python3 ytarchive.py retry`.

### Scheduled checks do nothing

First, has the folder moved? `status` says `BROKEN` and shows the stale path if
so — fix with `schedule install`.

**Windows:** confirm the task exists with `schtasks /Query /TN ytarchive`. It
only runs while you're logged in. If it exists but never runs, yt-dlp probably
isn't on the PATH the task inherits — set `yt_dlp_path` to the full path.

**macOS:** check `logs/schedule.err.log`. Permission errors mean the folder is
in `~/Desktop`, `~/Documents` or `~/Downloads`, which macOS shields from
background tasks. Move it to `~/ytarchive` and reinstall the schedule.

### "Path too long" (Windows)

Windows caps paths at 260 characters. Keep the archive near the drive root
(`D:\YouTubeArchive`), or enable long paths in an **administrator** PowerShell:

```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force
```

### Everything is downloading again

`state.db` was lost. Run `python3 ytarchive.py scan` — it rebuilds from the
filenames. Check `download_dir` actually points at your archive, and that files
still have their `[videoID]` suffix; renaming files breaks recovery.

### Adding a channel queued hundreds of videos

Expected — sources are tracked whole. Either re-add with `--limit N`, or set
`max_downloads_per_run` so each check takes a few at a time.

### Videos stuck in the queue

Anything marked *not out yet* is a premiere or scheduled stream that hasn't
aired. Those retry each run without penalty. Real problems appear under
**Failed** with a reason.

### Disk filling up

`original` on a large channel adds up fast — 4K can exceed 5 GB an hour. Lower
`quality` per source, use `--limit`, or use `audio` for talk-heavy content. Free
space is shown in the interface sidebar.

### Where to look when something is wrong

```bash
python3 ytarchive.py status --failures
```

Then `logs/ytarchive.log`. On Windows: `Get-Content logs\ytarchive.log -Wait`;
on macOS: `tail -f logs/ytarchive.log`.

---

## Files

```
ytarchive.py    core: sources, downloading, CLI
platforms.py    everything OS-specific: locking, scheduling, filenames
webui.py        local web server
ui.html         the interface
config.json     your settings
state.db        sources, videos, run history
downloaded.txt  yt-dlp download archive
logs/           rotated activity log
```

And in each source's video folder, `ytarchive.json` — the record of what was
downloaded from that source. Back up `state.db` if you like, but between the
manifests and `scan`, everything is rebuildable from the videos themselves.

---

## A note on scope

This is for personal archiving. Downloading is against YouTube's Terms of
Service, and archived videos remain the copyright of whoever made them — keep
them for yourself rather than redistributing them.
