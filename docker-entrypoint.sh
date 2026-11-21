#!/bin/sh
# Container entrypoint: match host file ownership, optionally refresh yt-dlp,
# then hand off to ytarchive as an unprivileged user.
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-022}"
DATA="${YTARCHIVE_DATA:-/config}"
DOWNLOADS="${YTARCHIVE_DOWNLOADS:-/downloads}"

umask "$UMASK"

# --- user matching the host's ids -----------------------------------------
# Unraid defaults to 99:100 (nobody:users). Files written need to belong to that
# owner on the host, so a user with those exact ids is created here.
if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -g "$PGID" ytarchive 2>/dev/null || true
fi
GROUP_NAME="$(getent group "$PGID" | cut -d: -f1)"
GROUP_NAME="${GROUP_NAME:-ytarchive}"

if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -M -d "$DATA" -s /sbin/nologin ytarchive 2>/dev/null || true
fi
USER_NAME="$(getent passwd "$PUID" | cut -d: -f1)"
USER_NAME="${USER_NAME:-ytarchive}"

# --- directories ----------------------------------------------------------
mkdir -p "$DATA" "$DATA/logs" "$DOWNLOADS"

# Only the config directory is chowned. Recursively chowning the media library
# would be slow and destructive on a large array; if it is not writable the app
# reports that clearly on the first run instead.
chown -R "$PUID:$PGID" "$DATA" 2>/dev/null || \
    echo "warning: could not chown $DATA - check the host permissions"

if ! gosu "$PUID:$PGID" test -w "$DOWNLOADS" 2>/dev/null; then
    echo "warning: $DOWNLOADS is not writable by $PUID:$PGID."
    echo "         Fix the host directory's ownership, or set PUID/PGID to match it."
fi

# --- yt-dlp ownership ----------------------------------------------------
# yt-dlp lives in /opt/yt-dlp so the app can upgrade it while running. Hand it
# to the app user - but only when ownership is wrong, since a recursive chown
# of the whole environment on every start would be wasted work.
if [ -d /opt/yt-dlp ] && [ "$(stat -c %u:%g /opt/yt-dlp)" != "$PUID:$PGID" ]; then
    chown -R "$PUID:$PGID" /opt/yt-dlp || \
        echo "warning: could not hand /opt/yt-dlp to $PUID:$PGID - auto-update will fail"
fi

echo "ytarchive starting as ${USER_NAME}(${PUID}):${GROUP_NAME}(${PGID})"
echo "  data      : $DATA"
echo "  downloads : $DOWNLOADS"
echo "  yt-dlp    : $(yt-dlp --version 2>/dev/null || echo 'NOT FOUND')"
echo "  deno      : $(deno --version 2>/dev/null | head -1 || echo 'NOT FOUND')"
echo "  auto-update yt-dlp every ${YTARCHIVE_YTDLP_UPDATE_HOURS:-0}h (${YTARCHIVE_YTDLP_CHANNEL:-stable})"

# --- dispatch -------------------------------------------------------------
# "serve" (the default) starts the interface plus the interval loop. Any other
# argument is passed straight through, so `docker exec … ytarchive list` works.
if [ "$1" = "serve" ] || [ "$1" = "ui" ]; then
    shift
    set -- serve --no-open "$@"
fi

exec gosu "$PUID:$PGID" python /app/ytarchive.py "$@"
