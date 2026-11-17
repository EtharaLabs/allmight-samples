"""Shiftbook verifier harness: drives the submission through its interface contract and keeps the model."""

from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import secrets
import subprocess
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # Reward Kit imports every file standalone

import psycopg  # noqa: E402

from kitlib.mail import Mail, links
from kitlib.web import Client, FormNotFound, Response, csrf_from, find_form, submit_form, visible_text
from kitlib.wf import InfraError, StepFailed
from reference import payroll as P
from reference.model import Emp, Model, State, to_punches

PASSWORD = "Correct-Horse-9"
REDIRECTS = (301, 302, 303, 307, 308)
CSV_HEADER = ["employee_id", "full_name", "location", "reg_hours", "ot_hours", "dt_hours", "meal_premiums",
              "straight_cents", "ot_premium_cents", "dt_premium_cents", "meal_cents", "adjustment_cents", "gross_cents"]


class Harness:
    def __init__(self):
        e = os.environ
        self.base = e["PUBLIC_BASE_URL"].rstrip("/")
        self.db_url = e["DATABASE_URL"]
        self.mail = Mail(e["MAILSINK_HTTP_URL"])
        self.reset_ttl = int(e.get("RESET_LINK_TTL_SECONDS", "20"))
        self.lockout = int(e.get("LOGIN_LOCKOUT_SECONDS", "15"))
        self.restart_hook = e.get("FK_RESTART_APP", "")
        self.workspace = e.get("FK_WORKSPACE", "/app")
        self.nonce = secrets.token_hex(3)
        self.rnd = random.Random(int(e.get("FK_SEED", "20261110")))
        self.admin: Client | None = None
        self.admin2: Client | None = None
        self.devices: dict[int, str] = {}
        self.device_ids: dict[int, str] = {}  # contract ids are "ids from the database": kept as strings
        self.baseline = self._baseline()
        self.model = Model(self.baseline["locations"])
        self._load_legacy()
        self.created: list[int] = []
        self.reset_tokens: list[str] = []  # plain tokens seen in emails (R2.7 at-rest scan)
        self.signin_cookies: list[tuple[str, str]] = []  # (name, value) set at sign-in (R2.7 at-rest scan)
        self._session_names: set[str] | None = None  # names of the session cookie(s), found by session_cookie_names

    # -- database (read-only views for the model and integrity checks) --------------------------
    def q(self, sql: str, args=()):
        with psycopg.connect(self.db_url, connect_timeout=10) as c:
            cur = c.execute(sql, args)
            cols = [d.name for d in cur.description] if cur.description else []
            return [dict(zip(cols, r)) for r in cur.fetchall()] if cols else []

    def _baseline(self) -> dict:
        path = os.environ.get("FK_BASELINE")
        if path and os.path.exists(path):
            with open(path) as fh:
                b = json.load(fh)
            b["locations"] = {int(k): v for k, v in b["locations"].items()}
            return b
        raise InfraError("baseline snapshot missing")

    def _load_legacy(self) -> None:
        b = self.baseline
        locs = b["locations"]
        for r in b["employees"]:
            loc = locs[r["location_id"]]
            self.model.emps[r["id"]] = Emp(r["id"], r["email"], r["full_name"], r["location_id"], loc["timezone"],
                                           loc["ruleset"], r["role"], r["manager_id"], r["active"], legacy=True)
        for r in b["job_rates"]:
            self.model.emps[r["employee_id"]].rates.setdefault(r["job_code"], []).append(
                (date.fromisoformat(r["effective_from"]), r["rate_cents"]))
        for r in b["legacy_punches"]:
            emp = self.model.emps[r["employee_id"]]
            us = P.legacy_to_us(datetime.fromisoformat(r["local_time"]), r["fold"], emp.tz)
            emp.punches.append(P.Punch(f"legacy:{r['id']}", r["kind"], us, r["job_code"]))
        for r in b["legacy_approvals"]:
            self.model.emps[r["employee_id"]].approved.add(date.fromisoformat(r["week_start"]))

    # -- accounts ------------------------------------------------------------------------------------
    def client(self) -> Client:
        return Client(self.base)

    def request_reset(self, email: str, client: Client | None = None) -> Response:
        c = client or self.client()
        return submit_form(c, "/password-reset", "request-reset", {"email": email}, follow=True)

    def reset_link(self, email: str, since_ids: set, timeout: float = 35.0) -> str | None:
        msgs = self.mail.wait_for(email.strip().lower(), lambda m: m["id"] not in since_ids, 1, timeout)
        for m in reversed(msgs):
            for l in links(m["text"] or m["html"]):
                if l.startswith(self.base) and "/password-reset/" in l:
                    self.reset_tokens.append(l.rstrip("/").rsplit("/", 1)[-1])
                    return l
        return None

    def mail_ids(self, email: str) -> set:
        return {m["id"] for m in self.mail.to(email.strip().lower())}

    def set_password(self, email: str, password: str = PASSWORD) -> bool:
        before = self.mail_ids(email)
        self.request_reset(email)
        link = self.reset_link(email, before)
        if not link:
            return False
        c = self.client()
        page = c.get(link, follow=True)
        if page.status != 200:
            return False
        r = submit_form(c, link, "set-password", {"password": password}, follow=True, page=page)
        return r.status < 400

    def signin(self, email: str, password: str = PASSWORD, client: Client | None = None) -> tuple[Client, Response]:
        c = client or self.client()
        r = submit_form(c, "/signin", "signin", {"email": email, "password": password}, follow=False)
        for h in r.header_all("Set-Cookie"):
            first = h.split(";", 1)[0]
            if "=" in first:
                k, v = (x.strip() for x in first.split("=", 1))
                if len(v) >= 16:
                    self.signin_cookies.append((k, v))
        return c, r

    def signed_in(self, c: Client) -> bool:
        """Judged by the page /timesheet finally shows (redirects followed): signed in when it is not the
        sign-in page and carries the sign-out button."""
        r = c.get("/timesheet", follow=True)
        path = urllib.parse.urlsplit(r.url).path.rstrip("/")
        return r.status == 200 and path != "/signin" and r.dom.attr_one("data-action", "signout") is not None

    def session_cookie_names(self, c: Client | None = None) -> set[str]:
        """R2.6/R2.7 constrain session cookies: the cookie(s) whose removal signs a signed-in client out (or,
        when no single removal does, those that sign in on their own). Other cookies, such as a double-submit
        CSRF cookie, are not session cookies. Found once from a signed-in client and cached."""
        if self._session_names and c is None:
            return self._session_names
        c = c or self.admin
        if c is None or not c.cookies or not self.signed_in(c):
            return self._session_names or set()
        names: set[str] = set()
        for name in list(c.cookies):
            probe = self.client()
            probe.cookies = {k: v for k, v in c.cookies.items() if k != name}
            if not self.signed_in(probe):
                names.add(name)
        if not names:
            for name, v in list(c.cookies.items()):
                probe = self.client()
                probe.cookies = {name: v}
                if self.signed_in(probe):
                    names.add(name)
        if names:
            self._session_names = names
        return names or (self._session_names or set())

    def session_values(self, n: int) -> list[str]:
        """The last n session identifiers set at sign-in (values of session cookies only; every cookie set at
        sign-in when the session cookie cannot be identified)."""
        names = self.session_cookie_names()
        vals = [v for k, v in self.signin_cookies if not names or k in names]
        return list(dict.fromkeys(vals))[-n:]

    @staticmethod
    def key_needles(keys) -> list[str]:
        """R2.7 at-rest needles for time-clock keys: the full key and its secret part, i.e. every tail of the key
        that follows a separator and is at least 16 characters. A leading public part (a prefix or lookup id)
        may legitimately be stored or shown, so it is never a needle on its own."""
        out: list[str] = []
        for k in keys:
            out.append(k)
            out += [k[i + 1:] for i, ch in enumerate(k) if ch in "-_.:" and len(k) - i - 1 >= 16]
        return list(dict.fromkeys(out))

    def account(self, email: str) -> Client:
        if not self.set_password(email):
            raise StepFailed(f"could not set a password for {email} through /password-reset")
        c, r = self.signin(email)
        if not self.signed_in(c):
            raise StepFailed(f"sign-in for {email} did not produce a signed-in session (status {r.status})")
        return c

    def ensure_admins(self) -> None:
        if self.admin is None:
            self.admin = self.account("dana.whitfield@shiftbook.example")
        if self.admin2 is None:
            self.admin2 = self.account("omar.haddad@shiftbook.example")

    # -- staff, devices, punches -----------------------------------------------------------------------
    def create_employee(self, location_id: int, name: str | None = None, role: str = "employee",
                        manager_id: int | None = 1, job: str = "COOK", cents: int = 2400,
                        eff: date = date(2026, 1, 1), email: str | None = None) -> int:
        self.ensure_admins()
        n = len(self.created) + 1
        email = email or f"v{self.nonce}.{n}@verify.example"
        name = name or f"Verifier Person {n}"
        r = submit_form(self.admin, "/admin/employees/new", "create-employee", {
            "email": email, "full_name": name, "location_id": str(location_id),
            "manager_id": "" if manager_id is None else str(manager_id), "role": role, "job_code": job,
            "rate_cents": str(cents), "effective_from": eff.isoformat()}, follow=True)
        node = r.dom.attr_one("data-employee-id")
        if r.status >= 400 or node is None:
            raise StepFailed(f"creating employee {email} answered {r.status} without data-employee-id")
        eid = int(node.attrs["data-employee-id"])
        loc = self.model.locations[location_id]
        emp = Emp(eid, email, name, location_id, loc["timezone"], loc["ruleset"], role, manager_id, True)
        emp.rates[job] = [(eff, cents)]
        self.model.emps[eid] = emp
        self.created.append(eid)
        return eid

    def add_rate(self, eid: int, job: str, cents: int, eff: date) -> Response:
        r = submit_form(self.admin, f"/admin/employees/{eid}", "add-rate",
                        {"job_code": job, "rate_cents": str(cents), "effective_from": eff.isoformat()}, follow=True)
        if r.status < 400:
            self.model.add_rate(eid, job, eff, cents)
        return r

    def create_device(self, location_id: int, label: str) -> tuple[int, str]:
        self.ensure_admins()
        r = submit_form(self.admin, "/admin/devices", "create-device",
                        {"location_id": str(location_id), "label": label}, follow=True)
        did = r.dom.attr_one("data-device-id")
        key = r.dom.attr_one("data-device-key")
        if r.status >= 400 or did is None or key is None:
            raise StepFailed(f"creating a time clock answered {r.status} without data-device-id/data-device-key")
        return did.attrs["data-device-id"], key.attrs["data-device-key"]

    def ensure_devices(self) -> None:
        for loc in self.model.locations:
            if loc not in self.devices:
                did, key = self.create_device(loc, f"Verifier clock {loc}")
                self.devices[loc], self.device_ids[loc] = key, did

    def api(self, key: str, rows) -> Response:
        c = self.client()
        return c.post_json("/api/punches", {"punches": rows} if isinstance(rows, list) else rows,
                           headers={"Authorization": f"Bearer {key}"})

    def import_rows(self, rows: list[dict], expect_all: bool = True) -> list[dict]:
        """Send rows through the matching location's clock, 400 at a time; record accepted punches."""
        self.ensure_devices()
        by_loc: dict[int, list] = {}
        for r in rows:
            by_loc.setdefault(self.model.emps[r["employee_id"]].location_id, []).append(r)
        results = []
        for loc, lrows in by_loc.items():
            for i in range(0, len(lrows), 400):
                chunk = lrows[i:i + 400]
                resp = self.api(self.devices[loc], chunk)
                if resp.status != 200:
                    raise StepFailed(f"punch API answered {resp.status}: {resp.text[:300]}")
                try:
                    res = resp.json()["results"]
                except Exception:
                    raise StepFailed(f"punch API answered a body without results: {resp.text[:300]}")
                if len(res) != len(chunk):
                    raise StepFailed(f"punch API returned {len(res)} results for {len(chunk)} rows")
                accepted: dict[int, list] = {}
                for row, rr in zip(chunk, res):
                    if rr.get("status") == "accepted":
                        accepted.setdefault(row["employee_id"], []).append(row)
                    elif expect_all:
                        raise StepFailed(f"punch {row['punch_id']} was {rr.get('status')} ({rr.get('reason')}), expected accepted")
                for eid, acc in accepted.items():
                    self.model.add_punches(eid, to_punches(acc))
                results.extend(res)
        return results

    # -- approvals --------------------------------------------------------------------------------------
    def timesheet(self, c: Client, eid: int, ws: date) -> Response:
        return c.get(f"/employees/{eid}/timesheet?week={ws.isoformat()}", follow=True)

    def approve(self, c: Client, eid: int, ws: date, record: bool = True) -> Response:
        page = self.timesheet(c, eid, ws)
        try:
            r = submit_form(c, "", "approve-week", {}, follow=True, page=page)
        except FormNotFound:
            raise StepFailed(f"timesheet of {eid} week {ws} has no approve-week form (status {page.status})")
        if r.status < 400 and record:
            self.model.approve(eid, ws)
        return r

    def approve_all(self, eids, weeks) -> None:
        self.ensure_admins()
        for eid in eids:
            for ws in weeks:
                st_weeks = State(self.model.emps[eid])
                if not st_weeks.has_work(ws) or ws in st_weeks.exceptions or st_weeks.status(ws) == "approved":
                    continue
                r = self.approve(self.admin, eid, ws)
                if r.status >= 400:
                    raise StepFailed(f"approving employee {eid} week {ws} answered {r.status}")

    # -- payroll ------------------------------------------------------------------------------------------
    def create_run(self, period: date, client: Client | None = None) -> tuple[Response, str | None, float]:
        c = client or self.admin
        t0 = time.monotonic()
        page = c.get("/payroll", follow=True)
        r = submit_form(c, "/payroll", "create-run", {"period_start": period.isoformat()}, follow=False, page=page)
        took = time.monotonic() - t0
        rid = None
        loc = urllib.parse.urlsplit(r.location or "").path
        m = re.search(r"/payroll/runs/([^/?#]+)/?$", loc)
        if r.status in REDIRECTS and m:
            rid = urllib.parse.unquote(m.group(1))
        return r, rid, took

    def run_page(self, rid: str, c: Client | None = None) -> Response:
        return (c or self.admin).get(f"/payroll/runs/{rid}", follow=True)

    def export(self, rid: str, c: Client | None = None) -> tuple[Response, dict[int, dict]]:
        r = (c or self.admin).get(f"/payroll/runs/{rid}/export.csv", follow=False)
        rows: dict[int, dict] = {}
        if r.status == 200:
            reader = csv.reader(io.StringIO(r.body.decode("utf-8-sig", "replace"), newline=""))
            data = list(reader)
            if data and data[0] == CSV_HEADER:
                for row in data[1:]:
                    if len(row) == len(CSV_HEADER):
                        try:
                            rows[int(row[0])] = dict(zip(CSV_HEADER, row))
                        except ValueError:
                            pass
        return r, rows

    def compare_lines(self, period: date, rows: dict[int, dict], eids) -> list[str]:
        problems = []
        for eid in eids:
            want = self.model.csv_row(eid, period)
            got = rows.get(eid)
            if want is None:
                if got is not None and any(got[k] not in ("0", "0.0") for k in ("gross_cents",)):
                    problems.append(f"employee {eid}: unexpected line {self._fmt(got)}")
                continue
            if got is None:
                problems.append(f"employee {eid}: missing line, expected {want}")
                continue
            diffs = {k: (got.get(k), v) for k, v in want.items() if not self._same(k, got.get(k), v)}
            if diffs:
                problems.append(f"employee {eid}: " + ", ".join(f"{k} got {g} want {w}" for k, (g, w) in diffs.items()))
        return problems

    @staticmethod
    def _same(col: str, got: str | None, want: str) -> bool:
        """Hours are shown with exactly one decimal place and money is whole cents, so those compare as text.
        The spec states no format for the meal-premium count, so it compares as a number ("2" = "2.0")."""
        if col == "meal_premiums" and got is not None:
            try:
                return Decimal(got.strip()) == Decimal(want)
            except (InvalidOperation, ValueError):
                return False
        return got == want

    @staticmethod
    def _fmt(row: dict) -> str:
        return ",".join(f"{k}={row[k]}" for k in CSV_HEADER[3:])

    def restart_app(self) -> bool:
        if not self.restart_hook:
            return False
        # The hook is the verifier's own root-owned script (tests/test.sh writes it); no user input reaches it.
        return subprocess.run([self.restart_hook], check=False).returncode == 0
