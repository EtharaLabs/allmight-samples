"""Shiftbook domain layer: data access, employee week state, approvals, payroll runs, outbox."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import smtplib
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.headerregistry import Address
from email.utils import make_msgid
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from . import engine
from .engine import Punch

ENV = os.environ
DATABASE_URL = ENV.get("DATABASE_URL", "")
FIRST_PERIOD = engine.PERIOD_ANCHOR
SCHEMA = """
CREATE TABLE IF NOT EXISTS sb_credentials (
    employee_id integer PRIMARY KEY REFERENCES employees (id),
    password_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sb_login_state (
    employee_id integer PRIMARY KEY REFERENCES employees (id),
    failures integer NOT NULL DEFAULT 0,
    locked_until timestamptz
);
CREATE TABLE IF NOT EXISTS sb_reset_tokens (
    token_hash text PRIMARY KEY,
    employee_id integer NOT NULL REFERENCES employees (id),
    created_at timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS sb_sessions (
    sid_hash text PRIMARY KEY,
    employee_id integer NOT NULL REFERENCES employees (id),
    csrf text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS sb_devices (
    id serial PRIMARY KEY,
    location_id integer NOT NULL REFERENCES locations (id),
    label text NOT NULL,
    salt text NOT NULL,
    key_hash text NOT NULL,
    revoked boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sb_punches (
    punch_id text PRIMARY KEY,
    employee_id integer NOT NULL REFERENCES employees (id),
    kind text NOT NULL,
    at timestamptz NOT NULL,
    job_code text,
    device_id integer NOT NULL,
    UNIQUE (employee_id, at)
);
CREATE TABLE IF NOT EXISTS sb_approvals (
    employee_id integer NOT NULL REFERENCES employees (id),
    week_start date NOT NULL,
    state text NOT NULL,
    approved_by integer,
    changed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (employee_id, week_start)
);
CREATE TABLE IF NOT EXISTS sb_runs (
    id serial PRIMARY KEY,
    period_start date NOT NULL UNIQUE,
    status text NOT NULL,
    version integer NOT NULL,
    computed jsonb NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now(),
    finalized_at timestamptz
);
CREATE TABLE IF NOT EXISTS sb_paid_weeks (
    run_id integer NOT NULL REFERENCES sb_runs (id),
    employee_id integer NOT NULL,
    week_start date NOT NULL,
    gross_cents bigint NOT NULL,
    PRIMARY KEY (run_id, employee_id, week_start)
);
CREATE TABLE IF NOT EXISTS sb_outbox (
    id serial PRIMARY KEY,
    dedupe_key text NOT NULL UNIQUE,
    to_addr text NOT NULL,
    subject text NOT NULL,
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);
CREATE INDEX IF NOT EXISTS sb_punches_employee ON sb_punches (employee_id);
ALTER TABLE sb_outbox ADD COLUMN IF NOT EXISTS to_name text;
INSERT INTO sb_approvals (employee_id, week_start, state, approved_by, changed_at)
    SELECT employee_id, week_start, 'approved', approved_by, approved_at FROM legacy_approvals
    ON CONFLICT DO NOTHING;
"""


def connect(**kw):
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=4, **kw)


def apply_schema() -> None:
    while True:
        try:
            with connect() as conn:
                conn.execute("SELECT pg_advisory_lock(424242)")
                try:
                    conn.execute(SCHEMA)
                    conn.commit()
                finally:
                    conn.execute("SELECT pg_advisory_unlock(424242)")
                    conn.commit()
            return
        except psycopg.OperationalError:
            time.sleep(1)


def norm_email(s: str) -> str:
    return (s or "").strip().lower()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---- password hashing ---------------------------------------------------------------------------
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + salt.hex() + "$" + dk.hex()


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return secrets.compare_digest(got.hex(), dk)
    except Exception:
        return False


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ---- reference data -----------------------------------------------------------------------------
_LOC_CACHE: dict[int, dict] = {}


def locations(conn) -> dict[int, dict]:
    if not _LOC_CACHE:
        for r in conn.execute("SELECT id, name, timezone, ruleset FROM locations ORDER BY id"):
            _LOC_CACHE[r["id"]] = r
    return _LOC_CACHE


def employee(conn, eid: int) -> dict | None:
    return conn.execute("SELECT * FROM employees WHERE id = %s", (eid,)).fetchone()


def is_below(conn, emp_id: int, manager_id: int) -> bool:
    row = conn.execute(
        """WITH RECURSIVE chain(id, manager_id, depth) AS (
               SELECT id, manager_id, 0 FROM employees WHERE id = %s
               UNION ALL
               SELECT e.id, e.manager_id, c.depth + 1 FROM employees e JOIN chain c ON e.id = c.manager_id
               WHERE c.depth < 100)
           SELECT 1 AS ok FROM chain WHERE id = %s AND depth > 0 LIMIT 1""",
        (emp_id, manager_id),
    ).fetchone()
    return row is not None


def below_ids(conn, manager_id: int) -> list[int]:
    rows = conn.execute(
        """WITH RECURSIVE sub(id, depth) AS (
               SELECT id, 1 FROM employees WHERE manager_id = %s
               UNION ALL SELECT e.id, s.depth + 1 FROM employees e JOIN sub s ON e.manager_id = s.id
               WHERE s.depth < 100)
           SELECT DISTINCT id FROM sub""",
        (manager_id,),
    ).fetchall()
    return [r["id"] for r in rows]


def can_view(conn, viewer: dict, emp_id: int) -> bool:
    if viewer["id"] == emp_id or viewer["role"] == "payroll_admin":
        return True
    return viewer["role"] == "manager" and is_below(conn, emp_id, viewer["id"])


def can_approve(conn, viewer: dict, emp_id: int) -> bool:
    if viewer["id"] == emp_id:
        return False
    if viewer["role"] == "payroll_admin":
        return True
    return viewer["role"] == "manager" and is_below(conn, emp_id, viewer["id"])


# ---- punches and week state -----------------------------------------------------------------------
def load_rates(conn, emp_ids: list[int]) -> dict[int, dict[str, list[tuple[date, int]]]]:
    out: dict[int, dict[str, list]] = {e: {} for e in emp_ids}
    for r in conn.execute(
        "SELECT employee_id, job_code, rate_cents, effective_from FROM job_rates WHERE employee_id = ANY(%s)"
        " ORDER BY effective_from", (emp_ids,)
    ):
        out.setdefault(r["employee_id"], {}).setdefault(r["job_code"], []).append((r["effective_from"], r["rate_cents"]))
    return out


def rate_fn(hist: dict[str, list[tuple[date, int]]]):
    def rate_for(job, day):
        best = None
        for eff, cents in hist.get(job, ()):
            if eff <= day:
                best = cents
        return best
    return rate_for


def load_punches(conn, emp_ids: list[int], tz_of: dict[int, str]) -> dict[int, list[Punch]]:
    out: dict[int, list[Punch]] = {e: [] for e in emp_ids}
    for r in conn.execute(
        "SELECT id, employee_id, job_code, kind, local_time, fold FROM legacy_punches WHERE employee_id = ANY(%s)",
        (emp_ids,),
    ):
        us = engine.legacy_to_us(r["local_time"], r["fold"], tz_of[r["employee_id"]])
        out[r["employee_id"]].append(Punch(f"legacy:{r['id']}", r["kind"], us, r["job_code"]))
    for r in conn.execute(
        "SELECT punch_id, employee_id, kind, at, job_code FROM sb_punches WHERE employee_id = ANY(%s)", (emp_ids,)
    ):
        out[r["employee_id"]].append(Punch(r["punch_id"], r["kind"], engine.to_us(r["at"]), r["job_code"]))
    return out


class EmpState:
    """Everything derived for one employee from punches, rates, and approvals."""

    def __init__(self, emp: dict, loc: dict, punches: list[Punch], rates, approvals: dict[date, str]):
        self.emp, self.loc = emp, loc
        tz, rs = loc["timezone"], loc["ruleset"]
        self.rate_for = rate_fn(rates)
        shifts, exc = engine.build_shifts(punches)
        exc = exc + engine.rate_exceptions(shifts, tz, self.rate_for)
        self.exceptions = engine.exception_weeks(exc, tz)
        self.weeks = engine.compute_weeks(shifts, tz, rs, self.rate_for)
        self.approvals = approvals

    def status(self, ws: date) -> str:
        if ws in self.exceptions:
            return "exception"
        if self.approvals.get(ws) == "approved":
            return "approved"
        return "pending"

    def has_work(self, ws: date) -> bool:
        wp = self.weeks.get(ws)
        return bool(wp and wp.total_units)

    def payable(self, ws: date) -> bool:
        return self.status(ws) == "approved" and self.has_work(ws)

    def fingerprint(self) -> dict[date, str]:
        out = {}
        for ws in set(self.weeks) | set(self.exceptions):
            wp = self.weeks.get(ws)
            data = {
                "iv": wp.intervals if wp else [],
                "mp": wp.meal_premiums if wp else 0,
                "c": wp.cents() if wp else {},
                "x": sorted(self.exceptions.get(ws, [])),
            }
            out[ws] = sha(json.dumps(data, sort_keys=True, default=str))
        return out


def employee_states(conn, emp_ids: list[int]) -> dict[int, EmpState]:
    if not emp_ids:
        return {}
    emps = {r["id"]: r for r in conn.execute("SELECT * FROM employees WHERE id = ANY(%s)", (emp_ids,))}
    locs = locations(conn)
    tz_of = {e: locs[emps[e]["location_id"]]["timezone"] for e in emps}
    punches = load_punches(conn, list(emps), tz_of)
    rates = load_rates(conn, list(emps))
    approvals: dict[int, dict[date, str]] = {e: {} for e in emps}
    for r in conn.execute("SELECT employee_id, week_start, state FROM sb_approvals WHERE employee_id = ANY(%s)", (list(emps),)):
        approvals[r["employee_id"]][r["week_start"]] = r["state"]
    return {
        e: EmpState(emps[e], locs[emps[e]["location_id"]], punches[e], rates.get(e, {}), approvals[e])
        for e in emps
    }


def lock_employees(conn, emp_ids) -> None:
    for e in sorted(set(emp_ids)):
        conn.execute("SELECT pg_advisory_xact_lock(1, %s)", (e,))


def revoke_changed(conn, before: dict[int, dict], after: dict[int, dict]) -> None:
    for e, fp_after in after.items():
        fp_before = before.get(e, {})
        for ws in set(fp_before) | set(fp_after):
            if fp_before.get(ws) != fp_after.get(ws):
                conn.execute(
                    "UPDATE sb_approvals SET state = 'revoked', changed_at = now() WHERE employee_id = %s"
                    " AND week_start = %s AND state = 'approved'",
                    (e, ws),
                )


# ---- punch API ------------------------------------------------------------------------------------
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$")


def parse_instant(s) -> datetime | None:
    if not isinstance(s, str) or not TS_RE.match(s):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("z", "Z").replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def import_punches(device: dict, rows: list) -> list[dict]:
    for attempt in range(5):
        try:
            return _import_punches(device, rows)
        except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure):
            time.sleep(0.05 * (attempt + 1))
    raise RuntimeError("could not import punches")


def _import_punches(device: dict, rows: list) -> list[dict]:
    results: list[dict] = []
    with connect() as conn:
        emp_ids = sorted({r.get("employee_id") for r in rows if isinstance(r, dict) and type(r.get("employee_id")) is int})
        lock_employees(conn, emp_ids)
        existing_emps = {r["id"]: r for r in conn.execute("SELECT * FROM employees WHERE id = ANY(%s)", (emp_ids,))}
        valid_ids = list(existing_emps)
        before = {e: s.fingerprint() for e, s in employee_states(conn, valid_ids).items()}
        rates = load_rates(conn, valid_ids)
        locs = locations(conn)
        tz_of = {e: locs[existing_emps[e]["location_id"]]["timezone"] for e in existing_emps}
        instants: dict[int, set[int]] = {e: {p.at_us for p in ps} for e, ps in load_punches(conn, valid_ids, tz_of).items()}
        for row in rows:
            pid = row.get("punch_id") if isinstance(row, dict) else None
            res = {"punch_id": pid, "status": "rejected", "reason": ""}
            results.append(res)
            if not isinstance(row, dict):
                res["reason"] = "row is not an object"
                continue
            if not isinstance(pid, str) or not (1 <= len(pid) <= 64):
                res["reason"] = "punch_id must be a string of 1 to 64 characters"
                continue
            eid, kind, at_s, job = row.get("employee_id"), row.get("kind"), row.get("at"), row.get("job_code")
            if type(eid) is not int:
                res["reason"] = "employee_id must be an integer"
                continue
            if kind not in ("in", "out", "break_start", "break_end"):
                res["reason"] = "kind must be in, out, break_start, or break_end"
                continue
            at = parse_instant(at_s)
            if at is None:
                res["reason"] = "at must be an RFC 3339 timestamp with Z or an offset"
                continue
            if kind == "in":
                if not isinstance(job, str) or not job:
                    res["reason"] = "an in punch needs a job_code"
                    continue
            else:
                job = None
            at_us = engine.to_us(at)
            existing = conn.execute("SELECT * FROM sb_punches WHERE punch_id = %s", (pid,)).fetchone()
            if existing:
                same = (existing["employee_id"] == eid and existing["kind"] == kind
                        and engine.to_us(existing["at"]) == at_us and (existing["job_code"] or None) == job)
                res["status"] = "duplicate" if same else "rejected"
                res["reason"] = "already received" if same else "punch_id reused with different fields"
                continue
            emp = existing_emps.get(eid)
            if emp is None:
                res["reason"] = "unknown employee"
                continue
            if not emp["active"]:
                res["reason"] = "employee is inactive"
                continue
            if emp["location_id"] != device["location_id"]:
                res["reason"] = "employee does not work at this time clock's location"
                continue
            if kind == "in" and job not in rates.get(eid, {}):
                res["reason"] = f"employee has no rate for job {job}"
                continue
            if at_us in instants.setdefault(eid, set()):
                res["reason"] = "employee already has a punch at this instant"
                continue
            cur = conn.execute(
                "INSERT INTO sb_punches (punch_id, employee_id, kind, at, job_code, device_id) VALUES (%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT DO NOTHING RETURNING punch_id",
                (pid, eid, kind, at, job, device["id"]),
            ).fetchone()
            if cur is None:
                again = conn.execute("SELECT * FROM sb_punches WHERE punch_id = %s", (pid,)).fetchone()
                same = bool(again) and (again["employee_id"] == eid and again["kind"] == kind
                                        and engine.to_us(again["at"]) == at_us and (again["job_code"] or None) == job)
                res["status"] = "duplicate" if same else "rejected"
                res["reason"] = "already received" if same else "punch conflicts with an existing punch"
                continue
            instants[eid].add(at_us)
            res["status"], res["reason"] = "accepted", ""
        after = {e: s.fingerprint() for e, s in employee_states(conn, valid_ids).items()}
        revoke_changed(conn, before, after)
        conn.commit()
    return results


# ---- approvals --------------------------------------------------------------------------------------
def approve_week(conn, viewer: dict, emp_id: int, ws: date) -> tuple[int, str]:
    lock_employees(conn, [emp_id])
    st = employee_states(conn, [emp_id]).get(emp_id)
    if st is None:
        return 404, "No such employee."
    if st.status(ws) == "exception":
        return 409, "This week has a punch exception; fix the punches before approving."
    if not st.has_work(ws):
        return 409, "This week has no worked time to approve."
    if st.status(ws) == "approved":
        return 409, "This week is already approved."
    conn.execute(
        "INSERT INTO sb_approvals (employee_id, week_start, state, approved_by, changed_at) VALUES (%s,%s,'approved',%s,now())"
        " ON CONFLICT (employee_id, week_start) DO UPDATE SET state='approved', approved_by=EXCLUDED.approved_by, changed_at=now()",
        (emp_id, ws, viewer["id"]),
    )
    return 200, "Week approved. It will be paid in the next payroll run; any later change returns it to pending."


def add_rate(conn, emp_id: int, job: str, cents: int, eff: date) -> tuple[int, str]:
    lock_employees(conn, [emp_id])
    if conn.execute("SELECT 1 FROM job_rates WHERE employee_id=%s AND job_code=%s AND effective_from=%s", (emp_id, job, eff)).fetchone():
        return 409, "A rate for that job and effective date already exists."
    before = {e: s.fingerprint() for e, s in employee_states(conn, [emp_id]).items()}
    conn.execute("INSERT INTO job_rates (employee_id, job_code, rate_cents, effective_from) VALUES (%s,%s,%s,%s)",
                 (emp_id, job, cents, eff))
    after = {e: s.fingerprint() for e, s in employee_states(conn, [emp_id]).items()}
    revoke_changed(conn, before, after)
    return 200, "Rate added."


# ---- payroll ------------------------------------------------------------------------------------------
def is_period_start(d: date) -> bool:
    return d >= FIRST_PERIOD and (d - FIRST_PERIOD).days % 14 == 0


def compute_run(conn, period_start: date) -> dict:
    weeks = [period_start, period_start + timedelta(days=7)]
    ids = [r["id"] for r in conn.execute(
        "SELECT DISTINCT employee_id AS id FROM legacy_punches UNION SELECT DISTINCT employee_id FROM sb_punches"
        " UNION SELECT DISTINCT employee_id FROM sb_paid_weeks"
    )]
    paid: dict[tuple[int, date], int] = {}
    for r in conn.execute(
        "SELECT p.employee_id, p.week_start, sum(p.gross_cents) AS g FROM sb_paid_weeks p JOIN sb_runs r ON r.id = p.run_id"
        " WHERE r.status = 'finalized' GROUP BY 1, 2"
    ):
        paid[(r["employee_id"], r["week_start"])] = int(r["g"])
    lines, held = {}, []
    states = employee_states(conn, ids)
    for eid in sorted(states):
        st = states[eid]
        line = {"reg": 0, "ot": 0, "dt": 0, "meal": 0, "straight": 0, "otp": 0, "dtp": 0, "mealc": 0,
                "weeks": {}, "adjustments": {}}
        has = False
        for ws in weeks:
            if st.payable(ws):
                wp = st.weeks[ws]
                c = wp.cents()
                line["reg"] += wp.reg_units
                line["ot"] += wp.ot_units
                line["dt"] += wp.dt_units
                line["meal"] += wp.meal_premiums
                line["straight"] += c["straight_cents"]
                line["otp"] += c["ot_premium_cents"]
                line["dtp"] += c["dt_premium_cents"]
                line["mealc"] += c["meal_cents"]
                line["weeks"][ws.isoformat()] = c["gross_cents"]
                has = True
            elif st.has_work(ws) or ws in st.exceptions:
                reason = ("exception: " + "; ".join(st.exceptions[ws])) if ws in st.exceptions else "not approved"
                held.append({"employee_id": eid, "week_start": ws.isoformat(), "reason": reason})
        earlier = {ws for ws in st.weeks if FIRST_PERIOD <= ws < period_start} | {
            w for (e, w) in paid if e == eid and w < period_start}
        for ws in sorted(earlier):
            if not st.payable(ws):
                continue
            due = st.weeks[ws].cents()["gross_cents"]
            diff = due - paid.get((eid, ws), 0)
            if diff:
                line["adjustments"][ws.isoformat()] = diff
                has = True
        if has:
            line["adj"] = sum(line["adjustments"].values())
            line["gross"] = line["straight"] + line["otp"] + line["dtp"] + line["mealc"] + line["adj"]
            lines[str(eid)] = line
    return {"lines": lines, "held": held}


def create_or_recompute_run(period_start: date) -> tuple[int, int | None, str]:
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(2, %s)", (period_start.toordinal(),))
        run = conn.execute("SELECT * FROM sb_runs WHERE period_start = %s FOR UPDATE", (period_start,)).fetchone()
        if run and run["status"] == "finalized":
            return 409, run["id"], "This period's run is already finalized; changes are paid as adjustments in a later run."
        computed = compute_run(conn, period_start)
        if run:
            conn.execute("UPDATE sb_runs SET computed=%s, version=version+1, computed_at=now() WHERE id=%s",
                         (json.dumps(computed), run["id"]))
            rid = run["id"]
        else:
            rid = conn.execute(
                "INSERT INTO sb_runs (period_start, status, version, computed) VALUES (%s,'draft',1,%s) RETURNING id",
                (period_start, json.dumps(computed)),
            ).fetchone()["id"]
        conn.commit()
        return 200, rid, "Draft computed."


def finalize_run(run_id: int, version: str) -> tuple[int, str]:
    with connect() as conn:
        run = conn.execute("SELECT * FROM sb_runs WHERE id = %s FOR UPDATE", (run_id,)).fetchone()
        if run is None:
            return 404, "No such run."
        if run["status"] != "draft":
            return 409, "This run is already finalized."
        if str(run["version"]) != str(version):
            return 409, "This draft is out of date: it was recomputed since you loaded it. Reload the draft and review it again."
        fresh = compute_run(conn, run["period_start"])
        if json.dumps(fresh, sort_keys=True) != json.dumps(run["computed"], sort_keys=True):
            return 409, "This draft is out of date: punches, approvals, or rates changed since it was computed. Recompute the draft, review it, and finalize again."
        conn.execute("UPDATE sb_runs SET status='finalized', finalized_at=now() WHERE id=%s", (run_id,))
        ps = run["period_start"]
        pe = ps + timedelta(days=13)
        emps = {r["id"]: r for r in conn.execute("SELECT id, email, full_name FROM employees WHERE id = ANY(%s)",
                                                  ([int(k) for k in fresh["lines"]],))}
        for k, line in fresh["lines"].items():
            eid = int(k)
            for ws, g in line["weeks"].items():
                conn.execute("INSERT INTO sb_paid_weeks VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (run_id, eid, ws, g))
            for ws, g in line["adjustments"].items():
                conn.execute(
                    "INSERT INTO sb_paid_weeks VALUES (%s,%s,%s,%s) ON CONFLICT (run_id, employee_id, week_start)"
                    " DO UPDATE SET gross_cents = sb_paid_weeks.gross_cents + EXCLUDED.gross_cents", (run_id, eid, ws, g))
            body = (
                f"Hello {emps[eid]['full_name']},\n\nYour pay statement for the pay period {ps.isoformat()} to {pe.isoformat()}:\n\n"
                f"  Regular hours: {hours(line['reg'])}\n  Overtime hours: {hours(line['ot'])}\n"
                f"  Double-time hours: {hours(line['dt'])}\n  Meal premiums: {line['meal']}\n"
                f"  Adjustments for earlier weeks: {dollars(line['adj'])}\n  Gross pay: {dollars(line['gross'])}\n\n"
                "Shiftbook payroll\n"
            )
            conn.execute(
                "INSERT INTO sb_outbox (dedupe_key, to_addr, subject, body, to_name) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (f"paystub:{run_id}:{eid}", norm_email(emps[eid]["email"]),
                 f"Pay statement {ps.isoformat()} to {pe.isoformat()}", body, emps[eid]["full_name"]),
            )
        conn.commit()
    OUTBOX_EVENT.set()
    return 200, "Run finalized. Pay statements are being emailed."


def hours(units: int) -> str:
    return f"{units // 10}.{units % 10}"


def dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    c = abs(cents)
    return f"{sign}${c // 100:,}.{c % 100:02d}"


# ---- outbox -----------------------------------------------------------------------------------------------
OUTBOX_EVENT = threading.Event()


def enqueue_mail(conn, key: str, to: str, subject: str, body: str, to_name: str = "") -> None:
    conn.execute("INSERT INTO sb_outbox (dedupe_key, to_addr, subject, body, to_name) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                 (key, to, subject, body, to_name))


def _message(to: str, to_name: str, subject: str, body: str, key: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = ENV["MAIL_FROM"]
    name = "".join(ch for ch in (to_name or "") if ord(ch) >= 0x20 and ord(ch) != 0x7F)
    msg["To"] = Address(display_name=name, addr_spec=to) if name else to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(idstring=sha(key)[:16], domain="shiftbook.local")
    msg.set_content(body)
    return msg


def send_now(to: str, subject: str, body: str, to_name: str = "") -> None:
    """Send immediately; used for reset links so no plain token is ever stored (R2.7)."""
    try:
        with smtplib.SMTP(ENV["SMTP_HOST"], int(ENV["SMTP_PORT"]), timeout=10) as smtp:
            smtp.send_message(_message(to, to_name, subject, body, secrets.token_hex(8)))
    except Exception:
        traceback.print_exc()


def outbox_loop() -> None:
    while True:
        OUTBOX_EVENT.wait(1.0)
        OUTBOX_EVENT.clear()
        try:
            with connect() as conn:
                while True:
                    rows = conn.execute(
                        "SELECT * FROM sb_outbox WHERE sent_at IS NULL ORDER BY id LIMIT 50 FOR UPDATE SKIP LOCKED"
                    ).fetchall()
                    if not rows:
                        conn.commit()
                        break
                    with smtplib.SMTP(ENV["SMTP_HOST"], int(ENV["SMTP_PORT"]), timeout=10) as smtp:
                        for r in rows:
                            msg = EmailMessage()
                            msg["From"] = ENV["MAIL_FROM"]
                            name = "".join(ch for ch in (r.get("to_name") or "") if ord(ch) >= 0x20 and ord(ch) != 0x7F)
                            msg["To"] = Address(display_name=name, addr_spec=r["to_addr"]) if name else r["to_addr"]
                            msg["Subject"] = r["subject"]
                            msg["Message-ID"] = make_msgid(idstring=sha(r["dedupe_key"])[:16], domain="shiftbook.local")
                            msg.set_content(r["body"])
                            smtp.send_message(msg)
                            conn.execute("UPDATE sb_outbox SET sent_at = now() WHERE id = %s", (r["id"],))
                    conn.commit()
        except Exception:
            time.sleep(1)


def start_outbox() -> None:
    threading.Thread(target=outbox_loop, daemon=True).start()
    OUTBOX_EVENT.set()


def local_today(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()
