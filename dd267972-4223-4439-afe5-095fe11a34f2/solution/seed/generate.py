#!/usr/bin/env python3
"""Deterministic generator for Shiftbook's pre-existing data (environment and verifier seed).

Writes 001_schema.sql and 002_records.sql. The same bytes ship in environment/services/db/init/
and tests/services/db/init/. Re-running with the same SEED reproduces them exactly.

Usage: python3 generate.py <out_dir>
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SEED = 20261110
N_EMPLOYEES = 1500
WEEKS = [date(2026, 10, 18), date(2026, 10, 25), date(2026, 11, 1), date(2026, 11, 8)]
LOCATIONS = [
    (1, "Mission St, San Francisco", "America/Los_Angeles", "CA"),
    (2, "Roosevelt Row, Phoenix", "America/Phoenix", "FLSA"),
    (3, "Wicker Park, Chicago", "America/Chicago", "FLSA"),
]
JOBS = {"COOK": (2200, 3200), "PREP": (1800, 2400), "SERVER": (1650, 1900), "DISH": (1650, 1850),
        "HOST": (1700, 2000), "BAR": (1900, 2600)}
FIRST = ["Maria", "James", "Aiyana", "Wei", "Priya", "Luis", "Fatima", "Noah", "Keiko", "Omar", "Elena",
         "Tariq", "Grace", "Mateo", "Hana", "Darius", "Sofia", "Kwame", "Ines", "Rowan", "Yusuf", "Lena",
         "Arjun", "Chloe", "Diego", "Amara", "Felix", "Nadia", "Theo", "Zara"]
LAST = ["Lopez", "Nguyen", "Okafor", "Chen", "Patel", "Garcia", "Haddad", "Kim", "Silva", "Novak", "Reyes",
        "Moreau", "Ibrahim", "Kowalski", "Tanaka", "Mensah", "Rossi", "Fischer", "Abara", "Lindqvist",
        "O'Brien", "Dubois", "Varga", "Sato", "Mahlangu", "Costa", "Byrne", "Aziz", "Holm", "Quinn"]

SCHEMA = """\
CREATE TABLE locations (
    id integer PRIMARY KEY,
    name text NOT NULL,
    timezone text NOT NULL,
    ruleset text NOT NULL CHECK (ruleset IN ('CA', 'FLSA'))
);
CREATE TABLE employees (
    id serial PRIMARY KEY,
    email text NOT NULL,
    full_name text NOT NULL,
    location_id integer NOT NULL REFERENCES locations (id),
    manager_id integer REFERENCES employees (id),
    role text NOT NULL CHECK (role IN ('employee', 'manager', 'payroll_admin')),
    active boolean NOT NULL DEFAULT true,
    hired_on date NOT NULL
);
CREATE TABLE job_rates (
    id serial PRIMARY KEY,
    employee_id integer NOT NULL REFERENCES employees (id),
    job_code text NOT NULL,
    rate_cents integer NOT NULL CHECK (rate_cents > 0),
    effective_from date NOT NULL,
    UNIQUE (employee_id, job_code, effective_from)
);
CREATE TABLE legacy_punches (
    id bigint PRIMARY KEY,
    employee_id integer NOT NULL REFERENCES employees (id),
    job_code text,
    kind text NOT NULL CHECK (kind IN ('in', 'out', 'break_start', 'break_end')),
    local_time timestamp NOT NULL,
    fold smallint NOT NULL DEFAULT 0 CHECK (fold IN (0, 1))
);
CREATE INDEX legacy_punches_employee ON legacy_punches (employee_id);
CREATE TABLE legacy_approvals (
    employee_id integer NOT NULL REFERENCES employees (id),
    week_start date NOT NULL,
    approved_by integer NOT NULL REFERENCES employees (id),
    approved_at timestamptz NOT NULL,
    PRIMARY KEY (employee_id, week_start)
);
"""


def q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def copy_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")


def main(out: Path) -> None:
    rnd = random.Random(SEED)
    out.mkdir(parents=True, exist_ok=True)
    (out / "001_schema.sql").write_text(SCHEMA)
    lines: list[str] = ["BEGIN;"]
    for lid, name, tz, rs in LOCATIONS:
        lines.append(f"INSERT INTO locations VALUES ({lid}, {q(name)}, {q(tz)}, {q(rs)});")

    # ---- employees: 2 payroll admins, 3 GMs, 4 kitchen managers per location, staff --------------
    emps: list[dict] = []
    used_emails: set[str] = set()

    def new_emp(role, loc, manager):
        eid = len(emps) + 1
        while True:
            fn, ln = rnd.choice(FIRST), rnd.choice(LAST)
            base = f"{fn}.{ln}".lower().replace("'", "")
            email = f"{base}.{eid}@shiftbook.example"
            if email not in used_emails:
                break
        used_emails.add(email)
        stored = email
        roll = rnd.random()
        if roll < 0.06:
            stored = f"{fn}.{ln.replace(chr(39), '')}.{eid}@Shiftbook.Example"
        elif roll < 0.09:
            stored = f"  {email} "
        elif roll < 0.11:
            stored = email.upper()
        e = {"id": eid, "email": stored, "norm": email, "name": f"{fn} {ln}", "loc": loc,
             "manager": manager, "role": role, "active": True,
             "hired": date(2024, 1, 1) + timedelta(days=rnd.randrange(900))}
        emps.append(e)
        return e

    admin1 = new_emp("payroll_admin", 1, None)
    admin1["name"], admin1["email"], admin1["norm"] = "Dana Whitfield", "dana.whitfield@shiftbook.example", "dana.whitfield@shiftbook.example"
    admin2 = new_emp("payroll_admin", 3, None)
    admin2["name"], admin2["email"], admin2["norm"] = "Omar Haddad", "omar.haddad@shiftbook.example", "omar.haddad@shiftbook.example"
    used_emails.update({admin1["norm"], admin2["norm"]})
    managers_by_loc: dict[int, list[dict]] = {}
    for lid, *_ in LOCATIONS:
        gm = new_emp("manager", lid, admin1["id"])
        kms = [new_emp("manager", lid, gm["id"]) for _ in range(4)]
        managers_by_loc[lid] = [gm] + kms
    while len(emps) < N_EMPLOYEES:
        lid = rnd.choice([1, 1, 2, 3])
        km = rnd.choice(managers_by_loc[lid][1:])
        e = new_emp("employee", lid, km["id"])
        if rnd.random() < 0.02:
            e["active"] = False
    for e in emps:
        lines.append(
            "INSERT INTO employees (id, email, full_name, location_id, manager_id, role, active, hired_on) VALUES "
            f"({e['id']}, {q(e['email'])}, {q(e['name'])}, {e['loc']}, {e['manager'] if e['manager'] else 'NULL'}, "
            f"{q(e['role'])}, {'true' if e['active'] else 'false'}, '{e['hired'].isoformat()}');"
        )
    lines.append(f"SELECT setval('employees_id_seq', {len(emps)});")

    # ---- job rates ---------------------------------------------------------------------------------
    rates: dict[int, dict[str, list[tuple[date, int]]]] = {}
    rate_rows = []
    for e in emps:
        jobs = rnd.sample(list(JOBS), rnd.choice([1, 1, 1, 2, 2, 3]))
        rates[e["id"]] = {}
        for j in jobs:
            lo, hi = JOBS[j]
            r = rnd.randrange(lo, hi + 1, 5)
            rates[e["id"]][j] = [(date(2026, 1, 1), r)]
            rate_rows.append((e["id"], j, r, date(2026, 1, 1)))
        if rnd.random() < 0.10:  # raise mid legacy window
            j = rnd.choice(jobs)
            r2 = rates[e["id"]][j][0][1] + rnd.choice([50, 75, 100, 125, 250])
            eff = rnd.choice([date(2026, 10, 25), date(2026, 10, 28), date(2026, 11, 4)])
            rates[e["id"]][j].append((eff, r2))
            rate_rows.append((e["id"], j, r2, eff))
        if rnd.random() < 0.03:  # a job that starts mid-window
            extra = [j for j in JOBS if j not in jobs]
            if extra:
                j = rnd.choice(extra)
                lo, hi = JOBS[j]
                r = rnd.randrange(lo, hi + 1, 5)
                eff = date(2026, 10, 27)
                rates[e["id"]][j] = [(eff, r)]
                rate_rows.append((e["id"], j, r, eff))
    for i, (eid, j, r, eff) in enumerate(rate_rows, start=1):
        lines.append(f"INSERT INTO job_rates (id, employee_id, job_code, rate_cents, effective_from) VALUES ({i}, {eid}, {q(j)}, {r}, '{eff.isoformat()}');")
    lines.append(f"SELECT setval('job_rates_id_seq', {len(rate_rows)});")

    # ---- legacy punches ----------------------------------------------------------------------------
    tzs = {lid: ZoneInfo(tz) for lid, _, tz, _ in LOCATIONS}
    punch_rows: list[tuple] = []
    approvals: list[tuple] = []
    pid = 1000000

    def add(eid, job, kind, instant: datetime, zone):
        nonlocal pid
        local = instant.astimezone(zone)
        pid += rnd.randrange(1, 4)
        punch_rows.append((pid, eid, job if kind == "in" else None, kind, local.replace(tzinfo=None), local.fold))

    def jitter(dt: datetime) -> datetime:
        roll = rnd.random()
        if roll < 0.05:  # exact 3-minute tie past a 6-minute boundary
            base = dt.replace(second=0, microsecond=0)
            base -= timedelta(minutes=base.minute % 6)
            return base + timedelta(minutes=3)
        return dt + timedelta(seconds=rnd.randrange(-300, 301))

    for e in emps:
        if e["role"] == "payroll_admin" or not e["active"]:
            continue
        zone = tzs[e["loc"]]
        last_end: datetime | None = None
        for ws in WEEKS:
            nshifts = rnd.choice([3, 4, 5, 5, 6, 6, 7])
            days = sorted(rnd.sample(range(7), nshifts)) if nshifts < 7 else list(range(7))
            anomaly = rnd.random() < 0.025
            anomaly_day = rnd.choice(days) if anomaly else None
            week_has_exception = False
            for d in days:
                day = ws + timedelta(days=d)
                start_h = rnd.choice([5, 6, 6, 7, 9, 10, 11, 14, 15, 16, 17, 18, 20, 22])
                start = datetime(day.year, day.month, day.day, start_h, rnd.choice([0, 0, 15, 30, 45]), tzinfo=zone)
                if (e["loc"] in (1, 3) and day == date(2026, 10, 31) and rnd.random() < 0.5):
                    start = datetime(2026, 10, 31, rnd.choice([21, 22, 23]), 0, tzinfo=zone)  # across DST end
                length = timedelta(minutes=rnd.choice([240, 300, 330, 360, 420, 480, 510, 540, 600, 690, 750, 780]))
                start_utc = start.astimezone(timezone.utc)
                if last_end and start_utc < last_end + timedelta(hours=2):
                    continue
                end_utc = start_utc + length
                jobs_ok = list(rates[e["id"]])
                job = rnd.choice(jobs_ok)
                kind_anom = rnd.choice(["no_out", "double_in", "open_break", "too_long"]) if d == anomaly_day else None
                if kind_anom == "too_long":
                    end_utc = start_utc + timedelta(hours=16, minutes=30)
                add(e["id"], job, "in", jitter(start_utc), zone)
                if kind_anom == "double_in":
                    add(e["id"], job, "in", start_utc + timedelta(minutes=47), zone)
                    week_has_exception = True
                worked = length
                if worked > timedelta(hours=5) and rnd.random() < 0.8:
                    choices = [m for m in (180, 210, 240, 270, 294, 300, 306, 330) if m <= length.total_seconds() / 60 - 60]
                    b_at = start_utc + timedelta(minutes=rnd.choice(choices))
                    b_len = timedelta(minutes=rnd.choice([24, 30, 30, 30, 36, 45]))
                    add(e["id"], None, "break_start", jitter(b_at), zone)
                    if kind_anom == "open_break":
                        week_has_exception = True
                        last_end = end_utc
                        continue
                    add(e["id"], None, "break_end", jitter(b_at + b_len), zone)
                    end_utc += b_len
                elif kind_anom == "open_break":
                    add(e["id"], None, "break_start", start_utc + timedelta(hours=2), zone)
                    week_has_exception = True
                    last_end = end_utc
                    continue
                if kind_anom == "no_out":
                    week_has_exception = True
                    last_end = end_utc
                    continue
                if kind_anom == "too_long":
                    week_has_exception = True
                add(e["id"], None, "out", jitter(end_utc), zone)
                last_end = end_utc
            p_approve = {0: 0.96, 1: 0.95, 2: 0.7, 3: 0.3}[WEEKS.index(ws)]
            if rnd.random() < p_approve and (not week_has_exception or rnd.random() < 0.4):
                approvals.append((e["id"], ws, e["manager"] or admin1["id"],
                                  datetime(ws.year, ws.month, ws.day, 12, 0, tzinfo=timezone.utc) + timedelta(days=8)))
    lines.append("COPY legacy_punches (id, employee_id, job_code, kind, local_time, fold) FROM stdin;")
    for row in punch_rows:
        pid_, eid, job, kind, lt, fold = row
        lines.append("\t".join([str(pid_), str(eid), copy_text(job) if job else "\\N", kind,
                                lt.strftime("%Y-%m-%d %H:%M:%S"), str(fold)]))
    lines.append("\\.")
    lines.append("COPY legacy_approvals (employee_id, week_start, approved_by, approved_at) FROM stdin;")
    for eid, ws, by, at in approvals:
        lines.append(f"{eid}\t{ws.isoformat()}\t{by}\t{at.strftime('%Y-%m-%d %H:%M:%S+00')}")
    lines.append("\\.")
    lines.append("COMMIT;")
    (out / "002_records.sql").write_text("\n".join(lines) + "\n")
    print(f"employees={len(emps)} rates={len(rate_rows)} punches={len(punch_rows)} approvals={len(approvals)}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
