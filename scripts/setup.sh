#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
command -v docker >/dev/null 2>&1 || { echo "Docker is required." >&2; exit 1; }
docker compose up -d --build
printf '\nMistral Messenger Assistant is running.\n'
printf 'Local setup page: http://127.0.0.1:8080/setup\n'
printf 'For Telegram webhooks, publish port 8080 through HTTPS and enter that public base URL during setup.\n'
