#!/usr/bin/env python3
"""Authoring-time generator for tests/reference/ties.json (run once; the verifier only replays it).

Each entry is a (fee_cents, deposit_bp) pair whose deposit, fee x bp / 10000 (instruction R3.4), lands
exactly on half a cent, and where three approximate computations disagree with the rule:

  * round-half-to-even (Python round, Decimal's default context) rounds the tie down;
  * a float rate-first product, fee * (bp / 10000), falls just below the half cent;
  * half of the deposit for the 2-24 h band (R7.1, "half of it", rounded half up) differs from half of
    the unrounded deposit, and the deposit is odd with an even half so half-to-even also fails there.

Usage: python3 gen_ties.py <out.json>
"""
import json
import sys
from pathlib import Path


def half_up(num: int, den: int) -> int:
    q, r = divmod(num, den)
    return q + (1 if 2 * r >= den else 0)


def classes(fee: int, bp: int) -> dict:
    exact_num = fee * bp
    dep = half_up(exact_num, 10000)
    flt = fee * (bp / 10000)
    return {
        "tie": exact_num % 10000 == 5000,
        "banker_wrong": round(exact_num / 10000) != dep,
        "float_rate_first_wrong": int(flt + 0.5) != dep,
        "half_banker_wrong": round(dep / 2) != half_up(dep, 2),
        "half_from_fee_wrong": half_up(exact_num, 20000) != half_up(dep, 2),
        "deposit": dep,
    }


def main(out_path: str) -> None:
    picks = []
    for fee in range(4005, 30000, 5):
        for bp in range(525, 4000, 25):
            c = classes(fee, bp)
            if c["tie"] and c["banker_wrong"] and c["float_rate_first_wrong"] and c["half_banker_wrong"] and c["half_from_fee_wrong"]:
                picks.append({"fee": fee, "bp": bp, "deposit": c["deposit"], "half": half_up(c["deposit"], 2)})
    step = max(1, len(picks) // 160)
    chosen = picks[::step][:160]
    Path(out_path).write_text(json.dumps({"pairs": chosen, "found": len(picks)}, indent=0))
    print(f"{len(picks)} qualifying pairs; kept {len(chosen)}")


if __name__ == "__main__":
    main(sys.argv[1])
