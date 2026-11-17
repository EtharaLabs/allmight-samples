#!/bin/sh
cd "$(dirname "$0")"
export LD_LIBRARY_PATH="$PWD/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONTZPATH="$PWD/zoneinfo"
exec .venv/bin/python app.py
