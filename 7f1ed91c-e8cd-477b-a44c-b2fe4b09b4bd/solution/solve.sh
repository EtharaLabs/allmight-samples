#!/usr/bin/env bash
# Oracle: install the reference application into the workspace with its dependencies vendored.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p /app
cp -R "$HERE/app/." /app/
cd /app
python3 -m venv --copies .venv
# Dependencies install offline from the unmodified wheels in solution/wheels (CPython 3.12, linux x86_64
# and aarch64; see wheels/THIRD_PARTY_NOTICES.md), so the reference solution needs no network. Only if that
# fails does it fall back to the package index, with retries.
if ! ./.venv/bin/pip install --no-cache-dir --disable-pip-version-check -q --no-index --find-links "$HERE/wheels" -r requirements.txt; then
  for attempt in 1 2 3 4 5; do
    ./.venv/bin/pip install --no-cache-dir --disable-pip-version-check --retries 10 --timeout 60 -q -r requirements.txt && break
    [ "$attempt" = 5 ] && { echo "solve.sh: dependency install failed after 5 attempts" >&2; exit 1; }
    sleep $((attempt * 15))
  done
fi
chmod 755 start.sh
