#!/usr/bin/env python3
"""Shiftbook workflows. Each workflow is strict: it passes only when every substep passes.

Run: python3 workflows.py --out /logs/verifier/results.json
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import os
import random
import re
import secrets
import sys
import time
import zlib
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import CSV_HEADER, PASSWORD, REDIRECTS, Harness  # noqa: E402
from kitlib import browser as B  # noqa: E402
from kitlib.conc import simultaneously  # noqa: E402
from kitlib.web import Client, FormNotFound, csrf_from, find_form, submit_form, visible_text  # noqa: E402
from kitlib.wf import InfraError, StepFailed, main_guard, run_all, workflow  # noqa: E402
from reference import payroll as P  # noqa: E402
from reference.model import Emp, Gen, State, compliant_breaks, hours, to_punches  # noqa: E402

LA, PHX, CHI = 1, 2, 3
P1004, P1018, P1101, P0920, P1115, P1129, P1213 = (date(2026, 10, 4), date(2026, 10, 18), date(2026, 11, 1),
                                                    date(2026, 9, 20), date(2026, 11, 15), date(2026, 11, 29),
                                                    date(2026, 12, 13))
H: Harness = None  # type: ignore[assignment]


def weeks_of(period: date) -> list[date]:
    return [period, period + timedelta(days=7)]


def norm_text(t: str, *remove: str) -> str:
    for r in remove:
        if r:
            t = t.replace(r, "").replace(r.lower(), "").replace(r.strip(), "")
    return re.sub(r"\s+", " ", t).strip().lower()


def legacy_pick(pred, n: int = 1) -> list:
    out = [e for e in sorted(H.model.emps.values(), key=lambda e: e.eid) if e.legacy and pred(e)]
    if len(out) < n:
        raise StepFailed("verifier fixture: not enough legacy employees for this check")
    return out[:n]


def local(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm)


def landing(c: Client, r):
    """The page a refusal shows: the answer itself, or the page a redirect (any 3xx) leads to, fetched with GET."""
    if r.status in REDIRECTS and r.location:
        return c.get(urllib.parse.urljoin(r.url, r.location), follow=True)
    return r


def refused(c: Client, r) -> bool:
    """A refusal where the spec states no status code: an error answer, a page (200) showing the refusal, or a
    redirect to a page that answers. Callers verify separately that nothing changed."""
    if r.status >= 400 or r.status == 200:
        return True
    return r.status in REDIRECTS and bool(r.location) and landing(c, r).status < 400


def run_markers(page, rid) -> dict:
    """R7 run-page markers. The page carries data-run-id, data-run-status, data-period-start and data-version
    (not necessarily on one element): read each from the data-run-id element when it carries it, else from
    the first element on the page that does. Empty when the page carries no data-run-id."""
    root = page.dom.attr_one("data-run-id", str(rid)) or page.dom.attr_one("data-run-id")
    if root is None:
        return {}
    out = {"data-run-id": root.attrs.get("data-run-id")}
    for a in ("data-run-status", "data-period-start", "data-version"):
        node = root if a in root.attrs else page.dom.attr_one(a)
        out[a] = node.attrs.get(a) if node is not None else None
    return out


def admin_browser():
    """A real-browser page carrying the admin's session cookies (for judge evidence)."""
    br = B.Browser()
    cx = br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in H.admin.cookies.items()])
    return br, br.page(cx)


# ======================================================================================== W01
@workflow("W01", "Password links: anti-enumeration, single use, replacement, expiry, address matching", 3)
def w01(ctx):
    with ctx.step("both payroll admins set passwords from emailed links and sign in"):
        H.ensure_admins()
        ctx.surface("signin")
    quirky = legacy_pick(lambda e: e.active and e.role == "employee" and e.email != e.email.strip().lower(), 2)
    e1, e2 = quirky
    norm1 = e1.email.strip().lower()
    with ctx.step("a known and an unknown address get the same status and message; the unknown one gets no email"):
        before = H.mail_ids(norm1)
        rk = H.request_reset(norm1.upper())
        ghost = f"nobody.{H.nonce}@verify.example"
        ru = H.request_reset(ghost)
        ctx.eq(ru.status, rk.status, "status for an unknown address")
        tk, tu = norm_text(rk.text and visible_text(rk.text), norm1.upper(), norm1), norm_text(visible_text(ru.text), ghost)
        ctx.check(tk == tu, f"messages differ: known={tk[:200]!r} unknown={tu[:200]!r}")
        link = H.reset_link(norm1, before)
        ctx.check(link is not None, "no reset email reached the address on file")
        time.sleep(2)
        ctx.eq(len(H.mail.to(ghost)), 0, "emails to an unknown address")
        ctx.shared["w01_link1"] = link
    with ctx.step("an inactive employee's address receives no email"):
        inactive = legacy_pick(lambda e: not e.active)[0]
        addr = inactive.email.strip().lower()
        before = H.mail_ids(addr)
        H.request_reset(addr)
        time.sleep(4)
        ctx.eq(len(H.mail_ids(addr) - before), 0, "emails to an inactive employee")
    with ctx.step("a newer link replaces the older one; the older one sets nothing"):
        before = H.mail_ids(norm1)
        H.request_reset(norm1)
        link2 = H.reset_link(norm1, before)
        ctx.check(link2 and link2 != ctx.shared["w01_link1"], "no second, different link")
        c = H.client()
        page = c.get(ctx.shared["w01_link1"], follow=True)
        if page.status == 200:
            try:
                submit_form(c, "", "set-password", {"password": "Old-Link-Pass-1"}, follow=True, page=page)
            except FormNotFound:
                pass
        c2, _ = H.signin(norm1, "Old-Link-Pass-1")
        ctx.check(not H.signed_in(c2), "the replaced link set a working password")
        c = H.client()
        page = c.get(link2, follow=True)
        ctx.eq(page.status, 200, "newest link page status")
        submit_form(c, "", "set-password", {"password": PASSWORD}, follow=True, page=page)
        c3, _ = H.signin(norm1)
        ctx.check(H.signed_in(c3), "the newest link did not set a working password")
        ctx.shared["w01_link2"] = link2
    with ctx.step("a used link changes nothing"):
        c = H.client()
        page = c.get(ctx.shared["w01_link2"], follow=True)
        if page.status == 200:
            try:
                submit_form(c, "", "set-password", {"password": "Second-Use-Pass-1"}, follow=True, page=page)
            except FormNotFound:
                pass
        c4, _ = H.signin(norm1, "Second-Use-Pass-1")
        ctx.check(not H.signed_in(c4), "a used link set a password a second time")
        c5, _ = H.signin(norm1)
        ctx.check(H.signed_in(c5), "the password set before stopped working")
    with ctx.step("an expired link changes nothing"):
        addr2 = e2.email.strip().lower()
        before = H.mail_ids(addr2)
        H.request_reset(addr2)
        link3 = H.reset_link(addr2, before)
        ctx.check(link3, "no link for the second employee")
        time.sleep(H.reset_ttl + 3)
        c = H.client()
        page = c.get(link3, follow=True)
        if page.status == 200:
            try:
                submit_form(c, "", "set-password", {"password": "Expired-Link-1"}, follow=True, page=page)
            except FormNotFound:
                pass
        c6, _ = H.signin(addr2, "Expired-Link-1")
        ctx.check(not H.signed_in(c6), "an expired link set a password")
    with ctx.step("an unknown link answers 404"):
        r = H.client().get("/password-reset/" + secrets.token_urlsafe(24), follow=False)
        ctx.eq(r.status, 404, "unknown link status")
    with ctx.step("sign-in matches the address case-insensitively, ignoring surrounding spaces"):
        c7, _ = H.signin("  " + norm1.upper() + "  ")
        ctx.check(H.signed_in(c7), "sign-in with a differently cased, padded address failed")


