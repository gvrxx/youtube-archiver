# syntax=docker/dockerfile:1
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/gvrxx/youtube-archiver" \
      org.opencontainers.image.description="Archive YouTube playlists and channels locally" \
      org.opencontainers.image.licenses="NOASSERTION"

# ffmpeg merges streams and extracts audio.
# gosu drops privileges to the PUID/PGID the host asks for (Unraid convention).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        gosu \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp gets its own virtualenv, handed to the app user at start-up, so it can
# be upgraded in place while the container keeps running - Unraid containers go
# months without a restart, and YouTube breaks old yt-dlp builds within weeks.
#
# [default] brings yt-dlp-ejs, the solver for YouTube's JavaScript challenges,
# and [deno] the JavaScript runtime it executes on. Without both, YouTube
# downloads fail. Re-running this exact install is how the auto-update works.
RUN python -m venv /opt/yt-dlp \
    && /opt/yt-dlp/bin/pip install --no-cache-dir --disable-pip-version-check \
        "yt-dlp[default,deno]" \
    && /opt/yt-dlp/bin/yt-dlp --version \
    && /opt/yt-dlp/bin/deno --version

WORKDIR /app
COPY ytarchive.py platforms.py webui.py ui.html unraid-template.xml ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PATH="/opt/yt-dlp/bin:${PATH}" \
    YTARCHIVE_DATA=/config \
    YTARCHIVE_DOWNLOADS=/downloads \
    YTARCHIVE_HOST=0.0.0.0 \
    YTARCHIVE_PORT=8800 \
    YTARCHIVE_INTERVAL=60 \
    YTARCHIVE_YTDLP_UPDATE_HOURS=24 \
    YTARCHIVE_YTDLP_CHANNEL=stable \
    YTARCHIVE_CONTAINER=1 \
    PUID=99 \
    PGID=100 \
    UMASK=022 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

VOLUME ["/config", "/downloads"]
EXPOSE 8800

# A plain TCP connect: proves the server is listening without caring whether
# auth is enabled (an authenticated instance answers 401, which is healthy).
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import os,socket; \
socket.create_connection(('127.0.0.1', int(os.environ.get('YTARCHIVE_PORT','8800'))), 5).close()"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["serve"]
