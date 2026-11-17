"""Verifier-side model of Shiftbook: what every timesheet and payroll line must say.

The model mirrors the instruction's rules (R5-R7) from the verifier's own records: the legacy data
read from the database before the submission ever ran, every punch the verifier sent and saw
accepted, every rate it added, every week it approved, and every run it finalized. It never reads
the submission's own tables.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # importable standalone (Reward Kit)

from reference import payroll as P  # noqa: E402

FIRST_PERIOD = P.PERIOD_ANCHOR


def hours(units: int) -> str:
    return f"{units // 10}.{units % 10}"


@dataclass
class Emp:
    eid: int
    email: str
    name: str
    location_id: int
    tz: str
    ruleset: str
    role: str = "employee"
    manager_id: int | None = None
    active: bool = True
    rates: dict = field(default_factory=dict)  # job -> [(eff date, cents)] sorted
    punches: list = field(default_factory=list)  # P.Punch
    approved: set = field(default_factory=set)  # week_start dates
    legacy: bool = False

    def rate_for(self, job, day):
        best = None
        for eff, cents in sorted(self.rates.get(job, [])):
            if eff <= day:
                best = cents
        return best


class State:
    def __init__(self, emp: Emp):
        self.emp = emp
        shifts, exc = P.build_shifts(emp.punches)
        exc = exc + P.rate_exceptions(shifts, emp.tz, emp.rate_for)
        self.exceptions = P.exception_weeks(exc, emp.tz)
        self.weeks = P.compute_weeks(shifts, emp.tz, emp.ruleset, emp.rate_for)

    def status(self, ws: date) -> str:
        if ws in self.exceptions:
            return "exception"
        return "approved" if ws in self.emp.approved else "pending"

    def has_work(self, ws: date) -> bool:
        w = self.weeks.get(ws)
        return bool(w and w.total_units)

    def payable(self, ws: date) -> bool:
        return self.status(ws) == "approved" and self.has_work(ws)

    def fingerprint(self) -> dict:
        out = {}
        for ws in set(self.weeks) | set(self.exceptions):
            w = self.weeks.get(ws)
            out[ws] = json.dumps({"iv": w.intervals if w else [], "mp": w.meal_premiums if w else 0,
                                  "c": w.cents() if w else {}, "x": sorted(self.exceptions.get(ws, []))},
                                 sort_keys=True, default=str)
        return out


class Model:
    def __init__(self, locations: dict[int, dict]):
        self.locations = locations
        self.emps: dict[int, Emp] = {}
        self.paid: dict[tuple[int, date], int] = {}  # finalized gross per (employee, week)
        self.finalized_periods: set[date] = set()

    # -- changes that can return weeks to pending (R6.2) ----------------------------------------
    def change(self, eid: int, mutate) -> list[date]:
        emp = self.emps[eid]
        before = State(emp).fingerprint()
        mutate(emp)
        after = State(emp).fingerprint()
        changed = [ws for ws in set(before) | set(after) if before.get(ws) != after.get(ws)]
        for ws in changed:
            emp.approved.discard(ws)
        return changed

    def add_punches(self, eid: int, punches: list) -> list[date]:
        return self.change(eid, lambda e: e.punches.extend(punches))

    def add_rate(self, eid: int, job: str, eff: date, cents: int) -> list[date]:
        return self.change(eid, lambda e: e.rates.setdefault(job, []).append((eff, cents)))

    def approve(self, eid: int, ws: date) -> None:
        self.emps[eid].approved.add(ws)

    # -- payroll -------------------------------------------------------------------------------
    def line(self, eid: int, period: date) -> dict | None:
        st = State(self.emps[eid])
        weeks = [period, period + timedelta(days=7)]
        ln = {"reg": 0, "ot": 0, "dt": 0, "meal": 0, "straight": 0, "otp": 0, "dtp": 0, "mealc": 0,
              "weeks": {}, "adjustments": {}}
        has = False
        for ws in weeks:
            if st.payable(ws):
                w = st.weeks[ws]
                c = w.cents()
                ln["reg"] += w.reg_units
                ln["ot"] += w.ot_units
                ln["dt"] += w.dt_units
                ln["meal"] += w.meal_premiums
                ln["straight"] += c["straight_cents"]
                ln["otp"] += c["ot_premium_cents"]
                ln["dtp"] += c["dt_premium_cents"]
                ln["mealc"] += c["meal_cents"]
                ln["weeks"][ws] = c["gross_cents"]
                has = True
        earlier = {ws for ws in st.weeks if FIRST_PERIOD <= ws < period} | {
            w for (e, w) in self.paid if e == eid and w < period}
        for ws in sorted(earlier):
            if not st.payable(ws):
                continue
            diff = st.weeks[ws].cents()["gross_cents"] - self.paid.get((eid, ws), 0)
            if diff:
                ln["adjustments"][ws] = diff
                has = True
        if not has:
            return None
        ln["adj"] = sum(ln["adjustments"].values())
        ln["gross"] = ln["straight"] + ln["otp"] + ln["dtp"] + ln["mealc"] + ln["adj"]
        return ln

    def held(self, eid: int, period: date) -> list[date]:
        st = State(self.emps[eid])
        return [ws for ws in (period, period + timedelta(days=7))
                if not st.payable(ws) and (st.has_work(ws) or ws in st.exceptions)]

    def csv_row(self, eid: int, period: date) -> dict | None:
        ln = self.line(eid, period)
        if ln is None:
            return None
        return {"reg_hours": hours(ln["reg"]), "ot_hours": hours(ln["ot"]), "dt_hours": hours(ln["dt"]),
                "meal_premiums": str(ln["meal"]), "straight_cents": str(ln["straight"]),
                "ot_premium_cents": str(ln["otp"]), "dt_premium_cents": str(ln["dtp"]), "meal_cents": str(ln["mealc"]),
                "adjustment_cents": str(ln["adj"]), "gross_cents": str(ln["gross"])}

    def finalize(self, period: date, eids) -> dict[int, dict]:
        lines = {}
        for eid in eids:
            ln = self.line(eid, period)
            if ln:
                lines[eid] = ln
        for eid, ln in lines.items():
            for ws, g in ln["weeks"].items():
                self.paid[(eid, ws)] = self.paid.get((eid, ws), 0) + g
            for ws, g in ln["adjustments"].items():
                self.paid[(eid, ws)] = self.paid.get((eid, ws), 0) + g
        self.finalized_periods.add(period)
        return lines

    def day_breakdown(self, eid: int, ws: date) -> dict[date, tuple[str, str, str]]:
        st = State(self.emps[eid])
        w = st.weeks.get(ws)
        out = {}
        for i in range(7):
            d = ws + timedelta(days=i)
            r, o, t = (w.per_day.get(d, [0, 0, 0]) if w else [0, 0, 0])
            out[d] = (hours(r), hours(o), hours(t))
        return out


# ---- punch generation ------------------------------------------------------------------------------
class Gen:
    """Builds punch rows for one employee from shift plans in local wall-clock time."""

    def __init__(self, rnd: random.Random, prefix: str):
        self.rnd, self.prefix, self.n = rnd, prefix, 0

    def pid(self) -> str:
        self.n += 1
        return f"{self.prefix}-{self.n}"

    def shift(self, emp: Emp, start_local: datetime, worked_min: int, job: str,
              breaks: list[tuple[int, int]] = (), jitter: bool = True) -> list[dict]:
        """breaks: (worked minutes before the break starts, break length minutes)."""
        zone = ZoneInfo(emp.tz)
        start = start_local.replace(tzinfo=zone).astimezone(timezone.utc) if start_local.tzinfo is None else start_local
        rows, t, worked = [], start, 0

        def at(dt: datetime) -> str:
            if jitter:
                roll = self.rnd.random()
                if roll < 0.08:
                    dt = dt + timedelta(minutes=3)  # exact tie: rounds up
                elif roll < 0.6:
                    dt = dt + timedelta(seconds=self.rnd.randrange(-170, 171))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if self.rnd.random() < 0.5 else \
                dt.astimezone(zone).isoformat(timespec="seconds")

        rows.append({"punch_id": self.pid(), "employee_id": emp.eid, "kind": "in", "at": at(t), "job_code": job})
        for before, length in breaks:
            t = t + timedelta(minutes=before - worked)
            worked = before
            rows.append({"punch_id": self.pid(), "employee_id": emp.eid, "kind": "break_start", "at": at(t)})
            t = t + timedelta(minutes=length)
            rows.append({"punch_id": self.pid(), "employee_id": emp.eid, "kind": "break_end", "at": at(t)})
        t = t + timedelta(minutes=worked_min - worked)
        rows.append({"punch_id": self.pid(), "employee_id": emp.eid, "kind": "out", "at": at(t)})
        return rows


def to_punches(rows: list[dict]) -> list:
    out = []
    for r in rows:
        dt = datetime.fromisoformat(r["at"].replace("Z", "+00:00"))
        out.append(P.Punch(r["punch_id"], r["kind"], P.to_us(dt), r.get("job_code") if r["kind"] == "in" else None))
    return out


def compliant_breaks(rnd: random.Random, worked_min: int) -> list[tuple[int, int]]:
    """A meal break that keeps the shift premium-free: >= 30 min (after rounding), starting by 5 h worked."""
    if worked_min <= 300:
        return []
    return [(rnd.choice([150, 180, 210, 240, 270]), rnd.choice([36, 42, 45, 60]))]
