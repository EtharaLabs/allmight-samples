#!/usr/bin/env python3
"""Harborview entry point: apply the schema, start the background workers, serve HTTP on $PORT."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harborview.web import main  # noqa: E402

if __name__ == "__main__":
    main()
