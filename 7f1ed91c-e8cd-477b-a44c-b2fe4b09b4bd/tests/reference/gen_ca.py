#!/usr/bin/env python3
"""Authoring-time generator for tests/reference/ca_ties.json (run once; the verifier only replays it).

Each scenario is a few statement rows and corporate actions (instruction R13) for fresh securities in up to four
accounts (0: FIFO, 1: FIFO, 2: HIFO, 3: IRA). A scenario is kept only when the exact rules and at least one of
these common wrong implementations disagree on a realized row or an open lot:

spin-offs   "decimal_alloc"  allocation fraction in 28-digit Decimal; "float_alloc" in binary floating point;
            "per_share"      allocation computed per share first (Decimal), then times the quantity;
            "whole_first"    the whole new shares take their share of the allocated basis first (the fraction gets the rest);
            "cil_once"       cash in lieu converted without rounding to the cent in its currency first;
            "after_split"    a spin-off applied before the same day's split
mergers     "loss_recognized" a loss recognized as well; "full_gain" the whole realized gain recognized;
            "value_once"     cash and stock value converted without rounding to the cent in their currency first;
            "after_trades"   the merger applied after the same day's trades
returns     "negative_basis" the basis may go below 0 (no excess row); "account_total" the distribution rounded once per
            account and spread over its lots by the share rule; "after_trades" applied after the same day's buys
IRAs        "ira_taxable"    the IRA treated as a taxable account; "ira_sale_washed" a loss sale inside the IRA matched
                             by the wash-sale rule; "ira_dividends" dividends paid in the IRA reported

Usage: python3 gen_ca.py <records.sql> <out.json>
"""
from __future__ import annotations

import importlib.util
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
from reference.gen_ties import load_rates  # noqa: E402

PLAN = {"spinoff": 34, "merger": 30, "roc": 26, "ira": 26}
LABEL_SID = {"P": 1, "N": 2, "T": 3, "A": 4, "X": 5}
IRA_ACCT = 3


def dec(x) -> Decimal:
    x = F(x)
    return Decimal(x.numerator) / Decimal(x.denominator)


def dec_rhu(x: Decimal) -> int:
    q = int(x)
    return q + (1 if x - q >= Decimal("0.5") else 0)


ORIG_ACTION = E._action


