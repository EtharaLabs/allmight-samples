"""Seatwise money rules (instruction R9, R10.2): exact integer arithmetic, no floats, no Decimal.

Every amount is whole cents. "Half up" means a value exactly halfway between two cents goes to the
larger one. Allocation is the largest-remainder method on exact remainders; ties go to the ticket
that comes first in the order's seat order (section code, row letter, seat number).
"""

from __future__ import annotations

from dataclasses import dataclass, field

SERVICE_FEE_PER_MILLE = 75          # 7.5 % of a ticket's face price
TAX_PER_100K = 8875                 # 8.875 %
MAX_TICKETS_PER_BUYER = 6


def half_up(num: int, den: int) -> int:
    """round(num / den) with exact halves going up (num, den >= 0)."""
    if num < 0 or den <= 0:
        raise ValueError("half_up expects num >= 0 and den > 0")
    return (2 * num + den) // (2 * den)


def service_fee(face: int) -> int:
    return half_up(face * SERVICE_FEE_PER_MILLE, 1000)


def allocate(total: int, weights: list[int]) -> list[int]:
    """Largest remainder: floor of each exact share, then one cent each to the largest remainders;
    equal remainders favour the earlier position."""
    w = sum(weights)
    if total == 0 or w == 0:
        return [0] * len(weights)
    base = [total * x // w for x in weights]
    rem = [total * x % w for x in weights]
    left = total - sum(base)
    order = sorted(range(len(weights)), key=lambda i: (-rem[i], i))
    for i in order[:left]:
        base[i] += 1
    return base


@dataclass
class Code:
    code: str
    kind: str            # "percent" | "fixed"
    value: int           # percent 1..100, or cents
    min_subtotal: int = 0
    max_redemptions: int | None = None


@dataclass
class Line:
    key: str             # seat label, e.g. "ORCH-C-12"
    face: int
    discount: int = 0
    service: int = 0
    facility: int = 0
    tax: int = 0
    total: int = 0


@dataclass
class Quote:
    lines: list[Line]
    subtotal: int
    percent_discount: int
    fixed_discount: int
    discount: int
    service: int
    facility: int
    taxable: int
    tax: int
    total: int
    codes: list[str] = field(default_factory=list)


def quote(lines_in: list[tuple[str, int]], facility_fee: int, codes: list[Code] | tuple = ()) -> Quote:
    """lines_in: (seat label, face cents) in the order's seat order. codes: at most one percent and one fixed."""
    faces = [f for _, f in lines_in]
    sub = sum(faces)
    pct = [c for c in codes if c.kind == "percent"]
    fix = [c for c in codes if c.kind == "fixed"]
    d1 = half_up(sub * pct[0].value, 100) if pct else 0
    d2 = min(fix[0].value, sub - d1) if fix else 0
    disc = d1 + d2
    d = allocate(disc, faces)
    s = [service_fee(f) for f in faces]
    base = [f - di + si for f, di, si in zip(faces, d, s)]
    taxable = sum(base)
    tax = half_up(taxable * TAX_PER_100K, 100000)
    t = allocate(tax, base)
    lines = []
    for (key, f), di, si, ti in zip(lines_in, d, s, t):
        lines.append(Line(key, f, di, si, facility_fee, ti, f - di + si + facility_fee + ti))
    total = sum(l.total for l in lines)
    q = Quote(lines, sub, d1, d2, disc, sum(s), facility_fee * len(lines), taxable, tax, total,
              [c.code for c in pct + fix])
    assert total == sub - disc + sum(s) + facility_fee * len(lines) + tax
    return q


def refund_amount(ticket_total: int, percent: int) -> int:
    return half_up(ticket_total * percent, 100)


# ---- seat ordering ----------------------------------------------------------------------------------
def seat_key(label: str) -> tuple:
    """'ORCH-C-12' -> ('ORCH', 'C', 12): section code, row letter, seat number (numeric)."""
    sec, row, num = label.rsplit("-", 2)
    return (sec, row, int(num))


def sort_seats(labels):
    return sorted(labels, key=seat_key)


# ---- approximate implementations (used only by gen_ties.py to pick discriminating cases) -------------
def _approx_allocate(total: int, weights: list[int], mode: str) -> list[int]:
    import decimal
    import math
    w = sum(weights)
    if mode == "decimal":
        D = decimal.Decimal
        with decimal.localcontext() as c:
            c.prec = 28
            shares = [D(total) * D(x) / D(w) for x in weights]
            floors = [int(s.to_integral_value(rounding=decimal.ROUND_FLOOR)) for s in shares]
            rems = [s - fl for s, fl in zip(shares, floors)]
    else:
        shares = [total * x / w for x in weights]
        floors = [math.floor(s) for s in shares]
        rems = [s - fl for s, fl in zip(shares, floors)]
    left = total - sum(floors)
    order = sorted(range(len(weights)), key=lambda i: (-rems[i], i))
    for i in order[:max(0, left)]:
        floors[i] += 1
    return floors


def approx_quote(lines_in, facility_fee, codes, mode: str) -> list[int]:
    """Per-ticket totals computed with Decimal(28) or float shares (half-up rounding done correctly)."""
    faces = [f for _, f in lines_in]
    sub = sum(faces)
    pct = [c for c in codes if c.kind == "percent"]
    fix = [c for c in codes if c.kind == "fixed"]
    d1 = half_up(sub * pct[0].value, 100) if pct else 0
    d2 = min(fix[0].value, sub - d1) if fix else 0
    d = _approx_allocate(d1 + d2, faces, mode)
    s = [service_fee(f) for f in faces]
    base = [f - di + si for f, di, si in zip(faces, d, s)]
    tax = half_up(sum(base) * TAX_PER_100K, 100000)
    t = _approx_allocate(tax, base, mode)
    return [f - di + si + facility_fee + ti for f, di, si, ti in zip(faces, d, s, t)]


def bankers_quote(lines_in, facility_fee, codes) -> list[int]:
    """Per-ticket totals when every half-up rounding is done with round-half-even (Python round())."""
    from fractions import Fraction
    faces = [f for _, f in lines_in]
    sub = sum(faces)
    pct = [c for c in codes if c.kind == "percent"]
    fix = [c for c in codes if c.kind == "fixed"]
    d1 = round(Fraction(sub * pct[0].value, 100)) if pct else 0
    d2 = min(fix[0].value, sub - d1) if fix else 0
    d = allocate(d1 + d2, faces)
    s = [round(Fraction(f * SERVICE_FEE_PER_MILLE, 1000)) for f in faces]
    base = [f - di + si for f, di, si in zip(faces, d, s)]
    tax = round(Fraction(sum(base) * TAX_PER_100K, 100000))
    t = allocate(tax, base)
    return [f - di + si + facility_fee + ti for f, di, si, ti in zip(faces, d, s, t)]


if __name__ == "__main__":
    # The instruction's worked examples (R9 examples 1-4, R10 example 5).
    q = quote([("ORCH-C-11", 4000), ("ORCH-C-12", 2600)], 200)
    assert [l.total for l in q.lines] == [4882, 3243] and q.total == 8125, q
    q = quote([("BALC-A-3", 3000), ("BALC-A-4", 3000), ("ORCH-B-7", 5000)], 150, [Code("SPRING20", "percent", 20)])
    assert [l.total for l in q.lines] == [3008, 3008, 4913] and q.total == 10929, q
    q = quote([("BALC-A-3", 3000), ("BALC-A-4", 3000), ("ORCH-B-7", 5000)], 150,
              [Code("SPRING20", "percent", 20), Code("TAKE4", "fixed", 400)])
    assert [l.total for l in q.lines] == [2890, 2889, 4715] and q.total == 10494, q
    assert refund_amount(4715, 50) == 2358 and refund_amount(2890, 50) == 1445
    q = quote([("FLOOR-A-1", 2000)], 100, [Code("BIG", "fixed", 2500)])
    assert [(l.discount, l.service, l.tax, l.total) for l in q.lines] == [(2000, 150, 13, 263)], q
    print("worked examples reproduce")
