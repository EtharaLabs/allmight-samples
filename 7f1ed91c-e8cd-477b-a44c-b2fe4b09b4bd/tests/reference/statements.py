"""Broker statement files in the two formats of instruction.md R4 (Northwind Securities, Harbor Direct).

Each builder takes statement rows (dicts) and returns (bytes, [physical line number of each data row]).
A row dict: kind (buy|sell|dividend|reinvest|split), day (date), symbol, isin, name, currency, quantity
(Fraction, positive), amount (currency cents, non-negative), ratio ((new, old) for split), ref, and optional
description override.
"""

from __future__ import annotations

import csv
import io
import random
from datetime import date, timedelta
from fractions import Fraction

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # importable standalone (Reward Kit)

from reference.engine import qty_str  # noqa: E402

NW_HEADER = ["Trade Date", "Settlement Date", "Activity", "Symbol", "Description", "Quantity", "Price", "Commission",
             "Net Amount", "Currency", "Reference"]
HD_HEADER = ["Booking date", "Value date", "Transaction", "ISIN", "Name", "Shares", "Price", "Fees", "Amount",
             "Currency", "Transaction no."]
NW_KIND = {"buy": "BUY", "sell": "SELL", "dividend": "DIVIDEND", "reinvest": "REINVEST", "split": "SPLIT"}
HD_KIND = {"buy": "Purchase", "sell": "Sale", "dividend": "Dividend", "reinvest": "Dividend reinvestment", "split": "Split"}


def _group(int_part: str, sep: str) -> str:
    out = []
    while len(int_part) > 3:
        out.insert(0, int_part[-3:])
        int_part = int_part[:-3]
    out.insert(0, int_part)
    return sep.join(out)


def fmt_number(value: Fraction, *, decimals: int | None, thousands: str, point: str, grouping: bool = True) -> str:
    """Unsigned decimal string. decimals=None writes the exact decimal (no trailing zeros)."""
    v = abs(Fraction(value))
    if decimals is None:
        s = qty_str(v)
    else:
        n = round(v * 10 ** decimals)  # callers pass values that are exact at this precision
        s = f"{n // 10 ** decimals}" + (f".{n % 10 ** decimals:0{decimals}d}" if decimals else "")
    whole, _, frac = s.partition(".")
    whole = _group(whole, thousands) if grouping else whole
    return whole + (point + frac if frac else "")


def nw_num(value: Fraction, decimals: int | None, negative: bool, grouping: bool = True) -> str:
    s = fmt_number(value, decimals=decimals, thousands=",", point=".", grouping=grouping)
    return f"({s})" if negative else s


def hd_num(value: Fraction, decimals: int | None, negative: bool, grouping: bool = True) -> str:
    s = fmt_number(value, decimals=decimals, thousands=".", point=",", grouping=grouping)
    return f"-{s}" if negative else s


def _price(row) -> Fraction:
    if row.get("quantity"):
        return Fraction(row["amount"], 100) / Fraction(row["quantity"])
    return Fraction(0)


def _business(d: date, n: int) -> date:
    while n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _write(lines_before: list[str], header: list[str], rows: list[list[str]], footer: list[str], delim: str,
           crlf: bool, bom: bool) -> tuple[bytes, list[int]]:
    eol = "\r\n" if crlf else "\n"
    out = io.StringIO()
    line_no = 0
    for l in lines_before:
        out.write(l + eol)
        line_no += 1
    w = csv.writer(out, delimiter=delim, lineterminator=eol, quoting=csv.QUOTE_MINIMAL)
    w.writerow(header)
    line_no += 1
    numbers = []
    for r in rows:
        w.writerow(r)
        line_no += 1
        numbers.append(line_no)
    for l in footer:
        out.write(l + eol)
    text = out.getvalue()
    data = text.encode("utf-8")
    return (b"\xef\xbb\xbf" + data if bom else data), numbers


