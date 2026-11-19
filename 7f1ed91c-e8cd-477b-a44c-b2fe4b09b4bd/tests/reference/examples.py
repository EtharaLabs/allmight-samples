#!/usr/bin/env python3
"""The worked examples of instruction.md, reproduced by the reference engine. Run: python3 examples.py

Importing this module does nothing; the checks run only as a script.
"""

from __future__ import annotations

import sys
from datetime import date
from fractions import Fraction as F
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # importable standalone (Reward Kit)

from reference import engine as E  # noqa: E402
from reference.engine import Txn  # noqa: E402
from reference.model import Account, Household, Sec  # noqa: E402

RATES = E.Rates([
    ("EUR", date(2025, 3, 7), "1.085000", "usd_per_unit"),
    ("CAD", date(2024, 5, 15), "1.360000", "units_per_usd"),
])

_seq = [0]


def T(kind, day, acct=1, sec=1, q=None, amt=None, ratio=None, to=None, sel=None, tid=None):
    _seq[0] += 1
    return Txn(tid or _seq[0], _seq[0], acct, sec, kind, day, F(q) if q is not None else None, amt, ratio, to, sel)


def run(txns, cur="USD", methods=None, changes=()):
    return E.compute(txns, {1: cur, 2: cur}, RATES, E.method_lookup(methods or {1: "FIFO", 2: "FIFO"}, list(changes)))


def rows(res):
    return [(p.day.isoformat(), p.account, E.qty_str(p.qty), p.holding.isoformat(), p.term, p.proceeds, p.basis,
             p.disallowed, p.gain) for p in res.portions]


def lots(res):
    return sorted((l.account, l.acq, E.qty_str(l.qty), l.holding.isoformat(), l.basis) for l in res.lots)


def ex1():
    assert RATES.usd(432140, "EUR", date(2025, 3, 9)) == 468872  # 4,321.40 EUR x 1.085 = 4,688.719
    assert RATES.usd(1840, "CAD", date(2024, 5, 15)) == 1353      # 18.40 CAD / 1.36 = 13.5294...
    assert RATES.rate("EUR", date(2025, 3, 6)) is None
    # manual entry: 3 shares at 10.004 with a 1.25 fee -> 31.262 -> 31.26 in the security's currency
    assert E.rhu((F("3") * F("10.004") + F("1.25")) * 100) == 3126


def ex2():
    _seq[0] = 0
    r = run([T("buy", date(2024, 3, 4), q=10, amt=100003), T("buy", date(2024, 6, 3), q=4, amt=50002),
             T("sell", date(2025, 4, 1), q=12, amt=150002)])
    assert rows(r) == [("2025-04-01", 1, "10", "2024-03-04", "long", 125002, 100003, 0, 24999),
                       ("2025-04-01", 1, "2", "2024-06-03", "short", 25000, 25001, 0, -1)], rows(r)
    assert lots(r) == [(1, 2, "2", "2024-06-03", 25001)], lots(r)
    # the anniversary rule (R6.7), including February 29
    assert E.term_of(date(2024, 3, 4), date(2025, 3, 4)) == "short"
    assert E.term_of(date(2024, 2, 29), date(2025, 2, 28)) == "short"
    assert E.term_of(date(2024, 2, 29), date(2025, 3, 1)) == "long"


def ex3():
    _seq[0] = 0
    txns = [T("buy", date(2025, 1, 10), q=10, amt=1200000), T("buy", date(2025, 2, 10), q=10, amt=1500000),
            T("buy", date(2025, 3, 10), q=10, amt=1350000), T("sell", date(2025, 6, 30), q=5, amt=700000),
            T("sell", date(2025, 7, 8), q=12, amt=1680000)]
    r = run(txns, changes=[(1, date(2025, 7, 1), 1, "HIFO")])
    got = [(p.day.isoformat(), p.acq, E.qty_str(p.qty), p.proceeds, p.basis, p.gain) for p in r.portions]
    assert got == [("2025-06-30", 1, "5", 700000, 600000, 100000),
                   ("2025-07-08", 2, "10", 1400000, 1500000, -100000),
                   ("2025-07-08", 3, "2", 280000, 270000, 10000)], got


