"""Shiftbook pay engine (instruction.md R5).

Everything is integer arithmetic on 6-minute units ("tenths": 1 unit = 0.1 hour = 360 s) and
exact Fractions for money. The engine is deliberately written as a literal reading of the spec:
walk each workweek's worked time unit by unit in chronological order and classify each unit.

Public entry points:
    round_instant_us(us)            -> rounded epoch microseconds
    build_shifts(punches)           -> (shifts, exceptions)  for one employee
    compute_weeks(shifts, ...)      -> {week_start_date: WeekPay}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo

UNIT_S = 360
UNIT_US = UNIT_S * 1_000_000
HALF_US = UNIT_US // 2
MAX_SHIFT_S = 16 * 3600
KIND_ORDER = {"in": 0, "break_start": 1, "break_end": 2, "out": 3}
PERIOD_ANCHOR = date(2026, 9, 20)  # a Sunday; pay periods are 14 days from here


def round_instant_us(us: int) -> int:
    q, r = divmod(us, UNIT_US)
    return (q + (1 if r >= HALF_US else 0)) * UNIT_US


def to_us(dt: datetime) -> int:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    delta = dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def from_us(us: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=us)


def week_start_of(day: date) -> date:
    # Python: Monday=0 .. Sunday=6; workweek starts Sunday
    return day - timedelta(days=(day.weekday() + 1) % 7)


def period_start_of(day: date) -> date:
    n = (day - PERIOD_ANCHOR).days // 14
    return PERIOD_ANCHOR + timedelta(days=14 * n)


def half_up(x: Fraction) -> int:
    """Round a non-negative Fraction of cents half up to an integer."""
    if x < 0:
        raise ValueError("negative amount")
    n, d = x.numerator, x.denominator
    q, r = divmod(n, d)
    return q + (1 if 2 * r >= d else 0)


@dataclass
class Punch:
    punch_id: str
    kind: str
    at_us: int  # exact instant, epoch microseconds
    job_code: str | None = None


@dataclass
class Shift:
    job_code: str
    start_us: int  # rounded
    end_us: int  # rounded
    breaks: list[tuple[int, int]] = field(default_factory=list)  # rounded

    def worked_intervals(self) -> list[tuple[int, int]]:
        out, cur = [], self.start_us
        for b0, b1 in self.breaks:
            if b0 > cur:
                out.append((cur, b0))
            cur = max(cur, b1)
        if self.end_us > cur:
            out.append((cur, self.end_us))
        return out

    def worked_units(self) -> int:
        return sum((b - a) // UNIT_US for a, b in self.worked_intervals())


def build_shifts(punches: list[Punch]) -> tuple[list[Shift], list[tuple[int, str]]]:
    """Pair punches into shifts. Returns shifts and exceptions [(at_us, reason)].

    A valid sequence is: in, (break_start, break_end)*, out. Any violation makes an exception at the
    offending punch; a shift it interrupts never gets its out, so that shift is discarded and is
    also an exception at its own "in" ("shift has no out"); pairing restarts at the next "in".
    A shift whose exact in->out elapsed time exceeds 16 hours is an exception at its "in" and its
    worked time counts in no workweek; an "in" with no later "out" is an exception at that "in".
    """
    shifts: list[Shift] = []
    exceptions: list[tuple[int, str]] = []
    ps = sorted(punches, key=lambda p: p.at_us)
    state = "idle"
    cur: dict | None = None
    for p in ps:
        if state == "idle":
            if p.kind == "in":
                cur = {"in": p, "breaks": [], "bs": None}
                state = "working"
            else:
                exceptions.append((p.at_us, f"{p.kind} without an open shift"))
        elif state == "working":
            assert cur is not None
            if p.kind == "break_start":
                cur["bs"] = p
                state = "break"
            elif p.kind == "out":
                if p.at_us - cur["in"].at_us > MAX_SHIFT_S * 1_000_000:
                    exceptions.append((cur["in"].at_us, "shift longer than 16 hours"))
                else:
                    shifts.append(
                        Shift(
                            job_code=cur["in"].job_code,
                            start_us=round_instant_us(cur["in"].at_us),
                            end_us=round_instant_us(p.at_us),
                            breaks=[
                                (round_instant_us(a.at_us), round_instant_us(b.at_us))
                                for a, b in cur["breaks"]
                            ],
                        )
                    )
                state, cur = "idle", None
            else:
                exceptions.append((p.at_us, f"{p.kind} while a shift is open"))
                exceptions.append((cur["in"].at_us, "shift has no out punch"))
                state, cur = "idle", None
        elif state == "break":
            assert cur is not None
            if p.kind == "break_end":
                cur["breaks"].append((cur["bs"], p))
                cur["bs"] = None
                state = "working"
            else:
                exceptions.append((p.at_us, f"{p.kind} during a break"))
                exceptions.append((cur["in"].at_us, "shift has no out punch"))
                state, cur = "idle", None
    if state != "idle" and cur is not None:
        exceptions.append((cur["in"].at_us, "shift has no out punch"))
    return shifts, exceptions


@dataclass
class WeekPay:
    week_start: date
    reg_units: int = 0
    ot_units: int = 0
    dt_units: int = 0
    meal_premiums: int = 0
    straight: Fraction = Fraction(0)
    rr: Fraction = Fraction(0)
    days_with_exception: set = field(default_factory=set)
    per_day: dict = field(default_factory=dict)  # date -> [reg, ot, dt] units
    intervals: list = field(default_factory=list)  # (unit_start_us, job)

    @property
    def total_units(self) -> int:
        return self.reg_units + self.ot_units + self.dt_units

    def cents(self) -> dict:
        ot_prem = Fraction(self.ot_units, 10) * self.rr / 2
        dt_prem = Fraction(self.dt_units, 10) * self.rr
        meal = self.meal_premiums * self.rr
        c = {
            "straight_cents": half_up(self.straight),
            "ot_premium_cents": half_up(ot_prem),
            "dt_premium_cents": half_up(dt_prem),
            "meal_cents": half_up(meal),
        }
        c["gross_cents"] = sum(c.values())
        return c


RateFn = "callable(job_code, local_date) -> int cents"


def compute_weeks(shifts: list[Shift], tz: str, ruleset: str, rate_for) -> dict[date, WeekPay]:
    """Classify every worked unit and price each workweek.

    rate_for(job_code, workday_date) -> integer cents per hour (effective-dated).
    """
    zone = ZoneInfo(tz)
    # 1. expand to units: (start_us, job_code, workday, shift_index)
    units: list[tuple[int, str, date, int]] = []
    for si, sh in enumerate(shifts):
        for a, b in sh.worked_intervals():
            t = a
            while t < b:
                local = from_us(t).astimezone(zone)
                units.append((t, sh.job_code, local.date(), si))
                t += UNIT_US
    units.sort()
    # 2. group by workweek
    weeks: dict[date, list] = {}
    for u in units:
        weeks.setdefault(week_start_of(u[2]), []).append(u)
    result: dict[date, WeekPay] = {}
    for ws, us in sorted(weeks.items()):
        wp = WeekPay(week_start=ws)
        days_worked = {u[2] for u in us}
        seventh = ws + timedelta(days=6)
        is_seventh_week = ruleset == "CA" and len(days_worked) == 7
        day_count: dict[date, int] = {}
        reg = 0
        for t, job, day, si in us:
            h = day_count.get(day, 0)
            if ruleset == "CA":
                if is_seventh_week and day == seventh:
                    cat = "OT" if h < 80 else "DT"
                elif h >= 120:
                    cat = "DT"
                elif h >= 80:
                    cat = "OT"
                elif reg >= 400:
                    cat = "OT"
                else:
                    cat = "REG"
            else:
                cat = "REG" if reg < 400 else "OT"
            day_count[day] = h + 1
            pd = wp.per_day.setdefault(day, [0, 0, 0])
            pd[{"REG": 0, "OT": 1, "DT": 2}[cat]] += 1
            wp.intervals.append((t, job))
            if cat == "REG":
                reg += 1
                wp.reg_units += 1
            elif cat == "OT":
                wp.ot_units += 1
            else:
                wp.dt_units += 1
            rate = rate_for(job, day)
            wp.straight += Fraction(rate or 0, 10)
        total = wp.total_units
        wp.rr = (wp.straight / Fraction(total, 10)) if total else Fraction(0)
        result[ws] = wp
    # 3. meal premiums (CA only): per shift, attributed to the workday of the shift's start
    if ruleset == "CA":
        meal_days: set[date] = set()
        for sh in shifts:
            if sh.worked_units() <= 50:
                continue
            compliant = False
            worked_before = 0  # units worked in this shift before the current point
            cur = sh.start_us
            for b0, b1 in sh.breaks:
                worked_before += max(0, b0 - cur) // UNIT_US
                if (b1 - b0) >= 30 * 60 * 1_000_000 and worked_before <= 50:
                    compliant = True
                    break
                cur = max(cur, b1)
            if not compliant:
                day = from_us(sh.start_us).astimezone(zone).date()
                if day not in meal_days:
                    meal_days.add(day)
                    ws = week_start_of(day)
                    wp = result.setdefault(ws, WeekPay(week_start=ws))
                    wp.meal_premiums += 1
    return result


def rate_exceptions(shifts: list[Shift], tz: str, rate_for) -> list[tuple[int, str]]:
    """R5.6: work on a local date before the job's first effective rate; attributed to the shift's in."""
    zone = ZoneInfo(tz)
    out: list[tuple[int, str]] = []
    for sh in shifts:
        for a, b in sh.worked_intervals():
            t, bad = a, False
            while t < b:
                if rate_for(sh.job_code, from_us(t).astimezone(zone).date()) is None:
                    bad = True
                    break
                t += UNIT_US
            if bad:
                out.append((sh.start_us, f"no {sh.job_code} rate in effect for this work"))
                break
    return out


def exception_weeks(exceptions: list[tuple[int, str]], tz: str) -> dict[date, list[str]]:
    zone = ZoneInfo(tz)
    out: dict[date, list[str]] = {}
    for at_us, reason in exceptions:
        day = from_us(at_us).astimezone(zone).date()
        out.setdefault(week_start_of(day), []).append(reason)
    return out


def legacy_to_us(local_naive: datetime, fold: int, tz: str) -> int:
    """Interpret a legacy wall-clock time in the location's zone; fold picks the occurrence."""
    aware = local_naive.replace(tzinfo=ZoneInfo(tz), fold=fold)
    return to_us(aware)