def patched_engine(name: str, old: str, new: str):
    """A copy of the reference engine with one textual change (an authoring-time emulation of a wrong reading)."""
    src = Path(E.__file__).read_text()
    assert src.count(old) == 1, old
    spec = importlib.util.spec_from_loader(f"gen_ca_{name}", loader=None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    exec(compile(src.replace(old, new), f"<{name}>", "exec"), mod.__dict__)
    return mod


ENGINE_PATCHES = {
    "ira_sale_washed": ("            if wash and not in_ira:", "            if wash:"),
    "ira_dividends": ("        if t.account in res.ira:\n            continue\n", ""),
}
_ENGINES: dict = {}


def engine_for(v: str):
    """The reference engine, or (built on first use) its copy with the wrong reading `v`."""
    if v not in ENGINE_PATCHES:
        return E
    if v not in _ENGINES:
        _ENGINES[v] = patched_engine(v, *ENGINE_PATCHES[v])
    return _ENGINES[v]


def make_action(variant: str):
    def act(t, lots, where, currency_of, rates, res, accounts_holding, carry, ira):
        getcontext().prec = 28
        p = t.params
        sec, D = t.security, t.day
        cur = currency_of[sec]
        r_cur = rates.rate(cur, D)
        idx = [0]

        def row(kind, lot, acct, rsec, qty, proceeds, basis):
            res.portions.append(E.Portion(t, idx[0], lot.acq, acct, rsec, qty, lot.holding, proceeds, basis, kind=kind,
                                          ira=acct in ira))
            idx[0] += 1

        def usd_of(qty, price):
            if variant in ("cil_once", "value_once"):
                return E.rhu(qty * price * 100 * r_cur)
            return rates.usd(E.rhu(qty * price * 100), cur, D)

        if t.kind == "spinoff":
            new, r, fp, fn, cil = p["new_security"], p["ratio"], p["parent_fmv"], p["new_fmv"], p["cash_in_lieu_price"]
            a = r * fn / (r * fn + fp)
            for acct in accounts_holding(sec):
                for lot in lots[(acct, sec)]:
                    if variant == "decimal_alloc":
                        alloc = dec_rhu(Decimal(lot.basis) * (dec(r) * dec(fn) / (dec(r) * dec(fn) + dec(fp))))
                    elif variant == "float_alloc":
                        alloc = math.floor(lot.basis * (float(r) * float(fn) / (float(r) * float(fn) + float(fp))) + 0.5)
                    elif variant == "per_share":
                        alloc = dec_rhu(Decimal(lot.basis) / dec(lot.qty) * dec(a) * dec(lot.qty))
                    else:
                        alloc = E.rhu(lot.basis * a)
                    lot.basis -= alloc
                    n = lot.qty * r
                    whole = E.floor_of(n)
                    frac = n - whole
                    if variant == "whole_first" and frac and whole:
                        b_whole = E.share(alloc, F(whole), n)
                        b_frac = alloc - b_whole
                    else:
                        b_frac = E.share(alloc, frac, n) if frac else 0
                    if frac:
                        row("cash_in_lieu", lot, acct, new, frac, usd_of(frac, cil), b_frac)
                    if whole:
                        nl = carry(lot, new, F(whole), alloc - b_frac)
                        E._insert(lots.setdefault((acct, new), []), nl)
                        where[(lot.acq, new)] = acct
            return
        if t.kind == "merger":
            acq, s, k, fa = p.get("acquirer"), p["shares_per_share"], p["cash_per_share"], p.get("acquirer_fmv")
            for acct in accounts_holding(sec):
                for lot in lots[(acct, sec)]:
                    cash = usd_of(lot.qty, k) if k else 0
                    if not s:
                        row("merger", lot, acct, sec, lot.qty, cash, lot.basis)
                        continue
                    value = usd_of(lot.qty, s * fa)
                    realized = cash + value - lot.basis
                    if variant == "loss_recognized":
                        recognized = min(realized, cash)
                    elif variant == "full_gain":
                        recognized = realized if realized > 0 else 0
                    else:
                        recognized = min(realized, cash) if realized > 0 else 0
                    if cash or recognized:
                        row("merger", lot, acct, sec, lot.qty, cash, cash - recognized)
                    nl = carry(lot, acq, lot.qty * s, lot.basis - cash + recognized)
                    E._insert(lots.setdefault((acct, acq), []), nl)
                    where[(lot.acq, acq)] = acct
                lots[(acct, sec)] = []
            return
        d = p["amount_per_share"]
        for acct in accounts_holding(sec):
            acct_lots = lots[(acct, sec)]
            if variant == "account_total":
                q_tot = sum((l.qty for l in acct_lots), F(0))
                total = usd_of(q_tot, d)
                rem_u, rem_q = total, q_tot
                parts = []
                for lot in acct_lots:
                    u = E.share(rem_u, lot.qty, rem_q)
                    rem_u, rem_q = rem_u - u, rem_q - lot.qty
                    parts.append((lot, u))
            else:
                parts = [(lot, usd_of(lot.qty, d)) for lot in acct_lots]
            for lot, u in parts:
                if variant == "negative_basis" or u <= lot.basis:
                    lot.basis -= u
                else:
                    row("return_of_capital", lot, acct, sec, lot.qty, u - lot.basis, 0)
                    lot.basis = 0
    return act


@contextmanager
def variant(name: str):
    rank0 = dict(E.KIND_RANK)
    E._action = make_action(name) if name not in ("after_split", "after_trades", "ira_taxable", *ENGINE_PATCHES) else ORIG_ACTION
    if name == "after_split":
        E.KIND_RANK["spinoff"] = -1
    if name == "after_trades":
        for k in E.CA_KINDS:
            E.KIND_RANK[k] = 4.5
    try:
        yield
    finally:
        E._action = ORIG_ACTION
        E.KIND_RANK.clear()
        E.KIND_RANK.update(rank0)


def events_of(sc):
    txns, n = [], 0
    for r in sc["rows"]:
        n += 1
        t = E.Txn(n, n, r["a"], LABEL_SID[r["s"]], r["kind"], date.fromisoformat(r["day"]),
                  F(r["qty"]) if r.get("qty") else None, r.get("amount"), tuple(r["ratio"]) if r.get("ratio") else None)
        txns.append(t)
    for i, a in enumerate(sc["actions"]):
        p = {}
        for k, v in a["terms"].items():
            p[k] = LABEL_SID[v] if k in ("new_security", "acquirer") and v else (F(v) if v is not None else None)
        txns.append(E.Txn(10_000 + i, 10_000 + i, None, LABEL_SID[a["s"]], a["kind"], date.fromisoformat(a["day"]), params=p))
    return txns


def run(sc, rates, ira_on=True, eng=E):
    txns = events_of(sc)
    cur = {sid: sc["currency"] for sid in LABEL_SID.values()}
    res = eng.compute(txns, cur, rates, eng.method_lookup({0: "FIFO", 1: "FIFO", 2: "HIFO", 3: "FIFO"}, []),
                      ira={IRA_ACCT} if ira_on else ())
    if res.errors:
        return None
    rows = sorted((p.day, p.security, p.kind, p.qty, p.holding, p.proceeds, p.basis, p.disallowed) for p in res.portions if not p.ira)
    lots = sorted((l.account, l.security, l.acq, l.qty, l.holding, l.basis) for l in res.lots)
    figs = sorted((k, f.short, f.long, f.disallowed, f.dividends) for k, f in eng.year_figures(res).items())
    return rows, lots, figs


VARIANTS = {"spinoff": ["decimal_alloc", "float_alloc", "per_share", "whole_first", "cil_once", "after_split"],
            "merger": ["loss_recognized", "full_gain", "value_once", "after_trades"],
            "roc": ["negative_basis", "account_total", "after_trades"],
            "ira": ["ira_taxable", "ira_sale_washed", "ira_dividends"]}


def differs(cat, sc, rates):
    exact = run(sc, rates)
    if exact is None or not (exact[0] or exact[1]):
        return []
    out = []
    for v in VARIANTS[cat]:
        with variant(v):
            try:
                got = run(sc, rates, ira_on=(v != "ira_taxable"), eng=engine_for(v))
            except Exception:  # noqa: BLE001
                got = None
        if got != exact:
            out.append(v)
    return out


def day(rnd, lo, hi):
    return lo + timedelta(days=rnd.randrange((hi - lo).days + 1))


QTYS = [3, 5, 6, 7, 9, 10, 11, 13, 14, 15, 18, 21, 25, F("7.5"), F("12.25"), F("0.75"), F("3.3333")]


def iso(d):
    return d.isoformat()


def money(rnd, lo, hi):
    return F(rnd.randrange(lo, hi), 100)


def scenario(cat, rnd, rates):
    cur = rnd.choice(["USD", "USD", "CAD", "EUR"])
    early = cur == "CAD" and rnd.random() < 0.5
    lo, hi = (date(2024, 1, 8), date(2024, 5, 31)) if early else (date(2024, 9, 2), date(2025, 6, 30))
    D = day(rnd, lo + timedelta(days=60), hi + timedelta(days=60))
    if rates.rate(cur, D) is None:
        return None
    rows, actions = [], []

    def buy(acct, s, d, q, amt, kind="buy"):
        rows.append({"a": acct, "s": s, "kind": kind, "day": iso(d), "qty": str(F(q)), "amount": amt})

    ratio = fp = fn = None
    tie_bases = []
    if cat == "spinoff":
        ratio = rnd.choice([F("0.1"), F("0.2"), F("0.25"), F("0.3333"), F("0.125"), F("0.5"), F("1.5"), F("0.4")])
        if rnd.random() < 0.65:  # USD, and lot bases on which basis x a is an exact half cent (R13.2 rounds once)
            cur = "USD"
            fp, fn = F(rnd.randrange(8, 300)), F(rnd.randrange(1, 90))
            a = ratio * fn / (ratio * fn + fp)
            if a.denominator % 2 or not a.numerator % 2:
                return None
            base = a.denominator // 2
            js = [j for j in range(1, 3_000_000 // base + 1, 2) if 20_000 <= base * j <= 3_000_000]
            if not js:
                return None
            tie_bases = [base * rnd.choice(js) for _ in range(4)]
        else:
            fp, fn = money(rnd, 1000, 30000), money(rnd, 200, 9000)
    if cat in ("spinoff", "roc", "merger"):
        s = {"spinoff": "P", "merger": "T", "roc": "X"}[cat]
        nlots = rnd.choice([2, 3, 4])
        accts = [rnd.choice([0, 0, 1, 2]) for _ in range(nlots)]
        for i in range(nlots):
            d = D - timedelta(days=rnd.randrange(40, 500))
            if rates.rate(cur, d) is None:
                return None
            q = F(rnd.choice(QTYS))
            per = money(rnd, 500, 20000) if cat != "roc" else money(rnd, 50, 3000)
            amt = tie_bases[i] if tie_bases else E.rhu(q * per * 100) + rnd.randrange(0, 97)
            buy(accts[i], s, d, q, amt)
        if cat == "spinoff":
            if rnd.random() < 0.3:  # a split on the same date: the spin-off counts the post-split shares
                for a in sorted(set(accts)):
                    rows.append({"a": a, "s": "P", "kind": "split", "day": iso(D), "ratio": [rnd.choice([2, 3, 4]), 1]})
            actions.append({"kind": "spinoff", "s": "P", "day": iso(D), "terms": {"new_security": "N", "ratio": str(ratio),
                            "parent_fmv": str(fp), "new_fmv": str(fn), "cash_in_lieu_price": str(fn + money(rnd, 0, 50))}})
            if rnd.random() < 0.5:  # a later sale of some new shares (beyond 30 days)
                rows.append({"a": accts[0], "s": "N", "kind": "sell", "day": iso(D + timedelta(days=rnd.randrange(40, 200))),
                             "qty": "1", "amount": E.rhu(fn * 100) + rnd.randrange(-500, 500)})
        elif cat == "merger":
            s_ = rnd.choice([F(0), F("0.25"), F("0.35"), F("0.5"), F("1.2"), F("0.18")])
            k = rnd.choice([F(0), F("2.05"), F("4.75"), F("12.5"), F("31.2")]) if s_ else rnd.choice([F("8.4"), F("31.2"), F("55.15")])
            if not s_ and not k:
                return None
            terms = {"acquirer": "A" if s_ else None, "shares_per_share": str(s_), "cash_per_share": str(k),
                     "acquirer_fmv": str(money(rnd, 1000, 25000)) if s_ else None}
            if rnd.random() < 0.25:  # a buy of the target on the merger date comes after it: it stays a lot of the target
                buy(accts[0], "T", D, rnd.choice([2, 3]), rnd.randrange(5000, 50000))
            actions.append({"kind": "merger", "s": "T", "day": iso(D), "terms": terms})
            if s_ and rnd.random() < 0.5:
                rows.append({"a": accts[0], "s": "A", "kind": "sell", "day": iso(D + timedelta(days=rnd.randrange(40, 200))),
                             "qty": "1", "amount": rnd.randrange(5000, 90000)})
        else:
            for j in range(rnd.choice([1, 2, 3])):
                dd = D + timedelta(days=60 * j)
                if rates.rate(cur, dd) is None:
                    return None
                if j == 0 and rnd.random() < 0.3:  # a buy on the distribution date is not reduced
                    buy(accts[0], "X", dd, rnd.choice([4, 5]), rnd.randrange(2000, 9000))
                actions.append({"kind": "return_of_capital", "s": "X", "day": iso(dd), "terms": {"amount_per_share": str(money(rnd, 5, 900))}})
            if rnd.random() < 0.5:
                last = date.fromisoformat(actions[-1]["day"])
                rows.append({"a": accts[0], "s": "X", "kind": "sell", "day": iso(last + timedelta(days=rnd.randrange(40, 200))),
                             "qty": "1", "amount": rnd.randrange(500, 20000)})
    else:  # ira: a taxable loss sale with replacement shares in the household's IRA (and sometimes a taxable account)
        d0 = D - timedelta(days=rnd.randrange(60, 400))
        q = F(rnd.choice(QTYS))
        per = money(rnd, 2000, 20000)
        buy(0, "X", d0, q, E.rhu(q * per * 100))
        sq = q * rnd.choice([F(1), F(1, 2), F(1, 3)])
        if (sq * 10 ** 8).denominator != 1:
            return None
        rows.append({"a": 0, "s": "X", "kind": "sell", "day": iso(D), "qty": str(sq), "amount": E.rhu(sq * per * 100 * F(rnd.randrange(50, 95), 100))})
        for k in range(rnd.choice([1, 2])):
            gap = rnd.choice([-20, -3, 0, 5, 12, 29, 30])
            acct = IRA_ACCT if k == 0 or rnd.random() < 0.5 else 1
            rq = sq * rnd.choice([F(1, 2), F(1), F(3, 2), F(1, 3)])
            if (rq * 10 ** 8).denominator != 1:
                return None
            buy(acct, "X", D + timedelta(days=gap), rq, E.rhu(rq * per * 100 * F(rnd.randrange(60, 99), 100)))
        if rnd.random() < 0.6:  # the IRA sells some of its shares later (never reported), sometimes buying again soon after
            d2 = D + timedelta(days=rnd.randrange(45, 200))
            amt = E.rhu(per * 100 * F(rnd.randrange(40, 95), 100)) if rnd.random() < 0.75 else rnd.randrange(2000, 30000)
            rows.append({"a": IRA_ACCT, "s": "X", "kind": "sell", "day": iso(d2), "qty": "1", "amount": amt})
            if rnd.random() < 0.7:
                d3 = d2 + timedelta(days=rnd.choice([0, 4, 17, 30]))
                if rates.rate(cur, d3) is None:
                    return None
                buy(rnd.choice([IRA_ACCT, 1, 1]), "X", d3, rnd.choice([1, 2, 3]), rnd.randrange(2000, 30000))
        if rnd.random() < 0.4:  # a dividend paid in the IRA (never reported)
            d4 = D + timedelta(days=rnd.randrange(31, 44))
            if rates.rate(cur, d4) is None:
                return None
            rows.append({"a": IRA_ACCT, "s": "X", "kind": "dividend", "day": iso(d4), "amount": rnd.randrange(100, 9000)})
    rows.sort(key=lambda r: (r["day"], E.KIND_RANK[r["kind"]]))
    return {"currency": cur, "rows": rows, "actions": actions}


def main(sql_path, out_path):
    rates = load_rates(sql_path)
    out = {}
    for cat, n in PLAN.items():
        rnd = random.Random(sum(map(ord, cat)) * 104729)
        got, tries, seen = [], 0, {}
        while len(got) < n and tries < 40000:
            tries += 1
            sc = scenario(cat, rnd, rates)
            if sc is None:
                continue
            dif = differs(cat, sc, rates)
            if not dif:
                continue
            # keep a spread of wrong implementations: at most n/3 scenarios caught by only the most common variant
            key = tuple(dif)
            if len(VARIANTS[cat]) > 1 and len(dif) == 1 and seen.get(key, 0) >= max(3, n // 3):
                continue
            seen[key] = seen.get(key, 0) + 1
            sc["wrong"] = dif
            got.append(sc)
        out[cat] = got
        counts = {}
        for sc in got:
            for v in sc["wrong"]:
                counts[v] = counts.get(v, 0) + 1
        print(cat, len(got), counts, tries, flush=True)
    Path(out_path).write_text(json.dumps(out, indent=0, default=str))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
