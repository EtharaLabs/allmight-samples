#!/usr/bin/env python3
"""Seatwise workflows. Each workflow is strict: it passes only when every substep passes.

Run: python3 workflows.py --out /logs/verifier/results.json [--only W01,W05]

Order matters only where workflows read each other's shared results (W25 restarts the app and re-checks
W05/W15 records; W26 scans for secrets every earlier workflow handled; W27 stops the database last).
Every workflow builds its own organization, venue and events, so rule workflows are isolated; the
old-platform workflows (W21-W23) run after every workflow that touches an old event (W10, W13, W14),
and the model accounts for those touches.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import wf_a  # noqa: E402,F401  (W01-W05)
import wf_b  # noqa: E402,F401  (W06-W10)
import wf_c  # noqa: E402,F401  (W11-W16)
import wf_d  # noqa: E402,F401  (W17-W27)
from kitlib.wf import REGISTRY, main_guard, run_all  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--toml", action="store_true", help="print workflows.toml from the decorators and exit")
    a = ap.parse_args()
    if a.toml:
        print(toml())
        return
    harness.CURRENT[0] = harness.Harness()
    shared: dict = {}
    run_all(a.out, shared, set(x for x in a.only.split(",") if x) or None)


def toml() -> str:
    out = ["# One strict workflow per entry: the workflow scores only when every substep passes (tests/scoring.py).",
           "# critical = core business correctness: a failed critical workflow caps every workflow at critical_cap.", "",
           "[scoring]", "critical_cap = 0.4", ""]
    for w in sorted(REGISTRY, key=lambda w: w.order):
        title = w.title.replace('"', '\\"')
        out += ["[[workflow]]", f'id = "{w.wid}"', f'title = "{title}"', f"weight = {int(w.weight)}",
                f"critical = {'true' if w.critical else 'false'}", ""]
    return "\n".join(out)


if __name__ == "__main__":
    main_guard(main)
