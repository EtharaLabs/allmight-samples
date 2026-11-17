#!/usr/bin/env python3
"""Shiftbook entry point: apply the schema, start the outbox sender, serve HTTP on $PORT."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shiftbook.web import main  # noqa: E402

if __name__ == "__main__":
    main()
