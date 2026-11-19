"""Broker statement parsing (instruction R4.1-R4.4): Northwind Securities and Harbor Direct CSV files.

parse(broker, data, account_number, find_security) -> (rows, errors)
    rows:   [Row] for every data row that parsed (in file order), with its physical line number
    errors: [(line, reason)]; line 0 is a problem with the whole file
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction

NW_HEADER = ["Trade Date", "Settlement Date", "Activity", "Symbol", "Description", "Quantity", "Price", "Commission",
             "Net Amount", "Currency", "Reference"]
HD_HEADER = ["Booking date", "Value date", "Transaction", "ISIN", "Name", "Shares", "Price", "Fees", "Amount",
             "Currency", "Transaction no."]
NW_KINDS = {"buy": "buy", "sell": "sell", "dividend": "dividend", "reinvest": "reinvest", "split": "split"}
HD_KINDS = {"purchase": "buy", "sale": "sell", "dividend": "dividend", "dividend reinvestment": "reinvest", "split": "split"}

NW_NUM = re.compile(r"^(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?$")
HD_NUM = re.compile(r"^(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d+))?$")
RATIO = re.compile(r"^(\d+):(\d+)$")


@dataclass
class Row:
    line: int
    kind: str
    day: date
    security_id: int
    quantity: Fraction | None
    amount: int | None          # currency cents
    ratio: tuple[int, int] | None
    ref: str
    description: str


class Bad(Exception):
    pass


def _num(text: str, pattern: re.Pattern, group_sep: str) -> Fraction:
    m = pattern.match(text)
    if not m:
        raise Bad(f"'{text}' is not a number")
    whole = m.group(1).replace(group_sep, "")
    frac = m.group(2) or ""
    return Fraction(Decimal(whole + ("." + frac if frac else "")))


def nw_number(text: str) -> Fraction | None:
    t = text.strip()
    if t == "":
        return None
    neg = t.startswith("(") and t.endswith(")")
    if neg:
        t = t[1:-1].strip()
    v = _num(t, NW_NUM, ",")
    return -v if neg else v


def hd_number(text: str) -> Fraction | None:
    t = text.strip()
    if t == "":
        return None
    neg = t.startswith("-")
    if neg:
        t = t[1:]
    v = _num(t, HD_NUM, ".")
    return -v if neg else v


def ratio(text: str) -> tuple[int, int]:
    m = RATIO.match(text.strip())
    if not m:
        raise Bad(f"split ratio '{text}' is not N:M")
    n, o = int(m.group(1)), int(m.group(2))
    if n <= 0 or o <= 0 or (n != 1 and o != 1):
        raise Bad(f"split ratio '{text}' must be N:1 or 1:M")
    x = o
    for p in (2, 5):
        while x % p == 0:
            x //= p
    if x != 1:
        raise Bad(f"split ratio '{text}': M may only have the prime factors 2 and 5")
    return n, o


def decimals(v: Fraction) -> int:
    k = 0
    while (v * 10 ** k).denominator != 1:
        k += 1
        if k > 12:
            break
    return k


def _date(text: str, fmt: str) -> date:
    t = text.strip()
    try:
        if fmt == "nw":
            if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", t):
                raise ValueError
            mm, dd, yy = t.split("/")
        else:
            if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", t):
                raise ValueError
            dd, mm, yy = t.split(".")
        return date(int(yy), int(mm), int(dd))
    except ValueError:
        raise Bad(f"'{text}' is not a valid date")


def parse(broker: str, data: bytes, account_number: str, find_security) -> tuple[list[Row], list[tuple[int, str]]]:
    """find_security(broker, key) -> (security_id, currency) or None."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [], [(0, "the file is not UTF-8 text")]
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.split("\n")
    lines = [l[:-1] if l.endswith("\r") else l for l in lines]
    if lines and lines[-1] == "":
        lines.pop()
    nw = broker == "northwind"
    header = NW_HEADER if nw else HD_HEADER
    delim = "," if nw else ";"
    acct_prefix = "Account Number:" if nw else "Depot:"
    header_at = None
    acct_seen = None
    for i, l in enumerate(lines):
        try:
            fields = next(csv.reader([l], delimiter=delim))
        except (csv.Error, StopIteration):
            fields = []
        if fields == header:
            header_at = i
            break
        if l.strip().startswith(acct_prefix):
            acct_seen = l.strip()[len(acct_prefix):].strip()
    if header_at is None:
        return [], [(0, "no header row was found; is this a " + ("Northwind" if nw else "Harbor Direct") + " statement?")]
    if acct_seen is None:
        return [], [(0, f"the statement does not name its account ('{acct_prefix} ...')")]
    if acct_seen != account_number.strip():
        return [], [(0, f"the statement is for account {acct_seen}, not {account_number.strip()}")]
    rows, errors = [], []
    for i in range(header_at + 1, len(lines)):
        l = lines[i]
        line_no = i + 1
        if l.strip() == "":
            continue
        if nw and l.startswith("End of statement"):
            continue
        try:
            fields = next(csv.reader([l], delimiter=delim))
        except csv.Error:
            errors.append((line_no, "the line is not valid CSV"))
            continue
        if nw and fields and fields[0].startswith("Total"):
            continue
        if not nw and fields and fields[0] == "":
            continue
        try:
            rows.append(_row(nw, fields, line_no, find_security))
        except Bad as e:
            errors.append((line_no, str(e)))
    return rows, errors


