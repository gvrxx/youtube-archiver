#!/usr/bin/env bash
# Run the built image the way Unraid does and check what matters.
# Real YouTube downloads are deliberately not attempted: CI runners sit on
# datacenter IPs that YouTube bot-checks, so a failure there would mean nothing.
set -uo pipefail

IMG="${1:-ytarchive:test}"
NAME=yta-smoke
work="$(mktemp -d)"
mkdir -p "$work/config" "$work/downloads"
sudo chown 99:100 "$work/downloads"     # Unraid's nobody:users
# $work/config stays owned by the runner on purpose: the entrypoint must fix it.

fails=0
pass() { echo "  PASS  $1"; }
fail() { echo "  FAIL  $1"; fails=$((fails + 1)); }
as_app() { docker exec -u 99:100 "$@"; }

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

wait_healthy() {
  for _ in $(seq 1 45); do
    s="$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null || echo gone)"
    [ "$s" = healthy ] && return 0
    sleep 2
  done
  return 1
}

docker run -d --name "$NAME" --init -p 8800:8800 \
  -e PUID=99 -e PGID=100 -e YTARCHIVE_INTERVAL=0 -e YTARCHIVE_AUTH=admin:test \
  -v "$work/config:/config" -v "$work/downloads:/downloads" "$IMG" >/dev/null

echo "== start-up"
if wait_healthy; then pass "container became healthy"; else
  fail "container never became healthy"; docker logs "$NAME"; exit 1; fi
docker logs "$NAME" 2>&1 | sed 's/^/        /'

echo "== tools"
# Unraid installs read the template straight out of the image.
docker run --rm --entrypoint cat "$IMG" /app/unraid-template.xml | grep -q '<Repository>ghcr.io/gvrxx/youtube-archiver' \
  && pass "Unraid template ships in the image" || fail "Unraid template missing from the image"
v="$(docker exec "$NAME" yt-dlp --version)" && pass "yt-dlp $v" || fail "yt-dlp missing"
d="$(docker exec "$NAME" deno --version | head -1)" && pass "$d" || fail "deno missing"
hdr="$(as_app "$NAME" yt-dlp -v --simulate --ignore-config --socket-timeout 10 \
        'https://www.youtube.com/watch?v=xQFMnVRjW0Q' 2>&1 || true)"
echo "$hdr" | grep -qE 'JS runtimes:.*deno' \
  && pass "yt-dlp detects deno as its JS runtime" \
  || { fail "yt-dlp does not see deno"; echo "$hdr" | grep -i 'js runtime' ; }

echo "== privileges"
# docker top needs a PID column in the ps output, so ask for it and skip it.
uid="$(docker top "$NAME" -eo pid,uid,args | awk '/ytarchive.py/ {print $2; exit}')"
[ "$uid" = 99 ] && pass "app runs as uid 99, not root" || fail "app runs as uid '$uid'"
owner="$(stat -c %u:%g "$work/config/config.json" 2>/dev/null)"
[ "$owner" = 99:100 ] && pass "config written as 99:100" || fail "config owned by '$owner'"

echo "== web interface"
code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8800/api/state)"
[ "$code" = 401 ] && pass "no credentials -> 401" || fail "no credentials -> $code"
code="$(curl -s -o /dev/null -w '%{http_code}' -u admin:test http://127.0.0.1:8800/api/state)"
[ "$code" = 200 ] && pass "correct credentials -> 200" || fail "correct credentials -> $code"
curl -s -u admin:test http://127.0.0.1:8800/api/state | grep -q '"in_container": true' \
  && pass "interface knows it is in a container" || fail "in_container not reported"

echo "== command line inside the container"
as_app "$NAME" python /app/ytarchive.py status >/dev/null \
  && pass "status runs as the app user" || fail "status failed"
out="$(as_app "$NAME" python /app/ytarchive.py schedule install 2>&1)"
if [ $? -ne 0 ] && echo "$out" | grep -q YTARCHIVE_INTERVAL; then
  pass "schedule install refuses, pointing at YTARCHIVE_INTERVAL"
