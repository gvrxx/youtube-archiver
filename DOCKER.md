# Running ytarchive in Docker / Unraid

For Unraid, a NAS, or any always-on machine. The container serves the web
interface, checks your playlists and channels on an interval, and keeps its own
copy of yt-dlp up to date — no host scheduler, nothing to babysit.

> **How this is verified.** Every change is built by GitHub Actions and the
> resulting image is started the way Unraid runs it (PUID 99 / PGID 100, login
> enabled, a config folder it has to take ownership of). The test checks the
> privilege drop, the login, that yt-dlp can see its JavaScript runtime, and
> that the unprivileged app genuinely upgrades yt-dlp in place. What CI does
> *not* do is download from YouTube: build servers sit on datacenter IPs that
> YouTube bot-checks, so a result there would mean nothing.

---

## Contents

- [Getting the image](#getting-the-image)
- [Unraid](#unraid)
- [Moving an existing archive here](#moving-an-existing-archive-here)
- [Many playlists and channels](#many-playlists-and-channels)
- [Docker compose](#docker-compose)
- [Keeping yt-dlp current](#keeping-yt-dlp-current)
- [Environment variables](#environment-variables)
- [Volumes](#volumes)
- [Security](#security)
- [Running commands inside the container](#running-commands-inside-the-container)
- [Updating the container](#updating-the-container)
- [Troubleshooting](#troubleshooting)

---

## Getting the image

The image is published at:

```
ghcr.io/gvrxx/youtube-archiver:latest
```

It is rebuilt on every change and once a week, so a freshly created container
starts with recent yt-dlp, ffmpeg and base-image security fixes.

**Access depends on the package's visibility.** The repository is private, and
GitHub makes the image private to match. Unraid then can't pull it without
credentials. Two ways round that:

1. **Make the package public** (simplest). On GitHub: your profile →
   **Packages** → `youtube-archiver` → **Package settings** → **Change
   visibility** → Public. The code repository stays private, but be aware the
   image contains the program's source files, so anyone could pull and read
   them.

2. **Keep it private and log in on Unraid.** Create a GitHub token with only the
   `read:packages` scope, then in the Unraid terminal:

   ```bash
   docker login ghcr.io -u gvrxx
   ```

   Catch: Unraid keeps `/root` in RAM, so this login is forgotten on every
   reboot and image updates then fail. To persist it, copy
   `/root/.docker/config.json` to `/boot/config/docker-auth.json` and add this
   to `/boot/config/go`:

   ```bash
   mkdir -p /root/.docker && cp /boot/config/docker-auth.json /root/.docker/config.json
   ```

To build it yourself instead, clone the repository and run
`docker build -t ytarchive .` — then use `ytarchive` as the repository name below.

---

## Unraid

### With the template (recommended)

1. Open the Unraid terminal (the `>_` icon, top right) and paste this. It
   saves the template onto the flash drive:

   ```bash
   wget -qO /boot/config/plugins/dockerMan/templates-user/my-ytarchive.xml https://raw.githubusercontent.com/gvrxx/youtube-archiver/main/unraid-template.xml
   ```
2. **Docker** tab → **Add Container** → choose **ytarchive** from the
   **Template** dropdown.
3. Check the two paths, and set a **Login** (`user:password`). Don't skip the
   login — see [Security](#security).
4. **Apply**. The **WebUI** entry in the container's menu opens the interface.

### By hand

**Docker** → **Add Container**, switch to **Advanced View**:

| Field | Value |
| --- | --- |
| Repository | `ghcr.io/gvrxx/youtube-archiver:latest` |
| Network Type | `Bridge` |
| Extra Parameters | `--init` |
| Port | `8800` → `8800` |
| Path | `/mnt/user/appdata/ytarchive` → `/config` |
| Path | `/mnt/user/media/youtube` → `/downloads` |
| Variable | `YTARCHIVE_AUTH` = `admin:your-password` |
| Variable | `YTARCHIVE_INTERVAL` = `60` |
| Variable | `YTARCHIVE_YTDLP_UPDATE_HOURS` = `24` |
| Variable | `PUID` = `99`, `PGID` = `100` |

### Things specific to Unraid

- **Keep `/config` on the cache pool.** It holds a SQLite database written on
  every check; on spinning array disks that keeps drives awake. The usual
  `/mnt/user/appdata/…` is cache-backed already.
- **`/downloads` on the array is fine** — videos are written in large
  sequential chunks.
- **99:100 is `nobody:users`**, Unraid's default share owner. Keep it unless
  you've changed share ownership deliberately.
- For a media share, *cache: no* (or a mover schedule you're comfortable with)
  avoids a large backlog filling the cache before mover runs.
- Each source's folder gets a `ytarchive.json` listing what was downloaded from
  it. Remove a playlist and add it back later, and those videos are recognised
  rather than downloaded again — even after rebuilding the container from
  scratch.

---

## Moving an existing archive here

If you've been running ytarchive on a Mac or PC, the container can take over
that archive **without downloading any of it again**. Each source folder already
records what's in it, and every file carries its video ID.

**1. Stop the old install** — permanently. Two copies checking the same
playlists would download everything twice and double the requests from your IP,
which is what gets you bot-checked.

```bash
python3 ytarchive.py stop
python3 ytarchive.py schedule uninstall
```

**2. Copy the video folders** into the share you'll mount as `/downloads`, for
example `~/Movies/YouTubeArchive/Road Trips` → `\\your-server\media\youtube\Road Trips`. Copy
the whole folder, including `ytarchive.json`. Finder over SMB is fine.

Keep the folder names and the `[videoID]` at the end of each filename. Nothing
else about the names matters — accented letters arriving in a different Unicode
form are handled.

**3. Make the copy writable by the container.** On Unraid: **Tools → New
Permissions** on that share, or in the terminal:

```bash
chown -R nobody:users /mnt/user/media/youtube
```

**4. Copy your `config.json`** into `/mnt/user/appdata/ytarchive/` *before* the
container's first start. That carries your quality default and throttling
settings across. Paths, host and port inside it are overridden by the
container, so they don't need editing.

Don't copy `state.db` or `downloaded.txt` — they record the old machine's file
paths. The folders hold everything that's needed.

**5. Start the container and add each source again** from the interface, with
the same quality. The result line shows what it recognised:

```
playlist: Road Trips — queued 40 of 96 video(s) (56 already on disk)
```

The 56 on disk are archived; the 40 are the ones genuinely missing, including
anything added to the playlist since. **Queue** should list only videos you
don't have.

---

## Many playlists and channels

There's no practical limit on sources. Each check:

- **Playlists** are fully re-listed, so additions anywhere in them are found.
- **Channels** are checked via their RSS feed (fast), with a full re-listing
  every 12 hours (`deep_scan_hours`) so nothing slips through.
- New videos join the queue; anything already archived — in the database, the
  download record, or simply present in the folder — is never fetched again.

Two things worth knowing as the list grows:

- **Pace.** With the throttling settings, downloads run at about 3 an hour, and
  sources are checked a few seconds apart. Adding a channel with hundreds of
  uploads means days of catching up — use `--limit` or **Only future uploads**
  if you don't want the back catalogue.
- **Importing your YouTube subscriptions.** Export `subscriptions.csv` from
  [Google Takeout](https://takeout.google.com/), put it in
  `/mnt/user/appdata/ytarchive/`, then:

  ```bash
  docker exec -u 99:100 ytarchive python /app/ytarchive.py import /config/subscriptions.csv --limit 5
  ```

  Without a limit, importing a hundred channels queues tens of thousands of
  videos.

---

## Docker compose

For other hosts. From a clone of the repository:

```bash
docker compose up -d
docker compose logs -f
```

Edit `docker-compose.yml` first for paths, port and a login. To use the
published image instead of building, swap `build: .` for
`image: ghcr.io/gvrxx/youtube-archiver:latest`.

Plain `docker run`:

```bash
docker run -d --name ytarchive --init --restart unless-stopped \
  -p 8800:8800 \
  -v /path/to/config:/config \
  -v /path/to/videos:/downloads \
  -e PUID=1000 -e PGID=1000 \
  -e YTARCHIVE_AUTH=admin:pick-a-real-password \
  ghcr.io/gvrxx/youtube-archiver:latest
```

On a Linux desktop use your own ids (`id -u`, `id -g`); on Unraid use 99 / 100.

---

## Keeping yt-dlp current

YouTube changes how it serves video every few weeks, and each time older yt-dlp
builds stop working. When that happens nothing crashes — the app keeps checking,
keeps backing off politely, and simply stops archiving. That makes it easy to
miss for weeks.

So the container updates yt-dlp **while it runs**:

- At the start of each check, if the last update was more than
  `YTARCHIVE_YTDLP_UPDATE_HOURS` ago (default **24**), it upgrades first.
- The upgrade happens under the same lock as downloads, so files are never
  swapped out mid-download.
- yt-dlp's JavaScript challenge solver and the **deno** runtime it needs are
  upgraded together with it. YouTube downloads don't work without them, and the
  solver version is pinned to the yt-dlp release.
- If an update fails (no network, PyPI down), the installed version keeps
  working and the next check tries again.
- A brand-new container always checks on its first run, since the image may be
  a few days old.

**Stable or nightly.** `YTARCHIVE_YTDLP_CHANNEL=stable` (the default) takes
official releases. `nightly` takes the daily builds, which usually get YouTube
fixes days — sometimes weeks — earlier. If downloads keep failing on a
current stable, switch to nightly. Switching back to stable won't downgrade;
you'll stay on the nightly build until stable passes it.

**Check what you're running.** The interface sidebar shows the installed
version and the update setting. Or:

```bash
docker exec -u 99:100 ytarchive python /app/ytarchive.py status
```

**Force an update now:**

```bash
docker exec -u 99:100 ytarchive python /app/ytarchive.py update-ytdlp
```

Recreating the container (for example when Unraid updates the image) resets
yt-dlp to the version baked into the image; the first check afterwards brings
it current again.

---

## Environment variables

| Variable | Default | |
| --- | --- | --- |
| `YTARCHIVE_AUTH` | *(unset)* | `user:password` login for the interface. **Set this.** |
| `YTARCHIVE_INTERVAL` | `60` | Minutes between checks. `0` turns automatic checking off. |
| `YTARCHIVE_YTDLP_UPDATE_HOURS` | `24` | How often to update yt-dlp. `0` = never. |
| `YTARCHIVE_YTDLP_CHANNEL` | `stable` | `stable` or `nightly`. |
| `PUID` / `PGID` | `99` / `100` | Owner of everything written. |
| `UMASK` | `022` | Permission mask for new files. |
| `TZ` | *(unset)* | Timezone for log timestamps, e.g. `Europe/London`. |
| `YTARCHIVE_PORT` | `8800` | Port inside the container. |
| `YTARCHIVE_HOST` | `0.0.0.0` | Bind address inside the container. Leave it. |
| `YTARCHIVE_DATA` | `/config` | Settings, database, logs. |
| `YTARCHIVE_DOWNLOADS` | `/downloads` | Where videos go. |

Everything else — quality defaults, throttling, subtitles, retries — lives in
`/config/config.json`. Edit it and restart the container. For an archive you
want to keep running unattended, the throttling block in the main
[README](README.md#going-easy-on-youtube) is worth applying: an IP that gets
bot-checked downloads nothing until the block lifts.

---

## Volumes

| Mount | Holds | Back up? |
| --- | --- | --- |
| `/config` | `config.json`, `state.db`, `downloaded.txt`, `logs/` | Yes — small |
| `/downloads` | The videos, one folder per source, each with its `ytarchive.json` | Your call |

Lost `/config` but kept `/downloads`? Nothing needs re-downloading:

```bash
docker exec -u 99:100 ytarchive python /app/ytarchive.py scan
```

---

## Security

**Without `YTARCHIVE_AUTH` the interface has no login.** It can add sources,
start and stop downloads and change settings, and inside a container it has to
listen on all interfaces to be reachable. The container logs a warning at
start-up if you leave it unset.

The login is HTTP Basic over plain HTTP: fine on a home network, not something
to port-forward to the internet. For remote access use a VPN or Tailscale, or a
reverse proxy with TLS (SWAG, Nginx Proxy Manager).

The app runs as `PUID:PGID`, not root, and the container needs no privileges.

---

## Running commands inside the container

Use `-u 99:100` (your PUID:PGID) so anything written stays owned correctly:

```bash
docker exec -u 99:100 ytarchive python /app/ytarchive.py list
docker exec -u 99:100 ytarchive python /app/ytarchive.py status --failures
docker exec -u 99:100 ytarchive python /app/ytarchive.py add "https://youtube.com/playlist?list=PL…" --quality 720
docker exec -u 99:100 ytarchive python /app/ytarchive.py run --limit 3
```

`schedule install` refuses inside a container — there's no host scheduler to
register with. `YTARCHIVE_INTERVAL` does that job.

---

## Updating the container

On Unraid, **Check for Updates** on the Docker tab picks up a new image, then
**Apply Update**. With compose:

```bash
docker compose pull && docker compose up -d
```

`/config` and `/downloads` survive; yt-dlp catches up on the next check.

---

## Troubleshooting

### Nothing is downloading

Work down this list:

1. `docker exec -u 99:100 ytarchive python /app/ytarchive.py status --failures`
   — the reasons are usually right there.
2. **"Sign in to confirm you're not a bot"** — your IP has been flagged. Apply
   the throttling settings and wait; it typically clears within a day.
3. **403s, or "The page needs to be reloaded"** — YouTube changed again. Force
   an update (`update-ytdlp`); if a current stable still fails, switch
   `YTARCHIVE_YTDLP_CHANNEL` to `nightly`.
4. Is checking on? `YTARCHIVE_INTERVAL=0` disables it. The first check runs
   about 30 seconds after start.
5. Is anything tracked? `… ytarchive.py list`.

### `deno: NOT FOUND` or yt-dlp complaining about a JavaScript runtime

The image ships deno inside yt-dlp's environment. If it's missing, the image
is broken or very old — pull the latest image and recreate the container.

### Auto-update says "Permission denied"

`/opt/yt-dlp` should belong to your PUID:PGID; the entrypoint fixes this on
start. If you changed PUID or PGID, **recreate** the container (a restart
isn't enough). Look for `could not hand /opt/yt-dlp` in the start-up log.

### Image pull fails with "unauthorized" or "denied"

The package is private — see [Getting the image](#getting-the-image). On Unraid
this commonly appears after a reboot, because the login didn't persist.

### Container starts then stops

```bash
docker logs ytarchive
```

Usually `/config` isn't writable. On the host:

```bash
chown -R 99:100 /mnt/user/appdata/ytarchive
```

### `warning: /downloads is not writable`

The entrypoint deliberately never chowns the media library — on a large array
that's slow and destructive. Either fix the share's ownership, or set PUID/PGID
to whoever owns it already:

```bash
stat -c '%u %g' /mnt/user/media/youtube
```

### Can't reach the interface

`docker ps` should show the container as `healthy`. With `-p 8900:8800` you
browse to port **8900**; the right-hand number stays `8800`.

### Cookies

`cookies_from_browser` can't work in a container — there's no browser in it.
If you need cookies, export a `cookies.txt` on another machine (use a
throwaway account), put it in `/config`, and add
`["--cookies", "/config/cookies.txt"]` to `extra_ytdlp_args` in
`/config/config.json`.

### Log timestamps are off

Set `TZ` and recreate the container.
