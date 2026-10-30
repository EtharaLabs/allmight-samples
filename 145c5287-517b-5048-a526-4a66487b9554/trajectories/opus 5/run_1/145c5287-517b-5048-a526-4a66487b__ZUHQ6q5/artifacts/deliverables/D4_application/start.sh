#!/bin/sh
# Bring up the Ledgerline review application in the foreground.
# Needs nothing but /app and /app/inputs; makes no network connection.
set -eu
cd /app
exec python3 -m uvicorn --app-dir /app --host 127.0.0.1 --port 8080 --log-level warning app:app