else fail "schedule install did not refuse properly: $out"; fi

echo "== migrated archive with accented names in a different Unicode form"
# Linux filesystems compare names byte for byte, unlike macOS, so this is the
# only place the copied-from-a-Mac failure mode can be reproduced faithfully.
out="$(docker exec -i -u 99:100 "$NAME" python - <<'PYEOF'
import json, sys, unicodedata
sys.path.insert(0, "/app")
import ytarchive as ya
cfg = ya.load_config()
nfd = lambda t: unicodedata.normalize("NFD", t)
title = "Muzică veche"                           # as YouTube reports it (NFC)
vid = "AbCdEfGhIjK"
name = f"20240101 - Cântec în ploaie [{vid}].mkv"
folder = ya.download_root(cfg) / nfd(title)      # as an SMB copy can leave it
folder.mkdir(parents=True)
(folder / nfd(name)).write_bytes(b"x")
json.dump({"videos": {vid: {"file": name}}}, open(folder / "ytarchive.json", "w"))
exact = ya.download_root(cfg) / title / name
print("exact-name-lookup-finds-it", exact.exists())
print("resolves-existing-folder", ya.source_dir(cfg, title) == folder)
conn = ya.db()
conn.execute("INSERT INTO sources (id,kind,title,url,added_at,quality,container,folder)"
             " VALUES ('T','playlist',?,'',?,'720','mkv',?)", (title, ya.now(), title))
conn.commit()
print("adopted", ya.adopt_existing(conn, cfg, "T", title, {}))
PYEOF
)"
echo "$out" | sed 's/^/        /'
echo "$out" | grep -q "exact-name-lookup-finds-it False" \
  && pass "reproduced: exact names miss a re-encoded copy on Linux" \
  || fail "could not reproduce the encoding mismatch"
echo "$out" | grep -q "resolves-existing-folder True" \
  && pass "finds the copied folder despite the different encoding" \
  || fail "would have created a lookalike twin folder"
echo "$out" | grep -q "adopted 1" \
  && pass "adopts the copied video by ID - no re-download" \
  || fail "copied video not adopted"

echo "== yt-dlp auto-update, unprivileged"
before="$(docker exec "$NAME" yt-dlp --version)"
# Nightly is newer than the stable build baked in, so this must really write
# into the environment - "already up to date" would prove nothing.
out="$(as_app -e YTARCHIVE_YTDLP_CHANNEL=nightly "$NAME" python /app/ytarchive.py update-ytdlp 2>&1)"
after="$(docker exec "$NAME" yt-dlp --version)"
echo "$out" | sed 's/^/        /'
if [ "$before" != "$after" ]; then pass "upgraded as uid 99: $before -> $after"
else fail "version unchanged ($before) - the app user could not upgrade"; fi
docker exec "$NAME" test -f /opt/yt-dlp/.ytarchive-update.json \
  && pass "update time recorded" || fail "no update stamp"
docker exec "$NAME" deno --version >/dev/null \
  && pass "deno still works after the upgrade" || fail "deno broken by upgrade"

docker exec "$NAME" rm -f /opt/yt-dlp/.ytarchive-update.json
out="$(as_app "$NAME" python /app/ytarchive.py run --no-progress 2>&1)"
echo "$out" | grep -q "checking for a yt-dlp update" \
  && pass "a scheduled run updates yt-dlp when it is due" \
  || { fail "run did not trigger the update"; echo "$out"; }
out="$(as_app "$NAME" python /app/ytarchive.py run --no-progress 2>&1)"
echo "$out" | grep -q "checking for a yt-dlp update" \
  && fail "run updated again although it was not due" \
  || pass "a run does not update again within the interval"

echo "== restart"
docker restart "$NAME" >/dev/null
if wait_healthy; then pass "healthy after restart"; else fail "unhealthy after restart"; fi
[ "$(docker exec "$NAME" yt-dlp --version)" = "$after" ] \
  && pass "upgraded yt-dlp survives a restart" || fail "upgrade lost on restart"

echo
if [ "$fails" -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "$fails CHECK(S) FAILED"; fi
exit "$fails"