def northwind(account_number: str, rows: list[dict], rnd: random.Random | None = None, crlf: bool = True,
              bom: bool = False, pad_to: int | None = None, account_line: str | None = None) -> tuple[bytes, list[int]]:
    rnd = rnd or random.Random(1)
    days = [r["day"] for r in rows] or [date(2026, 1, 1)]
    d0, d1 = min(days), max(days)
    before = ["Northwind Securities", "Account Activity Statement",
              account_line if account_line is not None else f"Account Number: {account_number}",
              f"Statement Period: {d0:%m/%d/%Y} to {d1:%m/%d/%Y}", ""]
    out_rows = []
    total = 0
    for r in rows:
        k = r["kind"]
        act = NW_KIND[k]
        if rnd.random() < 0.2:
            act = act.title() if rnd.random() < 0.5 else act.lower()
        q, amt = r.get("quantity"), r.get("amount")
        if k == "split":
            qcell, net, price = f"{r['ratio'][0]}:{r['ratio'][1]}", "0.00", ""
        elif k == "dividend":
            qcell, net, price = "", nw_num(Fraction(amt, 100), 2, False), ""
            total += amt
        else:
            neg_amt = k in ("buy", "reinvest")
            qcell = nw_num(Fraction(q), None, k == "sell")
            net = nw_num(Fraction(amt, 100), 2, neg_amt)
            price = f"{float(_price(r)):.4f}"  # informational only (R4.4)
            total += -amt if neg_amt else amt
        desc = r.get("description") or r["name"]
        out_rows.append([r["day"].strftime("%m/%d/%Y"), _business(r["day"], 2).strftime("%m/%d/%Y"), act, r["symbol"],
                         desc, qcell, price, "0.00" if k in ("dividend", "split") else f"{rnd.choice([0, 4.95, 9.99]):.2f}",
                         net, r["currency"], r["ref"]])
    footer = [f'Total Net Amount,,,,,,,,"{nw_num(Fraction(abs(total), 100), 2, total < 0)}",,',
              f"End of statement. Generated {d1 + timedelta(days=3):%m/%d/%Y} 08:14 ET."]
    data, nums = _write(before, NW_HEADER, out_rows, footer, ",", crlf, bom)
    if pad_to is not None:
        data, nums = _pad(data, nums, pad_to, crlf)
    return data, nums


def harbor(depot: str, rows: list[dict], rnd: random.Random | None = None, crlf: bool = True, bom: bool = True,
           pad_to: int | None = None, account_line: str | None = None) -> tuple[bytes, list[int]]:
    rnd = rnd or random.Random(2)
    days = [r["day"] for r in rows] or [date(2026, 1, 1)]
    d1 = max(days)
    before = ["Harbor Direct Bank", "Transaction export",
              account_line if account_line is not None else f"Depot: {depot}",
              f"Created: {d1 + timedelta(days=2):%d.%m.%Y} 09:12", ""]
    out_rows = []
    balance = 0
    for r in rows:
        k = r["kind"]
        q, amt = r.get("quantity"), r.get("amount")
        if k == "split":
            scell, acell, price, fees = f"{r['ratio'][0]}:{r['ratio'][1]}", "", "", ""
        elif k == "dividend":
            scell, acell, price, fees = "", hd_num(Fraction(amt, 100), 2, False), "", "0,00"
            balance += amt
        else:
            neg = k in ("buy", "reinvest")
            scell = hd_num(Fraction(q), None, False)
            acell = hd_num(Fraction(amt, 100), 2, neg)
            price = f"{float(_price(r)):.4f}".replace(".", ",")
            fees = rnd.choice(["0,00", "4,90", "9,90"])
            balance += -amt if neg else amt
        desc = r.get("description") or r["name"]
        out_rows.append([r["day"].strftime("%d.%m.%Y"), _business(r["day"], 2).strftime("%d.%m.%Y"), HD_KIND[k],
                         r["isin"], desc, scell, price, fees, acell, r["currency"], r["ref"]])
    footer = [f";;;;;;;Closing balance;{hd_num(Fraction(abs(balance), 100), 2, balance < 0)};;"]
    data, nums = _write(before, HD_HEADER, out_rows, footer, ";", crlf, bom)
    if pad_to is not None:
        data, nums = _pad(data, nums, pad_to, crlf)
    return data, nums


def _pad(data: bytes, nums: list[int], size: int, crlf: bool) -> tuple[bytes, list[int]]:
    """Grow the file to exactly `size` bytes with one junk line before the header (shifts data lines by one)."""
    eol = b"\r\n" if crlf else b"\n"
    bom = data.startswith(b"\xef\xbb\xbf")
    body = data[3:] if bom else data
    need = size - len(data) - len(eol)
    if need < 1:
        raise ValueError("file already larger than the pad target")
    junk = b"#" * need
    out = (b"\xef\xbb\xbf" if bom else b"") + junk + eol + body
    assert len(out) == size, (len(out), size)
    return out, [n + 1 for n in nums]


def build(broker: str, number: str, rows: list[dict], rnd: random.Random | None = None, **kw) -> tuple[bytes, list[int]]:
    return (northwind if broker == "northwind" else harbor)(number, rows, rnd, **kw)
