#!/bin/sh
set -eu
cd "$(dirname "$0")"
export PYTHONHOME="$PWD/runtime"
exec ./runtime/lib/native/ld-linux-aarch64.so.1 --library-path "$PWD/runtime/lib/native" ./runtime/bin/python app.py