def _row(nw: bool, f: list[str], line_no: int, find_security) -> Row:
    if len(f) != 11:
        raise Bad(f"expected 11 fields, found {len(f)}")
    day = _date(f[0], "nw" if nw else "hd")
    act = f[2].strip().lower()
    kind = (NW_KINDS if nw else HD_KINDS).get(act)
    if kind is None:
        raise Bad(f"unknown activity '{f[2].strip()}'")
    key = f[3].strip()
    sec = find_security("northwind" if nw else "harbor", key)
    if sec is None:
        raise Bad(f"unknown security '{key}'" + (" (symbol)" if nw else " (ISIN)"))
    sid, cur = sec
    if f[9].strip() != cur:
        raise Bad(f"currency {f[9].strip() or '(empty)'} differs from the security's currency {cur}")
    ref = f[10].strip()
    if not 1 <= len(ref) <= 64:
        raise Bad("the reference must be 1 to 64 characters")
    number = nw_number if nw else hd_number
    qcell, acell = f[5], f[8]
    q = amt = None
    rat = None
    if kind == "split":
        rat = ratio(qcell)
        a = number(acell)
        if a not in (None, 0):
            raise Bad("a split carries no amount")
    else:
        a = number(acell)
        if a is None or a == 0:
            raise Bad("the amount is missing or zero")
        if decimals(a) > 2:
            raise Bad("the amount has more than 2 decimal places")
        if kind == "dividend":
            if qcell.strip() != "":
                raise Bad("a dividend row has no quantity")
            if a < 0:
                raise Bad("a dividend's amount must be positive")
        else:
            qq = number(qcell)
            if qq is None or qq == 0:
                raise Bad("the quantity is missing or zero")
            if decimals(qq) > 8:
                raise Bad("the quantity has more than 8 decimal places")
            if kind in ("buy", "reinvest"):
                if qq < 0 or a > 0:
                    raise Bad("a purchase needs a positive quantity and a negative amount")
            else:
                if nw and (qq > 0 or a < 0):
                    raise Bad("a sale needs a negative quantity and a positive amount")
                if not nw and (qq < 0 or a < 0):
                    raise Bad("a sale needs a positive quantity and a positive amount")
            q = abs(qq)
        amt = int(abs(a) * 100)
    return Row(line_no, kind, day, sid, q, amt, rat, ref, f[4])