def ex4():
    _seq[0] = 0
    txns = [T("buy", date(2024, 3, 4), acct=1, q=30, amt=300003), T("split", date(2024, 6, 10), acct=1, ratio=(10, 1)),
            T("transfer", date(2024, 9, 2), acct=1, to=2), T("split", date(2025, 1, 6), acct=2, ratio=(1, 8)),
            T("sell", date(2025, 3, 5), acct=2, q="37.5", amt=412550)]
    r = run(txns)
    assert rows(r) == [("2025-03-05", 2, "37.5", "2024-03-04", "long", 412550, 300003, 0, 112547)], rows(r)


def ex5():
    _seq[0] = 0
    txns = [T("buy", date(2025, 1, 6), acct=1, q=100, amt=500000),
            T("sell", date(2025, 3, 3), acct=1, q=100, amt=400000),
            T("buy", date(2025, 3, 20), acct=2, q=30, amt=123000),
            T("buy", date(2025, 3, 24), acct=1, q=80, amt=336001),
            T("buy", date(2025, 4, 17), acct=1, q=40, amt=170000)]
    r = run(txns)
    assert rows(r) == [("2025-03-03", 1, "100", "2025-01-06", "short", 400000, 500000, 100000, 0)], rows(r)
    assert lots(r) == [(1, 4, "10", "2025-03-24", 42000), (1, 4, "70", "2025-01-27", 364001),
                       (1, 5, "40", "2025-04-17", 170000), (2, 3, "30", "2025-01-23", 153000)], lots(r)


def ex6():
    _seq[0] = 0
    txns = [T("buy", date(2025, 5, 5), acct=1, q=20, amt=200000),     # tid 1: $100.00 a share
            T("buy", date(2025, 5, 20), acct=1, q=20, amt=260000),    # tid 2: $130.00 a share
            T("sell", date(2025, 6, 2), acct=1, q=10, amt=110000),    # HIFO: 10 of tid 2, loss 200.00
            T("sell", date(2025, 6, 9), acct=1, q=10, amt=115000),    # HIFO: tid 2's other 10, loss 150.00
            T("reinvest", date(2025, 6, 16), acct=2, q="0.4375", amt=5031)]
    r = run(txns, methods={1: "HIFO", 2: "FIFO"})
    assert rows(r) == [("2025-06-02", 1, "10", "2025-05-20", "short", 110000, 130000, 20000, 0),
                       ("2025-06-09", 1, "10", "2025-05-20", "short", 115000, 130000, 656, -14344)], rows(r)
    assert lots(r) == [(1, 1, "10", "2025-04-22", 120000), (1, 1, "10", "2025-05-05", 100000),
                       (2, 5, "0.4375", "2025-05-27", 5687)], lots(r)


def ex7():
    _seq[0] = 0
    h = Household(3, "Example")
    h.accounts[1] = Account(1, 3, "Brokerage", "northwind", "NW-1", "FIFO")
    h.securities[1] = Sec(1, 3, "ACME", "US0000000001", "Acme Corp", "USD")
    h.txns = [T("buy", date(2024, 2, 1), q=10, amt=100000), T("sell", date(2024, 8, 1), q=10, amt=150000)]
    h.touch()
    assert h.summary_rows(2024, RATES) == [["ACME", "Acme Corp", "50000", "0", "0", "0", "0", "0", "0"]]
    h.lock(2024, RATES)
    h.txns.append(T("buy", date(2024, 1, 15), q=10, amt=80000))  # a missed buy imported late
    h.touch()
    assert h.summary_rows(2024, RATES) == [["ACME", "Acme Corp", "50000", "0", "0", "0", "0", "0", "0"]]
    assert h.summary_rows(2025, RATES) == [["ACME", "Acme Corp", "0", "0", "0", "0", "20000", "0", "0"]], h.summary_rows(2025, RATES)
    h.lock(2025, RATES)
    h.txns.append(T("dividend", date(2024, 11, 15), amt=1234))
    h.touch()
    assert h.summary_rows(2026, RATES) == [["ACME", "Acme Corp", "0", "0", "0", "0", "0", "0", "1234"]], h.summary_rows(2026, RATES)


