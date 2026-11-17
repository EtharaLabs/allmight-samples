#!/usr/bin/env python3
"""Authoring-time generator for tests/reference/ties.json (run once; the verifier only replays it).

Each fixture event is a set of six section prices (sections PA..PF of the verifier's pricing venue),
a facility fee and six promo codes. Each fixture order picks seats per section and codes such that the
exact rules of instruction R9 (integer half-up rounding, largest remainder on exact remainders, ties to
the first seat in seat order) give a different ticket breakdown than at least one natural approximate
implementation:
  decimal  - allocation shares computed as 28-digit Decimals (remainders compared as Decimals)
  float    - allocation shares computed as floats
  bankers  - every half-up rounding done as round-half-even (Python round())
  floatall - everything in floats: fee/tax/discount via floor(x + 0.5), float shares
Usage: python3 gen_ties.py <out.json>
"""
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SECTIONS = ["PA", "PB", "PC", "PD", "PE", "PF"]


def floatall_lines(lines_in, fee, codes):
    faces = [f for _, f in lines_in]
    sub = sum(faces)
    pct = [c for c in codes if c.kind == "percent"]
    fix = [c for c in codes if c.kind == "fixed"]
    d1 = math.floor(sub * (pct[0].value / 100) + 0.5) if pct else 0
    d2 = min(fix[0].value, sub - d1) if fix else 0

    def alloc(total, w):
        W = sum(w)
        shares = [total * x / W for x in w]
        fl = [math.floor(s) for s in shares]
        rem = [s - f for s, f in zip(shares, fl)]
        left = total - sum(fl)
        for i in sorted(range(len(w)), key=lambda i: (-rem[i], i))[:max(0, left)]:
            fl[i] += 1
        return fl
    d = alloc(d1 + d2, faces)
    s = [math.floor(f * 0.075 + 0.5) for f in faces]
    base = [f - di + si for f, di, si in zip(faces, d, s)]
    tax = math.floor(sum(base) * 0.08875 + 0.5)
    t = alloc(tax, base)
    return [f - di + si + fee + ti for f, di, si, ti in zip(faces, d, s, t)]


def main(out_path: str) -> None:
    from reference import money as M
    rnd = random.Random(20261110)
    events = []
    totals = {"decimal": 0, "float": 0, "bankers": 0, "floatall": 0}
    want = {"decimal": 8, "float": 8, "bankers": 8, "floatall": 4}

    def breaks(lines_in, fee, used):
        exact = [l.total for l in M.quote(lines_in, fee, used).lines]
        bad = []
        if M.approx_quote(lines_in, fee, used, "decimal") != exact:
            bad.append("decimal")
        if M.approx_quote(lines_in, fee, used, "float") != exact:
            bad.append("float")
        if M.bankers_quote(lines_in, fee, used) != exact:
            bad.append("bankers")
        if floatall_lines(lines_in, fee, used) != exact:
            bad.append("floatall")
        return bad

    for ei in range(4):
        prices = {}
        for code in SECTIONS:
            p = rnd.randrange(1500, 9600, 5)
            if rnd.random() < 0.35:
                p = p - p % 40 + 20          # 7.5 % of the price ends in exactly half a cent
            prices[code] = p
        fee = rnd.choice([0, 125, 150, 175, 200, 250, 300])
        codes = [{"code": f"E{ei}PCT{j}", "kind": "percent", "value": rnd.choice([5, 7, 10, 12, 15, 17, 20, 25, 30, 33, 35])}
                 for j in range(3)]
        orders, got = [], {k: 0 for k in totals}
        tries = 0
        while any(got[k] < want[k] for k in want) and tries < 20000:
            tries += 1
            n = rnd.randint(2, 6)
            secs = sorted(rnd.sample(SECTIONS, rnd.randint(2, min(4, n))))
            counts = {x: 1 for x in secs}
            for _ in range(n - len(secs)):
                counts[rnd.choice(secs)] += 1
            lines_in = [(f"{x}-A-{k + 1}", prices[x]) for x in sorted(counts) for k in range(counts[x])]
            sub = sum(f for _, f in lines_in)
            pct = rnd.choice([None, None, codes[0], codes[1], codes[2]])
            pobj = [M.Code(pct["code"], "percent", pct["value"])] if pct else []
            need = [k for k in want if got[k] < want[k]]
            chosen = None
            if set(need) & {"decimal", "float"}:
                start_v = rnd.randrange(100, 2000)
                for v in list(range(start_v, min(3000, sub))) + list(range(100, start_v)):
                    fobj = M.Code(f"E{ei}T{len(orders)}", "fixed", v)
                    bad = breaks(lines_in, fee, pobj + [fobj])
                    if set(bad) & set(need):
                        chosen = (v, bad)
                        break
            if chosen is None:
                fx = rnd.choice([None, rnd.randrange(100, 3000, 5)])
                fobj = [M.Code(f"E{ei}T{len(orders)}", "fixed", fx)] if fx else []
                bad = breaks(lines_in, fee, pobj + fobj)
                if not set(bad) & set(need):
                    continue
                chosen = (fx, bad)
            v, bad = chosen
            for k in bad:
                got[k] += 1
                totals[k] += 1
            order = {"seats": counts, "percent": pct["code"] if pct else None, "breaks": bad}
            if v:
                order["fixed"] = {"code": f"E{ei}T{len(orders)}", "value": v}
            orders.append(order)
        events.append({"prices": prices, "fee": fee, "codes": codes, "orders": orders})
        print(f"event {ei}: {len(orders)} orders after {tries} tries {got}", flush=True)
    Path(out_path).write_text(json.dumps({"events": events, "breaks": totals}, indent=0))
    print("total breaks", totals)


if __name__ == "__main__":
    main(sys.argv[1])