# ======================================================================================== W02
@workflow("W02", "Sign-in refusals, lockout, session rotation and sign-out", 3)
def w02(ctx):
    e = legacy_pick(lambda e: e.active and e.role == "employee" and e.location_id == PHX)[0]
    addr = e.email.strip().lower()
    with ctx.step("set a password for a staff member"):
        H.ensure_admins()
        ctx.check(H.set_password(addr), "password link flow failed")
    with ctx.step("a wrong password and an unknown address are refused with the same message and no session"):
        c1, r1 = H.signin(addr, "Wrong-Password-1")
        ghost = f"ghost.{H.nonce}@verify.example"
        c2, r2 = H.signin(ghost, "Wrong-Password-1")
        ctx.check(not H.signed_in(c1) and not H.signed_in(c2), "a refused sign-in produced a session")
        p1, p2 = landing(c1, r1), landing(c2, r2)
        t1, t2 = norm_text(visible_text(p1.text), addr), norm_text(visible_text(p2.text), ghost)
        ctx.check(t1 == t2, f"refusal messages differ: {t1[:160]!r} vs {t2[:160]!r}")
    with ctx.step("five failures lock the account, even against the right password"):
        for _ in range(4):
            H.signin(addr, "Wrong-Password-1")
        H.signin(addr, "Wrong-Password-1")
        t_fifth = time.monotonic()  # after the fifth failure was answered: the wait below is a margin past it
        c, r = H.signin(addr)
        ctx.check(not H.signed_in(c), "the right password worked during the lockout")
        page = landing(c, r)
        ctx.check("lock" in visible_text(page.text).lower(), "the page does not say the account is locked")
        ctx.shared["w02_t5"] = t_fifth
    with ctx.step("the lock lifts LOGIN_LOCKOUT_SECONDS after the fifth failure"):
        wait = H.lockout + 2 - (time.monotonic() - ctx.shared["w02_t5"])
        if wait > 0:
            time.sleep(wait)
        c, _ = H.signin(addr)
        ctx.check(H.signed_in(c), "the right password still failed after the lockout")
    with ctx.step("a successful sign-in resets the failure count"):
        for _ in range(4):
            H.signin(addr, "Wrong-Password-1")
        c, _ = H.signin(addr)
        ctx.check(H.signed_in(c), "sign-in after 4 failures failed")
        for _ in range(4):
            H.signin(addr, "Wrong-Password-1")
        c, _ = H.signin(addr)
        ctx.check(H.signed_in(c), "the count was not reset by the successful sign-in")
    with ctx.step("signing in starts a new session identifier; an identifier held before never becomes signed in"):
        c = H.client()
        c.get("/signin", follow=True)
        c.get("/password-reset", follow=True)
        planted = {k: v for k, v in c.cookies.items()}
        _, r = H.signin(addr, client=c)
        set_now = {k: v for k, v in c.cookies.items()}
        ctx.check(H.signed_in(c), "sign-in failed")
        pre = H.client()
        pre.cookies = dict(planted)
        if planted:
            if H.signed_in(pre):
                ctx.breach("X9", "a session identifier held before sign-in became signed in")
                raise StepFailed("a session identifier held before sign-in became signed in")
            ctx.check(any(planted.get(k) != v for k, v in set_now.items()), "no cookie changed at sign-in")
        set_cookies = [h for h in r.header_all("Set-Cookie") if "=" in h.split(";")[0] and h.split(";")[0].split("=", 1)[1]]
        ctx.check(set_cookies, "sign-in set no cookie")
        # R2.6 constrains session cookies only: those whose removal signs this client out (a CSRF cookie is not one).
        names = H.session_cookie_names(c)
        ctx.observe("session_cookies", sorted(names))
        session_set = [h for h in set_cookies if h.split(";")[0].split("=", 1)[0].strip() in names] or set_cookies
        ctx.check(all("httponly" in h.lower() for h in session_set), "a session cookie set at sign-in is not HttpOnly")
        ctx.check(all(re.search(r"samesite\s*=\s*(lax|strict)", h.lower()) for h in session_set),
                  "a session cookie set at sign-in is not SameSite=Lax or Strict")
        ctx.shared["w02_client"] = c
    with ctx.step("signing out ends the session on the server"):
        c = ctx.shared["w02_client"]
        copy = c.copy()
        submit_form(c, "/timesheet", "signout", {}, follow=True)
        ctx.check(not H.signed_in(c), "still signed in after sign-out")
        if H.signed_in(copy):
            ctx.breach("X9", "a session cookie copied before sign-out still signs in")
            raise StepFailed("a cookie copied before sign-out still works")
    with ctx.step("a visitor sent to sign in from a protected page returns to that page after signing in"):
        target = "/profile?from=wf"
        c = H.client()
        r = c.get(target, follow=False)
        ctx.check(r.status in REDIRECTS and "/signin" in (r.location or ""), f"visitor answered {r.status}")
        page = c.get(r.location, follow=True)
        r2 = submit_form(c, "", "signin", {"email": addr, "password": PASSWORD}, page=page)
        loc = urllib.parse.urlsplit(urllib.parse.urljoin(H.base + "/", r2.location or ""))
        ctx.eq(loc.path + ("?" + loc.query if loc.query else ""), target, "where sign-in returned the visitor")
    with ctx.step("sign-in never redirects to another site, whatever next= says"):
        base_host = urllib.parse.urlsplit(H.base).netloc
        for evil in ("//evil.example/x", "/\\evil.example/x", "https://evil.example/x", "http:evil.example",
                     "\\\\evil.example", "///evil.example", "https:/evil.example"):
            c = H.client()
            page = c.get("/signin?next=" + urllib.parse.quote(evil, safe=""), follow=True)
            r = submit_form(c, "", "signin", {"email": addr, "password": PASSWORD}, page=page)
            loc = (r.location or "").strip()
            norm = loc.replace("\\", "/")
            dest = urllib.parse.urlsplit(urllib.parse.urljoin(H.base + "/", norm))
            offsite = bool(loc) and (
                norm.startswith("//")
                or (re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", norm) and not norm.startswith(H.base + "/"))
                or (dest.netloc and dest.netloc != base_host))
            if offsite:
                ctx.breach("X11", f"sign-in redirected to {loc!r} for next={evil!r}")
                raise StepFailed(f"open redirect to {loc!r} for next={evil!r}")
            ctx.check(r.status in REDIRECTS, f"sign-in answered {r.status} for next={evil!r}")


# ======================================================================================== W03
@workflow("W03", "Staff and rates: creation, duplicates, effective-dated rates, staff-page protection", 3)
def w03(ctx):
    with ctx.step("a payroll admin adds an employee with a first rate"):
        H.ensure_admins()
        eid = H.create_employee(CHI, name="Rhea Okonkwo", job="PREP", cents=1975, eff=date(2026, 9, 1), manager_id=None)
        rows = H.q("SELECT email, full_name, location_id, manager_id, role, active FROM employees WHERE id = %s", (eid,))
        ctx.check(rows, "no employees row for the new employee")
        r = rows[0]
        ctx.check(r["email"].strip().lower() == H.model.emps[eid].email and r["full_name"] == "Rhea Okonkwo"
                  and r["location_id"] == CHI and r["manager_id"] is None and r["role"] == "employee" and r["active"],
                  f"employees row differs: {r}")
        rates = H.q("SELECT job_code, rate_cents, effective_from FROM job_rates WHERE employee_id = %s", (eid,))
        ctx.eq([(x["job_code"], x["rate_cents"], x["effective_from"]) for x in rates], [("PREP", 1975, date(2026, 9, 1))], "job_rates")
        ctx.shared["w03_eid"] = eid
        ctx.surface("admin")
    with ctx.step("an address already on file is refused whatever its case and spacing; nothing changes"):
        existing = H.model.emps[ctx.shared["w03_eid"]].email
        n0 = H.q("SELECT count(*) AS n FROM employees")[0]["n"]
        r = submit_form(H.admin, "/admin/employees/new", "create-employee", {
            "email": "  " + existing.upper() + " ", "full_name": "Duplicate Person", "location_id": str(CHI), "manager_id": "",
            "role": "employee", "job_code": "PREP", "rate_cents": "1800", "effective_from": "2026-09-01"}, follow=True)
        n1 = H.q("SELECT count(*) AS n FROM employees")[0]["n"]
        ctx.eq(n1, n0, "employee rows after a duplicate address")
    with ctx.step("a rate is added; the same job and date again is refused"):
        eid = ctx.shared["w03_eid"]
        r = H.add_rate(eid, "PREP", 2050, date(2026, 10, 7))
        ctx.check(r.status < 400, f"adding a rate answered {r.status}")
        r2 = submit_form(H.admin, f"/admin/employees/{eid}", "add-rate",
                         {"job_code": "PREP", "rate_cents": "2999", "effective_from": "2026-10-07"}, follow=True)
        rates = H.q("SELECT rate_cents FROM job_rates WHERE employee_id=%s AND job_code='PREP' AND effective_from='2026-10-07'", (eid,))
        ctx.eq([x["rate_cents"] for x in rates], [2050], "rates for PREP from 2026-10-07")
    with ctx.step("adding a rate without the session token is refused with 403 and changes nothing"):
        eid = ctx.shared["w03_eid"]
        r = submit_form(H.admin, f"/admin/employees/{eid}", "add-rate",
                        {"job_code": "HOST", "rate_cents": "1800", "effective_from": "2026-10-01"}, drop_csrf=True)
        rows = H.q("SELECT 1 FROM job_rates WHERE employee_id=%s AND job_code='HOST'", (eid,))
        if rows:
            ctx.breach("X2", "a rate was added by a request without the session token")
        ctx.check(r.status == 403 and not rows, f"token-less rate request answered {r.status}, row added={bool(rows)}")
    with ctx.step("staff pages refuse a signed-in employee with 403 and a visitor is sent to sign in"):
        emp = legacy_pick(lambda e: e.active and e.role == "employee" and e.location_id == CHI)[0]
        c = H.account(emp.email.strip().lower())
        ctx.eq(c.get("/admin/employees/new").status, 403, "employee GET /admin/employees/new")
        prof = c.get("/profile", follow=True)
        token = [(i.attrs.get("name"), i.attrs.get("value", "")) for i in prof.dom.find_all(lambda n: n.tag == "input" and "data-csrf" in n.attrs)]
        n0 = H.q("SELECT count(*) AS n FROM employees")[0]["n"]
        r = c.post_form("/admin/employees/new", token + [("email", f"intruder.{H.nonce}@verify.example"), ("full_name", "Intruder"),
                        ("location_id", "1"), ("manager_id", ""), ("role", "payroll_admin"), ("job_code", "COOK"),
                        ("rate_cents", "9999"), ("effective_from", "2026-01-01")])
        n1 = H.q("SELECT count(*) AS n FROM employees")[0]["n"]
        if n1 != n0:
            ctx.breach("X1", "a signed-in employee created an employee through the staff form")
        ctx.check(r.status == 403 and n1 == n0, f"employee POST to the staff form answered {r.status}")
        anon = H.client().get("/admin/devices", follow=False)
        ctx.check(anon.status in REDIRECTS and "/signin" in (anon.location or ""), f"visitor answered {anon.status}")


# ======================================================================================== W04
@workflow("W04", "Time-clock keys and the punch API: auth, validation, idempotency, scope, races", 4)
def w04(ctx):
    with ctx.step("a time clock's key is shown once at creation and never again"):
        H.ensure_admins()
        did, key = H.create_device(LA, f"Back door {H.nonce}")
        listing = H.admin.get("/admin/devices", follow=True).text
        ctx.check(not any(f in listing for f in H.key_needles([key])), "the key is shown again on the time clock page")
        ctx.shared["w04"] = (did, key)
        ctx.surface("api")
    eid = None
    with ctx.step("an unknown key is refused with 401; a malformed body with 400"):
        ctx.eq(H.api("sbk_0_" + secrets.token_urlsafe(20), []).status, 401, "unknown key")
        ctx.eq(H.api("not-a-key", []).status, 401, "garbage key")
        key = ctx.shared["w04"][1]
        c = H.client()
        ctx.eq(c.request("POST", "/api/punches", body=b"{not json", headers={"Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}).status, 400, "malformed JSON")
        ctx.eq(H.api(key, {"punch": []}).status, 400, "wrong shape")
    with ctx.step("each row is accepted, a duplicate, or rejected for the stated reasons"):
        key = ctx.shared["w04"][1]
        eid = H.create_employee(LA, job="COOK", cents=2400)
        inactive = legacy_pick(lambda e: not e.active and e.location_id == LA)[0]
        other = legacy_pick(lambda e: e.active and e.location_id == PHX and e.role == "employee")[0]
        legacy_la = legacy_pick(lambda e: e.active and e.location_id == LA and e.role == "employee" and e.punches)[0]
        lp = legacy_la.punches[0]
        p = f"w04-{H.nonce}"
        rows = [
            {"punch_id": f"{p}-1", "employee_id": eid, "kind": "in", "at": "2026-10-06T15:02:00Z", "job_code": "COOK"},
            {"punch_id": f"{p}-2", "employee_id": eid, "at": "2026-10-06T19:00:00Z"},
            {"punch_id": f"{p}-3", "employee_id": eid, "kind": "out", "at": "2026-10-06T23:00:00"},
            {"punch_id": f"{p}-4", "employee_id": 9999999, "kind": "out", "at": "2026-10-06T23:00:00Z"},
            {"punch_id": f"{p}-5", "employee_id": inactive.eid, "kind": "in", "at": "2026-10-06T15:00:00Z", "job_code": "COOK"},
            {"punch_id": f"{p}-6", "employee_id": other.eid, "kind": "in", "at": "2026-10-06T15:00:00Z",
             "job_code": sorted(other.rates)[0]},
            {"punch_id": f"{p}-7", "employee_id": eid, "kind": "in", "at": "2026-10-07T15:00:00Z", "job_code": "ASTRONAUT"},
            {"punch_id": f"{p}-8", "employee_id": eid, "kind": "out", "at": "2026-10-06T23:05:00-07:00"},
            {"punch_id": f"{p}-1", "employee_id": eid, "kind": "in", "at": "2026-10-06T15:02:00Z", "job_code": "COOK"},
            {"punch_id": f"{p}-1", "employee_id": eid, "kind": "in", "at": "2026-10-06T15:08:00Z", "job_code": "COOK"},
            {"punch_id": f"{p}-9", "employee_id": eid, "kind": "break_start", "at": "2026-10-06T15:02:00+00:00"},
            {"punch_id": f"{p}-10", "employee_id": legacy_la.eid, "kind": "break_start",
             "at": datetime.fromtimestamp(lp.at_us / 1e6, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"},
            {"punch_id": "x" * 65, "employee_id": eid, "kind": "out", "at": "2026-10-08T01:00:00Z"},
        ]
        want = ["accepted", "rejected", "rejected", "rejected", "rejected", "rejected", "rejected", "accepted",
                "duplicate", "rejected", "rejected", "rejected", "rejected"]
        r = H.api(key, rows)
        ctx.eq(r.status, 200, "punch API status")
        res = r.json()["results"]
        got = [x.get("status") for x in res]
        if got[5] == "accepted":
            ctx.breach("X7", "a clock for one location recorded a punch for an employee of another location")
        if got[4] == "accepted":
            ctx.breach("X7", "a punch was accepted for an inactive employee")
        ctx.eq(got, want, "row statuses")
        ctx.check(all(isinstance(x.get("reason"), str) and x["reason"] for x, w in zip(res, want) if w == "rejected"),
                  "a rejected row has no reason")
        ctx.eq([x.get("punch_id") for x in res], [x["punch_id"] for x in rows], "result punch_ids in order")
        H.model.add_punches(eid, to_punches([rows[0], rows[7]]))
        ctx.shared["w04_eid"] = eid
    with ctx.step("the timesheet shows exactly the accepted punches for 2026-10-06 in Los Angeles"):
        eid = ctx.shared["w04_eid"]
        page = H.timesheet(H.admin, eid, date(2026, 10, 4))
        day = page.dom.attr_one("data-day", "2026-10-06")
        ctx.check(day is not None, "no data-day=2026-10-06")
        ctx.eq((day.attrs.get("data-reg-hours"), day.attrs.get("data-ot-hours"), day.attrs.get("data-dt-hours")),
               H.model.day_breakdown(eid, date(2026, 10, 4))[date(2026, 10, 6)], "hours on 2026-10-06")
    with ctx.step("simultaneous requests carrying one punch_id accept it once"):
        eid = ctx.shared["w04_eid"]
        key = ctx.shared["w04"][1]
        same = {"punch_id": f"w04race-{H.nonce}", "employee_id": eid, "kind": "in", "at": "2026-10-08T15:00:00Z", "job_code": "COOK"}
        outs = simultaneously([lambda: H.api(key, [same]) for _ in range(8)])
        statuses = []
        for kind, v in outs:
            ctx.check(kind == "ok" and v.status == 200, f"a racing request failed: {v}")
            statuses.append(v.json()["results"][0]["status"])
        ctx.eq(sorted(statuses), ["accepted"] + ["duplicate"] * 7, "statuses of 8 identical racing rows")
        diff = [{"punch_id": f"w04race2-{H.nonce}", "employee_id": eid, "kind": "out",
                 "at": f"2026-10-08T{18 + i}:00:00Z"} for i in range(6)]
        outs = simultaneously([lambda r=r: H.api(key, [r]) for r in diff])
        st2 = [v.json()["results"][0]["status"] for kind, v in outs if kind == "ok" and v.status == 200]
        ctx.eq(sorted(st2), ["accepted"] + ["rejected"] * 5, "statuses of 6 racing rows reusing one punch_id")
        winners = [r for (kind, v), r in zip(outs, diff) if kind == "ok" and v.json()["results"][0]["status"] == "accepted"]
        H.model.add_punches(eid, to_punches([same] + winners))
    with ctx.step("a revoked key is refused with 401 and records nothing"):
        did, key = ctx.shared["w04"]
        page = H.admin.get("/admin/devices", follow=True)
        form = None
        for f in page.dom.find_all(lambda n: n.tag == "form" and f"/admin/devices/{did}/revoke" in (n.attrs.get("action") or "")):
            form = f
        ctx.check(form is not None, "no revoke form for the new clock")
        from kitlib.web import form_fields
        H.admin.post_form(f"/admin/devices/{did}/revoke", form_fields(form), follow=True)
        eid = ctx.shared["w04_eid"]
        r = H.api(key, [{"punch_id": f"w04rev-{H.nonce}", "employee_id": eid, "kind": "in", "at": "2026-10-09T15:00:00Z", "job_code": "COOK"}])
        if r.status == 200 and any(x.get("status") == "accepted" for x in r.json().get("results", [])):
            ctx.breach("X7", "a revoked time clock key recorded a punch")
        ctx.eq(r.status, 401, "revoked key status")


# ======================================================================================== W05
@workflow("W05", "Timesheets: local days, midnight and DST splits, status markers", 3)
def w05(ctx):
    rnd = random.Random(505)
    g = Gen(rnd, f"w05-{H.nonce}")
    with ctx.step("create staff in all three locations and send their punches"):
        H.ensure_admins()
        la = H.create_employee(LA)
        phx = H.create_employee(PHX)
        chi = H.create_employee(CHI)
        exc = H.create_employee(LA)
        rows = []
        for eid in (la, phx, chi):
            emp = H.model.emps[eid]
            rows += g.shift(emp, local(2026, 10, 27, 18, 0), 9 * 60, "COOK", [(240, 36)], jitter=False)
            rows += g.shift(emp, local(2026, 10, 31, 22, 0), 8 * 60, "COOK", [], jitter=False)
            rows += g.shift(emp, local(2026, 11, 3, 6, 0), 13 * 60, "COOK", [(270, 42)], jitter=False)
        rows.append({"punch_id": g.pid(), "employee_id": exc, "kind": "out", "at": "2026-10-06T23:00:00Z"})
        H.import_rows(rows)
        ctx.shared["w05"] = (la, phx, chi, exc)
    with ctx.step("each day of each week shows the expected REG/OT/DT hours in the employee's local time"):
        la, phx, chi, exc = ctx.shared["w05"]
        for eid in (la, phx, chi):
            for ws in (date(2026, 10, 25), date(2026, 11, 1)):
                page = H.timesheet(H.admin, eid, ws)
                ctx.check(page.status == 200, f"timesheet {eid} {ws} answered {page.status}")
                root = page.dom.attr_one("data-week-start", ws.isoformat())
                ctx.check(root is not None and root.attrs.get("data-employee-id") == str(eid), f"timesheet {eid} {ws} markers missing")
                ctx.eq(root.attrs.get("data-status"), "pending", f"status of {eid} {ws}")
                want = H.model.day_breakdown(eid, ws)
                for d, hrs in want.items():
                    node = root.attr_one("data-day", d.isoformat()) or page.dom.attr_one("data-day", d.isoformat())
                    ctx.check(node is not None, f"no data-day {d}")
                    got = (node.attrs.get("data-reg-hours"), node.attrs.get("data-ot-hours"), node.attrs.get("data-dt-hours"))
                    ctx.eq(got, hrs, f"employee {eid} ({H.model.emps[eid].tz}) {d}")
    with ctx.step("an out punch with no open shift makes the week an exception that cannot be approved"):
        la, phx, chi, exc = ctx.shared["w05"]
        page = H.timesheet(H.admin, exc, date(2026, 10, 4))
        root = page.dom.attr_one("data-week-start", "2026-10-04")
        ctx.check(root is not None and root.attrs.get("data-status") == "exception", "the week is not marked exception")
        ctx.check(page.dom.attr_one("data-action", "approve-week") is None, "an approve button is offered for an exception week")
    with ctx.step("approving a pending week marks it approved"):
        la = ctx.shared["w05"][0]
        r = H.approve(H.admin, la, date(2026, 11, 1))
        ctx.check(r.status < 400, f"approve answered {r.status}")
        root = H.timesheet(H.admin, la, date(2026, 11, 1)).dom.attr_one("data-week-start", "2026-11-01")
        ctx.eq(root.attrs.get("data-status") if root else None, "approved", "status after approval")


# ======================================================================================== pay-rule groups
def _nonterminating(fr: Fraction) -> bool:
    d = fr.denominator
    for prime in (2, 5):
        while d % prime == 0:
            d //= prime
    return d != 1


def _tie_kinds(w) -> list[str]:
    """Premiums whose exact value is n + 1/2 cents while the regular rate does not terminate in decimal."""
    if not w.total_units or not _nonterminating(w.rr):
        return []
    out = []
    for name, val in (("ot", Fraction(w.ot_units, 10) * w.rr / 2), ("dt", Fraction(w.dt_units, 10) * w.rr),
                      ("meal", w.meal_premiums * w.rr)):
        if val and val - (val.numerator // val.denominator) == Fraction(1, 2):
            out.append(name)
    return out


def _approx_cents(kind: str, u1: int, u2: int, r1: int, r2: int, w, mode: str) -> int:
    """The amount computed the 'natural' approximate way: regular rate first (Decimal, 28 digits, or float),
    then the premium, then round half up. Used only to pick ties that approximate arithmetic gets wrong."""
    import decimal
    if mode == "decimal":
        D = decimal.Decimal
        with decimal.localcontext() as c:
            c.prec = 28
            straight = D(u1) * D(r1) / 10 + D(u2) * D(r2) / 10
            rr = straight / (D(u1 + u2) / 10)
            val = {"ot": D(w.ot_units) / 10 * rr / 2, "dt": D(w.dt_units) / 10 * rr, "meal": rr * w.meal_premiums}[kind]
            return int(val.quantize(D("1"), rounding=decimal.ROUND_HALF_UP))
    straight = u1 * r1 / 10 + u2 * r2 / 10
    rr = straight / ((u1 + u2) / 10)
    val = {"ot": w.ot_units / 10 * rr / 2, "dt": w.dt_units / 10 * rr, "meal": rr * w.meal_premiums}[kind]
    return int(val + 0.5)


def tie_staff(rnd, g, n: int, weeks: list[date], loc: int = LA) -> list[tuple[dict, list[dict]]]:
    """n (rates, rows) plans for two-job staff where some week of `weeks` has an exact half-cent premium
    under a repeating regular rate (R5.5: nothing but the four amounts is rounded). Found by fixing a
    random shift plan and scanning both rates over multiples of 5 cents."""
    out = []
    tz = H.model.locations[loc]["timezone"]
    while len(out) < n:
        stub = Emp(0, "x", "x", loc, tz, H.model.locations[loc]["ruleset"])
        stub.rates = {"COOK": [(date(2026, 1, 1), 2400)], "SERVER": [(date(2026, 1, 1), 1700)]}
        rows = []
        for ws in weeks:
            for d in sorted(rnd.sample(range(7), rnd.choice([5, 6]))):
                day = ws + timedelta(days=d)
                worked = rnd.choice([306, 330, 360, 420, 486, 510, 546, 600, 666, 720, 750, 786])
                br = [] if rnd.random() < 0.5 else compliant_breaks(rnd, worked)
                rows += g.shift(stub, local(day.year, day.month, day.day, rnd.choice([6, 7, 9]), 0), worked,
                                rnd.choice(["COOK", "SERVER"]), br)
        stub.punches = to_punches(rows)
        shifts, exc = P.build_shifts(stub.punches)
        if exc:
            continue
        base = P.compute_weeks(shifts, tz, stub.ruleset, stub.rate_for)
        units = {}
        for ws in weeks:
            w = base.get(ws)
            if not w or not w.total_units:
                continue
            per_job = {"COOK": 0, "SERVER": 0}
            for _t, job in w.intervals:
                per_job[job] += 1
            units[ws] = (per_job["COOK"], per_job["SERVER"], w)
        found = None
        for r1 in rnd.sample(range(1650, 3205, 5), 200):
            for r2 in range(1650, 3205, 5):
                for ws, (u1, u2, w) in units.items():
                    total = u1 + u2
                    rr = Fraction(u1 * r1 + u2 * r2, total)
                    if not _nonterminating(rr):
                        continue
                    for kind, val in (("ot", Fraction(w.ot_units, 10) * rr / 2), ("dt", Fraction(w.dt_units, 10) * rr),
                                      ("meal", w.meal_premiums * rr)):
                        if val and val - (val.numerator // val.denominator) == Fraction(1, 2):
                            exact = val.numerator // val.denominator + 1
                            mode = ("decimal", "float")[len(out) % 2]
                            if _approx_cents(kind, u1, u2, r1, r2, w, mode) != exact:
                                found = (r1, r2)
                                break
                    if found:
                        break
                if found:
                    break
            if found:
                break
        if not found:
            continue
        stub.rates = {"COOK": [(date(2026, 1, 1), found[0])], "SERVER": [(date(2026, 1, 1), found[1])]}
        check = P.compute_weeks(shifts, tz, stub.ruleset, stub.rate_for)
        if any(_tie_kinds(check[ws]) for ws in weeks if ws in check):
            out.append(({"COOK": found[0], "SERVER": found[1]}, rows))
    return out


TIES_FILE = Path(__file__).resolve().parent / "reference" / "ties.json"
_TIES_USED: dict = {}


def add_tie_staff(rnd, g, n: int, weeks: list[date]) -> tuple[list[int], list[dict]]:
    """Create n Los Angeles staff from the precomputed hard-tie plans for this pay period
    (tests/reference/ties.json, written by reference/gen_ties.py) and return their punch rows."""
    period = weeks[0].isoformat()
    plans = json.loads(TIES_FILE.read_text())[period]
    start = _TIES_USED.get(period, 0)
    if start + n > len(plans):
        raise InfraError(f"verifier fixture: only {len(plans)} tie plans for {period}")
    _TIES_USED[period] = start + n
    eids, rows = [], []
    for plan in plans[start:start + n]:
        rates = plan["rates"]
        eid = H.create_employee(LA, job="COOK", cents=rates["COOK"])
        r = H.add_rate(eid, "SERVER", rates["SERVER"], date(2026, 1, 1))
        if r.status >= 400:
            raise StepFailed(f"adding the SERVER rate for {eid} answered {r.status}")
        for pr in plan["punches"]:
            row = {"punch_id": g.pid(), "employee_id": eid, "kind": pr["kind"], "at": pr["at"]}
            if pr["kind"] == "in":
                row["job_code"] = pr["job_code"]
            rows.append(row)
        eids.append(eid)
    return eids, rows


def check_timesheets(ctx, eids: list[int], weeks: list[date], k: int = 4) -> None:
    """Interface contract on a sample of timesheets: per-day hours, status, approve button iff pending+work+allowed."""
    rnd = random.Random(len(eids) * 7919 + len(weeks))
    for eid in rnd.sample(eids, min(k, len(eids))):
        for ws in weeks:
            page = H.timesheet(H.admin, eid, ws)
            root = page.dom.attr_one("data-week-start", ws.isoformat())
            ctx.check(root is not None, f"timesheet {eid} {ws}: no data-week-start marker")
            st = State(H.model.emps[eid])
            ctx.eq(root.attrs.get("data-status"), st.status(ws), f"timesheet {eid} {ws} status")
            for d, hrs in H.model.day_breakdown(eid, ws).items():
                node = page.dom.attr_one("data-day", d.isoformat())
                ctx.check(node is not None, f"timesheet {eid}: no data-day {d}")
                ctx.eq((node.attrs.get("data-reg-hours"), node.attrs.get("data-ot-hours"), node.attrs.get("data-dt-hours")),
                       hrs, f"timesheet {eid} {d}")
            want_button = st.status(ws) == "pending" and st.has_work(ws)
            has_button = page.dom.attr_one("data-action", "approve-week") is not None
            ctx.eq(has_button, want_button, f"approve button on {eid} week {ws} ({st.status(ws)})")


def rule_group(ctx, wid: str, build, periods: list[date], ties: int = 8):
    rnd = random.Random(zlib.crc32(wid.encode()) & 0xFFFF)  # stable across processes (hash() is salted)
    g = Gen(rnd, f"{wid.lower()}-{H.nonce}")
    with ctx.step("create the staff for this rule and send their punches"):
        H.ensure_admins()
        eids, rows = build(rnd, g)
        if ties:
            t_eids, t_rows = add_tie_staff(rnd, g, ties, weeks_of(periods[-1]))
            eids, rows = eids + t_eids, rows + t_rows
        H.import_rows(rows)
        ctx.shared[wid] = eids
        ctx.observe("employees", eids)
    with ctx.step("before approval, timesheets follow the interface contract"):
        check_timesheets(ctx, ctx.shared[wid], weeks_of(periods[0]), k=3)
    with ctx.step("approve every week with worked time"):
        H.approve_all(ctx.shared[wid], sorted({w for p in periods for w in weeks_of(p)} | extra_weeks(ctx.shared[wid])))
    with ctx.step("after approval, timesheets follow the interface contract"):
        check_timesheets(ctx, ctx.shared[wid], weeks_of(periods[0]), k=3)
    for period in periods:
        with ctx.step(f"the draft run for {period} pays every hour and cent as the rules say"):
            r, rid, took = H.create_run(period)
            ctx.check(rid is not None, f"creating the draft answered {r.status} without redirecting to a run")
            resp, rows = H.export(rid)
            ctx.eq(resp.status, 200, "export status")
            problems = H.compare_lines(period, rows, ctx.shared[wid])
            ctx.check(not problems, f"{len(problems)} lines differ: " + " | ".join(problems[:6]))


def extra_weeks(eids) -> set:
    out = set()
    for eid in eids:
        out |= set(State(H.model.emps[eid]).weeks)
    return out


@workflow("W06", "Pay rules: CA daily overtime and double time", 5, critical=True)
def w06(ctx):
    def build(rnd, g):
        eids, rows = [], []
        for i in range(80):
            eid = H.create_employee(LA, cents=rnd.randrange(1800, 3200, 5))
            emp = H.model.emps[eid]
            for ws in weeks_of(P1004):
                days = sorted(rnd.sample(range(7), 4))
                for d in days:
                    day = ws + timedelta(days=d)
                    worked = rnd.choice([360, 450, 480, 486, 510, 600, 690, 720, 726, 780, 840])
                    start = local(day.year, day.month, day.day, rnd.choice([5, 6, 7, 8]), rnd.choice([0, 6, 30]))
                    rows += g.shift(emp, start, worked, "COOK", compliant_breaks(rnd, worked))
                if rnd.random() < 0.4:  # a split shift: two shifts on one day
                    day = ws + timedelta(days=[d for d in range(7) if d not in days][0])
                    rows += g.shift(emp, local(day.year, day.month, day.day, 6, 0), 300, "COOK", [])
                    rows += g.shift(emp, local(day.year, day.month, day.day, 13, 0), 270, "COOK", [])
            eids.append(eid)
        return eids, rows
    rule_group(ctx, "W06", build, [P1004])


@workflow("W07", "Pay rules: weekly overtime without pyramiding (CA and FLSA)", 5, critical=True)
def w07(ctx):
    def build(rnd, g):
        eids, rows = [], []
        for i in range(80):
            loc = [PHX, CHI, LA][i % 3]
            eid = H.create_employee(loc, cents=rnd.randrange(1650, 3000, 5))
            emp = H.model.emps[eid]
            for ws in weeks_of(P1004):
                days = sorted(rnd.sample(range(7), rnd.choice([5, 6])))
                for d in days:
                    day = ws + timedelta(days=d)
                    worked = rnd.choice([420, 480, 510, 540, 570, 600, 630, 660])
                    rows += g.shift(emp, local(day.year, day.month, day.day, rnd.choice([6, 7, 9]), 0), worked, "COOK",
                                    compliant_breaks(rnd, worked))
            eids.append(eid)
        return eids, rows
    rule_group(ctx, "W07", build, [P1004])


@workflow("W08", "Pay rules: the CA seventh consecutive day", 4, critical=True)
def w08(ctx):
    def build(rnd, g):
        eids, rows = [], []
        for i in range(60):
            eid = H.create_employee(LA if i % 6 else PHX, cents=rnd.randrange(1800, 3000, 5))
            emp = H.model.emps[eid]
            for wi, ws in enumerate(weeks_of(P1004)):
                ndays = 7 if (wi == 0 or i % 2) else 6
                for d in range(7 - ndays, 7):
                    day = ws + timedelta(days=d)
                    worked = rnd.choice([240, 300, 360, 420, 480, 540, 600, 750]) if d == 6 else rnd.choice([300, 360, 420, 480, 540])
                    rows += g.shift(emp, local(day.year, day.month, day.day, 8, 0), worked, "COOK", compliant_breaks(rnd, worked))
            eids.append(eid)
        return eids, rows
    rule_group(ctx, "W08", build, [P1004])


@workflow("W09", "Pay rules: regular rate across jobs, effective-dated rates, work before a job's first rate", 5, critical=True)
def w09(ctx):
    def build(rnd, g):
        eids, rows = [], []
        for i in range(60):
            loc = [PHX, CHI, LA, PHX][i % 4]
            eid = H.create_employee(loc, job="COOK", cents=rnd.randrange(2200, 3200, 5))
            emp = H.model.emps[eid]
            H.add_rate(eid, "SERVER", rnd.randrange(1650, 1900, 5), date(2026, 1, 1))
            if i % 3 == 0:
                H.add_rate(eid, "BAR", rnd.randrange(1900, 2600, 5), date(2026, 1, 1))
            if i % 4 == 1:
                H.add_rate(eid, "COOK", emp.rate_for("COOK", date(2026, 10, 1)) + 125, date(2026, 10, 7))
            jobs = sorted(emp.rates)
            for ws in weeks_of(P1004):
                for d in sorted(rnd.sample(range(7), rnd.choice([5, 6]))):
                    day = ws + timedelta(days=d)
                    worked = rnd.choice([360, 450, 480, 540, 600, 660])
                    rows += g.shift(emp, local(day.year, day.month, day.day, rnd.choice([6, 10, 15]), 0), worked,
                                    rnd.choice(jobs), compliant_breaks(rnd, worked))
            eids.append(eid)
        late = H.create_employee(CHI, job="COOK", cents=2400)
        H.add_rate(late, "HOST", 1800, date(2026, 10, 14))
        emp = H.model.emps[late]
        rows += g.shift(emp, local(2026, 10, 5, 9, 0), 480, "COOK", [(240, 36)])
        rows += g.shift(emp, local(2026, 10, 13, 9, 0), 480, "HOST", [(240, 36)])
        rows += g.shift(emp, local(2026, 10, 15, 9, 0), 480, "HOST", [(240, 36)])
        eids.append(late)
        return eids, rows
    rule_group(ctx, "W09", build, [P1004])
    with ctx.step("work before a job's first rate holds that week and the draft lists it as held"):
        late = next(e for e in ctx.shared["W09"]
                    if (H.model.emps[e].rates.get("HOST") or [(None, 0)])[0][0] == date(2026, 10, 14))
        r, rid, _ = H.create_run(P1004)
        page = H.run_page(rid)
        held = {(n.attrs.get("data-held-employee-id"), n.attrs.get("data-week-start")) for n in page.dom.attr_all("data-held-employee-id")}
        ctx.check((str(late), "2026-10-11") in held, "the week with work before the HOST rate is not listed as held")


@workflow("W10", "Pay rules: midnight, workweek, pay-period and DST boundaries; rounding ties", 5, critical=True)
def w10(ctx):
    def build(rnd, g):
        eids, rows = [], []
        for i in range(60):
            loc = [LA, CHI, PHX][i % 3]
            eid = H.create_employee(loc, cents=rnd.randrange(1800, 3000, 5))
            emp = H.model.emps[eid]
            plans = [
                (local(2026, 10, 6, 19, 0), rnd.choice([420, 540, 660, 780])),
                (local(2026, 10, 10, 20, 0), rnd.choice([480, 600, 720])),   # Saturday into Sunday: two workweeks
                (local(2026, 10, 17, 21, 0), rnd.choice([420, 540])),        # Saturday into Sunday: two pay periods
                (local(2026, 10, 31, 21, 0), rnd.choice([480, 600, 720])),   # across the DST change
                (local(2026, 11, 1, 23, 0), rnd.choice([360, 540])),
                (local(2026, 10, 13, 6, 0), rnd.choice([480, 570])),
            ]
            for start, worked in plans:
                rows += g.shift(emp, start, worked, "COOK", compliant_breaks(rnd, worked))
            eids.append(eid)
        return eids, rows
    rule_group(ctx, "W10", build, [P1004, P1018, P1101])


@workflow("W11", "Pay rules: CA meal premiums", 4, critical=True)
def w11(ctx):
    def build(rnd, g):
        eids, rows = [], []
        cases = [
            [(300, 30)], [(306, 30)], [(294, 30)], [(240, 24)], [(240, 30)], [(180, 18), (300, 30)],
            [(312, 36)], [], [(330, 60)], [(300, 29)],
        ]
        for i in range(60):
            loc = LA if i % 5 else PHX
            eid = H.create_employee(loc, cents=rnd.randrange(1800, 3000, 5))
            emp = H.model.emps[eid]
            for k, d in enumerate(rnd.sample(range(7), 5)):
                day = P1004 + timedelta(days=d)
                worked = rnd.choice([306, 330, 360, 420, 480])
                br = [b for b in cases[(i + k) % len(cases)] if b[0] <= worked - 12]
                rows += g.shift(emp, local(day.year, day.month, day.day, 7, 0), worked, "COOK", br, jitter=False)
            day = P1004 + timedelta(days=7 + rnd.randrange(6))
            rows += g.shift(emp, local(day.year, day.month, day.day, 6, 0), 330, "COOK", [], jitter=False)
            rows += g.shift(emp, local(day.year, day.month, day.day, 13, 30), 330, "COOK", [], jitter=False)
            rows += g.shift(emp, local(2026, 10, 16, 22, 0), 420, "COOK", [], jitter=False)
            eids.append(eid)
        return eids, rows
    rule_group(ctx, "W11", build, [P1004])


# ======================================================================================== W20
@workflow("W20", "Exceptions across week boundaries: interrupted, unclosed and over-16-hour shifts", 4, critical=True)
def w20(ctx):
    rnd = random.Random(2020)
    g = Gen(rnd, f"w20-{H.nonce}")
    W1, W2 = P1004, P1004 + timedelta(days=7)

    def p(emp, kind, y, m, d, hh, mm=0, job="COOK"):
        at = datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(emp.tz)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = {"punch_id": g.pid(), "employee_id": emp.eid, "kind": kind, "at": at}
        if kind == "in":
            row["job_code"] = job
        return row

    with ctx.step("staff whose punches break the rules across the 2026-10-10/11 week boundary"):
        H.ensure_admins()
        eids, rows = [], []
        for i in range(36):
            eid = H.create_employee([LA, PHX, CHI][i % 3], cents=rnd.randrange(1800, 3000, 5))
            emp = H.model.emps[eid]
            for d in (5, 6, 7):          # valid work in week 1 (Mon-Wed)
                rows += g.shift(emp, local(2026, 10, d, 9, 0), 480, "COOK", [(240, 36)])
            for d in (13, 14, 15):       # valid work in week 2 (Tue-Thu)
                rows += g.shift(emp, local(2026, 10, d, 9, 0), 480, "COOK", [(240, 36)])
            case = i % 6
            if case == 0:   # open shift on Saturday night interrupted by Monday's in
                rows += [p(emp, "in", 2026, 10, 10, 22), p(emp, "in", 2026, 10, 12, 9), p(emp, "out", 2026, 10, 12, 17)]
            elif case == 1:  # a 17-hour shift from Saturday into Sunday: counts in no week
                rows += [p(emp, "in", 2026, 10, 10, 20), p(emp, "out", 2026, 10, 11, 13)]
            elif case == 2:  # a break that never ends, then the next day's shift
                rows += [p(emp, "in", 2026, 10, 8, 9), p(emp, "break_start", 2026, 10, 8, 13), p(emp, "in", 2026, 10, 9, 9),
                         p(emp, "out", 2026, 10, 9, 17)]
            elif case == 3:  # an orphan out just after midnight on Sunday, after a valid Saturday shift
                rows += [p(emp, "in", 2026, 10, 10, 16), p(emp, "out", 2026, 10, 10, 23), p(emp, "out", 2026, 10, 11, 0, 30)]
            elif case == 4:  # two ins in a row on Saturday evening, closed after midnight
                rows += [p(emp, "in", 2026, 10, 10, 18), p(emp, "in", 2026, 10, 10, 19), p(emp, "out", 2026, 10, 11, 2)]
            else:            # an in on the last Saturday of the period with no out at all, then nothing
                rows += [p(emp, "in", 2026, 10, 17, 21)]
            eids.append(eid)
        t_eids, t_rows = add_tie_staff(rnd, g, 6, [W1, W2])
        H.import_rows(rows + t_rows)
        H.approve_all(eids + t_eids, [W1, W2])
        ctx.shared["w20"] = eids + t_eids
    with ctx.step("the draft for 2026-10-04 pays only exception-free weeks and holds the others"):
        r, rid, _ = H.create_run(P1004)
        ctx.check(rid is not None, f"draft answered {r.status}")
        resp, rows = H.export(rid)
        problems = H.compare_lines(P1004, rows, ctx.shared["w20"])
        ctx.check(not problems, f"{len(problems)} lines differ: " + " | ".join(problems[:5]))
        page = H.run_page(rid)
        got = {(int(n.attrs.get("data-held-employee-id") or 0), n.attrs.get("data-week-start"))
               for n in page.dom.attr_all("data-held-employee-id")}
        got = {x for x in got if x[0] in set(ctx.shared["w20"])}
        want = {(e, ws.isoformat()) for e in ctx.shared["w20"] for ws in H.model.held(e, P1004)}
        ctx.check(got == want, f"held weeks differ: missing {sorted(want - got)[:5]}, extra {sorted(got - want)[:5]}")


# ======================================================================================== W12
def legacy_location(ctx, loc: int, wkey: str):
    """One location's share of the old system's records: every line and held week in two runs."""
    legacy = sorted(e.eid for e in H.model.emps.values() if e.legacy and e.location_id == loc)
    for period in (P1018, P1101):
        with ctx.step(f"the draft for {period} answers within 120 s"):
            H.ensure_admins()
            r, rid, took = H.create_run(period)
            ctx.observe(f"seconds_{period}", round(took, 1))
            ctx.check(rid is not None, f"creating the draft answered {r.status}")
            ctx.check(took <= 120, f"the draft took {took:.0f} s")
            ctx.shared[f"{wkey}_{period}"] = rid
            ctx.shared[f"w12_{period}"] = rid
        with ctx.step(f"every legacy {H.model.locations[loc]['name']} line for {period} matches the rules"):
            resp, rows = H.export(ctx.shared[f"{wkey}_{period}"])
            ctx.eq(resp.status, 200, "export status")
            problems = H.compare_lines(period, rows, legacy)
            ctx.observe(f"mismatches_{period}", len(problems))
            ctx.check(not problems, f"{len(problems)} of {len(legacy)} legacy lines differ: " + " | ".join(problems[:5]))
        with ctx.step(f"the draft for {period} lists exactly this location's held legacy weeks"):
            page = H.run_page(ctx.shared[f"{wkey}_{period}"])
            got = {(int(n.attrs.get("data-held-employee-id", "0") or 0), n.attrs.get("data-week-start"))
                   for n in page.dom.attr_all("data-held-employee-id")}
            got = {x for x in got if x[0] in set(legacy)}
            want = {(e, ws.isoformat()) for e in legacy for ws in H.model.held(e, period)}
            ctx.check(got == want, f"held weeks differ: missing {sorted(want - got)[:5]}, extra {sorted(got - want)[:5]}")


@workflow("W12", "Legacy data at scale: Mission St (CA rules, Los Angeles) lines and held weeks", 4, timeout=1500, critical=True)
def w12(ctx):
    legacy_location(ctx, LA, "w12la")


@workflow("W21", "Legacy data at scale: Roosevelt Row (FLSA, Phoenix) lines and held weeks", 4, timeout=1500, critical=True)
def w21(ctx):
    legacy_location(ctx, PHX, "w21phx")


@workflow("W22", "Legacy data at scale: Wicker Park (FLSA, Chicago) lines and held weeks", 4, timeout=1500, critical=True)
def w22(ctx):
    legacy_location(ctx, CHI, "w22chi")


# ======================================================================================== W13
@workflow("W13", "Approvals: reporting tree, self-approval, peers, invalidation, CSRF", 4)
def w13(ctx):
    rnd = random.Random(1313)
    g = Gen(rnd, f"w13-{H.nonce}")
    with ctx.step("build a reporting tree with accounts"):
        H.ensure_admins()
        m1 = H.create_employee(LA, role="manager", manager_id=1)
        m2 = H.create_employee(LA, role="manager", manager_id=m1)
        peer = H.create_employee(LA, role="manager", manager_id=1)
        e1 = H.create_employee(LA, manager_id=m2)
        e2 = H.create_employee(LA, manager_id=m1)
        accts = {k: H.account(H.model.emps[v].email) for k, v in (("m1", m1), ("m2", m2), ("peer", peer), ("e1", e1), ("e2", e2))}
        rows = []
        for eid in (e1, e2, m2, 1):
            emp = H.model.emps[eid]
            rows += g.shift(emp, local(2026, 11, 17, 9, 0), 480, sorted(emp.rates)[0], [(240, 36)])
        H.import_rows(rows)
        ctx.shared["w13"] = (m1, m2, peer, e1, e2, accts)
    W = date(2026, 11, 15)
    with ctx.step("viewing follows the tree: managers above see, peers and colleagues get 403"):
        m1, m2, peer, e1, e2, a = ctx.shared["w13"]
        ctx.eq(H.timesheet(a["m1"], e1, W).status, 200, "manager two levels up viewing")
        ctx.eq(H.timesheet(a["m2"], e1, W).status, 200, "direct manager viewing")
        for who in ("peer", "e2"):
            r = H.timesheet(a[who], e1, W)
            if r.status == 200 and r.dom.attr_one("data-week-start"):
                ctx.breach("X1", f"{who} saw another branch's timesheet")
            ctx.eq(r.status, 403, f"{who} viewing")
        anon = H.client().get(f"/employees/{e1}/timesheet?week={W}", follow=False)
        ctx.check(anon.status in REDIRECTS and "/signin" in (anon.location or ""), f"visitor answered {anon.status}")
        rid = ctx.shared.get(f"w12_{P1018}")
        if rid:
            r = a["e1"].get(f"/payroll/runs/{rid}/export.csv")
            if r.status == 200 and b"employee_id" in r.body:
                ctx.breach("X1", "an employee downloaded a payroll export")
            ctx.eq(r.status, 403, "employee GET of a payroll export")
    with ctx.step("/approvals lists exactly the pending weeks the manager may approve"):
        m1, m2, peer, e1, e2, a = ctx.shared["w13"]
        page = a["m1"].get("/approvals", follow=True)
        got = {(n.attrs.get("data-employee-id"), n.attrs.get("data-week-start")) for n in page.dom.attr_all("data-week-start")
               if n.attrs.get("data-employee-id")}
        ctx.eq(got, {(str(e1), W.isoformat()), (str(e2), W.isoformat()), (str(m2), W.isoformat())}, "m1's approval list")
    with ctx.step("nobody approves their own week or a week outside their branch; a token-less approval is refused"):
        m1, m2, peer, e1, e2, a = ctx.shared["w13"]

        def status_of(eid):
            root = H.timesheet(H.admin2, eid, W).dom.attr_one("data-week-start", W.isoformat())
            return root.attrs.get("data-status") if root else None

        attempts = [("e1 approving own week", a["e1"], e1), ("peer approving", a["peer"], e1),
                    ("e2 approving a colleague", a["e2"], e1), ("payroll admin approving own week", H.admin, 1)]
        for label, c, target in attempts:
            page = c.get("/profile", follow=True)
            token = [(i.attrs.get("name"), i.attrs.get("value", "")) for i in page.dom.find_all(lambda n: n.tag == "input" and "data-csrf" in n.attrs)]
            r = c.post_form(f"/employees/{target}/weeks/{W.isoformat()}/approve", token, follow=False)
            if status_of(target) == "approved":
                ctx.breach("X1", f"{label} succeeded")
                raise StepFailed(f"{label} approved the week")
            ctx.check(r.status in (403, 409), f"{label} answered {r.status}")
        r = submit_form(a["m2"], f"/employees/{e1}/timesheet?week={W}", "approve-week", {}, drop_csrf=True)
        if status_of(e1) == "approved":
            ctx.breach("X2", "an approval without the session token took effect")
            raise StepFailed("token-less approval took effect")
        ctx.eq(r.status, 403, "token-less approval status")
    with ctx.step("the manager two levels up approves; the week is approved"):
        m1, m2, peer, e1, e2, a = ctx.shared["w13"]
        r = H.approve(a["m1"], e1, W)
        ctx.check(r.status < 400, f"approve answered {r.status}")
        root = H.timesheet(a["m1"], e1, W).dom.attr_one("data-week-start", W.isoformat())
        ctx.eq(root.attrs.get("data-status") if root else None, "approved", "status after approval")
    with ctx.step("a later punch returns the approved week to pending"):
        m1, m2, peer, e1, e2, a = ctx.shared["w13"]
        H.import_rows(g.shift(H.model.emps[e1], local(2026, 11, 19, 9, 0), 240, "COOK", []))
        root = H.timesheet(a["m1"], e1, W).dom.attr_one("data-week-start", W.isoformat())
        ctx.eq(root.attrs.get("data-status") if root else None, "pending", "status after a new punch")
    with ctx.step("a new punch returns a week approved by the old system to pending"):
        def free_saturday(e):
            z = ZoneInfo(e.tz)
            lo = datetime(2026, 11, 13, 12, 0, tzinfo=z).timestamp() * 1e6
            hi = datetime(2026, 11, 15, 0, 0, tzinfo=z).timestamp() * 1e6
            return not any(lo <= p.at_us < hi for p in e.punches)
        cand = legacy_pick(lambda e: e.active and e.role == "employee" and date(2026, 11, 8) in e.approved
                           and State(e).status(date(2026, 11, 8)) == "approved" and free_saturday(e))[0]
        H.import_rows(g.shift(cand, local(2026, 11, 14, 10, 0), 60, sorted(cand.rates)[0], []))
        root = H.timesheet(H.admin, cand.eid, date(2026, 11, 8)).dom.attr_one("data-week-start", "2026-11-08")
        ctx.eq(root.attrs.get("data-status") if root else None, "pending", f"legacy employee {cand.eid} week 2026-11-08")
    with ctx.step("an exception week cannot be approved"):
        m1, m2, peer, e1, e2, a = ctx.shared["w13"]
        H.import_rows([{"punch_id": g.pid(), "employee_id": e2, "kind": "out", "at": "2026-11-24T20:00:00Z"}])
        W2 = date(2026, 11, 22)
        page = H.timesheet(a["m1"], e2, W2)
        token = [(i.attrs.get("name"), i.attrs.get("value", "")) for i in page.dom.find_all(lambda n: n.tag == "input" and "data-csrf" in n.attrs)]
        if not token:
            token = [(i.attrs.get("name"), i.attrs.get("value", "")) for i in a["m1"].get("/profile", follow=True).dom.find_all(
                lambda n: n.tag == "input" and "data-csrf" in n.attrs)]
        r = a["m1"].post_form(f"/employees/{e2}/weeks/{W2.isoformat()}/approve", token)
        root = H.timesheet(a["m1"], e2, W2).dom.attr_one("data-week-start", W2.isoformat())
        ctx.eq(root.attrs.get("data-status") if root else None, "exception", "exception week status after an approval attempt")
        ctx.check(refused(a["m1"], r), f"approving an exception week answered {r.status}")


# ======================================================================================== W14
def capture_paystub(eid: int, before_ids: set, rid) -> None:
    """Judge evidence for R7.5 (one pay statement), taken as soon as the run is finalized so that it does not
    depend on the strict line and statement checks that follow. Best effort: never fails the workflow."""
    try:
        emp = H.model.emps[eid]
        new = H.mail.wait_for(emp.email, lambda m: m["id"] not in before_ids and "pay statement" in m["subject"].lower(), 1, 35)
        if not new:
            return
        gross = H.export(rid)[1].get(eid, {}).get("gross_cents")
        B.capture_text("paystub_email", f"Subject: {new[0]['subject']}\n\n{new[0]['text']}",
                       {"employee_id": eid, "gross_cents": int(gross) if gross and gross.lstrip("-").isdigit() else gross,
                        "period": ["2026-09-20", "2026-10-03"]})
    except Exception as e:  # noqa: BLE001 - evidence only
        print(f"[wf] W14 paystub evidence not captured: {type(e).__name__}: {e}"[:300], flush=True)


@workflow("W14", "Payroll run lifecycle: stale drafts, one finalization, lock, pay statements once", 5, critical=True)
def w14(ctx):
    rnd = random.Random(1414)
    g = Gen(rnd, f"w14-{H.nonce}")
    with ctx.step("staff with approved weeks in the period starting 2026-09-20"):
        H.ensure_admins()
        eids, rows = [], []
        for i in range(5):
            eid = H.create_employee([LA, PHX, CHI][i % 3], cents=rnd.randrange(1800, 3000, 5))
            emp = H.model.emps[eid]
            for d in (1, 2, 3, 8, 9, 10, 11):
                day = P0920 + timedelta(days=d)
                rows += g.shift(emp, local(day.year, day.month, day.day, 8, 0), rnd.choice([420, 480, 540, 600]), "COOK", [(240, 36)])
            eids.append(eid)
        t_eids, t_rows = add_tie_staff(rnd, g, 6, weeks_of(P0920))
        H.import_rows(rows + t_rows)
        H.approve_all(eids + t_eids, weeks_of(P0920))
        ctx.shared["w14"] = eids
    with ctx.step("the draft carries a version"):
        r, rid, _ = H.create_run(P0920)
        ctx.check(rid is not None, f"draft creation answered {r.status}")
        page = H.run_page(rid)
        mk = run_markers(page, rid)
        ctx.check(mk.get("data-run-status") == "draft" and mk.get("data-version"), f"draft markers {mk}")
        ctx.shared["w14_run"] = (rid, mk["data-version"], page)
    with ctx.step("a punch after the draft makes finalizing it (from the browser) refused with 409; it stays a draft"):
        rid, ver, page = ctx.shared["w14_run"]
        eid = ctx.shared["w14"][0]
        br, bp = admin_browser()
        try:
            bp.goto(H.base + f"/payroll/runs/{rid}")
            bp.wait_for_load_state()
            B.capture(bp, "finalize_before", {"status": "draft", "lines": len(ctx.shared["w14"])},
                      focus=["[data-action=finalize-run]"])
            H.import_rows(g.shift(H.model.emps[eid], local(2026, 9, 26, 9, 0), 120, "COOK", []))
            statuses = []
            bp.on("response", lambda resp: statuses.append(resp.status) if "/finalize" in resp.url else None)
            # R7.6: recomputing may take up to 120 s. The page may ask for confirmation first (dialogs are accepted).
            with bp.expect_response(lambda resp: "/finalize" in resp.url and resp.request.method == "POST",
                                    timeout=130000) as fin:
                bp.click("[data-action=finalize-run]", timeout=130000)
            if fin.value.status not in statuses:
                statuses.append(fin.value.status)
            bp.wait_for_load_state(timeout=130000)
            B.capture(bp, "finalize_refused", {"refused": True, "reason": "a punch arrived after the draft was computed"})
        finally:
            br.close()
        ctx.check(409 in statuses, f"finalizing a stale draft answered {statuses}")
        ctx.eq(run_markers(H.run_page(rid), rid).get("data-run-status"), "draft", "status after a refused finalize")
        H.approve_all([eid], [date(2026, 9, 20)])
    with ctx.step("two simultaneous finalize requests for the recomputed draft: exactly one succeeds"):
        r, rid, _ = H.create_run(P0920)
        page = H.run_page(rid)
        before_mail = {eid: H.mail_ids(H.model.emps[eid].email) for eid in ctx.shared["w14"]}
        ctx.shared["w14_before_mail"] = before_mail
        clients = [H.admin, H.admin2]
        pages = [c.get(f"/payroll/runs/{rid}", follow=True) for c in clients]
        outs = simultaneously([lambda c=c, p=p: submit_form(c, "", "finalize-run", {}, page=p) for c, p in zip(clients, pages)])
        capture_paystub(ctx.shared["w14"][0], before_mail[ctx.shared["w14"][0]], rid)  # judge evidence, not a check
        codes = sorted(v.status for kind, v in outs if kind == "ok")
        # R7.4 gives 409 only for a stale draft, so the loser may be refused any way (an error answer, or a redirect
        # that finalizes nothing). Exactly one finalization is then proved by its effects: the run is finalized,
        # its lines are the model's, and each employee gets one pay statement (next steps; X6 on a second one).
        ctx.check(len(codes) == 2 and all(c < 500 for c in codes) and any(c < 400 for c in codes),
                  f"finalize statuses {codes}")
        ctx.eq(run_markers(H.run_page(rid), rid).get("data-run-status"), "finalized", "run status")
        ctx.shared["w14_final"] = (rid, H.export(rid)[0].body)  # W18 checks this run whatever the money checks say
        lines = H.model.finalize(P0920, list(H.model.emps))
        resp, rows = H.export(rid)
        problems = H.compare_lines(P0920, {k: v for k, v in rows.items()}, [])
        want_ids = sorted(lines)
        ctx.eq(sorted(rows), want_ids, "employees with lines in the finalized run")
        for eid in want_ids:
            ctx.eq(rows[eid]["gross_cents"], str(lines[eid]["gross"]), f"gross for {eid}")
    with ctx.step("each employee with a line gets exactly one pay statement within 30 s"):
        rid, _ = ctx.shared["w14_final"]
        for eid in ctx.shared["w14"]:
            emp = H.model.emps[eid]
            new = H.mail.wait_for(emp.email, lambda m: m["id"] not in ctx.shared["w14_before_mail"][eid]
                                  and "pay statement" in m["subject"].lower(), 1, 35)
            ctx.eq(len(new), 1, f"pay statements for {eid}")
            body = new[0]["text"]
            ctx.check("2026-09-20" in body and "2026-10-03" in body, "the statement does not name the period's dates")
            gross = int(H.export(rid)[1][eid]["gross_cents"])
            ctx.check(f"${gross // 100:,}.{gross % 100:02d}" in body, f"the statement does not show ${gross / 100:,.2f}")
    with ctx.step("a date that is not the first day of a pay period is refused"):
        # 2026-10-11 is a workweek's Sunday inside the period of 2026-10-04, which is not finalized yet, so an app
        # that snapped the date to its period would create or recompute that draft rather than answer 409.
        day = "2026-10-11"
        bad, bad_rid, _ = H.create_run(date.fromisoformat(day))
        # R7.1 "is refused" (no status code): an error answer; a page (200) that is not a run page; or a redirect to
        # a page that is not a run page. Neither may show a run for that date.
        ok = bad.status >= 400
        if not ok and bad.status == 200:
            ok = bad.dom.attr_one("data-run-id") is None and bad.dom.attr_one("data-period-start", day) is None
        if not ok and bad.status in REDIRECTS and bad.location:
            dest = landing(H.admin, bad)
            ok = (dest.status < 400 and not (bad_rid and dest.dom.attr_one("data-run-id", bad_rid))
                  and dest.dom.attr_one("data-period-start", day) is None)
        ctx.check(ok, f"a non-period date answered {bad.status} {bad.location or ''}")
    with ctx.step("a finalized period cannot get a new draft, and finalizing again changes nothing"):
        rid, body0 = ctx.shared["w14_final"]
        r, rid2, _ = H.create_run(P0920)
        ctx.check(rid2 is None and r.status == 409, f"a new draft for a finalized period answered {r.status}")
        page = H.run_page(rid)
        ver = run_markers(page, rid).get("data-version") or "1"
        tok = [(i.attrs.get("name"), i.attrs.get("value", "")) for i in H.admin.get("/payroll", follow=True).dom.find_all(
            lambda n: n.tag == "input" and "data-csrf" in n.attrs)]
        r2 = H.admin.post_form(f"/payroll/runs/{rid}/finalize", tok + [("version", ver)])
        # refused any way; "changes nothing" is checked below (same export, no second statement)
        ctx.check(refused(H.admin, r2), f"finalizing a finalized run answered {r2.status}")
        time.sleep(5)
        resp, _ = H.export(rid)
        if resp.body != body0:
            ctx.breach("X6", "a finalized run's export changed")
            raise StepFailed("the finalized run changed")
        for eid in ctx.shared["w14"]:
            n = [m for m in H.mail.to(H.model.emps[eid].email) if m["id"] not in ctx.shared["w14_before_mail"][eid]
                 and "pay statement" in m["subject"].lower()]
            if len(n) > 1:
                ctx.breach("X6", f"employee {eid} received {len(n)} pay statements for one run")
            ctx.eq(len(n), 1, f"pay statements for {eid} after a second finalize")
    with ctx.step("a late punch for a paid week is re-approved (it will be paid as an adjustment)"):
        eid = ctx.shared["w14"][1]
        H.import_rows(g.shift(H.model.emps[eid], local(2026, 9, 26, 14, 0), 180, "COOK", []))
        H.approve_all([eid], [date(2026, 9, 20)])


# ======================================================================================== W15
def capture_run_page(rid, checkpoint: str, truth: dict, focus: list[str]) -> None:
    """Judge evidence of a run page in a browser carrying the admin's session. Best effort: never fails a workflow."""
    try:
        br, bp = admin_browser()
        try:
            bp.goto(H.base + f"/payroll/runs/{rid}")
            bp.wait_for_load_state()
            B.capture(bp, checkpoint, truth, focus=focus)
        finally:
            br.close()
    except Exception as e:  # noqa: BLE001 - evidence only
        print(f"[wf] {checkpoint} evidence not captured: {type(e).__name__}: {e}"[:300], flush=True)


@workflow("W15", "Adjustments: late punches, late approvals and rate changes reach the next run", 5, critical=True)
def w15(ctx):
    rnd = random.Random(1515)
    g = Gen(rnd, f"w15-{H.nonce}")
    WA, WB = P1004, P1004 + timedelta(days=7)
    with ctx.step("staff with work in the period starting 2026-10-04; finalize it"):
        H.ensure_admins()
        eids, rows = [], []
        for i in range(6):
            eid = H.create_employee([LA, PHX, CHI][i % 3], cents=rnd.randrange(2000, 2800, 5))
            emp = H.model.emps[eid]
            for d in (1, 2, 3, 4, 8, 9, 10):
                day = P1004 + timedelta(days=d)
                rows += g.shift(emp, local(day.year, day.month, day.day, 8, 0), rnd.choice([480, 540, 600]), "COOK", [(240, 36)])
            eids.append(eid)
        H.import_rows(rows)
        H.approve_all(eids[:5], weeks_of(P1004))
        H.approve_all(eids[5:], [WA])  # employee 6's second week stays unapproved (held)
        r, rid, _ = H.create_run(P1004)
        ctx.check(rid is not None, f"draft answered {r.status}")
        br, bp = admin_browser()
        try:
            bp.goto(H.base + f"/payroll/runs/{rid}")
            bp.wait_for_load_state()
            B.capture(bp, "run_draft", {"status": "draft", "held_weeks": [[eids[5], WB.isoformat()]]},
                      focus=[f"[data-held-employee-id='{eids[5]}']"])
        finally:
            br.close()
        page = H.run_page(rid)
        r = submit_form(H.admin, "", "finalize-run", {}, page=page)
        ctx.check(r.status < 400, f"finalize answered {r.status}")
        H.model.finalize(P1004, list(H.model.emps))
        ctx.shared["w15"] = eids
    with ctx.step("after the run: a late punch, a raise, a cut, a late approval, and an unapproved change"):
        a, b, c, d, e, f = ctx.shared["w15"]
        H.import_rows(g.shift(H.model.emps[a], local(2026, 10, 10, 9, 0), 300, "COOK", []))
        H.approve_all([a], [WA])
        r = H.add_rate(b, "COOK", H.model.emps[b].rate_for("COOK", WA) + 150, date(2026, 10, 7))
        ctx.check(r.status < 400, "raise refused")
        H.approve_all([b], [WA, WB])
        r = H.add_rate(c, "COOK", H.model.emps[c].rate_for("COOK", WA) - 100, date(2026, 10, 1))
        ctx.check(r.status < 400, "cut refused")
        H.approve_all([c], [WA, WB])
        H.approve_all([f], [WB])
        H.import_rows(g.shift(H.model.emps[e], local(2026, 10, 15, 9, 0), 240, "COOK", []))
    with ctx.step("the next run pays each difference as an adjustment, and nothing for the unapproved change"):
        t_eids, t_rows = add_tie_staff(rnd, g, 6, weeks_of(P1018))
        H.import_rows(t_rows)
        H.approve_all(t_eids, weeks_of(P1018))
        ctx.shared["w15"] = ctx.shared["w15"] + t_eids
        r, rid, _ = H.create_run(P1018)
        ctx.check(rid is not None, f"draft answered {r.status}")
        adjusted = [e for e in ctx.shared["w15"] if (H.model.line(e, P1018) or {}).get("adj")]
        capture_run_page(rid, "run_adjustments", {"status": "draft", "lines_with_adjustments": adjusted},
                         [f"[data-line-employee-id='{e}']" for e in adjusted[:2]])  # J05 evidence, before the checks
        resp, rows = H.export(rid)
        problems = H.compare_lines(P1018, rows, ctx.shared["w15"])
        ctx.check(not problems, " | ".join(problems[:6]))
        want = {eid: (H.model.line(eid, P1018) or {}).get("adj", 0) for eid in ctx.shared["w15"]}
        ctx.check(want[ctx.shared["w15"][0]] > 0 and want[ctx.shared["w15"][2]] < 0, "verifier fixture: adjustments not exercised")
        ctx.shared["w15_run"] = rid


# ======================================================================================== W16
@workflow("W16", "CSV export: exact format, quoting, spreadsheet safety; profile edits change only the name", 3)
def w16(ctx):
    rnd = random.Random(1616)
    g = Gen(rnd, f"w16-{H.nonce}")
    names = ['=HYPERLINK("http://example.invalid","x")', '+SUM(1,2)', "-2+3", "@cmd", 'Lee, "Sunny" Park', "Plain Name"]
    with ctx.step("staff whose names need quoting or neutralizing have approved work in 2026-11-29"):
        H.ensure_admins()
        eids, rows = [], []
        for nm in names[:6]:
            eid = H.create_employee(PHX, name=nm)
            emp = H.model.emps[eid]
            rows += g.shift(emp, local(2026, 12, 1, 9, 0), 480, "COOK", [(240, 36)])
            eids.append(eid)
        H.import_rows(rows)
        H.approve_all(eids, weeks_of(P1129))
        ctx.shared["w16"] = eids
    with ctx.step("the export has the exact header, CRLF rows ordered by employee_id, and correct quoting"):
        r, rid, _ = H.create_run(P1129)
        resp, rows = H.export(rid)
        ctx.eq(resp.status, 200, "export status")
        raw = resp.body.decode("utf-8-sig")
        ctx.check(raw.startswith(",".join(CSV_HEADER) + "\r\n"), "header row differs or does not end in CRLF")
        ctx.check("\n" not in raw.replace("\r\n", "") or '"' in raw, "a row does not end in CRLF")
        ids = [int(x) for x in rows]
        ctx.eq(ids, sorted(ids), "row order")
        ctx.shared["w16_rows"] = (rows, raw)
    with ctx.step("names starting with = + - @ or a tab are written with a leading quote; numbers stay plain"):
        rows, raw = ctx.shared["w16_rows"]
        for eid, nm in zip(ctx.shared["w16"], names[:6]):
            row = rows.get(eid)
            ctx.check(row is not None, f"no row for {eid}")
            cell = row["full_name"]
            if nm[0] in "=+-@\t" and cell == nm:
                ctx.breach("X4", f"export wrote {nm!r} as a live spreadsheet formula")
            want = ("'" + nm) if nm[0] in "=+-@\t" else nm
            ctx.eq(cell, want, f"full_name cell for {eid}")
            ctx.check(re.fullmatch(r"-?\d+", row["gross_cents"]) and re.fullmatch(r"\d+\.\d", row["reg_hours"]), "numeric columns")
        problems = H.compare_lines(P1129, rows, ctx.shared["w16"])
        ctx.check(not problems, " | ".join(problems[:4]))
    with ctx.step("an employee changes their display name; extra fields change nothing; a token-less change is refused"):
        eid = ctx.shared["w16"][0]
        c = H.account(H.model.emps[eid].email)
        before = H.q("SELECT email, location_id, manager_id, role, active FROM employees WHERE id=%s", (eid,))[0]
        rates0 = H.q("SELECT job_code, rate_cents, effective_from FROM job_rates WHERE employee_id=%s ORDER BY id", (eid,))
        r = submit_form(c, "/profile", "save-profile", {"full_name": "Renamed Person"}, follow=True,
                        override={"role": "payroll_admin", "location_id": "3", "manager_id": "", "active": "false",
                                  "rate_cents": "99999", "email": f"hijack.{H.nonce}@verify.example", "id": "1"})
        after = H.q("SELECT full_name, email, location_id, manager_id, role, active FROM employees WHERE id=%s", (eid,))[0]
        rates1 = H.q("SELECT job_code, rate_cents, effective_from FROM job_rates WHERE employee_id=%s ORDER BY id", (eid,))
        changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
        if changed or rates1 != rates0:
            ctx.breach("X5", f"profile form changed {changed or 'rates'}")
        ctx.check(not changed and rates1 == rates0, f"extra profile fields changed {changed}")
        ctx.eq(after["full_name"], "Renamed Person", "display name after the profile form")
        admin_name = H.q("SELECT full_name FROM employees WHERE id = 1")[0]["full_name"]
        r = submit_form(c, "/profile", "save-profile", {"full_name": "Token Less"}, drop_csrf=True)
        now = H.q("SELECT full_name FROM employees WHERE id=%s", (eid,))[0]["full_name"]
        if now == "Token Less":
            ctx.breach("X2", "a profile change without the session token took effect")
        ctx.check(r.status == 403 and now == "Renamed Person", f"token-less profile change answered {r.status}")
        ctx.eq(H.q("SELECT full_name FROM employees WHERE id = 1")[0]["full_name"], admin_name, "another employee's name")
        H.model.emps[eid].name = "Renamed Person"
    with ctx.step("names with control characters are refused; nothing typed adds an email header or recipient"):
        eid = ctx.shared["w16"][0]
        emp = H.model.emps[eid]
        c = H.account(emp.email)
        evil = f"Renamed Person\r\nBcc: x{H.nonce}@evil.example\r\nSubject: injected"
        submit_form(c, "/profile", "save-profile", {"full_name": evil}, follow=True)
        now = H.q("SELECT full_name FROM employees WHERE id=%s", (eid,))[0]["full_name"]
        stored_ctrl = any(ord(ch) < 0x20 for ch in now)
        before = H.mail_ids(emp.email)
        H.request_reset(emp.email)
        time.sleep(4)
        injected = [m for m in H.mail.summaries() if any("evil.example" in t for t in m["to"]) or "injected" in m["subject"]]
        if injected:
            ctx.breach("X12", f"a typed name added an email header or recipient: {[m['subject'] for m in injected][:2]}")
        ctx.check(not injected, "a typed name injected an email header or recipient")
        ctx.check(not stored_ctrl, f"a display name with control characters was stored: {now!r}")
        r = submit_form(H.admin, "/admin/employees/new", "create-employee", {
            "email": f"ctrl.{H.nonce}@verify.example", "full_name": "Bell\x07Name", "location_id": str(PHX), "manager_id": "",
            "role": "employee", "job_code": "COOK", "rate_cents": "2000", "effective_from": "2026-01-01"}, follow=True)
        ctx.check(not H.q("SELECT 1 FROM employees WHERE lower(btrim(email)) = %s", (f"ctrl.{H.nonce}@verify.example",)),
                  "an employee with a control character in the name was created")
    with ctx.step("emails are addressed with the employee's display name and address in To"):
        eid = ctx.shared["w16"][1]
        emp = H.model.emps[eid]
        before = H.mail_ids(emp.email)
        H.request_reset(emp.email)
        new = H.mail.wait_for(emp.email, lambda m: m["id"] not in before, 1, 35)
        ctx.check(new, "no email arrived")
        full = H.mail._get("/api/v1/message/" + str(new[-1]["id"]))
        names = [str(t.get("Name", "")) for t in (full.get("To") or [])]
        ctx.check(emp.name in names, f"To display names {names!r} do not include {emp.name!r}")


# ======================================================================================== W17
def w17_evidence(xs: list[int], mc: Client, exc: int | None) -> None:
    """Judge evidence for R12.1, R12.2 and R2.5, taken right after W17's setup in a browser of its own, so that it
    does not depend on the strict layout, escaping and approval steps. Best effort: never fails the workflow."""
    br = None
    try:
        br = B.Browser()
        if exc:
            acx = br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in H.admin.cookies.items()])
            ap = br.page(acx)
            ap.goto(H.base + f"/employees/{exc}/timesheet?week=2026-10-04")
            ap.wait_for_load_state()
            B.capture(ap, "timesheet_exception", {"status": "exception", "reason": "an out punch with no open shift on 2026-10-06"})
        mcx = br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in mc.cookies.items()])
        mp = br.page(mcx)
        mp.goto(H.base + f"/employees/{xs[3]}/timesheet?week=2026-11-01")
        mp.wait_for_load_state()
        B.capture(mp, "approve_before", {"employee_id": xs[3], "week": "2026-11-01", "status": "pending"})
        lock_emp = legacy_pick(lambda e: e.active and e.role == "employee" and e.location_id == LA, 30)[-1]
        H.set_password(lock_emp.email.strip().lower())
        lp = br.page(br.context())
        for i in range(6):
            lp.goto(H.base + "/signin")
            lp.fill("input[name=email]", lock_emp.email.strip().lower())
            lp.fill("input[name=password]", "Wrong-Password-1" if i < 5 else PASSWORD)
            lp.click("[data-action=signin]")
            lp.wait_for_load_state()
        B.capture(lp, "lockout", {"locked": True})
    except Exception as e:  # noqa: BLE001 - evidence only
        print(f"[wf] W17 evidence incomplete: {type(e).__name__}: {e}"[:300], flush=True)
    finally:
        if br:
            br.close()


@workflow("W17", "In a real browser: layout at 1280 and 390 px, escaping everywhere, clicking through", 3, timeout=1200)
def w17(ctx):
    rnd = random.Random(1717)
    g = Gen(rnd, f"w17-{H.nonce}")
    br = None
    try:
        with ctx.step("staff, labels and job codes carrying script payloads exist; a manager has a pending week"):
            H.ensure_admins()
            mgr = H.create_employee(CHI, role="manager", manager_id=1, name="Morgan Ellis")
            xs = [H.create_employee(CHI, name=B.payload(f"name{i}", i), manager_id=mgr, job=f"J{i}" if i else "COOK") for i in range(4)]
            H.add_rate(xs[0], B.payload("job", 0)[:32], 2000, date(2026, 1, 1))
            H.create_device(CHI, B.payload("label", 1))
            rows = []
            for eid in xs:
                emp = H.model.emps[eid]
                rows += g.shift(emp, local(2026, 11, 3, 9, 0), 480, sorted(emp.rates)[0], [(240, 36)])
            H.import_rows(rows)
            ctx.shared["w17"] = (mgr, xs, H.account(H.model.emps[mgr].email))
        w17_evidence(ctx.shared["w17"][1], ctx.shared["w17"][2], ctx.shared.get("w05", (None, None, None, None))[3])
        with ctx.step("signing in through the browser form reaches a signed-in page"):
            br = B.Browser()
            cx = br.context()
            page = br.page(cx)
            page.goto(H.base + "/signin")
            page.fill("input[name=email]", "dana.whitfield@shiftbook.example")
            page.fill("input[name=password]", PASSWORD)
            page.click("[data-action=signin]")
            page.wait_for_load_state()
            ctx.check(page.locator("[data-action=signout]").count() >= 1, "no sign-out button after browser sign-in")
            ctx.shared["w17_page"] = (cx, page)
        mgr, xs, mc = ctx.shared["w17"]
        cx, page = ctx.shared["w17_page"]
        urls = [f"/employees/{xs[1]}/timesheet?week=2026-11-01", f"/admin/employees/{xs[0]}", "/admin/devices", "/approvals",
                "/payroll", f"/employees/{xs[2]}/timesheet?week=2026-11-01"]
        with ctx.step("every page stays within 1280 and 390 CSS pixels without horizontal scrolling"):
            for u in urls:
                for w in (1280, 390):
                    page.set_viewport_size({"width": w, "height": 900})
                    page.goto(H.base + u)
                    page.wait_for_load_state()
                    ov = B.horizontal_overflow(page)
                    ctx.check(ov <= 2, f"{u} overflows by {ov}px at {w}px")
        with ctx.step("names, labels and job codes are shown as text on every page (no script runs)"):
            q = urllib.parse.quote
            reflected = [f"/employees/{xs[1]}/timesheet?week=" + q(B.payload("week", 0)),
                         "/payroll/runs/" + q(B.payload("run", 1)), "/" + q(B.payload("path", 0)),
                         "/approvals?x=" + q(B.payload("query", 2)), "/signin?next=" + q(B.payload("next", 3))]
            for u in urls + ["/timesheet"] + reflected:
                page.goto(H.base + u)
                page.wait_for_load_state()
                page.wait_for_timeout(300)
            rid = None
            r, rid, _ = H.create_run(P1101)
            if rid:
                page.goto(H.base + f"/payroll/runs/{rid}")
                page.wait_for_load_state()
            mcx = br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in mc.cookies.items()])
            mp = br.page(mcx)
            mp.goto(H.base + "/approvals")
            mp.wait_for_load_state()
            mp.goto(H.base + f"/employees/{xs[0]}/timesheet?week=2026-11-01")
            mp.wait_for_load_state()
            mp.wait_for_timeout(300)
            if br.xss_hits:
                ctx.breach("X3", f"typed text ran as script: {sorted(set(br.xss_hits))[:5]}")
            ctx.check(not br.xss_hits, f"script ran from typed text: {sorted(set(br.xss_hits))[:5]}")
            ctx.shared["w17_mgr_page"] = mp
        with ctx.step("the manager approves a week by clicking the button; the page reflects it"):
            mp = ctx.shared["w17_mgr_page"]
            mp.goto(H.base + f"/employees/{xs[3]}/timesheet?week=2026-11-01")
            mp.wait_for_load_state()
            with mp.expect_response(lambda resp: "/approve" in resp.url and resp.request.method == "POST"):
                mp.click("[data-action=approve-week]")  # a confirmation dialog, if the page shows one, is accepted
            mp.wait_for_load_state()
            mp.goto(H.base + f"/employees/{xs[3]}/timesheet?week=2026-11-01")
            st = mp.locator("[data-week-start='2026-11-01']").first.get_attribute("data-status")
            ctx.eq(st, "approved", "status after clicking approve")
            H.model.approve(xs[3], date(2026, 11, 1))
    finally:
        B.mark_unreached(["approve_before", "timesheet_exception", "run_draft", "run_adjustments", "finalize_before",
                          "finalize_refused", "lockout", "paystub_email"])
        if br:
            br.close()


# ======================================================================================== W18
PERIOD_STARTS = [P0920 + timedelta(days=14 * k) for k in range(27)]  # R5.1 pay periods, a year from the first


def statement_duplicates() -> dict:
    """R7.5: each employee with a line gets exactly one pay statement per finalized run, never re-sent. An employee
    paid in two runs correctly gets two, so statements are keyed by (recipient, run): the run is the pay period
    whose first and last dates the statement names (R7.5 requires both in the body); a statement naming none is
    keyed by its own subject and text. Only recipients with two or more statements need their bodies read."""
    per_rcpt: dict = {}
    for m in H.mail.summaries():
        if "pay statement" in m["subject"].lower():
            for t in m["to"]:
                per_rcpt.setdefault(t, []).append(m["id"])
    seen: dict = {}
    for t, ids in per_rcpt.items():
        if len(ids) < 2:
            continue
        for mid in ids:
            full = H.mail._full(mid)
            dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", f"{full['subject']}\n{full['text']}"))
            runs = frozenset(p for p in PERIOD_STARTS if p.isoformat() in dates and (p + timedelta(days=13)).isoformat() in dates)
            key = (t, runs) if runs else (t, full["subject"], full["text"])
            seen[key] = seen.get(key, 0) + 1
    return {k: v for k, v in seen.items() if v > 1}


@workflow("W18", "Deployment: README, restart persistence, nothing re-sent, no secrets (passwords, keys, reset tokens, sessions) at rest, records kept", 2)
def w18(ctx):
    with ctx.step("README.md and an executable start.sh are in the workspace"):
        ws = Path(H.workspace)
        ctx.check((ws / "README.md").is_file() and (ws / "README.md").stat().st_size > 40, "README.md missing")
        ctx.check(os.access(ws / "start.sh", os.X_OK), "start.sh is not executable")
    with ctx.step("after a restart: passwords, runs and punches are kept and no email is sent again"):
        H.ensure_admins()
        final = ctx.shared.get("w14_final")
        ctx.check(final, "no finalized run to check (W14 never reached a finalized run)")
        rid, body0 = final
        ctx.check(H.restart_app(), "the application did not answer /healthz after a restart")
        time.sleep(12)
        c, _ = H.signin("dana.whitfield@shiftbook.example")
        ctx.check(H.signed_in(c), "the admin password did not survive the restart")
        H.admin = c
        resp, _ = H.export(rid, c)
        if resp.body != body0:
            ctx.breach("X6", "a finalized run's export changed across a restart")
        ctx.check(resp.body == body0, "the finalized run changed across the restart")
        ctx.eq(run_markers(H.run_page(rid, c), rid).get("data-run-status"), "finalized", "run status after restart")
        eid = ctx.shared.get("w04_eid")
        if eid:
            day = H.timesheet(c, eid, date(2026, 10, 4)).dom.attr_one("data-day", "2026-10-06")
            ctx.check(day is not None and day.attrs.get("data-reg-hours") == H.model.day_breakdown(eid, date(2026, 10, 4))[date(2026, 10, 6)][0],
                      "punches did not survive the restart")
        dups = statement_duplicates()
        if dups:
            ctx.breach("X6", f"{len(dups)} pay statements were sent more than once")
        ctx.eq(len(dups), 0, "pay statements sent more than once (after a restart)")
    with ctx.step("no password or time-clock key is stored in plain text"):
        secrets_to_find = [PASSWORD] + H.key_needles(H.devices.values())
        secrets_to_find += [t for t in H.reset_tokens if len(t) >= 16][-5:] + H.session_values(5)
        hits = []
        for t in H.q("SELECT table_schema, table_name, column_name FROM information_schema.columns WHERE table_schema NOT IN "
                     "('pg_catalog','information_schema') AND data_type IN ('text','character varying','jsonb','json','bytea','character')"):
            col = f'"{t["table_schema"]}"."{t["table_name"]}"'
            for s in secrets_to_find:
                rows = H.q(f'SELECT 1 FROM {col} WHERE position(%s in "{t["column_name"]}"::text) > 0 LIMIT 1', (s,))
                if rows:
                    hits.append(f"{t['table_name']}.{t['column_name']}")
        if hits:
            ctx.breach("X8", f"plain-text secret in {sorted(set(hits))}")
        ctx.check(not hits, f"plain-text secret stored in {sorted(set(hits))}")
    with ctx.step("the old system's records are all still there, unchanged"):
        b = H.baseline
        changed = []
        cur = {r["id"]: r for r in H.q("SELECT id, email, full_name, location_id, manager_id, role, active, hired_on::text AS hired_on FROM employees")}
        renamed = {eid for eid, e in H.model.emps.items() if not e.legacy}
        for r in b["employees"]:
            now = cur.get(r["id"])
            keys = ["email", "location_id", "manager_id", "role", "active", "hired_on"] + ([] if r["id"] in renamed else ["full_name"])
            if now is None or any(str(now[k]) != str(r[k]) for k in keys):
                changed.append(f"employee {r['id']}")
        rates = {r["id"]: r for r in H.q("SELECT id, employee_id, job_code, rate_cents, effective_from::text AS effective_from FROM job_rates")}
        for r in b["job_rates"]:
            now = rates.get(r["id"])
            if now is None or any(str(now[k]) != str(r[k]) for k in ("employee_id", "job_code", "rate_cents", "effective_from")):
                changed.append(f"job_rate {r['id']}")
        n_p = H.q("SELECT count(*) AS n, coalesce(sum(hashtext(id::text||employee_id::text||coalesce(job_code,'')||kind||local_time::text||fold::text)),0) AS h FROM legacy_punches")[0]
        n_a = H.q("SELECT count(*) AS n FROM legacy_approvals")[0]["n"]
        if n_p["n"] != len(b["legacy_punches"]) or int(n_p["h"]) != int(b["legacy_punch_hash"]) or n_a != len(b["legacy_approvals"]):
            changed.append("legacy_punches/legacy_approvals")
        if changed:
            ctx.breach("X10", f"pre-existing records changed: {changed[:8]}")
        ctx.check(not changed, f"pre-existing records changed: {changed[:8]}")



# ======================================================================================== W19
@workflow("W19", "Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start", 2, timeout=400)
def w19(ctx):
    import subprocess
    stop, start = os.environ.get("FK_DB_STOP", ""), os.environ.get("FK_DB_START", "")

    def probe() -> int | None:
        try:
            c = Client(H.base, timeout=5)
            return c.get("/healthz").status
        except Exception:  # no answer within 5 s, or the connection failed
            return None

    with ctx.step("/healthz answers 200 while the database is up"):
        ctx.check(stop and start, "verifier fixture: no database hooks")
        ctx.eq(probe(), 200, "/healthz status")
    try:
        with ctx.step("within 10 s of the database stopping, /healthz answers a non-2xx status within 5 s"):
            if subprocess.run([stop], check=False, capture_output=True).returncode != 0:
                raise StepFailed("verifier fixture: the database did not stop")
            t0, seen = time.monotonic(), None
            while time.monotonic() - t0 < 10:
                st = probe()
                if st is not None and not (200 <= st < 300):
                    seen = round(time.monotonic() - t0, 1)
                    break
                time.sleep(0.5)
            ctx.observe("non2xx_after_s", seen)
            ctx.check(seen is not None, "no non-2xx answer within 10 s of the database stopping")
    finally:
        restarted = subprocess.run([start], check=False, capture_output=True).returncode == 0
    with ctx.step("within 60 s of the database starting again, /healthz answers 200 without a restart"):
        ctx.check(restarted, "verifier fixture: the database did not start again")
        t0, ok = time.monotonic(), False
        while time.monotonic() - t0 < 60:
            if probe() == 200:
                ok = True
                break
            time.sleep(1)
        ctx.observe("back_after_s", round(time.monotonic() - t0, 1))
        ctx.check(ok, "/healthz did not answer 200 within 60 s of the database starting")



# ======================================================================================== W23
@workflow("W23", "Cross-cutting safety: no secrets in the app's files, reset ends sessions, Host-proof links, headers, no stack traces", 3)
def w23(ctx):
    from kitlib import xcut
    from kitlib.web import form_fields
    emp = legacy_pick(lambda e: e.active and e.role == "employee" and e.location_id == CHI, 12)[-1]
    addr = emp.email.strip().lower()
    with ctx.step("setting a new password through a reset link signs out every existing session"):
        H.ensure_admins()
        c_old = H.account(addr)
        c_other, _ = H.signin(addr)
        ctx.check(H.signed_in(c_old) and H.signed_in(c_other), "could not sign in twice")
        ctx.check(H.set_password(addr, "Rotated-Pass-77"), "the reset flow failed")
        survivors = [n for n, c in (("first", c_old), ("second", c_other)) if H.signed_in(c)]
        if survivors:
            ctx.breach("X13", f"{len(survivors)} session(s) still signed in after the password was reset")
        ctx.check(not survivors, f"sessions survived the password reset: {survivors}")
        c_new, _ = H.signin(addr, "Rotated-Pass-77")
        ctx.check(H.signed_in(c_new), "the new password does not sign in")
    with ctx.step("a reset link always starts with PUBLIC_BASE_URL, whatever Host header the request carried"):
        before = H.mail_ids(addr)
        c = H.client()
        page = c.get("/password-reset", follow=True)  # the form and its token; only the POST carries the forged Host
        try:
            form = find_form(page.dom, "request-reset")
            fields = [(k, v) for k, v in form_fields(form) if k != "email"] + [("email", addr)]
        except Exception:
            fields = [("email", addr)]
        forged = c.post_form("/password-reset", fields, headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"})
        msgs = H.mail.wait_for(addr, lambda m: m["id"] not in before, 1, 35)
        # Refusing a request whose Host is not this site (4xx, no email) also keeps every link on PUBLIC_BASE_URL.
        ctx.check(msgs or 400 <= forged.status < 500, f"no reset email arrived (the forged-Host request answered {forged.status})")
        links_found = [l for m in msgs for l in __import__("kitlib.mail", fromlist=["links"]).links(m["text"] or m["html"])
                       if "/password-reset/" in l]
        bad = [l for l in links_found if not l.startswith(H.base + "/")]
        if bad:
            ctx.breach("X14", f"reset link built from the request's Host header: {bad[0][:80]}")
        ctx.check(not msgs or (links_found and not bad), f"reset links {links_found[:2]}")
        for l in links_found:
            H.reset_tokens.append(l.rstrip("/").rsplit("/", 1)[-1])
    with ctx.step("signed-in pages carry Cache-Control: no-store and an anti-framing header"):
        rid = ctx.shared.get(f"w12_{P1018}")
        pages = ["/timesheet", "/approvals", "/payroll", "/profile", "/admin/devices",
                 f"/employees/{emp.eid}/timesheet?week=2026-11-01"]
        if rid:
            pages += [f"/payroll/runs/{rid}", f"/payroll/runs/{rid}/export.csv"]
        problems = []
        for path in pages:
            r = H.admin.get(path, follow=True)  # e.g. /timesheet may redirect to the current week
            ctx.check(r.status == 200, f"{path} answered {r.status}")
            found = xcut.header_problems(r)
            if "text/html" not in (r.header("Content-Type") or "").lower():  # framing applies to pages, not the CSV
                found = [p for p in found if "anti-framing" not in p]
            problems += found
        problems += xcut.header_problems(H.client().get("/signin"), need_no_store=False)
        ctx.check(not problems, "; ".join(problems[:4]))
    with ctx.step("malformed requests get a plain error page without stack traces, SQL, or file paths"):
        tok = csrf_from(H.admin.get("/payroll", follow=True))
        probes = [
            H.admin.get(f"/employees/{emp.eid}/timesheet?week=2026-13-45"),
            H.admin.get(f"/employees/{emp.eid}/timesheet?week=%27%20OR%201%3D1--"),
            H.admin.get("/payroll/runs/99999999999999999999999"),
            H.admin.get("/employees/99999999999999999999/timesheet"),
            H.admin.post_form("/payroll/runs", tok + [("period_start", "2026-02-30")]),
            H.admin.post_form("/payroll/runs", tok + [("period_start", "' OR 1=1--")]),
            H.admin.post_form("/admin/employees/new", tok + [("email", "x@y.z"), ("full_name", "X"), ("location_id", "abc"),
                                                            ("role", "employee"), ("job_code", "COOK"), ("rate_cents", "1e99"),
                                                            ("effective_from", "never")]),
            H.api(H.devices.get(LA, "sbk_0_x"), {"punches": [{"punch_id": {"$gt": ""}, "employee_id": "1;DROP", "kind": None, "at": []}]}),
            H.client().get("/password-reset/" + "%00" * 3),
        ]
        problems = [p for r in probes for p in xcut.error_leaks(r)]
        ctx.check(not problems, "; ".join(problems[:4]))
        ctx.check(all(r.status < 500 or r.status in (500, 503) for r in probes), "a probe got no answer")
    with ctx.step("no password, reset token, session identifier, or time-clock key is stored in the app's files"):
        secrets_to_find = [PASSWORD, "Rotated-Pass-77"] + H.key_needles(H.devices.values())
        secrets_to_find += [t for t in H.reset_tokens if len(t) >= 16][-8:] + H.session_values(8)
        # R2.7: never *stored* in plain text: the database (W18) and the files the application writes.
        # Its stdout/stderr is captured by the platform, not stored by the application, so it is not scanned.
        hits = xcut.scan_files_for_secrets([H.workspace], secrets_to_find)
        if hits:
            ctx.breach("X8", f"secrets written by the application: {hits[:3]}")
        ctx.check(not hits, f"secrets in the app's files: {hits[:3]}")


def main() -> None:
    global H
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    H = Harness()
    shared: dict = {}
    run_all(a.out, shared, set(x for x in a.only.split(",") if x) or None)


if __name__ == "__main__":
    main_guard(main)