def CA(kind, day, sec, **params):
    _seq[0] += 1
    return Txn(_seq[0], _seq[0], None, sec, kind, day, params={k: F(v) if isinstance(v, str) else v for k, v in params.items()})


def sec_lots(res):
    return sorted((l.security, l.account, l.acq, E.qty_str(l.qty), l.acquired.isoformat(), l.holding.isoformat(), l.basis)
                  for l in res.lots)


def ex9():  # spin-off of N (security 2) from P (security 1)
    _seq[0] = 0
    r = run([T("buy", date(2024, 3, 4), q=10, amt=100003), T("buy", date(2024, 5, 6), q=7, amt=77000),
             CA("spinoff", date(2024, 8, 5), 1, new_security=2, ratio="0.5", parent_fmv="90", new_fmv="20",
                cash_in_lieu_price="21.50")])
    assert [(p.security, p.kind) for p in r.portions] == [(2, "cash_in_lieu")]
    assert rows(r) == [("2024-08-05", 1, "0.5", "2024-05-06", "short", 1075, 1100, 0, -25)], rows(r)
    assert sec_lots(r) == [(1, 1, 1, "10", "2024-03-04", "2024-03-04", 90003), (1, 1, 2, "7", "2024-05-06", "2024-05-06", 69300),
                           (2, 1, 1, "5", "2024-03-04", "2024-03-04", 10000), (2, 1, 2, "3", "2024-05-06", "2024-05-06", 6600)], sec_lots(r)


def ex10():  # T (security 1) merges into A (security 2) for stock and cash
    _seq[0] = 0
    r = run([T("buy", date(2023, 1, 10), q=10, amt=50000), T("buy", date(2023, 2, 14), q=10, amt=60000),
             CA("merger", date(2025, 6, 16), 1, acquirer=2, shares_per_share="0.5", cash_per_share="12.00", acquirer_fmv="80")])
    assert rows(r) == [("2025-06-16", 1, "10", "2023-01-10", "long", 12000, 10000, 0, 2000),
                       ("2025-06-16", 1, "10", "2023-02-14", "long", 12000, 12000, 0, 0)], rows(r)
    assert sec_lots(r) == [(2, 1, 1, "5", "2023-01-10", "2023-01-10", 40000), (2, 1, 2, "5", "2023-02-14", "2023-02-14", 48000)], sec_lots(r)


def ex11():  # two returns of capital
    _seq[0] = 0
    r = run([T("buy", date(2025, 2, 3), q=100, amt=4000), CA("return_of_capital", date(2025, 4, 1), 1, amount_per_share="0.25"),
             CA("return_of_capital", date(2025, 7, 1), 1, amount_per_share="0.25")])
    assert rows(r) == [("2025-07-01", 1, "100", "2025-02-03", "short", 1000, 0, 0, 1000)], rows(r)
    assert lots(r) == [(1, 1, "100", "2025-02-03", 0)], lots(r)


def ex12():  # a taxable loss replaced in an IRA (account 2)
    _seq[0] = 0
    txns = [T("buy", date(2025, 1, 6), acct=1, q=10, amt=40000), T("sell", date(2025, 3, 3), acct=1, q=10, amt=35000),
            T("buy", date(2025, 3, 10), acct=2, q=10, amt=30000)]
    r = E.compute(txns, {1: "USD"}, RATES, E.method_lookup({1: "FIFO", 2: "FIFO"}, []), ira={2})
    assert rows(r) == [("2025-03-03", 1, "10", "2025-01-06", "short", 35000, 40000, 5000, 0)], rows(r)
    assert lots(r) == [(2, 3, "10", "2025-03-10", 30000)], lots(r)
    assert not r.portions[0].ira and E.year_figures(r)[(2025, 1)].disallowed == 5000


if __name__ == "__main__":
    for fn in (ex1, ex2, ex3, ex4, ex5, ex6, ex7, ex9, ex10, ex11, ex12):
        fn()
        print("ok", fn.__name__)
