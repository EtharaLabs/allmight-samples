#!/usr/bin/env python3
"""Authoring-time generator for tests/reference/ties.json (run once; the verifier only replays it).

Each scenario is a handful of statement rows for one fresh security in one to three accounts. Every scenario
puts some rounding point of instruction R5 (a USD conversion, the share rule over a basis, over proceeds, or
over a wash-sale loss) on an exact half cent, where two common approximate implementations round the other
way:

* "float": binary floating point, per-share values first (basis / quantity * shares), rates as floats;
* "decimal": Python Decimal with 28 digits, per-share values first and rates normalized to USD per unit
  (units_per_usd rows inverted once).

A scenario is kept only when the exact rules and at least one approximation disagree on a realized row or an
open lot. Dates and accounts are fixed per category so each rule workflow can plant its own share.

Usage: python3 gen_ties.py <records.sql> <out.json>
"""
from __future__ import annotations

import json
import math
import random
import sys
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal, getcontext
from fractions import Fraction as F
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reference import engine as E  # noqa: E402

PLAN = {"fx": 40, "share": 40, "split": 24, "transfer": 20, "wash": 40, "dividend": 24}


def load_rates(sql_path: str) -> E.Rates:
    rows, on = [], False
    for line in Path(sql_path).read_text().splitlines():
        if line.startswith("COPY fx_rates"):
            on = True
            continue
        if on and line == "\\.":
            break
        if on:
            c, d, v, q = line.split("\t")
            rows.append((c, date.fromisoformat(d), F(v), q))
    return E.Rates(rows)


def _dec_rhu(x: Decimal) -> int:
    q = int(x)  # x >= 0
    return q + (1 if x - q >= Decimal("0.5") else 0)


@contextmanager
def variant(name: str, rates: E.Rates):
    getcontext().prec = 28
    share0, usd0 = E.share, E.Rates.usd
    raw = {c: [(d, r) for d, r in v] for c, v in rates.by.items()}

    def share(amount, m, q):
        if m == q:
            return amount
        if name == "float":
            return math.floor(float(amount) / float(q) * float(m) + 0.5)
        per = Decimal(amount) / (Decimal(q.numerator) / Decimal(q.denominator))
        return _dec_rhu(per * (Decimal(m.numerator) / Decimal(m.denominator)))

    def usd(self, cents, cur, d):
        r = self.rate(cur, d)
        if r is None:
            raise ValueError("no rate")
        if name == "float":
            return math.floor(cents * float(r) + 0.5)
        dr = Decimal(r.numerator) / Decimal(r.denominator)  # units_per_usd rows were inverted once (28 digits)
        return _dec_rhu(Decimal(cents) * dr)

    E.share, E.Rates.usd = share, usd
    try:
        yield
    finally:
        E.share, E.Rates.usd = share0, usd0
    del raw


def run(rows: list[dict], cur: str, rates: E.Rates, methods: list[str]):
    txns = []
    for i, r in enumerate(rows, start=1):
        t = E.Txn(i, i, r["a"], 1, r["kind"], date.fromisoformat(r["day"]), F(r["qty"]) if r.get("qty") else None,
                  r.get("amount"), tuple(r["ratio"]) if r.get("ratio") else None, r.get("to"))
        txns.append(t)
    res = E.compute(txns, {1: cur}, rates, E.method_lookup({i: m for i, m in enumerate(methods)}, []))
    if res.errors:
        return None
    return (sorted((p.day, p.index, p.proceeds, p.basis, p.disallowed, p.holding) for p in res.portions),
            sorted((l.account, l.acq, l.qty, l.basis, l.holding) for l in res.lots),
            sorted(u for _t, u in res.dividends))


def differs(rows, cur, rates, methods) -> list[str]:
    exact = run(rows, cur, rates, methods)
    if exact is None or not exact[0] and not exact[2]:
        return []
    out = []
    for v in ("float", "decimal"):
        with variant(v, rates):
            try:
                got = run(rows, cur, rates, methods)
            except Exception:
                got = None
        if got != exact:
            out.append(v)
    return out


