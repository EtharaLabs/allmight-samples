"""Functional dimension: one strict criterion per workflow (tests/workflows.toml), capped by breaches."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scoring  # noqa: E402

scoring.register_functional()
