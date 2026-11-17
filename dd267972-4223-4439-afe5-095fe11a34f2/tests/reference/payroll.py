"""Reference pay engine for Shiftbook (hidden from the agent; mirrors instruction.md "Pay rules").

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


def selftest() -> None:
    """Check the engine against instruction.md's own worked examples.

    Ground truth here is transcribed from instruction.md lines 164-179 -- the six worked
    examples -- plus one constructed tie case derived from R5 and from line 155, "rounded
    to the nearest cent (half a cent rounds up)". None of it is computed by this module.

    That independence is the point: this module is shared with the reference solution, so
    grading the solution against it cannot catch a pay rule that is wrong in both. These
    assertions can. Verified to fail on each of four injected defects -- a wrong 6-minute
    unit, OT premium paid at the full regular rate, DT premium paid at half, and half-up
    rounding flipped to half-down -- while passing unmodified.

    Mirrors the sibling task c5e57df8's tests/reference/rules.py::selftest.
    """
    LA = "America/Los_Angeles"

    def at(y, m, d, hh, mm, ss=0, tz=LA):
        return to_us(datetime(y, m, d, hh, mm, ss, tzinfo=ZoneInfo(tz)))

    def shifts_from(raw, tz=LA, job="COOK"):
        punches = [Punch(str(i), k, at(*t, tz=tz), j)
                   for i, e in enumerate(raw)
                   for k, t, j in [(e[0], e[1], e[2] if len(e) > 2 else job)]]
        sh, exc = build_shifts(punches)
        assert not exc, "unexpected exceptions: %r" % (exc,)
        return sh

    def weeks(sh, ruleset, rate_for, tz=LA):
        """compute_weeks without asserting a single workweek (example 6 spans two)."""
        return compute_weeks(sh, tz, ruleset, rate_for)

    def week(sh, ruleset, rate=2400, tz=LA, rate_for=None):
        wks = compute_weeks(sh, tz, ruleset, rate_for or (lambda job, day: rate))
        assert len(wks) == 1, "expected one workweek, got %r" % (sorted(wks),)
        return next(iter(wks.values()))

    # --- R5.2 six-minute rounding, stated at instruction.md:131 and example 5.
    # "In at 08:02:59, out at 16:03:00: rounds to 08:00-16:06"
    assert round_instant_us(at(2026, 9, 21, 8, 2, 59)) == at(2026, 9, 21, 8, 0), \
        "08:02:59 must round down to 08:00 (6-minute units)"
    assert round_instant_us(at(2026, 9, 21, 16, 3, 0)) == at(2026, 9, 21, 16, 6), \
        "16:03:00 must round up to 16:06 (6-minute units)"

    # --- Example 1: CA, rate 2400. Mon 06:00-19:00, break 10:00-10:30.
    # 12.5 hours = 8.0 REG, 4.0 OT, 0.5 DT. straight 30000, OT 4800, DT 1200, gross 36000.
    w = week(shifts_from([("in", (2026, 9, 21, 6, 0)), ("break_start", (2026, 9, 21, 10, 0)),
                          ("break_end", (2026, 9, 21, 10, 30)), ("out", (2026, 9, 21, 19, 0))]), "CA")
    assert (w.reg_units, w.ot_units, w.dt_units) == (80, 40, 5), \
        "ex1 hours: expected 8.0/4.0/0.5, got %s" % ((w.reg_units/10, w.ot_units/10, w.dt_units/10),)
    c = w.cents()
    assert c["straight_cents"] == 30000, "ex1 straight: %r" % c
    assert c["ot_premium_cents"] == 4800, "ex1 OT premium: %r" % c
    assert c["dt_premium_cents"] == 1200, "ex1 DT premium: %r" % c
    assert c["gross_cents"] == 36000, "ex1 gross: expected 36000, got %r" % c

    # --- Example 2: FLSA, rate 2400. Mon-Fri 08:00-17:00, no breaks.
    # 45.0 hours = 40.0 REG, 5.0 OT; gross 108000 + 6000 = 114000.
    raw = []
    for d in range(21, 26):  # Mon 2026-09-21 .. Fri 2026-09-25
        raw += [("in", (2026, 9, d, 8, 0)), ("out", (2026, 9, d, 17, 0))]
    w2 = week(shifts_from(raw), "FLSA")
    assert (w2.reg_units, w2.ot_units) == (400, 50), \
        "ex2 hours: expected 40.0/5.0, got %s" % ((w2.reg_units/10, w2.ot_units/10),)
    assert w2.cents()["gross_cents"] == 114000, "ex2 gross: expected 114000, got %r" % w2.cents()

    # --- Example 5: CA, rate 2400. in 08:02:59, out 16:03:00 -> 08:00-16:06 = 8.1 hours
    # = 8.0 REG, 0.1 OT, plus a meal premium; gross 19440 + 120 + 2400 = 21960.
    w5 = week(shifts_from([("in", (2026, 9, 21, 8, 2, 59)), ("out", (2026, 9, 21, 16, 3, 0))]), "CA")
    assert (w5.reg_units, w5.ot_units) == (80, 1), \
        "ex5 hours: expected 8.0 REG / 0.1 OT, got %s" % ((w5.reg_units/10, w5.ot_units/10),)
    assert w5.meal_premiums == 1, "ex5 meal premium: expected 1, got %r" % w5.meal_premiums
    c5 = w5.cents()
    assert c5["straight_cents"] == 19440, "ex5 straight: expected 19440, got %r" % c5
    assert c5["ot_premium_cents"] == 120, "ex5 OT premium: expected 120, got %r" % c5
    assert c5["meal_cents"] == 2400, "ex5 meal: expected 2400, got %r" % c5
    assert c5["gross_cents"] == 21960, "ex5 gross: expected 21960, got %r" % c5

    # --- Example 3: FLSA, two jobs at different rates in one workweek.
    # Mon-Wed 10 h as COOK at 2400, Thu-Fri 7.5 h as SERVER at 1650.
    # straight-time 72000 + 24750 = 96750 over 45.0 hours, so the regular rate is 2150
    # and the 5.0 OT hours earn 5375; gross 102125.
    raw3 = []
    for d in (21, 22, 23):                      # Mon-Wed, COOK, 10 h
        raw3 += [("in", (2026, 9, d, 8, 0), "COOK"), ("out", (2026, 9, d, 18, 0), "COOK")]
    for d in (24, 25):                          # Thu-Fri, SERVER, 7.5 h
        raw3 += [("in", (2026, 9, d, 9, 0), "SERVER"), ("out", (2026, 9, d, 16, 30), "SERVER")]
    rates = {"COOK": 2400, "SERVER": 1650}
    w3 = week(shifts_from(raw3), "FLSA", rate=None, rate_for=lambda job, day: rates[job])
    assert (w3.reg_units, w3.ot_units) == (400, 50), \
        "ex3 hours: expected 40.0/5.0, got %s" % ((w3.reg_units/10, w3.ot_units/10),)
    assert w3.straight == 96750, "ex3 straight-time: expected 96750, got %r" % w3.straight
    assert w3.rr == 2150, "ex3 regular rate: expected 2150, got %r" % w3.rr
    c3 = w3.cents()
    assert c3["ot_premium_cents"] == 5375, "ex3 OT earnings: expected 5375, got %r" % c3
    assert c3["gross_cents"] == 102125, "ex3 gross: expected 102125, got %r" % c3

    # --- Example 4: CA, rate 2400. Sunday to Saturday, 09:00-15:00 every day, no breaks.
    # 36.0 REG Sunday-Friday and 6.0 OT on Saturday (the seventh day). Every shift is over
    # 5 hours with no break, so seven meal premiums of 2400;
    # gross 100800 + 7200 + 16800 = 124800.
    raw4 = []
    for d in range(20, 27):                     # Sun 2026-09-20 .. Sat 2026-09-26
        raw4 += [("in", (2026, 9, d, 9, 0)), ("out", (2026, 9, d, 15, 0))]
    w4 = week(shifts_from(raw4), "CA")
    assert (w4.reg_units, w4.ot_units, w4.dt_units) == (360, 60, 0), \
        "ex4 hours: expected 36.0 REG / 6.0 OT (seventh day), got %s" \
        % ((w4.reg_units/10, w4.ot_units/10, w4.dt_units/10),)
    assert w4.meal_premiums == 7, "ex4 meal premiums: expected 7, got %r" % w4.meal_premiums
    c4 = w4.cents()
    assert c4["straight_cents"] == 100800, "ex4 straight: expected 100800, got %r" % c4
    assert c4["ot_premium_cents"] == 7200, "ex4 OT premium: expected 7200, got %r" % c4
    assert c4["meal_cents"] == 16800, "ex4 meal: expected 16800, got %r" % c4
    assert c4["gross_cents"] == 124800, "ex4 gross: expected 124800, got %r" % c4

    # --- Example 6: CA, rate 2400, Los Angeles. In Saturday 2026-10-31 22:00, out Sunday
    # 2026-11-01 06:00, with the clocks going back at 02:00 that night. Splits as 2.0 hours
    # on Saturday in the workweek of 2026-10-25 and 7.0 hours on Sunday in the workweek of
    # 2026-11-01 -- nine real hours across an eight-hour wall-clock span. The shift's meal
    # premium belongs to Saturday.
    sh6 = shifts_from([("in", (2026, 10, 31, 22, 0)), ("out", (2026, 11, 1, 6, 0))])
    w6 = weeks(sh6, "CA", lambda job, day: 2400)
    assert set(w6) == {date(2026, 10, 25), date(2026, 11, 1)}, \
        "ex6 must split across two workweeks, got %s" % (sorted(w6),)
    sat, sun = w6[date(2026, 10, 25)], w6[date(2026, 11, 1)]
    assert sat.total_units == 20, "ex6 Saturday: expected 2.0 h, got %s" % (sat.total_units/10)
    assert sun.total_units == 70, "ex6 Sunday: expected 7.0 h (DST fall-back), got %s" % (sun.total_units/10)
    assert sat.meal_premiums == 1, "ex6 meal premium belongs to Saturday, got %r" % sat.meal_premiums
    assert sun.meal_premiums == 0, "ex6 Sunday must carry no meal premium, got %r" % sun.meal_premiums

    # --- Tie case (not one of the author's six): the only assertion here that exercises
    # the rounding DIRECTION, derived by hand from R5 and from instruction.md line 155,
    # "rounded to the nearest cent (half a cent rounds up)".
    # 30 h COOK at 2400 + 15 h SERVER at 1653 under FLSA, one workweek:
    #   straight-time = 72000 + 24795 = 96795 over 45.0 hours
    #   regular rate  = 96795 / 45    = 2151 exactly
    #   40.0 REG / 5.0 OT, so the OT premium is 5.0 * 2151/2 = 5377.5 -- an exact half
    #   cent, which must round UP to 5378. Half-down or half-to-even gives 5377.
    rawT = []
    for d in (21, 22, 23):
        rawT += [("in", (2026, 9, d, 8, 0), "COOK"), ("out", (2026, 9, d, 18, 0), "COOK")]
    for d in (24, 25):
        rawT += [("in", (2026, 9, d, 9, 0), "SERVER"), ("out", (2026, 9, d, 16, 30), "SERVER")]
    ratesT = {"COOK": 2400, "SERVER": 1653}
    wT = week(shifts_from(rawT), "FLSA", rate=None, rate_for=lambda job, day: ratesT[job])
    assert wT.straight == 96795, "tie straight-time: expected 96795, got %r" % wT.straight
    assert wT.rr == 2151, "tie regular rate: expected exactly 2151, got %r" % wT.rr
    cT = wT.cents()
    assert cT["ot_premium_cents"] == 5378, \
        "tie OT premium: 5377.5 cents must round UP to 5378 (instruction.md:155), got %r" \
        % cT["ot_premium_cents"]
    assert cT["gross_cents"] == 102173, "tie gross: expected 102173, got %r" % cT["gross_cents"]

    print("payroll selftest ok")


if __name__ == "__main__":
    selftest()
