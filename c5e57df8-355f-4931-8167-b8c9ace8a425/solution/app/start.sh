#!/usr/bin/env bash
# Starts Pickpath in the foreground on $PORT. Everything it needs is already in this workspace.
set -euo pipefail
cd "$(dirname "$0")"
exec ./.venv/bin/python -u app.py
