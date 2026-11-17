#!/usr/bin/env python3
"""Pickpath entry point: apply the schema, start background workers, serve HTTP on $PORT."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pickpath.web import main  # noqa: E402

if __name__ == "__main__":
    main()
