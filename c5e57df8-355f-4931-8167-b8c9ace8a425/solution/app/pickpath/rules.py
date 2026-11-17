"""Pickpath rules used by the application: prices, tax, freight, FEFO, pick paths, legacy wall-clock times, FIFO."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

TZ = "America/New_York"
FREE_FREIGHT_SUBTOTAL = 250000
FREIGHT_BASE = 1200
FREIGHT_PER_HALF_KG = 90
COLD_SURCHARGE = 650
AISLE_PITCH = 4
BACK_AISLE = 21  # bays run 1..20; the front cross-aisle is bay 0 and the back cross-aisle bay 21


def half_up(x: Decimal) -> int:
    return int(x.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def tax_of(amount_cents: int, rate: Decimal) -> int:
    """R5.2: tax on an amount, rounded half-up to the cent."""
    return half_up(Decimal(amount_cents) * Decimal(rate))


# ---------------------------------------------------------------------------------------------- R5 money
@dataclass
class SkuInfo:
    code: str
    case_pack: int
    unit_weight_g: int
    length_cm: int
    width_cm: int
    height_cm: int
    storage: str  # ambient | chilled | frozen
    taxable: bool


def case_dim_g(s: SkuInfo) -> int:
    """Dimensional weight of one case in grams: ceil(L*W*H / 5)."""
    return -(-(s.length_cm * s.width_cm * s.height_cm) // 5)


def line_billable_g(s: SkuInfo, qty: int) -> int:
    cases = -(-qty // s.case_pack)
    return max(qty * s.unit_weight_g, cases * case_dim_g(s))


def freight_of(lines: list[tuple[SkuInfo, int]], subtotal: int) -> tuple[int, int]:
    """R5.3: (freight cents, half-kilograms billed)."""
    total_g = sum(line_billable_g(s, q) for s, q in lines)
    half_kgs = -(-total_g // 500)
    base = 0 if subtotal >= FREE_FREIGHT_SUBTOTAL else FREIGHT_BASE + FREIGHT_PER_HALF_KG * half_kgs
    cold = COLD_SURCHARGE if any(s.storage in ("chilled", "frozen") for s, _ in lines) else 0
    return base + cold, half_kgs


def price_on(entries: list[tuple[date, int]], day: date) -> int | None:
    """R5.1: the price whose effective date is the latest on or before `day`."""
    best = None
    for eff, cents in sorted(entries):
        if eff <= day:
            best = cents
    return best


def order_totals(lines: list[tuple[SkuInfo, int, int]], rate: Decimal, freight_taxable: bool) -> dict:
    """lines: (sku, qty, unit_price). Returns every amount of R5 for a new order."""
    out_lines = []
    for s, q, p in lines:
        amt = q * p
        out_lines.append({"sku": s.code, "qty": q, "unit_price_cents": p, "amount_cents": amt,
                          "tax_cents": tax_of(amt, rate) if s.taxable else 0})
    subtotal = sum(l["amount_cents"] for l in out_lines)
    tax = sum(l["tax_cents"] for l in out_lines)
    freight, _ = freight_of([(s, q) for s, q, _ in lines], subtotal)
    ftax = tax_of(freight, rate) if freight_taxable else 0
    return {"lines": out_lines, "subtotal_cents": subtotal, "tax_cents": tax, "freight_cents": freight,
            "freight_tax_cents": ftax, "total_cents": subtotal + tax + freight + ftax}


def line_value(units: int, price: int, taxable: bool, rate: Decimal) -> int:
    amt = units * price
    return amt + (tax_of(amt, rate) if taxable else 0)


def due_of(lines: list[dict], freight: int, freight_tax: int, rate: Decimal) -> int:
    """R5.10: what the customer owes for the units that are not cancelled.

    lines: dicts with qty, cancelled, unit_price_cents, taxable."""
    kept = [(l["qty"] - l["cancelled"], l) for l in lines]
    if sum(k for k, _ in kept) == 0:
        return 0
    return sum(line_value(k, l["unit_price_cents"], l["taxable"], rate) for k, l in kept) + freight + freight_tax


def shipment_line_tax(before: int, now: int, price: int, taxable: bool, rate: Decimal) -> int:
    """R8.2: tax on everything of the line shipped so far, minus tax already invoiced for it."""
    if not taxable:
        return 0
    return tax_of(now * price, rate) - tax_of(before * price, rate)


def credit_of(net_shipped: int, units: int, price: int, taxable: bool, rate: Decimal) -> int:
    """R8.6: credit for returning `units` of a line of which `net_shipped` are shipped and not yet returned."""
    return line_value(net_shipped, price, taxable, rate) - line_value(net_shipped - units, price, taxable, rate)


# ---------------------------------------------------------------------------------------------- R6 FEFO
@dataclass
class LotView:
    lot_id: int
    expiry: date
    available: int      # on hand minus reserved
    allocatable: bool   # not quarantine, not on hold


def fefo(need: int, lots: list[LotView], ship_date: date, min_days: int, case_pack: int,
         split: bool) -> tuple[list[tuple[int, int]], int]:
    """R6.2: allocate `need` units; returns ([(lot_id, units)], remainder)."""
    out = []
    cands = sorted((l for l in lots if l.allocatable and l.available > 0 and (l.expiry - ship_date).days >= min_days),
                   key=lambda l: (l.expiry, l.lot_id))
    for l in cands:
        if need <= 0:
            break
        take = min(need, l.available) if split else min(need, (l.available // case_pack) * case_pack)
        if take > 0:
            out.append((l.lot_id, take))
            need -= take
    return out, need


# ---------------------------------------------------------------------------------------------- R7 pick path
def parse_bin(code: str) -> tuple[str, int, int, int]:
    """'A03-07-2' -> ('A', 3, 7, 2)."""
    z, rest = code[0], code[1:]
    a, b, lv = rest.split("-")
    return z, int(a), int(b), int(lv)


def dist(p: tuple[int, int], q: tuple[int, int]) -> int:
    (a1, b1), (a2, b2) = p, q
    if a1 == a2:
        return abs(b1 - b2)
    return AISLE_PITCH * abs(a1 - a2) + min(b1 + b2, 2 * BACK_AISLE - b1 - b2)


def route(bins: list[str]) -> tuple[int, list[str]]:
    """R7.3: shortest closed tour from the depot (aisle 0, bay 0) over the distinct bins; among equally short
    tours, the one whose bin-code sequence is lexicographically smallest. Exact (Held-Karp)."""
    stops = sorted(set(bins))
    n = len(stops)
    if n == 0:
        return 0, []
    pos = [(parse_bin(c)[1], parse_bin(c)[2]) for c in stops]
    depot = (0, 0)
    full = (1 << n) - 1
    INF = math.inf
    # togo[mask][j]: shortest distance to visit every stop not in mask, starting at stop j (in mask), then the depot
    togo = [[INF] * n for _ in range(1 << n)]
    for j in range(n):
        togo[full][j] = dist(pos[j], depot)
    d = [[dist(pos[i], pos[j]) for j in range(n)] for i in range(n)]
    for mask in range(full - 1, 0, -1):
        rem = [k for k in range(n) if not mask & (1 << k)]
        for j in range(n):
            if not mask & (1 << j):
                continue
            best = INF
            for k in rem:
                v = d[j][k] + togo[mask | (1 << k)][k]
                if v < best:
                    best = v
            togo[mask][j] = best
    total = min(dist(depot, pos[k]) + togo[1 << k][k] for k in range(n))
    seq, mask, cur, spent = [], 0, None, 0
    while mask != full:
        for k in range(n):  # stops are sorted by code: the first that stays optimal is the smallest
            if mask & (1 << k):
                continue
            step = dist(depot if cur is None else pos[cur], pos[k])
            if spent + step + togo[mask | (1 << k)][k] == total:
                seq.append(stops[k])
                spent += step
                mask |= 1 << k
                cur = k
                break
    return int(total), seq


def tour_length(seq: list[str]) -> int:
    pts = [(0, 0)] + [(parse_bin(c)[1], parse_bin(c)[2]) for c in seq] + [(0, 0)]
    return sum(dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


# ---------------------------------------------------------------------------------------------- R10 legacy time
def legacy_instant(local: datetime, fold: int, tz: str = TZ) -> datetime:
    """R10.2: fold 0 = the UTC offset in effect before a clock change, fold 1 = after it."""
    return local.replace(tzinfo=ZoneInfo(tz), fold=int(fold)).astimezone(timezone.utc)


def utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------------------------- R9.5 FIFO value
def fifo_value(movements: list[tuple[datetime, int, int, Decimal]], as_of: datetime) -> tuple[int, int]:
    """movements: (instant, order key, units (signed), unit cost in dollars). Returns (on hand, value cents)."""
    upto = [m for m in movements if m[0] <= as_of]
    on_hand = sum(m[2] for m in upto)
    if on_hand <= 0:
        return 0, 0
    left, value = on_hand, Decimal(0)
    for at, key, units, cost in sorted((m for m in upto if m[2] > 0), key=lambda m: (m[0], m[1]), reverse=True):
        take = min(left, units)
        value += Decimal(take) * cost * 100
        left -= take
        if left == 0:
            break
    return on_hand, half_up(value)


def csv_safe(v: str) -> str:
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v
