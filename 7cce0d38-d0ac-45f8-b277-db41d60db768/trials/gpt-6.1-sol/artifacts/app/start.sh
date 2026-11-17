#!/bin/sh
cd "$(dirname "$0")"
export LD_LIBRARY_PATH="$PWD/runtime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONHOME="$PWD/runtime/python"
export PYTHONTZPATH="$PWD/runtime/zoneinfo"
exec "$PWD/.venv/bin/python" "$PWD/app.py"
