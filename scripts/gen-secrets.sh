#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import secrets
for name in ("TELEGRAM_WEBHOOK_SECRET", "ADMIN_TOKEN"):
    print(f"{name}={secrets.token_urlsafe(32).replace('.', '_')}")
PY