def day(rnd: random.Random, lo: date, hi: date) -> date:
    return lo + timedelta(days=rnd.randrange((hi - lo).days + 1))


def iso(d: date) -> str:
    return d.isoformat()


def tie_cents(rates, cur, d, rnd, lo=2000, hi=900000):
    """An amount whose USD value is an exact half cent on that day (or None). With the rate n/d in lowest
    terms, c * n / d has denominator 2 exactly when d is even and c is (d/2) times an odd number."""
    r = rates.rate(cur, d)
    if r is None or r.denominator % 2:
        return None
    base = r.denominator // 2
    ks = [k for k in range(1, hi // base + 1, 2) if lo <= base * k <= hi]
    return base * rnd.choice(ks) if ks else None


QTYS = [3, 6, 7, 9, 12, 14, 15, 18, 21, F("4.5"), F("10.5"), F("13.5"), F("0.75"), F("1.5")]


def scenario(cat: str, rnd: random.Random, rates: E.Rates) -> tuple[list[dict], str, list[str]] | None:
    cur = rnd.choice(["USD", "USD", "EUR", "CAD"]) if cat not in ("fx", "dividend") else rnd.choice(["CAD", "EUR", "CAD"])
    methods = ["FIFO", "FIFO", "HIFO"]
    lo, hi = (date(2024, 1, 8), date(2024, 6, 14)) if cur == "CAD" and rnd.random() < 0.6 else (date(2025, 1, 6), date(2025, 6, 30))
    d0 = day(rnd, lo, hi)
    rows = []
    amt = lambda d, lo_=20000, hi_=900000: (tie_cents(rates, cur, d, rnd, lo_, hi_) if cur != "USD" and rnd.random() < 0.7  # noqa: E731
                                            else rnd.randrange(lo_, hi_))
    if cat in ("fx", "dividend"):
        a = rnd.choice([0, 2])
        q1, q2 = rnd.choice(QTYS), rnd.choice(QTYS)
        d1 = d0 + timedelta(days=rnd.randrange(1, 20))
        rows += [{"a": a, "kind": "buy", "day": iso(d0), "qty": str(q1), "amount": amt(d0)},
                 {"a": a, "kind": "buy", "day": iso(d1), "qty": str(q2), "amount": amt(d1)}]
        if cat == "dividend":
            for k in range(3):
                dd = d0 + timedelta(days=30 + 41 * k + rnd.randrange(9))
                c = tie_cents(rates, cur, dd, rnd, 300, 90000)
                if c is None:
                    return None
                rows.append({"a": a, "kind": "dividend", "day": iso(dd), "amount": c})
                if rnd.random() < 0.5:
                    rows.append({"a": a, "kind": "reinvest", "day": iso(dd), "qty": str(F(rnd.randrange(1, 9999), 10000)), "amount": c})
        ds = d1 + timedelta(days=rnd.randrange(40, 400))
        sq = F(q1) + F(q2) * rnd.choice([F(1, 3), F(1, 2), F(2, 3), F(0)])
        rows.append({"a": a, "kind": "sell", "day": iso(ds), "qty": str(sq), "amount": amt(ds)})
    elif cat in ("share", "split", "transfer"):
        a = rnd.choice([0, 2])
        lots = rnd.choice([2, 3])
        dd = d0
        tot = F(0)
        for k in range(lots):
            q = F(rnd.choice(QTYS))
            rows.append({"a": a, "kind": "buy", "day": iso(dd), "qty": str(q), "amount": amt(dd, 1001, 300000)})
            tot += q
            dd += timedelta(days=rnd.randrange(2, 25))
        if cat == "split":
            ratio = rnd.choice([(2, 1), (3, 1), (1, 2), (1, 4), (10, 1), (1, 5)])
            rows.append({"a": a, "kind": "split", "day": iso(dd + timedelta(days=3)), "ratio": list(ratio)})
            tot = tot * ratio[0] / ratio[1]
            dd += timedelta(days=40)
        if cat == "transfer":
            dest = 1
            rows.append({"a": a, "kind": "transfer", "day": iso(dd + timedelta(days=5)), "to": dest})
            a = dest
            dd += timedelta(days=10)
        for k in range(rnd.choice([1, 2])):
            frac = rnd.choice([F(1, 2), F(1, 3), F(2, 3), F(1, 6), F(5, 6), F(3, 4), F(1, 4)])
            sq = tot * frac
            if (sq * 10 ** 8).denominator != 1 or sq <= 0:
                return None
            dd += timedelta(days=rnd.randrange(35, 200))
            rows.append({"a": a, "kind": "sell", "day": iso(dd), "qty": str(sq), "amount": amt(dd, 1001, 400000)})
            tot -= sq
    elif cat == "wash":
        a, b = rnd.choice([(0, 1), (2, 0), (0, 0), (2, 2)])
        q = F(rnd.choice(QTYS))
        rows.append({"a": a, "kind": "buy", "day": iso(d0), "qty": str(q), "amount": amt(d0, 50000, 400000)})
        D = d0 + timedelta(days=rnd.randrange(35, 300))
        sq = q * rnd.choice([F(1), F(1, 2), F(2, 3), F(1, 3)])
        loss_amt = amt(D, 1001, 40000)
        rows.append({"a": a, "kind": "sell", "day": iso(D), "qty": str(sq), "amount": loss_amt})
        gap = rnd.choice([-20, -5, 3, 12, 30, 30, 29, -30, 31])
        rq = sq * rnd.choice([F(1, 2), F(1, 3), F(2, 3), F(5, 6), F(1), F(3, 2)])
        if (rq * 10 ** 8).denominator != 1:
            return None
        rd = D + timedelta(days=gap)
        rows.append({"a": b, "kind": "buy", "day": iso(rd), "qty": str(rq), "amount": amt(rd, 1001, 200000)})
        rows.sort(key=lambda r: (r["day"], E.KIND_RANK[r["kind"]]))
        if rnd.random() < 0.6:
            q2 = rq * rnd.choice([F(1, 2), F(1, 3), F(1)])
            if (q2 * 10 ** 8).denominator != 1:
                return None
            rows.append({"a": b, "kind": "sell", "day": iso(max(D, rd) + timedelta(days=rnd.randrange(35, 250))),
                         "qty": str(q2), "amount": amt(D, 50000, 300000)})
    if any(r.get("amount") is None and r["kind"] not in ("split", "transfer") for r in rows):
        return None
    if any(r.get("qty") and (F(r["qty"]) * 10 ** 8).denominator != 1 for r in rows):
        return None
    return rows, cur, methods


def main(sql_path: str, out_path: str) -> None:
    rates = load_rates(sql_path)
    out: dict[str, list] = {}
    for cat, n in PLAN.items():
        rnd = random.Random(sum(map(ord, cat)) * 7919)
        got, tries = [], 0
        need = {"float": n // 3, "decimal": n // 2}
        have = {"float": 0, "decimal": 0}
        while len(got) < n and tries < 60000:
            tries += 1
            sc = scenario(cat, rnd, rates)
            if sc is None:
                continue
            rows, cur, methods = sc
            dif = differs(rows, cur, rates, methods)
            if not dif:
                continue
            if "decimal" not in dif and have["float"] >= need["float"]:
                continue
            for v in dif:
                have[v] += 1
            got.append({"currency": cur, "methods": methods, "rows": rows, "approximations_wrong": dif})
        out[cat] = got
        print(cat, len(got), have, tries, flush=True)
    Path(out_path).write_text(json.dumps(out, indent=0, default=str))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
