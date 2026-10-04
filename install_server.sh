#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
command -v python3 >/dev/null || { printf '%s\n' 'Install Python 3.11 first.'; exit 1; }
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    python3 setup_server.py
    docker compose up -d --build
else
    command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null || {
        printf '%s\n' 'Install FFmpeg first: sudo apt-get install ffmpeg python3-venv'; exit 1;
    }
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    .venv/bin/python setup_server.py
    printf '%s\n' 'Setup complete. Start: .venv/bin/python -m app.main'
fi
