# Changelog

Every change worth noting gets a version and an entry here. The running version
shows in `ytarchive.py status`, `ytarchive.py --version`, and next to the title
in the web interface. Docker images are tagged with it too, e.g.
`ghcr.io/gvrxx/youtube-archiver:1.1.4`.

## 1.1.4 (2026-11-21)

- Dark red colours in the web interface, matching the new red app icon.

## 1.1.3 (2026-11-21)

- A login set as just a password (no `user:`) now works with any username.
  Before, no username could log in. Passwords with accented letters work too.

## 1.1.2 (2026-11-21)

- Archives whole YouTube playlists and channels, each at its own quality and
  format, and checks them for new videos on a schedule. Each playlist and
  channel gets its own folder.
- Single videos can be added too: paste a video link (watch, youtu.be or
  shorts) and it downloads straight into the main download folder.
- Runs on macOS, Windows and Linux, or as a Docker container for Unraid. The
  Unraid template, with its icon, installs straight from GitHub.
- Never downloads a video twice: database, download record, video ID in each
  filename, and a record kept in each playlist's folder.
- Keeps yt-dlp updated automatically, however it was installed.
- Keeps a below-quality copy when the full-quality source is blocked, and
  replaces it once a better one has downloaded.
- Web interface with list and grid library views, live progress, and a login
  for network access.
