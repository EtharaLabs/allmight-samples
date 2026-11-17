#!/bin/sh
set -eu
cd "$(dirname "$0")"
export PYTHONHOME="$PWD/runtime"
export PYTHONNOUSERSITE=1
export LD_LIBRARY_PATH="$PWD/lib"
exec "$PWD/lib/ld-linux-aarch64.so.1" --library-path "$PWD/lib" "$PWD/runtime/bin/python3" app.py
