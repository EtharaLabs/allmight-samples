#!/usr/bin/env python3
"""Authoring-time generator for tests/reference/ties.json (run once; the verifier only replays it).

Each plan is a Los Angeles two-job employee (COOK, SERVER rates) whose punches put some week of the
plan's pay period on an exact half-cent premium under a repeating regular rate (instruction R5.5:
nothing but the four amounts is rounded). Plans alternate between ties that a 28-digit Decimal and a
float rate-first computation would round the wrong way, so they discriminate exact arithmetic.

Usage: python3 gen_ties.py <out.json>
"""
import json
import random
import sys
import types
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


PLAN = {"2026-09-20": 6, "2026-10-04": 48, "2026-10-18": 6, "2026-11-01": 8}

def main(out_path: str) -> None:
    import workflows as W

    W.H = types.SimpleNamespace(model=types.SimpleNamespace(locations={1: {"timezone": "America/Los_Angeles", "ruleset": "CA"}}))
    out = {}
    for period, n in PLAN.items():
        ps = date.fromisoformat(period)
        rnd = random.Random(int(period.replace("-", "")))
        g = W.Gen(rnd, "tie")
        plans = W.tie_staff(rnd, g, n, W.weeks_of(ps))
        out[period] = [{"rates": rates, "punches": [{k: r[k] for k in ("kind", "at", "job_code") if k in r} for r in rows]}
                       for rates, rows in plans]
        print(period, len(out[period]), flush=True)
    Path(out_path).write_text(json.dumps(out, indent=0))


if __name__ == "__main__":
    main(sys.argv[1])
