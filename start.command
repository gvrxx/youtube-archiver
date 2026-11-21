#!/bin/bash
# Double-click in Finder to start ytarchive and open its interface.
# It checks for a yt-dlp update on the way in, if one is due.
cd "$(dirname "$0")" || exit 1
# Finder starts scripts with a minimal PATH that leaves out Homebrew.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
python3 ytarchive.py ui
code=$?
if [ "$code" -ne 0 ]; then
  echo
  echo "ytarchive stopped with an error (code $code). Press Enter to close."
  read -r
fi
