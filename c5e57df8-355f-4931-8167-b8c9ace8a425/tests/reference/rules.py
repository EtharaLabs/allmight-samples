"""Pickpath exact rules (instruction.md R4-R10): money, freight, FEFO, pick paths, legacy time, FIFO value.

Pure functions only; tests/reference/world.py keeps the state these rules act on. Every worked example in
instruction.md is reproduced by `selftest()` at the bottom (python3 rules.py).
"""

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


# ---------------------------------------------------------------------------------------------- self test
def selftest() -> None:
    ny, rate_ny = True, Decimal("0.08875")
    olive = SkuInfo("OIL-750", 6, 1050, 30, 20, 32, "ambient", False)
    tins = SkuInfo("TIN-SAR", 24, 130, 30, 20, 12, "ambient", True)
    bur = SkuInfo("BUR-125", 12, 60, 25, 19, 9, "chilled", False)
    # Example 1 (R5): two lines, NY
    t = order_totals([(olive, 12, 1450), (tins, 30, 389)], rate_ny, ny)
    assert case_dim_g(olive) == 3840 and line_billable_g(olive, 12) == 12600, line_billable_g(olive, 12)
    assert case_dim_g(tins) == 1440 and line_billable_g(tins, 30) == 3900, line_billable_g(tins, 30)
    assert t["lines"][1]["tax_cents"] == 1036 and t["subtotal_cents"] == 29070, t
    assert t["freight_cents"] == 1200 + 90 * 33 == 4170, t
    assert t["freight_tax_cents"] == 370 and t["total_cents"] == 29070 + 1036 + 4170 + 370 == 34646, t
    # Example 2: free freight threshold + cold chain, PA (freight not taxable)
    t2 = order_totals([(bur, 600, 417)], Decimal("0.06"), False)
    assert t2["subtotal_cents"] == 250200 and t2["freight_cents"] == 650 and t2["total_cents"] == 250850, t2
    t3 = order_totals([(bur, 588, 417)], Decimal("0.06"), False)
    assert t3["subtotal_cents"] == 245196, t3
    assert line_billable_g(bur, 588) == 49 * 855 == 41895 and t3["freight_cents"] == 1200 + 90 * 84 + 650 == 9410, t3
    # Refunds (R5.10): cancel 18 of 30 tins (NY)
    lines = [{"qty": 12, "cancelled": 0, "unit_price_cents": 1450, "taxable": False},
             {"qty": 30, "cancelled": 0, "unit_price_cents": 389, "taxable": True}]
    d0 = due_of(lines, 4170, 370, rate_ny)
    lines[1]["cancelled"] = 18
    d1 = due_of(lines, 4170, 370, rate_ny)
    assert d0 == 34646 and d0 - d1 == 18 * 389 + 1036 - 414 == 7624, (d0, d1)
    # Invoice tax split (R8.2): 12 tins then 18 tins
    assert shipment_line_tax(0, 12, 389, True, rate_ny) == 414
    assert shipment_line_tax(12, 30, 389, True, rate_ny) == 622
    # Credit (R8.6): return 5 of 30 shipped tins
    assert credit_of(30, 5, 389, True, rate_ny) == 1945 + 1036 - 863 == 2118, credit_of(30, 5, 389, True, rate_ny)
    # FEFO (R6): ship 2026-12-01, min 14 days, case pack 12, whole cases
    lots = [LotView(501, date(2026, 12, 8), 40, True), LotView(502, date(2026, 12, 20), 30, True),
            LotView(503, date(2027, 1, 10), 100, True)]
    a, rem = fefo(60, lots, date(2026, 12, 1), 14, 12, False)
    assert a == [(502, 24), (503, 36)] and rem == 0, a
    a, rem = fefo(60, lots, date(2026, 12, 1), 14, 12, True)
    assert a == [(502, 30), (503, 30)], a
    a, rem = fefo(60, lots[:2], date(2026, 12, 1), 14, 12, False)
    assert a == [(502, 24)] and rem == 36, (a, rem)
    # Prices (R5.1, example 4)
    gold = [(date(2026, 1, 1), 1500), (date(2026, 12, 1), 1450)]
    assert price_on(gold, date(2026, 11, 20)) == 1500 and price_on(gold, date(2026, 12, 15)) == 1450
    # Pick path (R7)
    assert dist((3, 7), (3, 12)) == 5 and dist((3, 7), (5, 15)) == 8 + 20 and dist((3, 18), (5, 15)) == 8 + 9
    d, s = route(["A03-07-2", "A05-15-1", "A03-18-4", "A05-04-3"])
    assert (d, s) == (route(["A05-04-3", "A03-18-4", "A05-15-1", "A03-07-2"])), "order independent"
    assert d == 82 and s == ["A03-07-2", "A03-18-4", "A05-15-1", "A05-04-3"], (d, s)
    import itertools, random
    rnd = random.Random(7)
    for _ in range(300):
        n = rnd.randint(1, 7)
        bins = sorted({f"A{rnd.randint(1, 4):02d}-{rnd.choice([1, 2, 10, 11, 19, 20, rnd.randint(1, 20)]):02d}-{rnd.randint(1, 2)}"
                       for _ in range(n)})
        best = min((tour_length(list(p)), list(p)) for p in itertools.permutations(bins))
        assert route(bins) == best, (bins, route(bins), best)
    d, s = route(["A02-08-2", "A01-12-3", "A01-05-1"])
    assert (d, s) == (56, ["A01-05-1", "A01-12-3", "A02-08-2"]), (d, s)
    # Legacy time (R10.2)
    assert utc_z(legacy_instant(datetime(2026, 11, 1, 1, 30), 0)) == "2026-11-01T05:30:00Z"
    assert utc_z(legacy_instant(datetime(2026, 11, 1, 1, 30), 1)) == "2026-11-01T06:30:00Z"
    assert utc_z(legacy_instant(datetime(2026, 3, 8, 2, 30), 0)) == "2026-03-08T07:30:00Z"
    assert utc_z(legacy_instant(datetime(2026, 3, 8, 2, 30), 1)) == "2026-03-08T06:30:00Z"
    # FIFO value (R9.5)
    U = lambda s: datetime.fromisoformat(s)  # noqa: E731
    mv = [(U("2026-05-04T13:00:00+00:00"), 1, 40, Decimal("2.1250")),
          (U("2026-05-06T18:00:00+00:00"), 2, 30, Decimal("2.3000")),
          (U("2026-05-08T15:00:00+00:00"), 3, -45, Decimal("0"))]
    assert fifo_value(mv, U("2026-05-09T00:00:00+00:00")) == (25, 5750), fifo_value(mv, U("2026-05-09T00:00:00+00:00"))
    assert fifo_value(mv, U("2026-05-05T12:00:00+00:00")) == (40, 8500)
    print("rules selftest ok")


if __name__ == "__main__":
    selftest()
