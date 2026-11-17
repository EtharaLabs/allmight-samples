#!/usr/bin/env python3
"""Seatwise entry point: apply schema additions, start background work, serve HTTP on $PORT."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seatwise.web import main  # noqa: E402

if __name__ == "__main__":
    main()
