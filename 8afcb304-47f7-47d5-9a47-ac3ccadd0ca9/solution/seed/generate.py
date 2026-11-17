#!/usr/bin/env python3
"""Deterministic generator for Harborview Physio's pre-existing data (environment and verifier seed).

Writes 001_schema.sql and 002_records.sql. The same bytes ship in environment/services/db/init/
and tests/services/db/init/. Re-running with the same SEED reproduces them exactly.

Quirks the instruction tells the application how to treat:
  * addresses with odd case and stray spaces; a few addresses on two rows (R1.2), sometimes with
    the lower-id row inactive;
  * inactive users and clinicians; an inactive room; an inactive appointment type;
  * schedule windows whose end is not after their start; overlapping windows; windows ending 24:00;
    two versions with the same effective_from (higher id wins); a version with no windows;
  * night-clinic appointments on 2026-11-01 01:00-02:00 Seattle with fold 0 and fold 1, and two
    recorded at a wall-clock time that never happened (2027-03-14 02:30);
  * appointments with no room_id; cancelled appointments; notes and names holding markup.

Usage: python3 generate.py <out_dir>
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SEED = 20261110
CUTOFF = date(2026, 11, 10)  # the old system's "today": earlier appointments are history
FIRST_DAY, LAST_DAY = date(2026, 10, 1), date(2027, 6, 30)
FAR_FIRST, FAR_LAST = date(2027, 7, 1), date(2030, 6, 30)  # sparse far-future bookings (see valid_until)
N_SEA, N_HNL = 80, 70
N_PATIENTS = 4200
CLINICS = [(1, "Harborview Physio Seattle (South Lake Union)", "America/Los_Angeles"),
           (2, "Harborview Physio Honolulu (Kakaako)", "Pacific/Honolulu")]
ROOMS = [  # id, clinic, name, type, active
    (1, 1, "Treatment 1", "treatment", True), (2, 1, "Treatment 2", "treatment", True),
    (3, 1, "Treatment 3", "treatment", True), (4, 2, "Treatment A", "treatment", True),
    (5, 1, "Treatment 4", "treatment", True), (6, 2, "Treatment B", "treatment", True),
    (7, 1, "Treatment 5", "treatment", True), (8, 1, "Treatment 6", "treatment", True),
    (9, 2, "Treatment C", "treatment", True), (10, 1, "Gym North", "gym", True),
    (11, 2, "Treatment D", "treatment", True), (12, 1, "Gym South", "gym", True),
    (13, 1, "Hydro pool", "hydro", True), (14, 1, "Treatment 7 (closed)", "treatment", False),
    (15, 2, "Lanai gym", "gym", True), (16, 2, "Treatment E", "treatment", True),
]
TYPES = [  # id, code, name, duration, buffer, room type, active
    (1, "INIT60", "Initial assessment", 60, 15, "treatment", True),
    (2, "FU30", "Follow-up", 30, 10, "treatment", True),
    (3, "FU45", "Extended follow-up", 45, 15, "treatment", True),
    (4, "REHAB45", "Gym rehab session", 45, 15, "gym", True),
    (5, "HYDRO60", "Hydrotherapy", 60, 30, "hydro", True),
    (6, "DRYN30", "Dry needling (retired)", 30, 10, "treatment", False),
]
FEES = [  # type, effective_from, fee, bp
    (1, "2024-01-01", 16500, 2000), (2, "2024-01-01", 9500, 2000), (3, "2024-01-01", 12550, 2000),
    (4, "2024-01-01", 11000, 1750), (5, "2024-01-01", 14500, 2250), (6, "2024-01-01", 8000, 2000),
    (1, "2027-01-01", 17250, 2000), (2, "2027-01-01", 9900, 1750), (4, "2027-01-01", 11500, 1750),
]
FIRST = ["Ana", "Ben", "Keoni", "Mei", "Priya", "Luis", "Fatima", "Noah", "Keiko", "Omar", "Elena", "Kai",
         "Grace", "Mateo", "Hana", "Darius", "Sofia", "Kwame", "Ines", "Rowan", "Malia", "Lena", "Arjun",
         "Chloe", "Diego", "Amara", "Felix", "Nadia", "Theo", "Zara", "Iolana", "Tomas", "Yuki", "Ravi"]
LAST = ["Lopez", "Nguyen", "Okafor", "Chen", "Patel", "Garcia", "Kahananui", "Kim", "Silva", "Novak", "Reyes",
        "Moreau", "Ibrahim", "Kowalski", "Tanaka", "Mensah", "Rossi", "Fischer", "Akana", "Lindqvist",
        "O'Brien", "Dubois", "Varga", "Sato", "Mahoe", "Costa", "Byrne", "Aziz", "Holm", "Quinn"]
DOMAINS = ["mail.example", "inbox.example", "post.example", "kahu.example"]
NOTE_BITS = ["Reports knee pain on stairs.", "ROM improved 10 deg.", "Continue home exercise program.",
             "Lower back stiffness in the morning.", "Taped ankle; review in 2 weeks.", "Shoulder impingement signs.",
             "Gait retraining started.", "Pain 3/10 today.", "Post-op week 6: progress loading.", "Discharged with plan."]
XSS_NOTE = "Patient said <img src=x onerror=\"window.__fk_xss&&__fk_xss('legacy-note')\"> during session."
XSS_NAME = "Kalani <svg onload=\"window.__fk_xss&&__fk_xss('legacy-name')\"> Ho"
ODD_NAMES = ["Lee, Sunny", "=HYPERLINK(\"http://evil.example\",\"x\")", "+1 Patient", "-Dash Name", "@Mention Kai", 'Moana "Mo" Kealoha']
CLOSURES = [(1, "2026-11-26", "Thanksgiving"), (1, "2026-12-25", "Christmas Day"), (2, "2026-12-25", "Christmas Day"),
            (1, "2027-01-01", "New Year's Day"), (2, "2027-01-01", "New Year's Day"), (2, "2027-03-26", "Prince Kuhio Day"),
            (2, "2027-06-11", "King Kamehameha Day"), (1, "2027-05-31", "Memorial Day"), (2, "2026-11-11", "Staff training"),
            (1, "2027-02-15", "Presidents' Day")]
# later holidays the old system already had on file (appended, so every id above stays the same; see valid_until)
CLOSURES += [(1, "2027-07-05", "Independence Day (observed)"), (1, "2027-11-25", "Thanksgiving"), (1, "2027-12-24", "Christmas Eve"),
             (2, "2027-12-24", "Christmas Eve"), (2, "2028-03-27", "Prince Kuhio Day"), (1, "2028-05-29", "Memorial Day"),
             (2, "2028-06-12", "King Kamehameha Day"), (1, "2028-11-23", "Thanksgiving"), (1, "2028-12-25", "Christmas Day"),
             (2, "2028-12-25", "Christmas Day"), (1, "2029-01-01", "New Year's Day"), (2, "2029-01-01", "New Year's Day"),
             (2, "2029-03-26", "Prince Kuhio Day"), (1, "2029-05-28", "Memorial Day"), (2, "2029-06-11", "King Kamehameha Day"),
             (1, "2029-11-22", "Thanksgiving"), (1, "2029-12-25", "Christmas Day"), (2, "2029-12-25", "Christmas Day"),
             (1, "2030-01-01", "New Year's Day"), (2, "2030-01-01", "New Year's Day"), (1, "2030-02-18", "Presidents' Day"),
             (2, "2030-03-26", "Prince Kuhio Day"), (1, "2030-05-27", "Memorial Day"), (2, "2030-06-11", "King Kamehameha Day")]

SCHEMA = """\
CREATE TABLE clinics (
    id integer PRIMARY KEY,
    name text NOT NULL,
    timezone text NOT NULL
);
CREATE TABLE clinic_closures (
    id serial PRIMARY KEY,
    clinic_id integer NOT NULL REFERENCES clinics (id),
    on_date date NOT NULL,
    reason text NOT NULL,
    UNIQUE (clinic_id, on_date)
);
CREATE TABLE rooms (
    id serial PRIMARY KEY,
    clinic_id integer NOT NULL REFERENCES clinics (id),
    name text NOT NULL,
    room_type text NOT NULL,
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE users (
    id serial PRIMARY KEY,
    email text NOT NULL,
    full_name text NOT NULL,
    role text NOT NULL CHECK (role IN ('patient', 'clinician', 'front_desk', 'manager', 'admin')),
    clinic_id integer REFERENCES clinics (id),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE clinicians (
    id serial PRIMARY KEY,
    user_id integer NOT NULL UNIQUE REFERENCES users (id),
    clinic_id integer NOT NULL REFERENCES clinics (id),
    max_per_day integer NOT NULL CHECK (max_per_day > 0),
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE appointment_types (
    id serial PRIMARY KEY,
    code text NOT NULL UNIQUE,
    name text NOT NULL,
    duration_minutes integer NOT NULL CHECK (duration_minutes > 0),
    buffer_minutes integer NOT NULL DEFAULT 0 CHECK (buffer_minutes >= 0),
    room_type text NOT NULL,
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE type_fees (
    id serial PRIMARY KEY,
    type_id integer NOT NULL REFERENCES appointment_types (id),
    effective_from date NOT NULL,
    fee_cents integer NOT NULL CHECK (fee_cents > 0),
    deposit_bp integer NOT NULL CHECK (deposit_bp > 0 AND deposit_bp <= 10000),
    UNIQUE (type_id, effective_from)
);
CREATE TABLE schedule_versions (
    id serial PRIMARY KEY,
    clinician_id integer NOT NULL REFERENCES clinicians (id),
    effective_from date NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE schedule_windows (
    id serial PRIMARY KEY,
    version_id integer NOT NULL REFERENCES schedule_versions (id),
    weekday smallint NOT NULL CHECK (weekday BETWEEN 1 AND 7),
    start_time time NOT NULL,
    end_time time NOT NULL
);
CREATE TABLE schedule_exceptions (
    id serial PRIMARY KEY,
    clinician_id integer NOT NULL REFERENCES clinicians (id),
    on_date date NOT NULL,
    kind text NOT NULL CHECK (kind IN ('off', 'extra')),
    start_time time,
    end_time time
);
CREATE TABLE appointments (
    id serial PRIMARY KEY,
    patient_id integer NOT NULL REFERENCES users (id),
    clinician_id integer NOT NULL REFERENCES clinicians (id),
    type_id integer NOT NULL REFERENCES appointment_types (id),
    room_id integer REFERENCES rooms (id),
    local_start timestamp NOT NULL,
    fold smallint NOT NULL DEFAULT 0 CHECK (fold IN (0, 1)),
    status text NOT NULL,
    deposit_cents integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX appointments_clinician ON appointments (clinician_id, local_start);
CREATE INDEX appointments_room ON appointments (room_id, local_start);
CREATE INDEX appointments_patient ON appointments (patient_id);
CREATE TABLE clinical_notes (
    id serial PRIMARY KEY,
    appointment_id integer NOT NULL REFERENCES appointments (id),
    author_id integer NOT NULL REFERENCES users (id),
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX clinical_notes_appointment ON clinical_notes (appointment_id);
"""


def q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def hm(m: int) -> str:
    return "24:00" if m == 1440 else f"{m // 60:02d}:{m % 60:02d}"


def days(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def main(out: Path) -> None:
    rnd = random.Random(SEED)
    out.mkdir(parents=True, exist_ok=True)
    (out / "001_schema.sql").write_text(SCHEMA)
    L: list[str] = ["BEGIN;"]
    for cid, name, tz in CLINICS:
        L.append(f"INSERT INTO clinics VALUES ({cid}, {q(name)}, {q(tz)});")
    for i, (cid, d, why) in enumerate(CLOSURES, 1):
        L.append(f"INSERT INTO clinic_closures (id, clinic_id, on_date, reason) VALUES ({i}, {cid}, '{d}', {q(why)});")
    L.append(f"SELECT setval('clinic_closures_id_seq', {len(CLOSURES)});")
    for rid, cid, name, rt, act in ROOMS:
        L.append(f"INSERT INTO rooms (id, clinic_id, name, room_type, active) VALUES ({rid}, {cid}, {q(name)}, {q(rt)}, {str(act).lower()});")
    L.append(f"SELECT setval('rooms_id_seq', {len(ROOMS)});")
    for tid, code, name, dur, buf, rt, act in TYPES:
        L.append(f"INSERT INTO appointment_types (id, code, name, duration_minutes, buffer_minutes, room_type, active) VALUES "
                 f"({tid}, {q(code)}, {q(name)}, {dur}, {buf}, {q(rt)}, {str(act).lower()});")
    L.append(f"SELECT setval('appointment_types_id_seq', {len(TYPES)});")
    for i, (tid, eff, fee, bp) in enumerate(FEES, 1):
        L.append(f"INSERT INTO type_fees (id, type_id, effective_from, fee_cents, deposit_bp) VALUES ({i}, {tid}, '{eff}', {fee}, {bp});")
    L.append(f"SELECT setval('type_fees_id_seq', {len(FEES)});")

    # ---- users ---------------------------------------------------------------------------------
    users: list[dict] = []
    used: set[str] = set()

    def add_user(role, name=None, email=None, active=True, quirk=True, clinic=None):
        uid = len(users) + 1
        if name is None:
            name = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
        if email is None:
            base = name.lower().replace("'", "").replace(" ", ".")
            dom = "harborviewphysio.example" if role != "patient" else rnd.choice(DOMAINS)
            email = f"{base}.{uid}@{dom}"
        norm = email.strip().lower()
        used.add(norm)
        stored = email
        if quirk:
            roll = rnd.random()
            if roll < 0.06:
                stored = email.split("@")[0].title() + "@" + email.split("@")[1].upper()
            elif roll < 0.09:
                stored = f"  {email} "
            elif roll < 0.11:
                stored = email.upper()
        u = {"id": uid, "email": stored, "norm": norm, "name": name, "role": role, "active": active, "clinic": clinic,
             "created": datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(hours=rnd.randrange(20000))}
        users.append(u)
        return u

    add_user("admin", "Leilani Kahale", "leilani.kahale@harborviewphysio.example", quirk=False)
    add_user("admin", "Marcus Webb", "marcus.webb@harborviewphysio.example", quirk=False)
    for nm, cl in [("Jun Park", 1), ("Rosa Delgado", 1), ("Ikaika Long", 2), ("Bea Thornton", 2), ("Sione Fifita", 2),
                   ("Maren Olsen", 1)]:
        add_user("front_desk", nm, nm.lower().replace(" ", ".") + "@harborviewphysio.example",
                 active=(nm != "Maren Olsen"), quirk=False, clinic=cl)
    for nm, cl in [("Tess Harlow", 1), ("Kalea Nakoa", 2)]:
        add_user("manager", nm, nm.lower().replace(" ", ".") + "@harborviewphysio.example", quirk=False, clinic=cl)
    clin_users = [add_user("clinician", quirk=True) for _ in range(N_SEA + N_HNL)]
    patients = []
    while len(patients) < N_PATIENTS:
        p = add_user("patient", active=rnd.random() > 0.015)
        patients.append(p)
    # the XSS name on a patient with future appointments, and duplicates of addresses (R1.2)
    patients[17]["name"] = XSS_NAME
    for k, nm in enumerate(ODD_NAMES):
        patients[23 + 11 * k]["name"] = nm
    dup_pairs = []
    for i in range(30):
        src = patients[40 + i * 37]
        variants = [src["norm"].upper(), "  " + src["norm"] + "  ", src["norm"].title(), " " + src["norm"].upper()]
        dup = {"id": len(users) + 1, "email": variants[i % 4], "norm": src["norm"], "clinic": None,
               "name": src["name"] + (" (old record)" if i % 3 == 0 else ""), "role": "patient", "active": True,
               "created": src["created"] + timedelta(days=30)}
        if i % 5 == 0:
            src["active"] = False  # then the duplicate (higher id) is the account
        if i % 7 == 3:
            dup["active"] = False
        users.append(dup)
        patients.append(dup)
        dup_pairs.append((src["id"], dup["id"]))
    for u in users:
        L.append(f"INSERT INTO users (id, email, full_name, role, clinic_id, active, created_at) VALUES ({u['id']}, {q(u['email'])}, "
                 f"{q(u['name'])}, {q(u['role'])}, {u['clinic'] or 'NULL'}, {str(u['active']).lower()}, '{u['created'].isoformat()}');")
    L.append(f"SELECT setval('users_id_seq', {len(users)});")

    # ---- clinicians, versions, windows, exceptions --------------------------------------------
    clin: list[dict] = []
    for i, u in enumerate(clin_users):
        clinic = 1 if i < N_SEA else 2
        c = {"id": i + 1, "user": u["id"], "clinic": clinic, "max": rnd.choice([5, 6, 7, 8, 8, 9, 10, 12]),
             "active": i not in (11, 57, 101), "night": clinic == 1 and i in (3, 9, 14, 22, 31, 40)}
        clin.append(c)
        L.append(f"INSERT INTO clinicians (id, user_id, clinic_id, max_per_day, active) VALUES "
                 f"({c['id']}, {c['user']}, {clinic}, {c['max']}, {str(c['active']).lower()});")
    L.append(f"SELECT setval('clinicians_id_seq', {len(clin)});")

    versions: list[dict] = []  # {"id", "cid", "eff", "windows": [(wd, s, e)]}

    def pattern(c, odd=True):
        wins = []
        workdays = sorted(rnd.sample(range(1, 6), rnd.choice([3, 4, 4, 5, 5])))
        if rnd.random() < 0.35:
            workdays.append(6)
        for wd in workdays:
            s = rnd.choice([420, 450, 480, 490, 510, 540, 570]) + (rnd.choice([0, 5, 10, 20]) if odd else 0)
            e = rnd.choice([960, 990, 1020, 1050, 1080, 1110]) - (rnd.choice([0, 5, 10]) if odd else 0)
            if wd == 6:
                e = min(e, 780)
            if rnd.random() < 0.6:
                lunch = rnd.choice([720, 750, 765])
                wins.append((wd, s, lunch))
                wins.append((wd, lunch + rnd.choice([30, 45, 60]), e))
            else:
                wins.append((wd, s, e))
        if c["night"]:
            wins += [(7, 0, 180), (6, 1200, 1440), (7, 1320, 1440)]
        return wins

    for c in clin:
        v1 = {"cid": c["id"], "eff": date(2025, 1, 6) + timedelta(days=rnd.randrange(300)), "windows": pattern(c)}
        vs = [v1]
        r = rnd.random()
        if r < 0.6:
            vs.append({"cid": c["id"], "eff": date(2026, 9, 1) + timedelta(days=rnd.randrange(240)), "windows": pattern(c)})
        if r < 0.15:
            vs.append({"cid": c["id"], "eff": date(2027, 1, 4) + timedelta(days=7 * rnd.randrange(12)), "windows": pattern(c)})
        if c["id"] % 25 == 7:  # a version with no windows (leave), then back
            vs.append({"cid": c["id"], "eff": date(2027, 2, 1), "windows": []})
            vs.append({"cid": c["id"], "eff": date(2027, 3, 1), "windows": pattern(c)})
        if c["id"] % 23 == 5:  # two versions on the same date: the higher id wins
            eff = date(2026, 12, 7)
            vs.append({"cid": c["id"], "eff": eff, "windows": pattern(c)})
            vs.append({"cid": c["id"], "eff": eff, "windows": pattern(c, odd=False)})
        if c["id"] % 13 == 4:  # malformed windows (end not after start): ignored
            for v in vs:
                if v["windows"]:
                    v["windows"].append((rnd.randrange(1, 8), 1020, 540))
                    v["windows"].append((rnd.randrange(1, 8), 720, 720))
        if c["id"] % 11 == 6:  # overlapping windows
            for v in vs:
                v["windows"] += [(2, 480, 780), (2, 750, 1020)]
        for v in vs:
            v["id"] = len(versions) + 1
            versions.append(v)
    for v in versions:
        L.append(f"INSERT INTO schedule_versions (id, clinician_id, effective_from, created_at) VALUES "
                 f"({v['id']}, {v['cid']}, '{v['eff']}', '2025-01-01T00:00:00+00:00');")
    wid = 0
    for v in versions:
        for wd, s, e in v["windows"]:
            wid += 1
            L.append(f"INSERT INTO schedule_windows (id, version_id, weekday, start_time, end_time) VALUES "
                     f"({wid}, {v['id']}, {wd}, '{hm(s)}', '{hm(e)}');")
    L.append(f"SELECT setval('schedule_versions_id_seq', {len(versions)});")
    L.append(f"SELECT setval('schedule_windows_id_seq', {max(wid, 1)});")

    exceptions: list[dict] = []
    for c in clin:
        if rnd.random() < 0.45:  # a holiday run
            start = date(2026, 11, 10) + timedelta(days=rnd.randrange(200))
            for k in range(rnd.randrange(2, 9)):
                exceptions.append({"cid": c["id"], "d": start + timedelta(days=k), "kind": "off", "s": None, "e": None})
        for _ in range(rnd.randrange(0, 5)):
            d = date(2026, 11, 1) + timedelta(days=rnd.randrange(220))
            kind = rnd.choice(["off", "off", "extra"])
            if kind == "off":
                s = rnd.choice([480, 600, 690, 720, 810, 900])
                e = s + rnd.choice([45, 60, 90, 120, 180])
            else:
                s = rnd.choice([420, 480, 540, 1020, 1080])
                e = s + rnd.choice([60, 90, 120, 180])
            exceptions.append({"cid": c["id"], "d": d, "kind": kind, "s": s, "e": e})
        if c["id"] % 17 == 2:  # malformed range: ignored
            exceptions.append({"cid": c["id"], "d": date(2026, 12, 2) + timedelta(days=c["id"] % 9), "kind": "extra", "s": 900, "e": 840})
        if c["night"]:
            exceptions.append({"cid": c["id"], "d": date(2027, 3, 14), "kind": "extra", "s": 150, "e": 300})
            exceptions.append({"cid": c["id"], "d": date(2026, 11, 1), "kind": "extra", "s": 90, "e": 210})
    # far-future exceptions from their own random stream (appended, so every row above stays identical)
    xrnd = random.Random(SEED + 11)
    for c in clin:
        for _ in range(xrnd.randrange(1, 6)):
            d = FAR_FIRST + timedelta(days=xrnd.randrange((FAR_LAST - FAR_FIRST).days + 1))
            kind = xrnd.choice(["whole", "off", "off", "extra"])
            if kind == "whole":
                exceptions.append({"cid": c["id"], "d": d, "kind": "off", "s": None, "e": None})
                continue
            if kind == "off":
                s = xrnd.choice([480, 600, 690, 720, 810, 900])
                e = s + xrnd.choice([45, 60, 90, 120, 180])
            else:
                s = xrnd.choice([420, 480, 540, 1020, 1080])
                e = s + xrnd.choice([60, 90, 120, 180])
            exceptions.append({"cid": c["id"], "d": d, "kind": kind, "s": s, "e": e})
    for i, x in enumerate(exceptions, 1):
        st = f"'{hm(x['s'])}'" if x["s"] is not None else "NULL"
        en = f"'{hm(x['e'])}'" if x["e"] is not None else "NULL"
        L.append(f"INSERT INTO schedule_exceptions (id, clinician_id, on_date, kind, start_time, end_time) VALUES "
                 f"({i}, {x['cid']}, '{x['d']}', {q(x['kind'])}, {st}, {en});")
    L.append(f"SELECT setval('schedule_exceptions_id_seq', {max(len(exceptions), 1)});")

    # ---- appointments ----------------------------------------------------------------------------
    by_c_ver: dict[int, list] = {}
    for v in versions:
        by_c_ver.setdefault(v["cid"], []).append(v)
    exc_by: dict[tuple, list] = {}
    for x in exceptions:
        exc_by.setdefault((x["cid"], x["d"]), []).append(x)

    closed = {(cid, date.fromisoformat(d)) for cid, d, _ in CLOSURES}

    def wall_windows(c, d):
        if (c["clinic"], d) in closed and d != date(2026, 11, 11):
            return []
        vs = [v for v in by_c_ver[c["id"]] if v["eff"] <= d]
        if not vs:
            return []
        v = max(vs, key=lambda v: (v["eff"], v["id"]))
        spans = [(s, e) for wd, s, e in v["windows"] if wd == d.isoweekday() and e > s]
        offs = []
        for x in exc_by.get((c["id"], d), []):
            if x["kind"] == "off" and x["s"] is None:
                return []
            if x["kind"] == "extra" and x["e"] > x["s"]:
                spans.append((x["s"], x["e"]))
            if x["kind"] == "off" and x["s"] is not None:
                offs.append((x["s"], x["e"]))
        return [(s, e) for s, e in spans if not any(s < oe and os_ < e for os_, oe in offs)]

    type_by_id = {t[0]: t for t in TYPES}
    room_busy: dict[tuple, list] = {}  # (room, date) -> [(s, e)] in wall minutes
    appts: list[dict] = []
    pat_active = [p for p in patients if p["role"] == "patient"]
    for d in days(FIRST_DAY, LAST_DAY):
        dense = date(2026, 11, 10) <= d <= date(2027, 2, 28)
        for c in clin:
            wins = wall_windows(c, d)
            if not wins:
                continue
            want = rnd.choice([0, 0, 0, 1, 1, 2, 3, 5, 9]) if dense else rnd.choice([0, 0, 0, 0, 0, 0, 1, 1, 2])
            want = min(want, c["max"] + (1 if rnd.random() < 0.03 else 0))
            busy: list[tuple[int, int]] = []
            for _ in range(want * 3):
                if len(busy) >= want:
                    break
                tid = rnd.choice([1, 2, 2, 2, 3, 3, 4, 5] if c["clinic"] == 1 else [1, 2, 2, 2, 3, 3, 4])
                _, code, _, dur, buf, rt, _ = type_by_id[tid]
                s0, e0 = rnd.choice(wins)
                slots = [m for m in range(-(-s0 // 15) * 15, e0 - dur + 1, 15)]
                if not slots:
                    continue
                s = rnd.choice(slots)
                if any(s < be and bs < s + dur + buf for bs, be in busy):
                    continue
                room = None
                for rid, rc, _, rrt, ract in ROOMS:
                    if rc == c["clinic"] and rrt == rt and ract:
                        if not any(s < be and bs < s + dur + buf for bs, be in room_busy.get((rid, d), [])):
                            room = rid
                            break
                if room is None and rnd.random() < 0.7:
                    continue
                if rnd.random() < 0.01:
                    room = None
                busy.append((s, s + dur + buf))
                if room is not None:
                    room_busy.setdefault((room, d), []).append((s, s + dur + buf))
                appts.append({"pid": rnd.choice(pat_active)["id"], "cid": c["id"], "tid": tid, "room": room,
                              "d": d, "m": s, "fold": 0})
    # night clinic on the two clock-change Sundays
    nights = [c for c in clin if c["night"]]
    for k, c in enumerate(nights):
        d = date(2026, 11, 1)
        for m, fold in ((60, 0), (75, 1), (105, 0), (120, 0)) if k % 2 == 0 else ((90, 0), (90, 1), (30, 0)):
            appts.append({"pid": rnd.choice(pat_active)["id"], "cid": c["id"], "tid": 2, "room": None if k == 3 else 1 + k % 3,
                          "d": d, "m": m, "fold": fold})
    for k, c in enumerate(nights[:2]):
        appts.append({"pid": rnd.choice(pat_active)["id"], "cid": c["id"], "tid": 2, "room": 5 + k, "d": date(2027, 3, 14),
                      "m": 150, "fold": 0})
    appts.sort(key=lambda a: (a["d"], a["m"], a["fold"], a["cid"]))
    fee_rows = sorted(FEES, key=lambda f: f[1])
    patients[17]["future"] = True
    for i, a in enumerate(appts, 1):
        a["id"] = i
        past = a["d"] < CUTOFF
        r = rnd.random()
        if past:
            a["status"] = "completed" if r < 0.86 else ("no_show" if r < 0.91 else "cancelled")
        else:
            a["status"] = "booked" if r < 0.88 else "cancelled"
        if i % 997 == 0:
            a["pid"] = patients[17]["id"]
            a["status"] = "booked" if not past else a["status"]
        fee, bp = [(f, b) for t, e, f, b in fee_rows if t == a["tid"] and e <= a["d"].isoformat()][-1]
        a["deposit"] = (fee * bp + 5000) // 10000
        ls = f"{a['d'].isoformat()} {a['m'] // 60:02d}:{a['m'] % 60:02d}:00"
        created = datetime.combine(min(a["d"], CUTOFF) - timedelta(days=rnd.randrange(1, 40)), time(18, 0), timezone.utc)
        L.append(f"INSERT INTO appointments (id, patient_id, clinician_id, type_id, room_id, local_start, fold, status, deposit_cents, created_at) "
                 f"VALUES ({i}, {a['pid']}, {a['cid']}, {a['tid']}, {a['room'] if a['room'] else 'NULL'}, '{ls}', {a['fold']}, "
                 f"{q(a['status'])}, {a['deposit']}, '{created.isoformat()}');")
    # far-future bookings (sparse, from their own random stream so every row above stays identical): the task can be
    # graded years after it was written and still find old-system bookings ahead of "now" (valid_until in task.toml)
    frnd = random.Random(SEED + 7)
    extra = []
    for d in days(FAR_FIRST, FAR_LAST):
        for c in clin:
            if frnd.random() > 0.07:
                continue
            wins = wall_windows(c, d)
            if not wins:
                continue
            tid = frnd.choice([1, 2, 2, 3] if c["clinic"] == 2 else [1, 2, 2, 3, 4])
            _, code, _, dur, buf, rt, _ = type_by_id[tid]
            s0, e0 = frnd.choice(wins)
            slots = list(range(-(-s0 // 15) * 15, e0 - dur + 1, 15))
            if not slots:
                continue
            m = frnd.choice(slots)
            if m < 240:  # keep the far-future rows clear of the night hours that clock changes repeat or skip
                continue
            room = None
            for rid, rc, _, rrt, ract in ROOMS:
                if rc == c["clinic"] and rrt == rt and ract and not any(
                        m < be and bs < m + dur + buf for bs, be in room_busy.get((rid, d), [])):
                    room = rid
                    break
            if room is None:
                continue
            room_busy.setdefault((room, d), []).append((m, m + dur + buf))
            extra.append({"pid": frnd.choice(pat_active)["id"], "cid": c["id"], "tid": tid, "room": room, "d": d, "m": m,
                          "fold": 0, "status": "booked" if frnd.random() < 0.9 else "cancelled"})
    for a in extra:
        a["id"] = len(appts) + 1
        fee, bp = [(f, b_) for t, e, f, b_ in fee_rows if t == a["tid"] and e <= a["d"].isoformat()][-1]
        a["deposit"] = (fee * bp + 5000) // 10000
        ls = f"{a['d'].isoformat()} {a['m'] // 60:02d}:{a['m'] % 60:02d}:00"
        L.append(f"INSERT INTO appointments (id, patient_id, clinician_id, type_id, room_id, local_start, fold, status, deposit_cents, created_at) "
                 f"VALUES ({a['id']}, {a['pid']}, {a['cid']}, {a['tid']}, {a['room']}, '{ls}', 0, {q(a['status'])}, {a['deposit']}, "
                 f"'2026-11-01T18:00:00+00:00');")
        appts.append(a)
    L.append(f"SELECT setval('appointments_id_seq', {len(appts)});")

    # ---- clinical notes ----------------------------------------------------------------------------
    nid = 0
    for a in appts:
        if a["status"] != "completed" or rnd.random() > 0.6:
            continue
        nid += 1
        body = " ".join(rnd.sample(NOTE_BITS, rnd.randrange(1, 4)))
        if nid % 400 == 1:
            body = XSS_NOTE
        elif nid % 250 == 3:
            body = "Pain <b>8/10</b> & swelling > baseline; \"refer\" if no change."
        author = clin[a["cid"] - 1]["user"]
        created = datetime.combine(a["d"], time(23, 0), timezone.utc)
        L.append(f"INSERT INTO clinical_notes (id, appointment_id, author_id, body, created_at) VALUES "
                 f"({nid}, {a['id']}, {author}, {q(body)}, '{created.isoformat()}');")
    # notes on a few future booked appointments too (pre-visit notes), so they show on upcoming pages
    for a in appts:
        if a["status"] == "booked" and a["id"] % 300 == 0:
            nid += 1
            L.append(f"INSERT INTO clinical_notes (id, appointment_id, author_id, body, created_at) VALUES "
                     f"({nid}, {a['id']}, {clin[a['cid'] - 1]['user']}, {q(XSS_NOTE if a['id'] % 600 == 0 else 'Referral letter reviewed.')}, "
                     f"'2026-11-01T20:00:00+00:00');")
    L.append(f"SELECT setval('clinical_notes_id_seq', {max(nid, 1)});")
    L.append("COMMIT;")
    (out / "002_records.sql").write_text("\n".join(L) + "\n")
    print(f"users={len(users)} clinicians={len(clin)} versions={len(versions)} windows={wid} "
          f"exceptions={len(exceptions)} appointments={len(appts)} notes={nid}", file=sys.stderr)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
