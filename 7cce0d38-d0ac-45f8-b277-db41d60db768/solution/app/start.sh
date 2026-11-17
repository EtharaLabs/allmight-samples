#!/usr/bin/env bash
# Starts Seatwise in the foreground on 0.0.0.0:$PORT. Everything it needs is already in this workspace.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1
exec ./.venv/bin/python -u app.py
