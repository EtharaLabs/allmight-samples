#!/usr/bin/env python3
"""Harborview Physio workflows. Each workflow is strict: it passes only when every substep passes.

Run: python3 workflows.py --out /logs/verifier/results.json

Isolation: every rule workflow creates its own clinicians, appointment types, and rooms of a room
type nobody else uses, so a defect in one rule cluster fails that workflow only. Legacy clinicians and
legacy rooms are never booked by the verifier, so W07 (Seattle) and W22 (Honolulu) see exactly the old
system's data. W26 (restart) and W27 (database stop) run last because they disturb the running
application. Time-relative starts come from slot_near(), which falls back to other windows when a clinic
closure covers the one asked for, and every check that depends on how far away a start is reads the start
actually chosen; booking days come from quiet_block() and open_day(), which skip closures at both clinics.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (REDIRECTS, ADMIN_EMAIL, ALL_DAY, APPT_ACTIONS, CARD_DECLINE, CARD_OK, DESK_EMAIL, EFFECT_WAIT, HNL, PASSWORD,  # noqa: E402
                     REFUND_WAIT, SEA, Harness, actions, rnd_for)
from kitlib import browser as B  # noqa: E402
from kitlib.conc import simultaneously  # noqa: E402
from kitlib.web import Client, FormNotFound, csrf_from, find_form, form_fields, submit_form, visible_text  # noqa: E402
from kitlib.wf import InfraError, StepFailed, main_guard, run_all, workflow  # noqa: E402
from paylib import event_body  # noqa: E402
from reference import engine as E  # noqa: E402
from reference.model import norm  # noqa: E402

H: Harness = None  # type: ignore[assignment]


def norm_text(t: str, *remove: str) -> str:
    for r in remove:
        if r:
            t = t.replace(r, "").replace(r.lower(), "").replace(r.strip(), "").replace(r.strip().lower(), "")
    return re.sub(r"\s+", " ", t).strip().lower()


def problems_of(checks) -> list[str]:
    return [p for p in checks if p]


def legacy_users(pred) -> list[dict]:
    return [u for u in H.baseline["users"] if pred(u)]


def body_of(m: dict) -> str:
    """An email's readable content: the plain-text part, the HTML part as rendered text, and the raw HTML
    (for link targets). The spec does not fix plain text or HTML."""
    html = m.get("html") or ""
    return "\n".join(x for x in (m.get("text") or "", visible_text(html) if html else "", html) if x)


def body_links(m: dict) -> list[str]:
    return [l.rstrip(".,);]").replace("&amp;", "&") for l in re.findall(r"https?://[^\s<>\"']+", body_of(m))]


LIVE_LEGACY = ("booked", "completed", "no_show")


def listing_problem(got: list[int], user_id: int, label: str) -> str | None:
    """/appointments shows the account's appointments: every booked, completed or no-show one must be listed,
    cancelled ones may be, and nothing of anyone else's. None when that holds, else a description."""
    rows = [a for a in H.baseline["appointments"] if a["patient_id"] == user_id]
    required = {a["id"] for a in rows if a["status"] in LIVE_LEGACY}
    # allowed spans the baseline snapshot *and* appointments made for this account during the run:
    # earlier workflows (W21) book for these same quirky legacy patients, so a snapshot-only
    # whitelist rejects a page that is correct. Attribution is by patient_email, not patient_id,
    # because every add_booking caller passes patient_id=None (harness.py:395) and patient_id is
    # set only on legacy rows loaded from the snapshot (reference/model.py:63).
    allowed = {a["id"] for a in rows} | {a.aid for a in H.model.w.appts.values()
                                         if a.patient_email and H.model.account_for(a.patient_email) == user_id}
    gs = set(got)
    if required <= gs <= allowed:
        return None
    return f"{label}: missing {sorted(required - gs)[:5]}, not this account's {sorted(gs - allowed)[:5]}"


def page_appt_ids(r) -> list[int]:
    return [int(n.attrs["data-appointment-id"]) for n in r.dom.attr_all("data-appointment-id") if n.attrs["data-appointment-id"].isdigit()]


# ======================================================================================== W01
@workflow("W01", "Accounts: registration against quirky legacy addresses, reset links, duplicate-address accounts", 3)
def w01(ctx):
    with ctx.step("staff set passwords from emailed links and sign in; a new patient registers and is signed in"):
        H.ensure_staff()
        c, email = H.register()
        rows = H.q("SELECT role, active FROM users WHERE lower(btrim(email)) = %s", (email,))
        ctx.eq([(r["role"], r["active"]) for r in rows], [("patient", True)], "users rows for the new patient")
        ctx.shared["w01_patient"] = email
        ctx.surface("register")
    quirky = [u for u in H.baseline["users"] if u["role"] == "patient" and u["active"] and u["email"] != norm(u["email"])
              and H.model.account_for(u["email"]) == u["id"]]
    ctx.check(len(quirky) >= 2, "verifier fixture: no legacy patients with quirky addresses")
    with ctx.step("registering an address already on file, in any case or spacing, is refused and adds no row"):
        n0 = H.q("SELECT count(*) AS n FROM users")[0]["n"]
        for addr in ("  " + norm(quirky[0]["email"]).upper() + " ", ctx.shared["w01_patient"].title()):
            c = H.client()
            r = submit_form(c, "/register", "register", {"email": addr, "full_name": "Dup Person", "password": PASSWORD}, follow=True)
            ctx.check(not H.signed_in(c), f"registering {addr.strip()!r} signed the visitor in")
        ctx.eq(H.q("SELECT count(*) AS n FROM users")[0]["n"], n0, "users rows after duplicate registrations")
    with ctx.step("a known and an unknown address get the same reset response; only the known one gets an email"):
        known = norm(quirky[0]["email"])
        before = H.mail_ids(known)
        rk = H.request_reset(known.upper())
        ghost = f"nobody.{H.nonce}@verify.example"
        ru = H.request_reset(ghost)
        ctx.eq(ru.status, rk.status, "status for an unknown address")
        tk, tu = norm_text(visible_text(rk.text), known.upper(), known), norm_text(visible_text(ru.text), ghost)
        ctx.check(tk == tu, f"messages differ: known={tk[:160]!r} unknown={tu[:160]!r}")
        link = H.reset_link(known, before)
        ctx.check(link is not None, "no reset email reached the address on file")
        ctx.shared["w01_link1"] = link
        inactive = legacy_users(lambda u: not u["active"] and u["role"] == "patient" and H.model.account_for(u["email"]) is None)[0]
        ib = H.mail_ids(inactive["email"])
        H.request_reset(inactive["email"])
        time.sleep(4)
        ctx.eq(len(H.mail.to(ghost)), 0, "emails to an unknown address")
        ctx.eq(len(H.mail_ids(inactive["email"]) - ib), 0, "emails to an address whose only row is inactive")
    with ctx.step("a newer link replaces the older; a used link and an unknown link change nothing"):
        known = norm(quirky[0]["email"])
        before = H.mail_ids(known)
        H.request_reset(known)
        link2 = H.reset_link(known, before)
        ctx.check(link2 and link2 != ctx.shared["w01_link1"], "no second, different link")
        c = H.client()
        page = c.get(ctx.shared["w01_link1"], follow=True)
        if page.status == 200:
            try:
                submit_form(c, "", "set-password", {"password": "Old-Link-Pass-1"}, follow=True, page=page)
            except FormNotFound:
                pass
        ctx.check(not H.signed_in(H.signin(known, "Old-Link-Pass-1")[0]), "the replaced link set a working password")
        c = H.client()
        page = c.get(link2, follow=True)
        ctx.eq(page.status, 200, "newest link page status")
        submit_form(c, "", "set-password", {"password": PASSWORD}, follow=True, page=page)
        ctx.check(H.signed_in(H.signin("  " + known.upper() + " ")[0]), "the newest link did not set a working password "
                  "(or sign-in does not match the address case-insensitively, ignoring spaces)")
        page = H.client().get(link2, follow=True)
        if page.status == 200:
            try:
                submit_form(H.client(), link2, "set-password", {"password": "Second-Use-Pass-1"}, follow=True, page=page)
            except FormNotFound:
                pass
        ctx.check(not H.signed_in(H.signin(known, "Second-Use-Pass-1")[0]), "a used link set a password again")
        ctx.eq(H.client().get("/password-reset/" + secrets.token_urlsafe(24)).status, 404, "unknown link status")
    with ctx.step("an expired link changes nothing"):
        addr = norm(quirky[1]["email"])
        before = H.mail_ids(addr)
        H.request_reset(addr)
        link3 = H.reset_link(addr, before)
        ctx.check(link3, "no link for the second patient")
        time.sleep(H.reset_ttl + 3)
        c = H.client()
        page = c.get(link3, follow=True)
        if page.status == 200:
            try:
                submit_form(c, "", "set-password", {"password": "Expired-Link-1"}, follow=True, page=page)
            except FormNotFound:
                pass
        ctx.check(not H.signed_in(H.signin(addr, "Expired-Link-1")[0]), "an expired link set a password")
    with ctx.step("an address on two rows signs in as its account (lowest active id) and sees only that row's appointments"):
        by_norm: dict = {}
        for u in H.baseline["users"]:
            by_norm.setdefault(norm(u["email"]), []).append(u)
        appts_of: dict = {}
        for a in H.baseline["appointments"]:
            appts_of.setdefault(a["patient_id"], []).append(a["id"])
        pairs = [rows for rows in by_norm.values() if len(rows) > 1]
        # one pair whose lower id is inactive (the account is the higher id), one where both are active
        cases = [next(p for p in pairs if not p[0]["active"] and p[1]["active"] and appts_of.get(p[1]["id"])),
                 next(p for p in pairs if p[0]["active"] and p[1]["active"] and appts_of.get(p[0]["id"]) and appts_of.get(p[1]["id"]))]
        for rows in cases:
            acct = H.model.account_for(rows[0]["email"])
            c = H.account(rows[1]["email"])
            got = sorted(set(page_appt_ids(c.get("/appointments", follow=True))))
            want = sorted(appts_of.get(acct, []))
            other = [u["id"] for u in rows if u["id"] != acct]
            leaked = sorted(set(got) & {a for o in other for a in appts_of.get(o, [])})
            if leaked:
                ctx.breach("X1", f"address {norm(rows[0]['email'])} showed appointments of user rows that are not its account: {leaked[:5]}")
            err = listing_problem(got, acct, f"appointments listed for {norm(rows[0]['email'])} (account is user {acct})")
            ctx.check(err is None, err)


# ======================================================================================== W02
@workflow("W02", "Sessions and roles: refusals, fixation, sign-out, role gates, profile mass assignment, CSRF", 3)
def w02(ctx):
    with ctx.step("a wrong password and an unknown address are refused with the same message and no session"):
        H.ensure_staff()
        c, email = H.register()
        ctx.shared["w02"] = (c, email)
        c1, r1 = H.signin(email, "Wrong-Password-1")
        ghost = f"ghost.{H.nonce}@verify.example"
        c2, r2 = H.signin(ghost, "Wrong-Password-1")
        ctx.check(not H.signed_in(c1) and not H.signed_in(c2), "a refused sign-in produced a session")
        p1 = r1 if r1.status not in REDIRECTS else c1.get(r1.location, follow=True)
        p2 = r2 if r2.status not in REDIRECTS else c2.get(r2.location, follow=True)
        ctx.check(norm_text(visible_text(p1.text), email) == norm_text(visible_text(p2.text), ghost), "refusal messages differ")
    with ctx.step("signing in starts a new session identifier; one held before never becomes signed in; cookies are HttpOnly"):
        c, email = ctx.shared["w02"]
        fresh = H.client()
        fresh.get("/signin", follow=True)
        fresh.get("/book", follow=True)
        planted = dict(fresh.cookies)
        _, r = H.signin(email, client=fresh)
        ctx.check(H.signed_in(fresh), "sign-in failed")
        if planted:
            pre = H.client()
            pre.cookies = dict(planted)
            if H.signed_in(pre):
                ctx.breach("X7", "a session identifier held before sign-in became signed in")
                raise StepFailed("a session identifier held before sign-in became signed in")
        set_cookies = [h for h in r.header_all("Set-Cookie") if "=" in h.split(";")[0] and h.split(";")[0].split("=", 1)[1]]
        ctx.check(set_cookies, "sign-in set no cookie")
        # R2.4 is about session cookies: those that alone keep the browser signed in (all of them if none does alone)
        alone = []
        for h in set_cookies:
            name = h.split(";")[0].split("=", 1)[0].strip()
            probe = H.client()
            probe.cookies = {name: fresh.cookies.get(name, "")}
            if H.signed_in(probe):
                alone.append(h)
        session_cookies = alone or set_cookies
        ctx.check(all("httponly" in h.lower() for h in session_cookies), f"the session cookie is not HttpOnly: {session_cookies}")
        ctx.check(all(re.search(r"samesite=(lax|strict)", h, re.I) for h in session_cookies),
                  f"the session cookie is not SameSite=Lax/Strict: {session_cookies}")
        ctx.shared["w02_fresh"] = fresh
    with ctx.step("signing out ends the session on the server"):
        fresh = ctx.shared["w02_fresh"]
        copy = fresh.copy()
        submit_form(fresh, "/profile", "signout", {}, follow=True)
        ctx.check(not H.signed_in(fresh), "still signed in after sign-out")
        if H.signed_in(copy):
            ctx.breach("X7", "a session cookie copied before sign-out still signs in")
            raise StepFailed("a cookie copied before sign-out still works")
    with ctx.step("role gates: 403 for signed-in users outside their role, sign-in redirect for visitors, public availability"):
        c, email = ctx.shared["w02"]
        for path in ("/desk/day", "/desk/reminders", "/admin/rooms/new", "/admin/types/new", "/clinician/day", "/clinicians/1/schedule"):
            ctx.eq(c.get(path).status, 403, f"patient GET {path}")
        ctx.eq(H.desk.get("/admin/clinicians/new").status, 403, "front desk GET /admin/clinicians/new")
        ctx.eq(H.desk.get("/clinicians/1/schedule").status, 403, "front desk GET a clinician's schedule")
        n0 = H.q("SELECT count(*) AS n FROM rooms")[0]["n"]
        r = c.post_form("/admin/rooms/new", H.token(c) + [("clinic_id", "1"), ("name", "Intruder room"), ("room_type", "treatment")])
        if H.q("SELECT count(*) AS n FROM rooms")[0]["n"] != n0:
            ctx.breach("X1", "a patient created a room through the admin form")
        ctx.eq(r.status, 403, "patient POST /admin/rooms/new")
        for path in ("/appointments", "/desk/day", "/profile", "/waitlist"):
            r = H.client().get(path)
            ctx.check(r.status in REDIRECTS and "/signin" in (r.location or ""), f"visitor GET {path} answered {r.status}")
        st, slots, _ = H.availability(1, "FU30", H.today(SEA) + timedelta(days=3))
        ctx.check(st == 200 and slots is not None, f"a visitor's availability request answered {st}")
    with ctx.step("sign-in follows next only to a path on this site; visitors are sent to /signin?next=<path>"):
        c, email = ctx.shared["w02"]
        r = H.client().get("/appointments?x=1")
        loc = urllib.parse.urlsplit(r.location or "")
        nxt = urllib.parse.parse_qs(loc.query).get("next")
        ctx.check(loc.path == "/signin" and nxt in (["/appointments?x=1"], ["/appointments"]),
                  f"a visitor to /appointments?x=1 was sent to {r.location!r}")
        cl = H.client()
        page = cl.get("/signin?next=%2Fwaitlist", follow=True)
        r = submit_form(cl, "", "signin", {"email": email, "password": PASSWORD}, page=page)
        ctx.check(r.status in (301, 302, 303, 307, 308) and urllib.parse.urlsplit(urllib.parse.urljoin(H.base + "/", r.location or "")).path == "/waitlist",
                  f"signing in with next=/waitlist redirected to {r.location!r}")
        bad = []
        for evil in ("//evil.example/x", "/\\evil.example/x", "https://evil.example/x", "https:evil.example", "/\t/evil.example",
                     "http://127.0.0.1.evil.example/", "/%5Cevil.example", " //evil.example"):
            cl = H.client()
            page = cl.get("/signin?next=" + urllib.parse.quote(evil, safe=""), follow=True)
            r = submit_form(cl, "", "signin", {"email": email, "password": PASSWORD}, page=page, override={"next": evil})
            loc = (r.location or "").strip()
            target = urllib.parse.urlsplit(urllib.parse.urljoin(H.base + "/", loc.replace("\\", "/")))
            off = loc.startswith(("//", "/\\", "\\")) or "evil.example" in (target.hostname or "") or (
                bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", loc)) and target.hostname != urllib.parse.urlsplit(H.base).hostname)
            if off:
                ctx.breach("X11", f"sign-in redirected off the site for next={evil!r}: Location {loc!r}")
                bad.append(f"{evil!r} -> {loc!r}")
        ctx.check(not bad, f"open redirects: {bad}")
    with ctx.step("names with line breaks are refused; typed text never adds an email recipient or header"):
        trap = f"inject.{H.nonce}@evil.example"
        n0 = H.q("SELECT count(*) AS n FROM users")[0]["n"]
        for name in (f"Mallory\r\nBcc: {trap}", f"Mallory\nCc: {trap}", f"Mallory\rTo: {trap}"):
            cl = H.client()
            em = f"crlf.{H.uid()}@verify.example"
            r = submit_form(cl, "/register", "register", {"email": em, "full_name": name, "password": PASSWORD}, follow=False)
            ctx.check(r.status == 400 and not H.signed_in(cl), f"registering the name {name!r} answered {r.status}")
            if H.q("SELECT 1 FROM users WHERE lower(btrim(email)) = %s", (em,)):
                H.request_reset(em)
        c, email = ctx.shared["w02"]
        r = submit_form(c, "/profile", "save-profile", {"full_name": f"Eve\r\nBcc: {trap}"}, follow=False)
        ctx.eq(r.status, 400, "a profile name with a line break")
        r = submit_form(H.admin, "/admin/types/new", "create-type", {"code": f"CRLF{H.uid()}", "name": f"Massage\r\nBcc: {trap}",
                        "duration_minutes": "30", "buffer_minutes": "0", "room_type": "treatment", "fee_cents": "1000", "deposit_bp": "2000",
                        "effective_from": "2024-01-01"}, follow=False)
        ctx.eq(r.status, 400, "a type name with a line break")
        H.request_reset(email)
        time.sleep(5)
        if trap in H.all_mail_recipients():
            ctx.breach("X12", f"typed text added the recipient {trap} to an email")
            raise StepFailed("a line break in a name injected an email recipient")
    with ctx.step("the profile form changes only the display name; a token-less change is refused"):
        c, email = ctx.shared["w02"]
        before = H.q("SELECT id, email, role, active FROM users WHERE lower(btrim(email)) = %s", (email,))[0]
        r = submit_form(c, "/profile", "save-profile", {"full_name": "Renamed Verifier"}, follow=True,
                        override={"role": "admin", "email": f"hijack.{H.nonce}@verify.example", "active": "false", "id": "1", "user_id": "1"})
        after = H.q("SELECT id, email, role, active, full_name FROM users WHERE id = %s", (before["id"],))[0]
        changed = {k: (before[k], after[k]) for k in ("email", "role", "active") if before[k] != after[k]}
        admin_name = H.q("SELECT full_name FROM users WHERE id = 1")[0]["full_name"]
        if changed or admin_name == "Renamed Verifier":
            ctx.breach("X4", f"the profile form changed {changed or 'another user'}")
        ctx.check(not changed, f"extra profile fields changed {changed}")
        ctx.eq(after["full_name"], "Renamed Verifier", "display name after the profile form")
        H.renamed_users.add(before["id"])
        r = submit_form(c, "/profile", "save-profile", {"full_name": "Token Less"}, drop_csrf=True)
        now = H.q("SELECT full_name FROM users WHERE id = %s", (before["id"],))[0]["full_name"]
        if now == "Token Less":
            ctx.breach("X2", "a profile change without the session token took effect")
        ctx.check(r.status == 403 and now == "Renamed Verifier", f"token-less profile change answered {r.status}")


# ======================================================================================== availability helpers
def compare_all(ctx, cases, label: str, limit: int = 6) -> None:
    probs = problems_of(H.compare_slots(*c) for c in cases)
    ctx.observe(f"{label}_lists", len(cases))
    ctx.observe(f"{label}_nonempty", sum(1 for c in cases if H.model.slots(*c, H.now())))
    ctx.check(not probs, f"{len(probs)} of {len(cases)} slot lists differ: " + " | ".join(probs[:limit]))


def hm(m: int) -> str:
    return E.fmt_hm(m)


def edge_windows(rnd) -> str:
    """Weekly hours dense in boundary cases: off-grid edges, exact-length windows, touching, nested, midnight."""
    lines = []
    for d in E.DAY_NAMES:
        if rnd.random() < 0.12:
            continue
        p = rnd.randrange(10)
        if p == 0:  # one minute after the grid, one minute before it
            lines.append(f"{d} {hm(rnd.choice([481, 541, 601, 451]))}-{hm(rnd.choice([959, 1019, 1079]))}")
        elif p == 1:  # exactly a type's length, one minute short, and long enough for two
            s = rnd.choice([480, 495, 510])
            lines += [f"{d} {hm(s)}-{hm(s + 45)}", f"{d} {hm(s + 120)}-{hm(s + 179)}", f"{d} {hm(s + 240)}-{hm(s + 330)}",
                      f"{d} {hm(s + 400)}-{hm(s + 425)}"]
        elif p == 2:  # touching windows join
            a = rnd.choice([540, 600, 615])
            b = a + rnd.choice([30, 45, 60, 75])
            lines += [f"{d} {hm(a)}-{hm(b)}", f"{d} {hm(b)}-{hm(b + rnd.choice([15, 20, 30, 90]))}"]
        elif p == 3:  # overlapping
            lines += [f"{d} 08:10-12:20", f"{d} 11:50-15:05"]
        elif p == 4:  # nested
            lines += [f"{d} 07:30-17:30", f"{d} 09:00-10:00"]
        elif p == 5:  # to midnight
            lines.append(f"{d} {hm(rnd.choice([1260, 1275, 1290, 1301, 1335]))}-24:00")
        elif p == 6:  # from midnight
            lines += [f"{d} 00:00-{hm(rnd.choice([45, 61, 90, 100]))}", f"{d} 09:00-17:00"]
        elif p == 7:  # odd minutes, a lunch gap of a few minutes
            s, mid, g = rnd.choice([487, 503, 527]), rnd.choice([718, 722, 735]), rnd.choice([1, 7, 15, 29])
            lines += [f"{d} {hm(s)}-{hm(mid)}", f"{d} {hm(mid + g)}-{hm(rnd.choice([1013, 1046, 1080]))}"]
        elif p == 8:  # several short windows
            lines += [f"{d} {hm(t)}-{hm(t + rnd.choice([25, 30, 50, 60]))}" for t in (480, 600, 720, 840)]
        else:
            lines.append(f"{d} {hm(rnd.choice([420, 480, 540]))}-{hm(rnd.choice([960, 1020, 1080]))}")
    return "\n".join(lines)


def pay_now(aid: int) -> None:
    """Pays a hold's Checkout Session right after it is made, so no hold waits behind a batch of slower requests
    (holds last only HOLD_TTL_SECONDS)."""
    st, body = H.pay.pay(H.sessions[aid])
    if st != 200 or not body.get("ok"):
        raise StepFailed(f"paying for appointment {aid} answered {st}")


def wait_confirmed(aids: list[int]) -> None:
    end = time.monotonic() + 60
    left = list(aids)
    while left and time.monotonic() < end:
        left = [a for a in left if H.appt_marker(None, a).get("data-status") != "confirmed"]
        if left:
            time.sleep(0.5)
    if left:
        raise StepFailed(f"{len(left)} paid appointments were not confirmed within 60 s: {left[:5]}")
    for a in aids:
        H.model.set_status(a, "confirmed")


def adjacent_bookings(rnd, c, email, cids, codes, days, pairs: int = 1, check_rooms: bool = False) -> int:
    """Per clinician: a booking, and another starting exactly where the first's busy interval ends."""
    pending, n, wrong = [], 0, []
    for cid in cids:
        for _ in range(pairs):
            code, day = rnd.choice(codes), rnd.choice(days)
            slots = H.model.slots(cid, code, day, H.now())
            if not slots:
                continue
            s1 = rnd.choice(slots)
            t = H.model.w.types[code]
            for code_, st in ((code, s1), (rnd.choice(codes), s1 + (t.duration + t.buffer) * 60)):
                if st not in H.model.slots(cid, code_, day, H.now()):
                    continue
                want = H.model.predict_room(cid, code_, st, H.now())
                aid = H.hold(c, email, cid, code_, st)
                pay_now(aid)
                if check_rooms and H.model.appt(aid).room_id != want:
                    wrong.append(f"appointment {aid}: room {H.model.appt(aid).room_id}, expected {want}")
                pending.append(aid)
                n += 1
        if len(pending) >= 12:
            wait_confirmed(pending)
            pending = []
    if pending:
        wait_confirmed(pending)
    if wrong:
        raise StepFailed(f"{len(wrong)} bookings got the wrong room (lowest-id free room expected): " + " | ".join(wrong[:4]))
    return n


# ======================================================================================== W03
@workflow("W03", "Availability: 15-minute grid, fitting inside working time, buffers, merged and overlapping windows", 5, timeout=1800, critical=True)
def w03(ctx):
    rnd = rnd_for("W03")
    with ctx.step("120 clinicians at both clinics with weekly hours dense in edge cases; six types; plenty of rooms"):
        H.ensure_staff()
        rt = H.room_type("W03")
        for clinic in (SEA, HNL):
            for _ in range(10):
                H.create_room(clinic, rt)
        codes = [H.create_type(d, b, rt, 9500, 2000) for d, b in ((25, 5), (30, 15), (45, 15), (50, 25), (60, 0), (90, 10))]
        cids = []
        for i in range(120):
            clinic = SEA if i % 2 == 0 else HNL
            cid = H.create_clinician(clinic, max_per_day=40)
            H.add_version(cid, H.today(clinic) - timedelta(days=30), edge_windows(rnd))
            cids.append(cid)
        ctx.shared["W03"] = (codes, cids)
        ctx.observe("clinicians", len(cids))
    with ctx.step("confirmed bookings, each pair touching at a busy-interval boundary"):
        codes, cids = ctx.shared["W03"]
        c, email = H.register()
        days = {cl: [H.quiet_block(cl, 3, 7) + timedelta(days=k) for k in range(7)] for cl in (SEA, HNL)}
        n = 0
        for cl in (SEA, HNL):
            mine = [cid for cid in cids if H.model.w.clinicians[cid].clinic_id == cl]
            n += adjacent_bookings(rnd, c, email, mine, codes, days[cl], pairs=1)
        ctx.observe("bookings", n)
        ctx.shared["W03_days"] = days
    with ctx.step("every slot list (three days of the week per clinician, six types) matches the rules exactly"):
        codes, cids = ctx.shared["W03"]
        days = ctx.shared["W03_days"]
        cases = []
        for cid in cids:
            for d in rnd.sample(days[H.model.w.clinicians[cid].clinic_id], 3):
                cases += [(cid, code, d) for code in codes]
        compare_all(ctx, cases, "w03")
    with ctx.step("unknown clinician or type answers 404; a malformed date 400"):
        codes, cids = ctx.shared["W03"]
        d = H.today(SEA) + timedelta(days=3)
        ctx.eq(H.availability(99999999, codes[0], d)[0], 404, "unknown clinician")
        ctx.eq(H.availability(cids[0], "NO-SUCH-TYPE", d)[0], 404, "unknown type")
        ctx.eq(H.client().get(f"/api/availability?clinician_id={cids[0]}&type={codes[0]}&date=2026-13-45").status, 400, "malformed date")


# ======================================================================================== W04
def edge_exceptions(rnd, cid: int, days: list[date]) -> list[tuple]:
    """Exceptions aligned with the clinician's own windows on those days."""
    out = []
    c = H.model.w.clinicians[cid]
    for d in rnd.sample(days, rnd.randrange(2, 7)):
        v = E.version_on(c, d)
        wins = sorted((s, e) for w, s, e in (v.windows if v else []) if w == d.isoweekday())
        k = rnd.randrange(9)
        if k == 0 or not wins:
            out.append((d, "off", None, None) if k == 0 else (d, "extra", hm(rnd.choice([420, 600, 1200])), hm(rnd.choice([1260, 1440]))))
            continue
        s, e = rnd.choice(wins)
        if k == 1:
            out.append((d, "off", hm(s), hm(e)))  # exactly a window
        elif k == 2:
            out.append((d, "off", hm(max(0, s - 10)), hm(min(1440, s + 20))))  # across its start
        elif k == 3 and e <= 1380:
            out.append((d, "extra", hm(e), hm(e + rnd.choice([15, 30, 45, 60]))))  # touching its end: joins
        elif k == 4:
            out.append((d, "extra", hm(max(0, s - 30)), hm(min(1440, s + 30))))  # overlapping its start
        elif k == 5:
            mid = (s + e) // 2
            out.append((d, "off", hm(mid), hm(min(1440, mid + rnd.choice([1, 14, 15, 44])))))  # a short gap in the middle
        elif k == 6:
            out += [(d, "extra", "18:00", "21:00"), (d, "off", "19:00", "19:30")]  # off inside extra
        elif k == 7:
            out.append((d, "off", hm(max(0, e - 5)), hm(min(1440, e + 60))))  # trims the end by 5 minutes
        else:
            out += [(d, "extra", "21:30", "24:00")]
    return out


@workflow("W04", "Availability: effective-dated schedule versions, exceptions, schedule edits and who may make them", 4, timeout=1800, critical=True)
def w04(ctx):
    rnd = rnd_for("W04")
    with ctx.step("100 clinicians with several weekly-hours versions each, ties on the same date, versions with no hours"):
        H.ensure_staff()
        rt = H.room_type("W04")
        for clinic in (SEA, HNL):
            for _ in range(8):
                H.create_room(clinic, rt)
        codes = [H.create_type(60, 15, rt, 16500, 2000), H.create_type(30, 0, rt, 9500, 2000)]
        base = {cl: H.quiet_block(cl, 1, 16) for cl in (SEA, HNL)}
        cids, selfedit = [], []
        for i in range(100):
            clinic = SEA if i % 2 == 0 else HNL
            cid = H.create_clinician(clinic, 30)
            cl_client = None
            if i < 3:
                cl_client = H.account(H.clinician_email(cid))
                selfedit.append((cid, cl_client))
            b = base[clinic]
            H.add_version(cid, b - timedelta(days=20), edge_windows(rnd), client=cl_client)
            effs = sorted(b + timedelta(days=rnd.randrange(1, 15)) for _ in range(rnd.randrange(1, 4)))
            if rnd.random() < 0.3:
                effs.append(effs[0])  # a second version on the same date: the later one wins
            for eff in effs:
                H.add_version(cid, eff, "" if rnd.random() < 0.12 else edge_windows(rnd), client=cl_client)
            cids.append((cid, cl_client))
        ctx.shared["W04"] = (codes, cids, base, selfedit)
    with ctx.step("one-day exceptions aligned with each clinician's windows: whole and partial days off, joining extra hours"):
        codes, cids, base, selfedit = ctx.shared["W04"]
        n = 0
        for cid, cl_client in cids:
            b = base[H.model.w.clinicians[cid].clinic_id]
            for d, kind, s, e in edge_exceptions(rnd, cid, [b + timedelta(days=k) for k in range(1, 16)]):
                H.add_exception(cid, d, kind, s, e, client=cl_client)
                n += 1
        ctx.observe("exceptions", n)
    with ctx.step("a malformed schedule form is refused and changes nothing"):
        codes, cids, base, selfedit = ctx.shared["W04"]
        a, ca = selfedit[0]
        d = base[H.model.w.clinicians[a].clinic_id] + timedelta(days=2)
        for windows in ("MON 12:00-11:00", "MONDAY 09:00-10:00", "TUE 09:00-10:00\nWED 25:00-26:00", "FRI 10:00-10:00", "SAT 09:00 10:00"):
            r = H.add_version(a, d, windows, client=ca, record=False)
            ctx.check(r.status == 400, f"weekly hours {windows!r} answered {r.status}")
        for kind, s, e in (("extra", "", ""), ("off", "10:00", ""), ("extra", "11:00", "10:00"), ("holiday", "", "")):
            r = H.add_exception(a, d, kind, s or None, e or None, client=ca, record=False)
            ctx.check(r.status == 400, f"exception {kind} {s}-{e} answered {r.status}")
    with ctx.step("nobody edits another clinician's schedule; a token-less edit is refused"):
        codes, cids, base, selfedit = ctx.shared["W04"]
        (a, ca), (b, cb) = selfedit[0], selfedit[1]
        d = base[H.model.w.clinicians[b].clinic_id] + timedelta(days=3)
        before = H.availability(b, codes[1], d)[1]
        r1 = ca.post_form(f"/clinicians/{b}/schedule/exceptions", tokens(ca, f"/clinicians/{a}/schedule") +
                          [("on_date", d.isoformat()), ("kind", "off"), ("start_time", ""), ("end_time", "")])
        r2 = H.desk.post_form(f"/clinicians/{b}/schedule/versions", tokens(H.desk) + [("effective_from", d.isoformat()), ("windows", "")])
        if H.availability(b, codes[1], d)[1] != before:
            ctx.breach("X1", "a clinician or front desk changed another clinician's schedule")
        ctx.check(r1.status == 403 and r2.status == 403, f"foreign schedule edits answered {r1.status} (clinician) and {r2.status} (front desk)")
        want = H.model.slots(a, codes[1], d, H.now())
        r3 = submit_form(ca, f"/clinicians/{a}/schedule", "add-exception", {"on_date": d.isoformat(), "kind": "off", "start_time": "",
                                                                            "end_time": ""}, drop_csrf=True)
        if want and H.availability(a, codes[1], d)[1] == []:
            ctx.breach("X2", "a schedule exception without the session token took effect")
        ctx.eq(r3.status, 403, "token-less schedule edit")
    with ctx.step("slot lists across version changes, ties, versions without hours, and every exception match"):
        codes, cids, base, selfedit = ctx.shared["W04"]
        cases = [(cid, code, base[H.model.w.clinicians[cid].clinic_id] + timedelta(days=k)) for cid, _ in cids for code in codes
                 for k in range(0, 16)]
        compare_all(ctx, cases, "w04")


# ======================================================================================== W05
@workflow("W05", "Availability: shared rooms and lowest-id assignment, daily limits with holds, fees in effect, inactive records", 5, timeout=1800, critical=True)
def w05(ctx):
    rnd = rnd_for("W05")
    with ctx.step("worked example 3: two shared rooms, three clinicians, lowest-id room for each booking"):
        H.ensure_staff()
        rt = H.room_type("W05")
        r1, r2 = H.create_room(SEA, rt), H.create_room(SEA, rt)
        d = H.quiet_block(SEA, 4, 8)
        t = H.create_type(45, 15, rt, 11000, 1750)
        a, b, c3 = H.create_clinician(SEA, 3), H.create_clinician(SEA, 20), H.create_clinician(SEA, 20)
        for cid in (a, b, c3):
            H.add_version(cid, d - timedelta(days=10), "\n".join(f"{x} 08:00-17:00" for x in E.DAY_NAMES))
        pc, pe = H.register()
        x = H.booked(pc, pe, a, t, H.at(SEA, d, "10:00"))
        y = H.booked(pc, pe, b, t, H.at(SEA, d, "10:30"))
        ctx.eq([H.appt_marker(None, x).get("data-room-id"), H.appt_marker(None, y).get("data-room-id")], [str(r1), str(r2)],
               "rooms of the 10:00 and 10:30 bookings")
        gap = [s for s in (H.at(SEA, d, h) for h in ("09:45", "10:00", "10:15", "10:30", "10:45")) if s in (H.availability(c3, t, d)[1] or [])]
        ctx.eq(gap, [], "third clinician's slots from 09:45 to 10:45")
        z = H.booked(pc, pe, c3, t, H.at(SEA, d, "11:00"))
        ctx.eq(H.appt_marker(None, z).get("data-room-id"), str(r1), "room of the third clinician's 11:00 booking")
        n = len(H.model.slots(c3, t, d, H.now()))
        ctx.eq((book_buttons(pc, c3, t, d), book_buttons(H.client(), c3, t, d), book_buttons(H.desk_for(SEA), c3, t, d)), (n, 0, 0),
               "book-slot buttons for a patient, a visitor and front desk")
        ctx.shared["W05"] = (rt, r1, r2, d, t, a, b, c3, pc, pe)
    with ctx.step("four groups of eight clinicians share two or three rooms each; 120 bookings each get the lowest-id free room"):
        rt, r1, r2, d, t, a, b, c3, pc, pe = ctx.shared["W05"]
        groups = []
        # one block on a fixed weekday for every group: the groups' hours are a fixed seeded draw, so the number of
        # bookings they take (121) does not depend on the run date or on how far Seattle's date is ahead of Honolulu's
        day0 = quiet_monday(5, 3)
        for g in range(4):
            clinic = SEA if g % 2 == 0 else HNL
            grt = H.room_type("W05g")
            rooms = [H.create_room(clinic, grt) for _ in range(2 + g % 2)]
            codes = [H.create_type(45, 15, grt, 11000, 1750), H.create_type(30, 10, grt, 9000, 2000), H.create_type(60, 30, grt, 14500, 2250)]
            members = []
            for i in range(8):
                cid = H.create_clinician(clinic, rnd.choice([2, 3, 4, 6, 20]))
                H.add_version(cid, day0 - timedelta(days=10), edge_windows(rnd))
                members.append(cid)
            groups.append((clinic, rooms, codes, [day0, day0 + timedelta(days=1)], members))
        n = 0
        for clinic, rooms, codes, days, members in groups:
            n += adjacent_bookings(rnd, pc, pe, members * 4, codes, days, pairs=1, check_rooms=True)
        ctx.observe("bookings", n)
        ctx.check(n >= 100, f"verifier fixture: only {n} bookings")
        ctx.shared["W05_groups"] = groups
    with ctx.step("every group's slot lists match: rooms, daily limits, buffers"):
        groups = ctx.shared["W05_groups"]
        compare_all(ctx, [(cid, code, day) for clinic, rooms, codes, days, members in groups for cid in members for code in codes
                          for day in days], "w05_groups")
    with ctx.step("a clinician at the daily limit has no slots; an unexpired hold counts toward it"):
        rt, r1, r2, d, t, a, b, c3, pc, pe = ctx.shared["W05"]
        for hhmm in ("14:00", "15:30"):
            H.booked(pc, pe, a, t, H.at(SEA, d, hhmm))
        ctx.eq(H.availability(a, t, d)[1], [], "slots of a clinician with 3 of 3 appointments that day")
        d2 = d + timedelta(days=2)
        H.booked(pc, pe, a, t, H.at(SEA, d2, "09:00"))
        H.booked(pc, pe, a, t, H.at(SEA, d2, "11:00"))
        hid = H.hold(pc, pe, a, t, H.at(SEA, d2, "13:00"))
        ctx.eq(H.availability(a, t, d2)[1], [], "slots with two appointments and one unexpired hold against a limit of 3")
        submit_form(pc, f"/appointments/{hid}", "release-hold", {}, follow=True)
        H.model.set_status(hid, "released")
        err = H.compare_slots(a, t, d2)
        ctx.check(err is None, f"after releasing the hold: {err}")
    with ctx.step("no fee in effect yet, no room of the type, inactive types and clinicians: no slots"):
        rt, r1, r2, d, t, a, b, c3, pc, pe = ctx.shared["W05"]
        late = H.create_type(30, 0, rt, 9000, 2000, eff=d + timedelta(days=4))
        hn = H.create_clinician(HNL, 20)
        H.add_version(hn, d - timedelta(days=10), "\n".join(f"{x} 08:00-17:00" for x in E.DAY_NAMES))
        legacy_active = [c.cid for c in H.model.w.clinicians.values() if c.cid in H.model.legacy_clinicians and c.active]
        legacy_inactive = [c.cid for c in H.model.w.clinicians.values() if c.cid in H.model.legacy_clinicians and not c.active]
        legacy_hnl = [c.cid for c in H.model.w.clinicians.values() if c.cid in H.model.legacy_clinicians and c.active and c.clinic_id == HNL]
        cases = [(b, late, d + timedelta(days=k)) for k in (2, 3, 4, 5)] + [(hn, t, d)]
        cases += [(cid, "DRYN30", d + timedelta(days=1)) for cid in legacy_active[:10]]
        cases += [(cid, code, d + timedelta(days=1)) for cid in legacy_inactive for code in ("FU30", "INIT60")]
        cases += [(cid, "HYDRO60", d + timedelta(days=1)) for cid in legacy_hnl[:10]]
        compare_all(ctx, cases, "w05_misc")
        ctx.check(H.model.slots(b, late, d + timedelta(days=4), H.now()), "verifier fixture: the first fee day has no slots")


# ======================================================================================== W06
DST_TIMES = [0, 30, 59, 60, 75, 90, 105, 119, 120, 135, 150, 165, 179, 180, 195, 210, 240, 300]


@workflow("W06", "Time: notice and horizon from now, clock-change days, local dates per clinic, fees by local date", 5, timeout=1800, critical=True)
def w06(ctx):
    rnd = rnd_for("W06")
    with ctx.step("round-the-clock clinicians at both clinics: notice from now and the last bookable date"):
        H.ensure_staff()
        rt = H.room_type("W06")
        for clinic in (SEA, HNL):
            for _ in range(6):
                H.create_room(clinic, rt)
        codes = [H.create_type(60, 0, rt, 10000, 2000), H.create_type(30, 0, rt, 9900, 1750), H.create_type(45, 15, rt, 12550, 2000),
                 H.create_type(90, 10, rt, 18000, 1250)]
        allday = all_day_clinicians(SEA, 3) + all_day_clinicians(HNL, 3)
        cases = []
        for cid in allday:
            td = H.today(H.model.w.clinicians[cid].clinic_id)
            for code in codes:
                cases += [(cid, code, td), (cid, code, td + timedelta(days=1)), (cid, code, td - timedelta(days=1)),
                          (cid, code, td + timedelta(days=H.horizon)), (cid, code, td + timedelta(days=H.horizon + 1))]
        compare_all(ctx, cases, "w06_notice")
        hday = H.today(SEA) + timedelta(days=H.horizon)
        ctx.check((SEA, hday) in H.model.w.closures or H.model.slots(allday[0], codes[2], hday, H.now()), "verifier fixture: horizon day empty")
        ctx.shared["W06"] = (rt, codes, allday)
    with ctx.step("forty clinicians whose hours and exceptions cross the next Seattle clock change get exactly the rules' slots"):
        rt, codes, allday = ctx.shared["W06"]
        tz = H.model.w.clinics[SEA]
        dst = None
        for k in range(1, H.horizon + 1):
            day = H.today(SEA) + timedelta(days=k)
            if E.local_of(E.wall_to_epoch(day, 0, tz), tz).utcoffset() != E.local_of(E.wall_to_epoch(day, 1440, tz), tz).utcoffset():
                dst = day
                break
        ctx.check(dst is not None, "verifier fixture: no clock change within the booking horizon")
        if (SEA, dst) in H.model.w.closures:
            raise StepFailed("verifier fixture: the clock-change day is a closure")
        dn = E.DAY_NAMES[dst.isoweekday() - 1]
        dcids = []
        for i in range(40):
            cid = H.create_clinician(SEA, 40)
            lines = []
            for _ in range(rnd.randrange(1, 4)):
                s, e = sorted(rnd.sample(DST_TIMES, 2))
                lines.append(f"{dn} {hm(s)}-{hm(e)}")
            if i % 5 == 0:
                lines.append(f"{dn} {hm(rnd.choice([1320, 1335, 1380]))}-24:00")
            H.add_version(cid, dst - timedelta(days=8), "\n".join(lines))
            if i % 2 == 0:
                s, e = sorted(rnd.sample(DST_TIMES, 2))
                H.add_exception(cid, dst, rnd.choice(["off", "extra"]), hm(s), hm(e))
            dcids.append(cid)
        cases = [(cid, code, dst + timedelta(days=k)) for cid in dcids for code in codes for k in (-7, 0, 7)]
        compare_all(ctx, cases, "w06_dst")
        ctx.observe("clock_change_day", dst.isoformat())
        ctx.shared["W06_dst"] = (dst, dcids)
    with ctx.step("deposits around a fee change use each appointment's local date at its own clinic"):
        rt, codes, allday = ctx.shared["W06"]
        t30 = codes[1]
        prev = H.open_day(SEA, 4, 1, (HNL,))  # neither clinic closed on either date
        eff = prev + timedelta(days=1)
        H.add_fee(t30, eff, 12345, 1250)
        c, email = H.register()
        # Seattle is two hours (winter) or three hours (summer) ahead of Honolulu: just after Seattle's midnight it is
        # still the previous date in Honolulu, so one instant gets the new fee in Seattle and the old one in Honolulu
        ctx.check(E.local_date(H.at(SEA, eff, "00:30"), H.model.w.clinics[HNL]) == prev, "verifier fixture: instants differ")
        got, want = [], []
        holds = []
        for cid in allday:
            clinic = H.model.w.clinicians[cid].clinic_id
            for day, times in ((prev, ("22:00", "23:00", "23:30", "23:45")), (eff, ("00:00", "00:15", "00:30", "01:00"))):
                for hhmm in times:
                    st = H.at(clinic, day, hhmm)
                    if st not in H.model.slots(cid, t30, day, H.now()):
                        continue
                    aid = H.hold(c, email, cid, t30, st)
                    sess = H.pay.session(H.sessions[aid])
                    got.append((aid, sess.get("amount_total"), H.appt_marker(None, aid).get("data-deposit-cents")))
                    dep = H.model.deposit_on(t30, day)
                    want.append((aid, dep, str(dep)))
                    holds.append((aid, cid, st))
        ctx.eq(got, want, "Checkout amounts and deposits (appointment, amount_total, data-deposit-cents)")
        ctx.check(len({w[1] for w in want}) == 2 and len(holds) >= 20, "verifier fixture: fees not exercised")
        ctx.shared["W06_holds"] = holds
    with ctx.step("each new appointment is a row in appointments whose local_start and fold give its start at its clinic"):
        dst, dcids = ctx.shared["W06_dst"]
        rt, codes, allday = ctx.shared["W06"]
        holds = list(ctx.shared.get("W06_holds", []))
        tz = H.model.w.clinics[SEA]
        if E.local_of(E.wall_to_epoch(dst, 0, tz), tz).utcoffset() > E.local_of(E.wall_to_epoch(dst, 1440, tz), tz).utcoffset():
            c, email = H.register()
            for minute in (60, 75, 90):  # the second occurrence of 01:00-01:45 on the night clocks go back
                second = E.wall_to_epoch(dst, minute, tz, fold=1)
                if second in H.model.slots(allday[0], codes[1], dst, H.now()):
                    holds.append((H.hold(c, email, allday[0], codes[1], second), allday[0], second))
        bad = []
        for aid, cid, st in holds:
            rows = H.q("SELECT to_char(local_start, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS ls, fold FROM appointments WHERE id = %s", (aid,))
            loc = E.local_of(st, H.model.tz_of(cid))
            want = (loc.strftime("%Y-%m-%dT%H:%M:%S"), loc.fold)
            if not rows or (rows[0]["ls"], rows[0]["fold"]) != want:
                bad.append(f"appointment {aid}: row {rows[0] if rows else None}, expected local_start/fold {want}")
        ctx.check(not bad, " | ".join(bad[:4]))


# ======================================================================================== W07, W22: legacy segments
def legacy_segment(ctx, clinic: int, wid: str) -> None:
    """The old system's clinicians, appointments and patients of one clinic, served as they are."""
    rnd = rnd_for(wid)
    m = H.model
    tz = m.w.clinics[clinic]
    today = H.today(clinic)
    lo, hi = today + timedelta(days=1), today + timedelta(days=H.horizon)
    clins = sorted(c.cid for c in m.w.clinicians.values() if c.cid in m.legacy_clinicians and c.clinic_id == clinic)
    with ctx.step(f"slot lists for every old clinician of the clinic on busy, exceptional, version-change and closure dates match"):
        interesting: dict[int, set] = {}
        for a in m.w.appts.values():
            if a.legacy and a.status == "booked" and a.clinician_id in clins:
                d = E.local_date(a.start, tz)
                if lo <= d <= hi:
                    interesting.setdefault(a.clinician_id, set()).add(d)
        for cid in clins:
            c = m.w.clinicians[cid]
            for x in c.exceptions:
                if lo <= x.on_date <= hi:
                    interesting.setdefault(cid, set()).add(x.on_date)
            for v in c.versions:
                if lo <= v.effective_from <= hi:
                    interesting.setdefault(cid, set()).add(v.effective_from)
                    interesting[cid].add(v.effective_from - timedelta(days=1))
            for (cl, d) in m.w.closures:
                if cl == clinic and lo <= d <= hi:
                    interesting.setdefault(cid, set()).add(d)
        codes = ["FU30", "INIT60", "FU45", "REHAB45"] + (["HYDRO60"] if clinic == SEA else [])
        cases = []
        for cid in clins:
            days = sorted(interesting.get(cid, set()))
            pick = rnd.sample(days, min(8, len(days))) + [today + timedelta(days=rnd.randrange(1, H.horizon)) for _ in range(2)]
            for d in pick:
                for code in rnd.sample(codes, 3):
                    cases.append((cid, code, d))
        march = date(2027, 3, 14)
        if clinic == SEA and lo <= march <= hi:
            for cid in clins:
                if any(v.windows and any(w[0] == 7 and w[1] == 0 for w in v.windows) for v in m.w.clinicians[cid].versions):
                    cases += [(cid, "FU30", march), (cid, "INIT60", march), (cid, "FU45", march)]
        compare_all(ctx, cases, wid.lower(), 5)
    with ctx.step("the clinic's front desk sees its day sheets with the old appointments in time order"):
        H.ensure_staff()
        desk = H.desk_for(clinic)
        busiest: dict = {}
        for a in m.w.appts.values():
            if a.legacy and a.clinician_id in clins:
                d = E.local_date(a.start, tz)
                busiest[d] = busiest.get(d, 0) + 1
        days = sorted(busiest, key=lambda d: (-busiest[d], d))[:3]
        days += [date(2026, 11, 1), date(2027, 3, 14)] if clinic == SEA else [date(2026, 11, 11), date(2026, 12, 24)]
        bad = []
        for day in days:
            r = desk.get(f"/desk/day?clinic_id={clinic}&date={day.isoformat()}", follow=True)
            got = [(int(n.attrs["data-appointment-id"]), E.parse_instant(n.attrs.get("data-start", "1970-01-01T00:00:00Z")))
                   for n in r.dom.attr_all("data-appointment-id")]
            want = [(a.aid, a.start) for a in m.day_sheet(clinic, day)]
            if sorted(got) != sorted(want):
                miss = sorted(set(want) - set(got))[:3]
                extra = sorted(set(got) - set(want))[:3]
                bad.append(f"{day}: {len(got)} rows, expected {len(want)}; missing {miss} extra/wrong {extra}")
            elif [x for _, x in got] != sorted(x for _, x in got):
                bad.append(f"{day}: not in time order")
        ctx.check(not bad, " | ".join(bad))
    with ctx.step("old patients with quirky or shared addresses see exactly their account's appointments, with no change actions"):
        appts_of: dict = {}
        for a in H.baseline["appointments"]:
            appts_of.setdefault(a["patient_id"], []).append(a["id"])
        mine = [a for a in m.w.appts.values() if a.legacy and a.status == "booked" and a.clinician_id in clins and a.start > H.now() + 3 * 86400]
        cands = []
        for a in mine:
            u = m.users[a.patient_id]
            if u["active"] and m.account_for(u["email"]) == u["id"] and u["email"] != norm(u["email"]):
                cands.append(a)
        ctx.check(len(cands) >= 3, "verifier fixture: too few quirky legacy patients")
        for a in rnd.sample(cands, 3):
            u = m.users[a.patient_id]
            c = H.account(u["email"])
            got = sorted(set(page_appt_ids(c.get("/appointments", follow=True))))
            err = listing_problem(got, u["id"], f"appointments listed for {norm(u['email'])}")
            ctx.check(err is None, err)
            r = H.appt_page(c, a.aid)
            node = r.dom.attr_one("data-appointment-id", str(a.aid))
            ctx.check(node is not None and node.attrs.get("data-status") == "booked"
                      and E.parse_instant(node.attrs.get("data-start", "1970-01-01T00:00:00Z")) == a.start,
                      f"old appointment {a.aid} shows {node.attrs if node else None}")
            ctx.eq(sorted(actions(r) & APPT_ACTIONS), [], f"actions offered to the patient on old appointment {a.aid}")
            ctx.eq(sorted(actions(H.appt_page(H.desk_for(clinic), a.aid)) & APPT_ACTIONS), [],
                   f"actions offered to front desk on old appointment {a.aid}")
    if clinic == SEA:
        with ctx.step("an old night-clinic clinician sees the clock-change night on their own day sheet, both 01:30s in order"):
            night = [a for a in m.w.appts.values() if a.legacy and a.status != "cancelled" and a.clinician_id in clins
                     and E.local_date(a.start, tz) == date(2026, 11, 1) and m.legacy_appts[a.aid]["fold"] == 1]
            ctx.check(night, "verifier fixture: no fold=1 appointments")
            cid = night[0].clinician_id
            c = H.account(m.users[m.w.clinicians[cid].user_id]["email"])
            r = c.get("/clinician/day?date=2026-11-01", follow=True)
            got = [(int(n.attrs["data-appointment-id"]), E.parse_instant(n.attrs.get("data-start", "1970-01-01T00:00:00Z")))
                   for n in r.dom.attr_all("data-appointment-id")]
            want = [(a.aid, a.start) for a in m.day_sheet(SEA, date(2026, 11, 1)) if a.clinician_id == cid]
            ctx.eq(sorted(got), sorted(want), f"clinician {cid}'s day sheet for 2026-11-01 (id, instant)")
            ctx.check([x for _, x in got] == sorted(x for _, x in got), f"clinician {cid}'s day sheet is not in time order")
            for a in night:
                r = H.desk.get(f"/appointments/{a.aid}", follow=True)
                node = r.dom.attr_one("data-appointment-id", str(a.aid))
                ctx.check(node is not None and E.parse_instant(node.attrs.get("data-start", "1970-01-01T00:00:00Z")) == a.start,
                          f"appointment {a.aid} (fold 1) shows start {node.attrs.get('data-start') if node else None}, "
                          f"expected {E.iso(a.start, tz)}")
    else:
        with ctx.step("Honolulu clinicians' own day sheets list their old appointments"):
            for cid in rnd.sample(clins, 2):
                days = sorted({E.local_date(a.start, tz) for a in m.w.appts.values() if a.legacy and a.clinician_id == cid
                               and a.status in ("booked", "completed")})
                ctx.check(days, "verifier fixture: clinician without appointments")
                day = days[len(days) // 2]
                c = H.account(m.users[m.w.clinicians[cid].user_id]["email"])
                r = c.get(f"/clinician/day?date={day.isoformat()}", follow=True)
                got = [int(n.attrs["data-appointment-id"]) for n in r.dom.attr_all("data-appointment-id")]
                want = [a.aid for a in m.day_sheet(HNL, day) if a.clinician_id == cid]
                ctx.eq(sorted(got), sorted(want), f"clinician {cid}'s day sheet for {day}")
                starts = [m.w.appts[a].start for a in got if a in m.w.appts]
                ctx.check(starts == sorted(starts), f"clinician {cid}'s day sheet is not in time order")


@workflow("W07", "Legacy Seattle: the old system's schedules, appointments, folds and gaps, served as they are", 5, timeout=1800,
          critical=True)
def w07(ctx):
    legacy_segment(ctx, SEA, "W07")

# ======================================================================================== money helpers
def quiet_monday(first: int, length: int) -> date:
    """The first Monday on or after today+first that starts a quiet block (quiet_block: no clock change in Seattle,
    no closure at either clinic, for length+1 days)."""
    k = first
    while True:
        d = H.quiet_block(SEA, k, length)
        if d.isoweekday() == 1:
            return d
        k = (d - H.today(SEA)).days + 1


def first_open_slot(cid: int, code: str, days: list) -> int:
    """The model's first slot for the clinician on the first of `days` that has one."""
    for day in days:
        slots = H.model.slots(cid, code, day, H.now())
        if slots:
            return slots[0]
    raise StepFailed(f"verifier fixture: clinician {cid} has no {code} slot on {days[0]}..{days[-1]}")


def tie_fees(rnd, n: int) -> list[tuple[int, int]]:
    """(fee, bp) pairs: mostly from reference/ties.json (deposits and half refunds on exact half cents), one in
    five an ordinary fee, shuffled so the hard cases sit among ordinary ones."""
    ties = rnd.sample(H.ties(), min(n, len(H.ties())))
    out = [ties[i] if (i % 5 != 4 or n == 1) else (rnd.randrange(5000, 25000, 100), rnd.choice([1500, 2000, 2500]))
           for i in range(len(ties))]
    rnd.shuffle(out)
    return out


def ui_appt(ctx, c: Client, aid: int, want: set, label: str) -> None:
    """Interface contract: the appointment page offers exactly the actions the viewer may take now."""
    r = H.appt_page(c, aid)
    ctx.eq(sorted(actions(r) & APPT_ACTIONS), sorted(want), f"{label}: actions offered on appointment {aid}")


def slot_near(cid: int, code: str, hours: float, lo_h: float, hi_h: float, ignore: int | None = None,
              fallbacks: tuple = (), required: bool = True) -> int | None:
    """The model's slot closest to now + hours, within [now + lo_h, now + hi_h] (any local date). A clinic closure
    can cover that whole window on some run dates; then the first of `fallbacks` ((lo_h, hi_h) pairs) that has a
    slot is used, or None when `required` is false. Callers judge by the start chosen, not the hours asked for."""
    now = H.now()
    d0 = H.model.local_day(cid, now)
    pool = [s for k in range(0, 5) for s in H.model.slots(cid, code, d0 + timedelta(days=k), now, ignore=ignore)]
    for lo, hi in ((lo_h, hi_h), *fallbacks):
        cands = [s for s in pool if now + lo * 3600 <= s <= now + hi * 3600]
        if cands:
            return min(cands, key=lambda s: abs(s - (now + hours * 3600)))
    if not required:
        return None
    raise StepFailed(f"verifier fixture: clinician {cid} has no {code} slot {lo_h}-{hi_h} h ahead")


def book_buttons(c: Client, cid: int, code: str, day: date) -> int:
    r = c.get(f"/book?clinician_id={cid}&type={urllib.parse.quote(code)}&date={day.isoformat()}", follow=True)
    return len([n for n in r.dom.attr_all("data-action", "book-slot") if n.ancestor("form") is not None])


def all_day_clinicians(clinic: int, n: int, max_per_day: int = 96) -> list[int]:
    out = []
    for _ in range(n):
        cid = H.create_clinician(clinic, max_per_day)
        H.add_version(cid, H.today(SEA) - timedelta(days=3), ALL_DAY)
        out.append(cid)
    return out


def tokens(c: Client, path: str = "/profile") -> list[tuple[str, str]]:
    return H.token(c, path)


def cancel(c: Client, aid: int, drop_csrf: bool = False):
    page = H.appt_page(c, aid)
    try:
        return submit_form(c, "", "cancel-appointment", {}, page=page, drop_csrf=drop_csrf)
    except FormNotFound:
        return c.post_form(f"/appointments/{aid}/cancel", [] if drop_csrf else tokens(c))


# ======================================================================================== judge evidence backstop
def evidence_backstop(checkpoint: str, make):
    """Judge evidence must not depend on strict checks passing (one defect costs one workflow, not also a judge
    criterion). When the decorated workflow stops before capturing `checkpoint`, `make` builds it from a fresh,
    minimal scenario of its own: best effort, never a check, and nothing it does is graded."""
    def deco(fn):
        @functools.wraps(fn)
        def run(ctx):
            try:
                fn(ctx)
            except InfraError:
                raise
            except Exception:
                if not (B.UX_DIR / checkpoint / "visible.txt").exists():
                    try:
                        make()
                    except InfraError:
                        raise
                    except Exception:  # noqa: BLE001 - the workflow has already failed; evidence is best effort
                        pass
                raise
        return run
    return deco


def evidence_booking(tag: str):
    """A fresh Seattle clinician, type and room, and a patient with one confirmed appointment five days ahead."""
    H.ensure_staff()
    rt = H.room_type(tag)
    H.create_room(SEA, rt)
    code = H.create_type(45, 0, rt, 9000, 2000, name="Initial assessment")
    cid = H.create_clinician(SEA, 20, name="Rowan Ellis")
    H.add_version(cid, H.today(SEA) - timedelta(days=3), ALL_DAY)
    c, email = H.register(name="Noa Kealoha")
    day = H.open_day(SEA, 5)
    start = H.first_slot(cid, code, day, after=H.at(SEA, day, "10:00"))
    return c, email, cid, H.booked(c, email, cid, code, start), start


def confirmation_evidence() -> None:
    c, email, cid, aid, start = evidence_booking("EVC")
    mails = H.wait_mails(email, "Appointment confirmed", 1)
    if mails:
        loc = E.local_of(start, H.model.tz_of(cid))
        B.capture_text("confirmation_email", f"Subject: {mails[-1]['subject']}\n\n{body_of(mails[-1])}",
                       {"appointment": aid, "local": loc.isoformat(), "clinic": H.model.clinic_names[SEA],
                        "clinician": H.model.w.clinicians[cid].name})


def cancellation_evidence() -> None:
    c, email, cid, aid, start = evidence_booking("EVX")
    seen = H.mail_ids(email)
    t0 = H.now()
    cancel(H.desk_for(SEA), aid)
    m = H.wait_status(aid, "cancelled", 30)
    a = H.model.appt(aid)
    if m.get("data-status") == "cancelled":
        a.status, a.refund = "cancelled", int(m.get("data-refund-cents") or 0)
    new = H.mail.wait_for(email, lambda m_: m_["id"] not in seen and "appointment cancelled" in (m_["subject"] or "").lower(), 1, EFFECT_WAIT)
    if new:
        B.capture_text("cancellation_email", f"Subject: {new[0]['subject']}\n\n{new[0]['text'] or visible_text(new[0]['html'])}",
                       {"appointment_id": aid, "cancelled_by": "clinic", "deposit_cents": a.deposit, "refund_cents": a.deposit,
                        "hours_before_start_at_cancellation": round((start - t0) / 3600, 2),
                        "rule": "clinic cancellations refund the whole deposit"})


def offer_evidence() -> None:
    H.ensure_staff()
    rt = H.room_type("EVO")
    H.create_room(SEA, rt)
    code = H.create_type(30, 0, rt, 9500, 2000, name="Follow-up")
    d = H.quiet_block(SEA, 5, 1)
    w = H.create_clinician(SEA, 10, name="Kai Mahoe")
    H.add_version(w, d - timedelta(days=7), f"{E.DAY_NAMES[d.isoweekday() - 1]} 10:00-10:30")
    p, pe = H.register()
    x = H.booked(p, pe, w, code, H.at(SEA, d, "10:00"))
    q, qe = H.register(name="Mei Tanaka")
    seen = H.mail_ids(qe)
    submit_form(q, "/waitlist", "join-waitlist", {"clinician_id": str(w), "type": code, "date": d.isoformat()})
    cancel(p, x)
    H.model.set_status(x, "cancelled")
    msgs = H.mail.wait_for(qe, lambda m: m["id"] not in seen and "slot opened" in (m["subject"] or "").lower(), 1, EFFECT_WAIT)
    if msgs:
        B.capture_text("offer_email", f"Subject: {msgs[0]['subject']}\n\n{msgs[0]['text'] or visible_text(msgs[0]['html'])}",
                       {"start": E.iso(H.at(SEA, d, "10:00"), "America/Los_Angeles")})


# ======================================================================================== W08
@workflow("W08", "Booking: deposits on Checkout, mass assignment, confirmation only by signed webhook, one email", 5, critical=True)
@evidence_backstop("confirmation_email", confirmation_evidence)
def w08(ctx):
    rnd = rnd_for("W08")
    with ctx.step("thirty appointment types with fees that land on half cents; clinicians in both clinics"):
        H.ensure_staff()
        rt = H.room_type("W08")
        for clinic in (SEA, HNL):
            for _ in range(4):
                H.create_room(clinic, rt)
        codes = [H.create_type(rnd.choice([30, 45, 60]), rnd.choice([0, 10, 15]), rt, fee, bp) for fee, bp in tie_fees(rnd, 40)]
        sea = all_day_clinicians(SEA, 2)
        hnl = all_day_clinicians(HNL, 1)
        c, email = H.register()
        ctx.shared["W08"] = (rt, codes, sea, hnl, c, email)
    with ctx.step("each hold's Checkout Session carries the exact deposit, metadata, URLs and address, whatever extra fields say"):
        rt, codes, sea, hnl, c, email = ctx.shared["W08"]
        bad = []
        sneaky = {"deposit_cents": "1", "amount": "1", "fee_cents": "1", "price": "1", "unit_amount": "1", "deposit_bp": "1",
                  "status": "confirmed", "patient_id": "1", "room_id": "1"}
        day = H.quiet_block(SEA, 2, 3)
        for i, code in enumerate(codes):
            cid = (sea + hnl)[i % 3]
            start = H.first_slot(cid, code, day + timedelta(days=i % 3))
            t = H.now()
            r, aid, sid = H.book(c, cid, code, start, extra=sneaky if i % 2 else None)
            if aid is None:
                bad.append(f"{code}: booking answered {r.status}")
                continue
            H.record_hold(aid, cid, code, start, sid, email, t)
            sess = H.pay.session(sid)
            dep = H.model.deposit_on(code, H.model.local_day(cid, start))
            mk = H.appt_marker(None, aid)
            if i < 4:
                ui_appt(ctx, c, aid, {"release-hold"}, "the patient, while held")
                ui_appt(ctx, H.desk_for(H.clinic_of(cid)), aid, set(), "front desk, while held")
            if sess.get("amount_total") == 1 or mk.get("data-deposit-cents") == "1" or mk.get("data-status") == "confirmed":
                ctx.breach("X4", f"extra booking fields changed appointment {aid}: amount {sess.get('amount_total')}, marker {mk.get('data-deposit-cents')}")
            name = ((sess.get("line_items") or [{}])[0].get("price_data") or {}).get("product_data", {}).get("name", "")
            got = (sess.get("amount_total"), sess.get("currency"), sess.get("mode"), (sess.get("metadata") or {}).get("appointment_id"),
                   sess.get("success_url"), sess.get("cancel_url"), norm(sess.get("customer_email") or ""),
                   H.model.w.types[code].name.lower() in name.lower(), mk.get("data-status"), mk.get("data-deposit-cents"))
            want = (dep, "usd", "payment", str(aid), f"{H.base}/appointments/{aid}?checkout=success",
                    f"{H.base}/appointments/{aid}?checkout=cancelled", email, True, "held", str(dep))
            if got != want:
                bad.append(f"{code} appointment {aid}: got {got}, want {want}")
            submit_form(c, f"/appointments/{aid}", "release-hold", {}, follow=True)
            H.model.set_status(aid, "released")
        ctx.check(not bad, f"{len(bad)} of {len(codes)} holds differ: " + " | ".join(bad[:4]))
    with ctx.step("returning from payment does not confirm; the verified event does, and one confirmation email follows"):
        rt, codes, sea, hnl, c, email = ctx.shared["W08"]
        H.pay.hold(["checkout.session.completed"])
        try:
            start = H.first_slot(sea[0], codes[0], H.open_day(SEA, 5))
            aid = H.hold(c, email, sea[0], codes[0], start)
            st, body = H.pay.pay(H.sessions[aid])
            ctx.check(st == 200 and body.get("ok"), f"paying answered {st}")
            r = c.get(f"/appointments/{aid}?checkout=success", follow=True)
            node = r.dom.attr_one("data-appointment-id", str(aid))
            time.sleep(3)
            m2 = H.appt_marker(None, aid)
            if (node is not None and node.attrs.get("data-status") == "confirmed") or m2.get("data-status") == "confirmed":
                ctx.breach("X10", f"appointment {aid} was confirmed without a verified webhook")
                raise StepFailed("the booking was confirmed before any webhook arrived")
            ctx.eq(m2.get("data-status"), "held", "status after returning from payment with the webhook held back")
        finally:
            H.pay.release()
        m = H.wait_status(aid, "confirmed", 60)
        ctx.eq(m.get("data-status"), "confirmed", "status after the webhook was delivered")
        H.model.set_status(aid, "confirmed")
        mails = H.wait_mails(email, "Appointment confirmed", 1)
        mine = [x for x in mails if str(aid) in body_of(x) or E.local_of(start, H.model.tz_of(sea[0])).strftime("%H:%M") in body_of(x)]
        loc = E.local_of(start, H.model.tz_of(sea[0]))
        clin = H.model.w.clinicians[sea[0]]
        if mine:  # judge evidence before the strict checks below (common rule 12)
            B.capture_text("confirmation_email", f"Subject: {mine[-1]['subject']}\n\n{body_of(mine[-1])}",
                           {"appointment": aid, "local": loc.isoformat(), "clinic": H.model.clinic_names[SEA], "clinician": clin.name})
        ui_appt(ctx, c, aid, {"cancel-appointment", "reschedule"}, "the patient, confirmed five days ahead")
        ui_appt(ctx, H.desk_for(SEA), aid, {"cancel-appointment"}, "front desk, confirmed")
        ui_appt(ctx, H.admin, aid, {"cancel-appointment"}, "an admin, confirmed")
        ctx.check(mine, "no confirmation email naming the appointment's local time")
        body = body_of(mine[-1])
        ctx.check(loc.strftime("%Y-%m-%d") in body and loc.strftime("%H:%M") in body and clin.name in body
                  and H.model.clinic_names[SEA] in body, "the confirmation email does not name the clinic, clinician, local date and time")
        raw = H.mail_raw(mine[-1]["id"])
        names = [t.get("Name", "") for t in raw.get("To") or []]
        ctx.check(any(n.strip() for n in names), f"the confirmation email's To header has no display name: {raw.get('To')}")
        ctx.shared["W08_confirmed"] = (aid, start)
    with ctx.step("repeated and simultaneous deliveries of the same event change nothing and send nothing"):
        rt, codes, sea, hnl, c, email = ctx.shared["W08"]
        aid, start = ctx.shared["W08_confirmed"]
        evs = H.pay.events_for(aid, "checkout.session.completed")
        ctx.check(evs, "verifier fixture: no completed event at the provider")
        H.pay.redeliver(evs[0]["id"])
        body = event_body(evs[0])
        outs = simultaneously([lambda: H.client().request("POST", "/webhooks/payments", body=body, headers={
            "Content-Type": "application/json", "Stripe-Signature": H.pay.sign(body)}) for _ in range(6)])
        ctx.check(all(k == "ok" and 200 <= v.status < 300 for k, v in outs), f"verified repeated deliveries answered {[getattr(v, 'status', v) for k, v in outs]}")
        time.sleep(6)
        n = len([x for x in H.mails(email, "Appointment confirmed") if E.local_of(start, H.model.tz_of(sea[0])).strftime("%H:%M") in body_of(x)])
        if n > 1:
            ctx.breach("X6", f"appointment {aid} produced {n} confirmation emails")
        ctx.eq(n, 1, "confirmation emails after repeated deliveries")
        ctx.eq(H.appt_marker(None, aid).get("data-status"), "confirmed", "status after repeated deliveries")
    with ctx.step("a declined card leaves the hold in place; a good card then confirms it"):
        rt, codes, sea, hnl, c, email = ctx.shared["W08"]
        aid = H.hold(c, email, hnl[0], codes[1], H.first_slot(hnl[0], codes[1], H.open_day(HNL, 6)))
        st, body = H.pay.pay(H.sessions[aid], CARD_DECLINE)
        ctx.check(st == 402 and not body.get("ok"), f"declined card answered {st}")
        time.sleep(2)
        ctx.eq(H.appt_marker(None, aid).get("data-status"), "held", "status after a declined card")
        H.confirm(aid)
    with ctx.step("refused bookings create nothing: staff, no token, not a slot, unknown type, provider failure"):
        rt, codes, sea, hnl, c, email = ctx.shared["W08"]
        day = H.open_day(SEA, 7)
        free = H.first_slot(sea[1], codes[2], day)
        n0 = len(H.pay.state().get("checkout_sessions", {}))
        r = H.desk.post_form("/book", tokens(H.desk) + [("clinician_id", str(sea[1])), ("type", codes[2]), ("start", E.iso(free, H.model.tz_of(sea[1])))])
        ctx.eq(r.status, 403, "front desk POST /book")
        r, aid, _ = H.book(c, sea[1], codes[2], free, drop_csrf=True)
        if aid is not None:
            ctx.breach("X2", "a booking without the session token created a hold")
        ctx.eq(r.status, 403, "token-less POST /book")
        now_s = int(H.now())
        for st, label in ((free + 7 * 60, "off the 15-minute grid"), (now_s - 3600 - now_s % 900, "in the past"),
                          (free + 1, "one second after a slot")):
            r, aid, _ = H.book(c, sea[1], codes[2], st)
            ctx.check(aid is None and r.status == 409, f"booking {label} answered {r.status}")
        r, aid, _ = H.book(c, sea[1], "NO-SUCH-TYPE", free)
        ctx.check(aid is None and r.status >= 400, f"booking an unknown type answered {r.status}")
        ctx.eq(len(H.pay.state().get("checkout_sessions", {})), n0, "Checkout Sessions created by refused bookings")
        H.pay.fault(checkout_create="error")
        n1 = len(H.pay.state().get("checkout_sessions", {}))
        r, aid, _ = H.book(c, sea[1], codes[2], free)
        # R6.1 says only that the patient sees an error; the checks below prove nothing was held or charged
        ctx.check(aid is None, f"booking while the provider fails answered {r.status} {r.location or ''}")
        ctx.eq(len(H.pay.state().get("checkout_sessions", {})), n1, "Checkout Sessions created while the provider failed")
        err = H.compare_slots(sea[1], codes[2], day)
        ctx.check(err is None, f"after the provider failed, the slot is not free: {err}")


# ======================================================================================== W09
@workflow("W09", "Holds: expiry measured from creation, release, late and out-of-order payments refunded in full", 5, critical=True)
def w09(ctx):
    rnd = rnd_for("W09")
    with ctx.step("a dozen holds occupy their slots until HOLD_TTL_SECONDS after creation, then free them"):
        H.ensure_staff()
        rt = H.room_type("W09")
        H.create_room(SEA, rt)
        H.create_room(HNL, rt)
        code = H.create_type(30, 15, rt, 9900, 1750)
        sea, hnl = all_day_clinicians(SEA, 2), all_day_clinicians(HNL, 1)
        c, email = H.register()
        day = H.quiet_block(SEA, 3, 2)
        holds = []
        for i in range(12):
            cid = (sea + hnl)[i % 3]
            slots = H.model.slots(cid, code, day, H.now())
            holds.append(H.hold(c, email, cid, code, rnd.choice(slots)))
        t_last = max(H.model.appt(a).expires for a in holds) - H.hold_ttl  # the latest any of them can expire, less ttl
        t_first = min(H.hold_lo[a] for a in holds) - H.hold_ttl  # the earliest any of them can expire, less ttl
        compare_all(ctx, [(cid, code, day) for cid in sea + hnl], "w09_held")
        wait = t_first + H.hold_ttl - 6 - H.now()
        if wait > 0:
            time.sleep(wait)
        if H.now() < t_first + H.hold_ttl - 2:
            compare_all(ctx, [(cid, code, day) for cid in sea + hnl], "w09_still_held")
        wait = t_last + H.hold_ttl + 3 - H.now()
        if wait > 0:
            time.sleep(wait)
        compare_all(ctx, [(cid, code, day) for cid in sea + hnl], "w09_expired")
        sts = [H.wait_status(a, "expired", 15).get("data-status") for a in holds]
        ctx.eq(sts, ["expired"] * len(holds), "statuses of expired holds")
        ui_appt(ctx, c, holds[0], set(), "the patient, after expiry")
        for a in holds:
            H.model.set_status(a, "expired")
        ctx.shared["W09"] = (code, sea, hnl, c, email, day, holds)
    with ctx.step("the application expires each expired hold's Checkout Session at the provider"):
        code, sea, hnl, c, email, day, holds = ctx.shared["W09"]
        end = time.monotonic() + EFFECT_WAIT
        while time.monotonic() < end:
            st = H.pay.state()["checkout_sessions"]
            left = [a for a in holds if st.get(H.sessions[a], {}).get("status") != "expired"]
            if not left:
                break
            time.sleep(1)
        ctx.check(not left, f"{len(left)} expired holds still have an open Checkout Session")
    with ctx.step("payments whose events arrive after the hold expired are refunded in full and never confirm"):
        code, sea, hnl, c, email, day, holds = ctx.shared["W09"]
        H.pay.hold(["checkout.session.completed"])
        late = []
        try:
            for i in range(3):
                cid = (sea + hnl)[i]
                aid = H.hold(c, email, cid, code, H.first_slot(cid, code, day + timedelta(days=1)))
                st, body = H.pay.pay(H.sessions[aid])
                ctx.check(st == 200, f"paying answered {st}")
                late.append(aid)
            time.sleep(max(0.0, H.model.appt(late[-1]).expires + 3 - H.now()))
        finally:
            ids = [e["id"] for a in reversed(late) for e in H.pay.events_for(a, "checkout.session.completed")]
            H.pay.release(ids) if ids else None
            H.pay.release()
        end = time.monotonic() + EFFECT_WAIT
        ms = {}
        while time.monotonic() < end:
            ms = {a: H.appt_marker(None, a) for a in late}
            if all(m.get("data-refund-status") in ("pending", "refunded") or m.get("data-status") == "confirmed" for m in ms.values()):
                break
            time.sleep(1)
        ctx.eq([ms[a].get("data-status") for a in late], ["expired"] * len(late), "statuses once the late completions arrived")
        for a in late:
            H.model.set_status(a, "expired")
        ctx.shared["W09_late"] = late
    with ctx.step("a hold released by the patient frees its slot at once; a payment forced through afterwards is refunded"):
        code, sea, hnl, c, email, day, holds = ctx.shared["W09"]
        cid = sea[0]
        aid = H.hold(c, email, cid, code, H.first_slot(cid, code, day + timedelta(days=1)))
        r = submit_form(c, f"/appointments/{aid}", "release-hold", {}, follow=True)
        H.model.set_status(aid, "released")
        ctx.eq(H.appt_marker(None, aid).get("data-status"), "released", "status after releasing")
        ui_appt(ctx, c, aid, set(), "the patient, after releasing")
        err = H.compare_slots(cid, code, day + timedelta(days=1))
        ctx.check(err is None, f"after the release: {err}")
        end = time.monotonic() + EFFECT_WAIT
        while time.monotonic() < end and H.pay.session(H.sessions[aid]).get("status") != "expired":
            time.sleep(1)
        ctx.eq(H.pay.session(H.sessions[aid]).get("status"), "expired", "Checkout Session status after the release")
        H.pay.force_complete(H.sessions[aid])
        exp = holds[0]
        H.pay.force_complete(H.sessions[exp])
        ctx.shared["W09_late"] += [aid, exp]
    with ctx.step("each late payment is refunded once, in full, with an Idempotency-Key; one refund email each; no confirmation"):
        code, sea, hnl, c, email, day, holds = ctx.shared["W09"]
        late = ctx.shared["W09_late"]
        end = time.monotonic() + REFUND_WAIT
        while time.monotonic() < end:
            ms = {a: H.appt_marker(None, a) for a in late}
            if all(m.get("data-refund-status") == "refunded" for m in ms.values()):
                break
            time.sleep(1)
        st = H.pay.state()
        bad = []
        for a in late:
            m = ms[a]
            pi = H.pay.session(H.sessions[a]).get("payment_intent")
            refunds = H.pay.refunds_for(pi, st)
            reqs = H.pay.refund_requests(pi, st)
            amount = H.pay.session(H.sessions[a]).get("amount_total")
            if len(refunds) > 1:
                ctx.breach("X6", f"late payment for appointment {a} was refunded {len(refunds)} times")
            got = (m.get("data-status"), m.get("data-refund-status"), m.get("data-refund-cents"), [r["amount"] for r in refunds],
                   all(r.get("idempotency_key") for r in reqs))
            want = (H.model.appt(a).status, "refunded", str(amount), [amount], True)
            if got != want:
                bad.append(f"appointment {a}: got {got}, want {want}")
        ctx.check(not bad, " | ".join(bad[:4]))
        conf = [x for x in H.mails(email, "Appointment confirmed")]
        ctx.eq(len(conf), 0, "confirmation emails for holds that were never confirmed")
        time.sleep(3)
        ctx.eq(len(H.mails(email, "Refund issued")), len(late), "refund emails for the late payments")
        compare_all(ctx, [(cid, code, day + timedelta(days=1)) for cid in sea + hnl], "w09_after_late")
    with ctx.step("visiting the cancel link alone changes nothing; paying within the hold then confirms"):
        code, sea, hnl, c, email, day, holds = ctx.shared["W09"]
        aid = H.hold(c, email, hnl[0], code, H.first_slot(hnl[0], code, day + timedelta(days=1)))
        c.get(f"/appointments/{aid}?checkout=cancelled", follow=True)
        time.sleep(1)
        ctx.eq(H.appt_marker(None, aid).get("data-status"), "held", "status after visiting cancel_url")
        H.confirm(aid)


# ======================================================================================== W10
@workflow("W10", "Webhook security: unsigned, mis-signed, tampered and stale deliveries change nothing", 4)
def w10(ctx):
    with ctx.step("a held appointment and forged completion events for it"):
        H.ensure_staff()
        rt = H.room_type("W10")
        H.create_room(SEA, rt)
        code = H.create_type(45, 0, rt, 12000, 2000)
        cid = all_day_clinicians(SEA, 1)[0]
        c, email = H.register()
        aid = H.hold(c, email, cid, code, H.first_slot(cid, code, H.open_day(SEA, 4)))
        sess = dict(H.pay.session(H.sessions[aid]))
        sess.update({"status": "complete", "payment_status": "paid", "payment_intent": "pi_forged_" + H.nonce})
        ev = {"id": "evt_forged_" + H.nonce, "object": "event", "type": "checkout.session.completed", "created": int(H.now()),
              "data": {"object": sess}}
        ctx.shared["W10"] = (code, cid, c, email, aid, ev)
    with ctx.step("missing, malformed, wrong-key, tampered, stale and future signatures are answered 400 and change nothing"):
        code, cid, c, email, aid, ev = ctx.shared["W10"]
        body = event_body(ev)
        other = event_body({**ev, "id": ev["id"] + "x"})
        now = int(H.now())
        variants = [("no header", body, None), ("malformed", body, "v1=deadbeef"), ("empty", body, "t=,v1="),
                    ("wrong key", body, H.pay.sign(body, key="whsec_wrong")), ("tampered", other, H.pay.sign(body)),
                    ("stale", body, H.pay.sign(body, t=now - 400)), ("future", body, H.pay.sign(body, t=now + 400)),
                    ("no t", body, "v1=" + H.pay.sign(body).split("v1=")[1])]
        bad = []
        for label, b, sig in variants:
            hdr = {"Content-Type": "application/json"}
            if sig is not None:
                hdr["Stripe-Signature"] = sig
            r = H.client().request("POST", "/webhooks/payments", body=b, headers=hdr)
            st = H.appt_marker(None, aid).get("data-status")
            if st == "confirmed":
                ctx.breach("X5", f"a webhook with a {label} signature confirmed appointment {aid}")
                raise StepFailed(f"a {label} signature confirmed the booking")
            if r.status != 400:
                bad.append(f"{label}: {r.status}")
        ctx.check(not bad, f"forged deliveries not answered 400: {bad}")
        time.sleep(2)
        ctx.eq(len(H.mails(email, "Appointment confirmed")), 0, "confirmation emails after forged deliveries")
    with ctx.step("verified events of other types or for unknown appointments are answered 2xx within 5 s and ignored"):
        code, cid, c, email, aid, ev = ctx.shared["W10"]
        for e in ({"id": "evt_other_" + H.nonce, "type": "customer.subscription.created", "data": {"object": {"id": "sub_x"}}},
                  {"id": "evt_unknown_" + H.nonce, "type": "checkout.session.completed",
                   "data": {"object": {"id": "cs_x", "payment_status": "paid", "amount_total": 100, "payment_intent": "pi_x",
                                       "metadata": {"appointment_id": "999999999"}}}},
                  {"id": "evt_refund_unknown_" + H.nonce, "type": "refund.created",
                   "data": {"object": {"id": "re_x", "status": "succeeded", "amount": 5, "metadata": {"appointment_id": "999999998"}}}}):
            b = event_body(e)
            t0 = time.monotonic()
            r = H.client().request("POST", "/webhooks/payments", body=b, headers={"Content-Type": "application/json", "Stripe-Signature": H.pay.sign(b)})
            ctx.check(200 <= r.status < 300 and time.monotonic() - t0 < 5, f"{e['type']} answered {r.status} after {time.monotonic() - t0:.1f}s")
        ctx.eq(H.appt_marker(None, aid).get("data-status"), "held", "status after unrelated events")
    with ctx.step("the genuine payment then confirms; a second verified completion with a new event id sends nothing more"):
        code, cid, c, email, aid, ev = ctx.shared["W10"]
        H.confirm(aid)
        real = H.pay.events_for(aid, "checkout.session.completed")[0]
        dup = {**real, "id": real["id"] + "_again"}
        b = event_body(dup)
        r = H.client().request("POST", "/webhooks/payments", body=b, headers={"Content-Type": "application/json", "Stripe-Signature": H.pay.sign(b)})
        ctx.check(200 <= r.status < 300, f"a second verified completion answered {r.status}")
        time.sleep(5)
        n = len(H.mails(email, "Appointment confirmed"))
        if n > 1:
            ctx.breach("X6", f"appointment {aid} produced {n} confirmation emails")
        ctx.eq(n, 1, "confirmation emails")
        mk = H.appt_marker(None, aid)
        ctx.eq((mk.get("data-status"), mk.get("data-refund-status"), H.pay.refunds_for(H.payment_intent(aid))), ("confirmed", "none", []),
               "a second completion for a confirmed booking changes nothing (status, refund status, refunds)")


# ======================================================================================== W11
@workflow("W11", "Races: the last slot, the last room and the last daily place each go to exactly one patient", 4, critical=True)
def w11(ctx):
    with ctx.step("fixtures: a one-slot day, a one-room pair of clinicians, a clinician with one place left"):
        H.ensure_staff()
        rt = H.room_type("W11")
        room = H.create_room(SEA, rt)
        t60 = H.create_type(60, 0, rt, 10000, 2000)
        d = H.quiet_block(SEA, 4, 2)
        dn = E.DAY_NAMES[d.isoweekday() - 1]
        one, b, c3, lim = (H.create_clinician(SEA, 10), H.create_clinician(SEA, 10), H.create_clinician(SEA, 10), H.create_clinician(SEA, 1))
        H.add_version(one, d - timedelta(days=7), f"{dn} 10:00-11:00")
        for cid in (b, c3, lim):
            H.add_version(cid, d - timedelta(days=7), f"{dn} 13:00-17:00")
        pats = [H.register() for _ in range(8)]
        ctx.shared["W11"] = (t60, d, one, b, c3, lim, pats)
        ctx.check(len(H.model.slots(one, t60, d, H.now())) == 1, "verifier fixture: the one-slot day has another number of slots")

    def race(label, reqs):
        prepared = []
        for (c, email), cid, start in reqs:
            page = c.get(f"/book?clinician_id={cid}&type={t60}&date={d.isoformat()}", follow=True)
            tok = csrf_from(page) or tokens(c)
            prepared.append((c, email, cid, start, tok + [("clinician_id", str(cid)), ("type", t60), ("start", E.iso(start, H.model.tz_of(cid)))]))
        t = H.now()
        outs = simultaneously([lambda p=p: p[0].post_form("/book", p[4]) for p in prepared])
        wins = []
        for p, (kind, v) in zip(prepared, outs):
            if kind == "ok" and v.status in REDIRECTS and "/pay/" in (v.location or ""):
                sid = re.search(r"/pay/(cs_[A-Za-z0-9_]+)", v.location).group(1)
                aid = int(H.pay.session(sid)["metadata"]["appointment_id"])
                H.record_hold(aid, p[2], t60, p[3], sid, p[1], t)
                wins.append(aid)
            elif kind == "ok" and v.status != 409:
                raise StepFailed(f"{label}: a losing request answered {v.status}, expected 409")
        if len(wins) > 1:
            ctx.breach("X6", f"{label}: {len(wins)} holds were created for one place")
        ctx.eq(len(wins), 1, f"{label}: holds created")
        H.confirm(wins[0])
        return wins[0]

    with ctx.step("eight patients ask for the only slot of the day at the same moment: exactly one hold"):
        t60, d, one, b, c3, lim, pats = ctx.shared["W11"]
        s = H.model.slots(one, t60, d, H.now())[0]
        race("the last slot", [(p, one, s) for p in pats])
        ctx.eq(H.availability(one, t60, d)[1], [], "slots after the race")
    with ctx.step("two clinicians, one room: simultaneous requests for the same hour get one booking between them"):
        t60, d, one, b, c3, lim, pats = ctx.shared["W11"]
        s = H.at(SEA, d, "14:00")
        race("the last room", [(p, [b, c3][i % 2], s) for i, p in enumerate(pats)])
        compare_all(ctx, [(b, t60, d), (c3, t60, d)], "w11_room")
    with ctx.step("a clinician with one place left that day: simultaneous requests for different times get one booking"):
        t60, d, one, b, c3, lim, pats = ctx.shared["W11"]
        slots = H.model.slots(lim, t60, d, H.now())
        win = race("the last daily place", [(p, lim, slots[i % len(slots)]) for i, p in enumerate(pats)])
        compare_all(ctx, [(lim, t60, d)], "w11_limit")
    with ctx.step("the same patient submitting the same booking twice at once gets one hold"):
        t60, d, one, b, c3, lim, pats = ctx.shared["W11"]
        s = H.model.slots(b, t60, d, H.now())[0]
        race("a double submit", [(pats[0], b, s), (pats[0], b, s)])

# ======================================================================================== refund helpers
def refund_bands(start: int, t0: float, t1: float, paid: int, staff: bool) -> set[int]:
    """Refunds the rules allow for a cancellation that happened somewhere in [t0, t1]."""
    return {E.refund(paid, start - t0, staff), E.refund(paid, start - t1, staff)}


def wait_refunds(aids: list[int], timeout: float = REFUND_WAIT) -> dict[int, dict]:
    end = time.monotonic() + timeout
    ms = {}
    while time.monotonic() < end:
        ms = {a: H.appt_marker(None, a) for a in aids}
        if all(m.get("data-refund-status") in ("refunded", "none") for m in ms.values()):
            break
        time.sleep(1)
    return ms


def check_refunds(ctx, expected: dict[int, int], ms: dict[int, dict], label: str) -> None:
    st = H.pay.state()
    bad = []
    for a, want in expected.items():
        m = ms[a]
        pi = H.payment_intent(a)
        refunds = H.pay.refunds_for(pi, st) if pi else []
        reqs = H.pay.refund_requests(pi, st) if pi else []
        total = sum(r["amount"] for r in refunds)
        if len(refunds) > 1 or total > H.model.appt(a).deposit:
            ctx.breach("X6", f"{label}: appointment {a} got {len(refunds)} refunds totalling {total}")
        meta_ok = all(str((r.get("metadata") or {}).get("appointment_id")) == str(a) for r in refunds)
        got = (m.get("data-status"), m.get("data-refund-cents"), m.get("data-refund-status"), [r["amount"] for r in refunds],
               meta_ok, all(r.get("idempotency_key") for r in reqs), len({r.get("idempotency_key") for r in reqs}) <= 1)
        exp = ("cancelled", str(want), "refunded" if want else "none", [want] if want else [], True, True, True)
        if got != exp:
            bad.append(f"appointment {a}: got {got}, want {exp}")
    ctx.check(not bad, f"{len(bad)} of {len(expected)} refunds differ: " + " | ".join(bad[:4]))


# ======================================================================================== W12
@workflow("W12", "Cancellation: refund bands by elapsed time, half-cent rounding, clinic cancellations, emails once", 5, timeout=1500, critical=True)
@evidence_backstop("cancellation_email", cancellation_evidence)
def w12(ctx):
    rnd = rnd_for("W12")
    with ctx.step("round-the-clock clinicians; patients book and pay appointments near the 2-hour and 24-hour boundaries"):
        H.ensure_staff()
        rt = H.room_type("W12")
        for clinic in (SEA, HNL):
            for _ in range(6):
                H.create_room(clinic, rt)
        codes = [H.create_type(30, 0, rt, fee, bp) for fee, bp in tie_fees(rnd, 6)]
        clins = all_day_clinicians(SEA, 4) + all_day_clinicians(HNL, 3)
        pats = [H.register() for _ in range(6)]
        now = H.now()
        offsets = []
        for base in (7200, 86400):
            offsets += [base + k * 900 for k in range(-3, 4)]
        offsets += [H.notice * 60 + 900, 3 * 86400, 26 * 3600, 5 * 3600, 23 * 3600]
        plan = []
        for i in range(84):
            cid, code = clins[i % len(clins)], codes[i % len(codes)]
            off = offsets[i % len(offsets)]
            start = -(-int(now + off) // 900) * 900
            plan.append((pats[i % len(pats)], cid, code, start, i % 5 == 4))
        booked = []
        skipped = []
        for (c, email), cid, code, start, staff in plan:
            day = H.model.local_day(cid, start)
            free = [s for s in H.model.slots(cid, code, day, H.now()) + H.model.slots(cid, code, day + timedelta(days=1), H.now())
                    if s >= start]
            if not free:
                skipped.append((cid, code, E.iso(start, H.model.tz_of(cid))))
                continue
            booked.append((H.booked(c, email, cid, code, free[0]), c, email, staff))
        ctx.check(len(booked) >= 75, f"verifier fixture: only {len(booked)} appointments could be booked; skipped {skipped[:5]}")
        ctx.shared["W12"] = (codes, clins, pats, booked)
    with ctx.step("another patient cannot cancel; a cancellation without the session token is refused"):
        codes, clins, pats, booked = ctx.shared["W12"]
        aid, c, email, staff = booked[0]
        other = next(p for p in pats if p[1] != email)[0]
        r = other.post_form(f"/appointments/{aid}/cancel", tokens(other))
        if H.appt_marker(None, aid).get("data-status") == "cancelled":
            ctx.breach("X1", f"another patient cancelled appointment {aid}")
            raise StepFailed("another patient's cancellation took effect")
        ctx.eq(r.status, 403, "another patient's cancellation")
        r = cancel(c, aid, drop_csrf=True)
        if H.appt_marker(None, aid).get("data-status") == "cancelled":
            ctx.breach("X2", f"a cancellation without the session token took effect for {aid}")
            raise StepFailed("a token-less cancellation took effect")
        ctx.eq(r.status, 403, "token-less cancellation")
    with ctx.step("each cancellation fixes the refund its band gives at the moment it happens"):
        codes, clins, pats, booked = ctx.shared["W12"]
        expected, bad = {}, []
        # judge evidence (J09): the email of one specific clinic cancellation, captured unambiguously
        ev_i = next((i for i, b in enumerate(booked) if b[3]), 0)
        ev_prior = sum(1 for b in booked[:ev_i] if b[2] == booked[ev_i][2])
        for i, (aid, c, email, staff) in enumerate(booked):
            a = H.model.appt(aid)
            if i == ev_i:
                H.wait_mails(email, "Appointment cancelled", ev_prior)
                ev_before = H.mail_ids(email)
            if i % 6 == 0 and abs(a.start - H.now() - 86400) > 120:
                want_ui = {"cancel-appointment", "reschedule"} if a.start - H.now() > 86400 else {"cancel-appointment"}
                ui_appt(ctx, c, aid, want_ui, f"the patient, {(a.start - H.now()) / 3600:.1f} h before")
            t0 = H.now()
            r = cancel(((H.manager_for if i % 2 else H.desk_for)(H.clinic_of(a.clinician_id))) if staff else c, aid)
            t1 = H.now()
            m = H.appt_marker(None, aid)
            allowed = refund_bands(a.start, t0, t1, a.deposit, staff)
            got = int(m.get("data-refund-cents", "-1") or -1)
            if r.status >= 400 or m.get("data-status") != "cancelled" or got not in allowed:
                bad.append(f"appointment {aid} ({(a.start - t0) / 3600:.2f} h before, deposit {a.deposit}, staff={staff}): "
                           f"status {r.status}/{m.get('data-status')}, refund {got}, allowed {sorted(allowed)}")
                continue
            expected[aid] = got
            a.status, a.refund = "cancelled", got
            if i == ev_i:
                new = H.mail.wait_for(email, lambda m_: m_["id"] not in ev_before and "appointment cancelled" in (m_["subject"] or "").lower(),
                                      1, EFFECT_WAIT)
                if new:  # judge evidence now, before the later strict steps (common rule 12)
                    B.capture_text("cancellation_email", f"Subject: {new[0]['subject']}\n\n{new[0]['text'] or visible_text(new[0]['html'])}", {
                        "appointment_id": aid, "cancelled_by": "clinic" if staff else "patient", "deposit_cents": a.deposit,
                        "refund_cents": got, "hours_before_start_at_cancellation": round((a.start - t0) / 3600, 2),
                        "rule": "clinic cancellations refund the whole deposit" if staff else "refund by notice band (R7.1)"})
            if i % 10 == 0:
                ui_appt(ctx, c, aid, set(), "the patient, after cancelling")
        ctx.check(not bad, f"{len(bad)} of {len(booked)} cancellations differ: " + " | ".join(bad[:4]))
        ctx.observe("refunds", sorted(set(expected.values())))
        ctx.shared["W12_expected"] = expected
    with ctx.step("refunds go through the provider once each, keyed, and complete on the refund webhook"):
        expected = ctx.shared["W12_expected"]
        check_refunds(ctx, expected, wait_refunds(list(expected)), "W12")
    with ctx.step("one cancellation email per appointment; one refund email per refund above zero"):
        codes, clins, pats, booked = ctx.shared["W12"]
        expected = ctx.shared["W12_expected"]
        by_email: dict = {}
        for aid, c, email, staff in booked:
            by_email.setdefault(email, []).append(aid)
        H.wait_counts([(e, "Appointment cancelled", len(v)) for e, v in by_email.items()]
                      + [(e, "Refund issued", sum(1 for a in v if expected.get(a, 0) > 0)) for e, v in by_email.items()])
        bad = []
        for email, aids in by_email.items():
            nc = len(H.mails(email, "Appointment cancelled"))
            nr = len(H.mails(email, "Refund issued"))
            want_r = sum(1 for a in aids if expected.get(a, 0) > 0)
            if nc > len(aids) or nr > want_r:
                ctx.breach("X6", f"{email}: {nc} cancellation and {nr} refund emails for {len(aids)} cancellations")
            if (nc, nr) != (len(aids), want_r):
                bad.append(f"{email}: {nc} cancellation emails (want {len(aids)}), {nr} refund emails (want {want_r})")
        ctx.check(not bad, " | ".join(bad))
    with ctx.step("a cancelled appointment cannot be cancelled again; its slot is free; the old system's appointments refuse"):
        codes, clins, pats, booked = ctx.shared["W12"]
        aid, c, email, staff = booked[2]
        r = c.post_form(f"/appointments/{aid}/cancel", tokens(c))
        ctx.eq(r.status, 409, "cancelling a cancelled appointment")
        freed = [H.model.appt(b[0]) for b in booked[:8]]
        compare_all(ctx, [(a.clinician_id, a.type_code, H.model.local_day(a.clinician_id, a.start)) for a in freed], "w12_freed")
        leg = next(a for a in H.model.w.appts.values() if a.legacy and a.status == "booked" and a.start > H.now() + 86400 * 2
                   and H.model.account_for(H.model.users[a.patient_id]["email"]) == a.patient_id)
        pc = H.account(H.model.users[leg.patient_id]["email"])
        r = pc.post_form(f"/appointments/{leg.aid}/cancel", tokens(pc))
        row = H.q("SELECT status FROM appointments WHERE id = %s", (leg.aid,))[0]["status"]
        if row != "booked":
            ctx.breach("X9", f"cancelling an old-system appointment changed its row (status {row})")
        ctx.eq(r.status, 409, "cancelling an old-system appointment")


# ======================================================================================== W13
@workflow("W13", "Refund retries: provider errors, lost answers and timeouts never refund twice; events before answers", 4, timeout=1200, critical=True)
def w13(ctx):
    with ctx.step("paid appointments more than a day ahead"):
        H.ensure_staff()
        rt = H.room_type("W13")
        H.create_room(SEA, rt)
        H.create_room(SEA, rt)
        code = H.create_type(30, 0, rt, 12345, 2000)
        cid = all_day_clinicians(SEA, 1)[0]
        c, email = H.register()
        day = H.open_day(SEA, 3)
        slots = H.model.slots(cid, code, day, H.now())
        aids = [H.booked(c, email, cid, code, slots[i * 3]) for i in range(5)]
        ctx.shared["W13"] = (c, email, aids)
    expected: dict[int, int] = {}
    with ctx.step("a provider error: the cancellation stands, the refund is pending, then retried with the same key"):
        c, email, aids = ctx.shared["W13"]
        H.pay.fault(refund_create="error")
        r = cancel(c, aids[0])
        m = H.appt_marker(None, aids[0])
        ctx.check(r.status < 400 and m.get("data-status") == "cancelled", f"cancelling while the provider fails answered {r.status}")
        ctx.check(m.get("data-refund-status") in ("pending", "refunded"),  # a quick retry may already have completed it
                  f"refund status right after the failed provider call: {m.get('data-refund-status')!r}")
        expected[aids[0]] = H.model.appt(aids[0]).deposit
        ctx.eq(wait_refunds([aids[0]])[aids[0]].get("data-refund-status"), "refunded", "refund status after the retries")
    with ctx.step("a lost answer: the provider refunds but answers 503; the retry with the same key replays it"):
        c, email, aids = ctx.shared["W13"]
        H.pay.fault(refund_create="lost_response", refund_create_delay=3)
        r = cancel(c, aids[1])
        ctx.check(r.status < 400, f"cancelling answered {r.status}")
        expected[aids[1]] = H.model.appt(aids[1]).deposit
        ctx.eq(wait_refunds([aids[1]])[aids[1]].get("data-refund-status"), "refunded", "refund status after the lost answer")
        time.sleep(5)
    with ctx.step("a timeout: the provider does nothing and answers late; the retry refunds once"):
        c, email, aids = ctx.shared["W13"]
        H.pay.fault(refund_create="timeout", refund_create_delay=4)
        r = cancel(c, aids[2])
        ctx.check(r.status < 400, f"cancelling answered {r.status}")
        expected[aids[2]] = H.model.appt(aids[2]).deposit
        ctx.eq(wait_refunds([aids[2]])[aids[2]].get("data-refund-status"), "refunded", "refund status after the timeout")
    with ctx.step("refund events held back: the refund stays pending until its event arrives, in any order"):
        c, email, aids = ctx.shared["W13"]
        H.pay.hold(["refund.created", "charge.refunded"])
        try:
            for a in aids[3:5]:
                r = cancel(c, a)
                ctx.check(r.status < 400, f"cancelling {a} answered {r.status}")
                expected[a] = H.model.appt(a).deposit
            end = time.monotonic() + 60
            while time.monotonic() < end and len([e for a in aids[3:5] for e in H.pay.events_for(a, "refund.created")]) < 2:
                time.sleep(0.5)
            time.sleep(2)
            sts = [H.appt_marker(None, a).get("data-refund-status") for a in aids[3:5]]
            ctx.eq(sts, ["pending", "pending"], "refund status while the refund events are held")
            ids = [e["id"] for a in reversed(aids[3:5]) for e in H.pay.events_for(a, "refund.created")]
        finally:
            H.pay.release(ids) if ids else None
            H.pay.release()
    with ctx.step("every refund completes exactly once with one Idempotency-Key per refund; one refund email each"):
        c, email, aids = ctx.shared["W13"]
        for a in aids:
            H.model.appt(a).status = "cancelled"
        check_refunds(ctx, expected, wait_refunds(list(expected)), "W13")
        st = H.pay.state()
        ctx.check(len(H.pay.refund_requests(H.payment_intent(aids[0]), st)) >= 2, "the failed refund was not retried")
        H.wait_counts([(email, "Refund issued", len(expected))])
        n = len(H.mails(email, "Refund issued"))
        if n > len(expected):
            ctx.breach("X6", f"{n} refund emails for {len(expected)} refunds")
        ctx.eq(n, len(expected), "refund emails")


# ======================================================================================== W14
@workflow("W14", "Rescheduling: 24-hour window, the original never blocks itself, the deposit carries over", 4, critical=True)
def w14(ctx):
    with ctx.step("clinicians at both clinics; a type whose fee changes mid-way; paid appointments"):
        H.ensure_staff()
        rt = H.room_type("W14")
        for clinic in (SEA, HNL):
            H.create_room(clinic, rt)
            H.create_room(clinic, rt)
        fee, bp = tie_fees(rnd_for("W14"), 1)[0]
        code = H.create_type(30, 10, rt, fee, bp)
        change = H.open_day(SEA, 10, 1)
        H.add_fee(code, change, 9900, 1750)
        a, b = all_day_clinicians(SEA, 2)
        hn = all_day_clinicians(HNL, 1)[0]
        c, email = H.register()
        other, oemail = H.register()
        d = H.quiet_block(SEA, 3, 2)
        x = H.booked(c, email, a, code, H.at(SEA, d, "10:00"))
        ctx.shared["W14"] = (code, change, a, b, hn, c, email, other, oemail, d, x)

    def resched(cl, aid, cid, start, extra=None):
        page = H.appt_page(cl, aid)
        tok = csrf_from(page) or tokens(cl)
        fields = tok + [("clinician_id", str(cid)), ("start", E.iso(start, H.model.tz_of(cid)))] + list((extra or {}).items())
        return cl.post_form(f"/appointments/{aid}/reschedule", fields)

    def new_id(r):
        m = re.search(r"/appointments/(\d+)", r.location or "")
        return int(m.group(1)) if r.status in REDIRECTS and m else None

    def moved(old, cid, start, r):
        nid = new_id(r)
        if nid is None:
            raise StepFailed(f"rescheduling {old} answered {r.status} without redirecting to the new appointment")
        o = H.model.appt(old)
        room = H.appt_marker(None, nid).get("data-room-id")
        H.model.set_status(old, "rescheduled")
        n = H.model.add_booking(nid, cid, o.type_code, int(room) if room else None, start, "confirmed", None, o.deposit)
        n.patient_email, n.patient_name = o.patient_email, o.patient_name
        H.created_appts.append(nid)
        H.sessions[nid] = H.sessions.get(old)
        return nid

    with ctx.step("moving 15 minutes later the same day is allowed: the original does not block itself"):
        code, change, a, b, hn, c, email, other, oemail, d, x = ctx.shared["W14"]
        s = H.at(SEA, d, "10:15")
        ctx.check(s in H.model.slots(a, code, d, H.now(), ignore=x) and s not in H.model.slots(a, code, d, H.now()), "verifier fixture")
        n0 = len(H.pay.state().get("checkout_sessions", {}))
        y = moved(x, a, s, resched(c, x, a, s, {"deposit_cents": "1", "patient_id": "1", "type": "FU30"}))
        m = H.appt_marker(None, y)
        ctx.eq((m.get("data-status"), m.get("data-start") and E.parse_instant(m["data-start"]), m.get("data-deposit-cents"), m.get("data-type")),
               ("confirmed", s, str(H.model.appt(x).deposit), code), "the new appointment (status, start, deposit, type)")
        ctx.eq(H.appt_marker(None, x).get("data-status"), "rescheduled", "the original's status")
        ui_appt(ctx, c, x, set(), "the patient, on the rescheduled original")
        ui_appt(ctx, c, y, {"cancel-appointment", "reschedule"}, "the patient, on the new appointment")
        ctx.eq(len(H.pay.state().get("checkout_sessions", {})), n0, "Checkout Sessions created by rescheduling")
        compare_all(ctx, [(a, code, d)], "w14_same_day")
        ctx.shared["W14_y"] = y
    with ctx.step("moving to another clinician after the fee change carries the deposit paid, not the new fee's"):
        code, change, a, b, hn, c, email, other, oemail, d, x = ctx.shared["W14"]
        y = ctx.shared["W14_y"]
        day = change + timedelta(days=1)
        s = H.first_slot(b, code, day)
        z = moved(y, b, s, resched(c, y, b, s))
        m = H.appt_marker(None, z)
        ctx.eq((m.get("data-deposit-cents"), m.get("data-room-id")), (str(H.model.appt(x).deposit), str(H.model.appt(z).room_id)),
               "deposit and room after the second move")
        ctx.check(H.model.deposit_on(code, day) != H.model.appt(x).deposit, "verifier fixture: fees equal")
        compare_all(ctx, [(a, code, d), (b, code, day)], "w14_moved")
        ctx.shared["W14_z"] = z
    with ctx.step("refused: another clinic, a taken time, another patient, the old system's appointments, under 24 hours"):
        code, change, a, b, hn, c, email, other, oemail, d, x = ctx.shared["W14"]
        z = ctx.shared["W14_z"]
        r = resched(c, z, hn, H.first_slot(hn, code, H.open_day(HNL, 4)))
        ctx.eq(r.status, 409, "moving to a Honolulu clinician")
        busy = H.booked(other, oemail, a, code, H.first_slot(a, code, d + timedelta(days=1)))
        r = resched(c, z, a, H.model.appt(busy).start)
        ctx.eq(r.status, 409, "moving onto a taken time")
        r = resched(other, z, a, H.first_slot(a, code, d + timedelta(days=1)))
        if H.appt_marker(None, z).get("data-status") != "confirmed":
            ctx.breach("X1", f"another patient rescheduled appointment {z}")
        ctx.eq(r.status, 403, "another patient rescheduling")
        # any start under 24 hours away; when a clinic closure leaves none (the next local date is closed), no app
        # can offer one either, so this refusal is not exercised on that run
        st = slot_near(a, code, 20, 0.9, 23.75, required=False)
        soon = None
        if st is None:
            ctx.observe("under_24h_refusal", "not exercised: no slot under 24 hours ahead (clinic closure)")
        else:
            soon = H.booked(c, email, a, code, st)
            ui_appt(ctx, c, soon, {"cancel-appointment"}, f"the patient, {(st - H.now()) / 3600:.1f} h before")
            r = resched(c, soon, a, H.first_slot(a, code, d + timedelta(days=2)))
            ctx.eq(r.status, 409, "rescheduling under 24 hours before the start")
        ctx.shared["W14_soon"] = soon
    with ctx.step("a move to a time a few hours away, then a cancellation, refunds the carried deposit by its band"):
        code, change, a, b, hn, c, email, other, oemail, d, x = ctx.shared["W14"]
        z = ctx.shared["W14_z"]
        s = slot_near(a, code, 6, 3, 20, ignore=z, fallbacks=((1, 23), (25, 70)))
        w = moved(z, a, s, resched(c, z, a, s))
        t0 = H.now()
        r = cancel(c, w)
        t1 = H.now()
        dep = H.model.appt(x).deposit
        allowed = refund_bands(s, t0, t1, dep, False)
        m = H.appt_marker(None, w)
        ctx.check(int(m.get("data-refund-cents", -1)) in allowed, f"refund {m.get('data-refund-cents')} for the carried deposit {dep}, allowed {allowed}")
        H.model.set_status(w, "cancelled")
        ms = wait_refunds([w])
        ctx.eq(ms[w].get("data-refund-status"), "refunded", "refund status")
        pi = H.payment_intent(x)
        ctx.eq([r_["amount"] for r_ in H.pay.refunds_for(pi)], [int(m["data-refund-cents"])], "refunds against the original payment")
    with ctx.step("one reschedule email per move, no new confirmation"):
        code, change, a, b, hn, c, email, other, oemail, d, x = ctx.shared["W14"]
        confirmed = 2 if ctx.shared.get("W14_soon") else 1  # the original booking, and the under-24-hour one when made
        H.wait_counts([(email, "Appointment rescheduled", 3), (email, "Appointment confirmed", confirmed)])
        ctx.eq(len(H.mails(email, "Appointment rescheduled")), 3, "reschedule emails")
        ctx.eq(len(H.mails(email, "Appointment confirmed")), confirmed, "confirmation emails (the original booking and the under-24-hour one)")
    with ctx.step("thirty moves at the daily limit and in single rooms: rooms, deposits and slot lists follow the rules"):
        rnd = rnd_for("W14b")
        rt = H.room_type("W14b")
        for _ in range(4):
            H.create_room(SEA, rt)
        fee, bp = tie_fees(rnd, 1)[0]
        code2 = H.create_type(45, 15, rt, fee, bp)
        later = H.open_day(SEA, 9, 1)
        H.add_fee(code2, later, 13300, 1850)
        d2 = H.quiet_block(SEA, 3, 1)
        clins = []
        for _ in range(10):
            cid = H.create_clinician(SEA, 2)
            H.add_version(cid, d2 - timedelta(days=7), "\n".join(f"{x} 07:00-19:00" for x in E.DAY_NAMES))
            clins.append(cid)
        pc, pe = H.register()
        plan = []
        for k, cid in enumerate(clins):
            first = H.at(SEA, d2, f"{7 + k % 5:02d}:00")
            plan += [(pc, pe, cid, code2, first), (pc, pe, cid, code2, first + 4 * 3600)]
        firsts = H.book_many(plan)
        ctx.check(len(firsts) >= 18, f"verifier fixture: only {len(firsts)} bookings")
        bad = []
        moves = 0
        for aid in firsts:
            o = H.model.appt(aid)
            day = H.model.local_day(o.clinician_id, o.start)
            for target_day, shift in ((day, 900), (later + timedelta(days=1), None)):
                cur = H.model.appt(aid)
                if shift is not None:
                    st = cur.start + shift
                else:
                    opts = H.model.slots(cur.clinician_id, code2, target_day, H.now(), ignore=aid)
                    st = opts[len(opts) // 2] if opts else None
                if st is None or st not in H.model.slots(cur.clinician_id, code2, target_day, H.now(), ignore=aid):
                    continue
                want_room = H.model.predict_room(cur.clinician_id, code2, st, H.now(), ignore=aid)
                r = resched(pc, aid, cur.clinician_id, st)
                nid = new_id(r)
                if nid is None:
                    bad.append(f"moving {aid} to {E.iso(st, H.model.tz_of(cur.clinician_id))} answered {r.status}")
                    break
                mk = H.appt_marker(None, nid)
                got = (mk.get("data-room-id"), mk.get("data-deposit-cents"), mk.get("data-status"))
                want = (str(want_room), str(H.model.appt(aid).deposit), "confirmed")
                aid = moved(aid, cur.clinician_id, st, r)
                H.model.appt(aid).room_id = want_room
                moves += 1
                if got != want:
                    bad.append(f"new appointment {nid}: (room, deposit, status) {got}, expected {want}")
        ctx.observe("moves", moves)
        ctx.check(not bad, f"{len(bad)} moves differ: " + " | ".join(bad[:4]))
        ctx.check(moves >= 25, f"verifier fixture: only {moves} moves")
        compare_all(ctx, [(cid, code2, dd) for cid in clins for dd in (d2, later + timedelta(days=1))], "w14_moves")
        ctx.shared["W14_bulk"] = (pc, pe, moves)


# ======================================================================================== W15
@workflow("W15", "Waitlist: join order, offers that occupy, offer and hold lifetimes, decline and expiry chains", 5, timeout=1200, critical=True)
@evidence_backstop("offer_email", offer_evidence)
def w15(ctx):
    with ctx.step("a full day: two appointments fill a short window; four patients join the waitlist in order"):
        H.ensure_staff()
        rt = H.room_type("W15")
        H.create_room(SEA, rt)
        H.create_room(SEA, rt)
        t60, t30 = H.create_type(60, 0, rt, 16500, 2000), H.create_type(30, 10, rt, 9500, 2000)
        d = H.quiet_block(SEA, 4, 2)
        dn = E.DAY_NAMES[d.isoweekday() - 1]
        w = H.create_clinician(SEA, 10)
        H.add_version(w, d - timedelta(days=7), f"{dn} 09:00-10:50")
        p, pe = H.register()
        x1 = H.booked(p, pe, w, t60, H.at(SEA, d, "09:00"))
        x2 = H.booked(p, pe, w, t30, H.at(SEA, d, "10:00"))
        ctx.eq((H.availability(w, t60, d)[1], H.availability(w, t30, d)[1]), ([], []), "slots on the full day")
        people = {k: H.register(name=f"{k} Waitlister {H.uid()}") for k in ("ana", "ben", "cy", "dee")}
        for k, code in (("ana", t60), ("ben", t30), ("cy", t30), ("dee", t30)):
            c, e = people[k]
            r = submit_form(c, "/waitlist", "join-waitlist", {"clinician_id": str(w), "type": code, "date": d.isoformat()}, follow=False)
            ctx.check(r.status < 400, f"{k} joining the waitlist answered {r.status}")
            time.sleep(0.3)
        c, e = people["ben"]
        r = c.post_form("/waitlist", tokens(c) + [("clinician_id", str(w)), ("type", t30), ("date", d.isoformat())])
        ctx.eq(r.status, 409, "joining the same waitlist twice")
        r = c.post_form("/waitlist", tokens(c) + [("clinician_id", str(w)), ("type", t30),
                                                 ("date", (H.today(SEA) + timedelta(days=H.horizon + 3)).isoformat())])
        ctx.eq(r.status, 400, "joining for a date beyond the horizon")
        ctx.shared["W15"] = (t60, t30, d, w, p, pe, x1, x2, people)

    def entries(k):
        c, e = ctx.shared["W15"][8][k]
        r = c.get("/waitlist", follow=True)
        return [n.attrs.get("data-entry-status") for n in r.dom.attr_all("data-entry-status")]

    def offer_link(k, since: set, timeout=EFFECT_WAIT):
        c, e = ctx.shared["W15"][8][k]
        msgs = H.mail.wait_for(e, lambda m: m["id"] not in since and "a slot opened up" in m["subject"].lower(), 1, timeout)
        for m in msgs:
            for l in body_links(m):
                if "/offers/" in l:
                    return l.rstrip(".,)"), m
        return None, None

    with ctx.step("a cancellation frees 40 minutes: the FU30 waiter first in line gets the offer, the INIT60 waiter keeps waiting"):
        t60, t30, d, w, p, pe, x1, x2, people = ctx.shared["W15"]
        seen = {k: H.mail_ids(people[k][1]) for k in people}
        ctx.shared["W15_seen"] = seen
        t_cancel = H.now()
        r = cancel(p, x2)
        H.model.set_status(x2, "cancelled")
        link, msg = offer_link("ben", seen["ben"])
        if msg:  # judge evidence before the strict checks below (common rule 12)
            B.capture_text("offer_email", f"Subject: {msg['subject']}\n\n{msg['text'] or visible_text(msg['html'])}",
                           {"start": E.iso(H.at(SEA, d, '10:00'), 'America/Los_Angeles')})
        ctx.check(link, f"Ben received no offer email within {EFFECT_WAIT:.0f} s of the cancellation")
        ctx.check(link.startswith(H.base + "/offers/"), f"the offer link does not start with PUBLIC_BASE_URL/offers/: {link}")
        page = people["ben"][0].get(link, follow=True)
        node = page.dom.attr_one("data-offer-status")
        ctx.check(node is not None and node.attrs.get("data-offer-status") == "offered"
                  and E.parse_instant(node.attrs.get("data-start", "1970-01-01T00:00:00Z")) == H.at(SEA, d, "10:00"),
                  f"Ben's offer page shows {node.attrs if node else None}")
        ctx.eq(sorted(actions(page) & {"accept-offer", "decline-offer"}), ["accept-offer", "decline-offer"], "actions on an open offer")
        ctx.eq((entries("ana"), entries("ben"), entries("cy")), (["waiting"], ["offered"], ["waiting"]), "entry statuses")
        ctx.eq(H.availability(w, t30, d)[1], [], "FU30 slots while the offer stands")
        for k in ("ana", "cy", "dee"):
            ctx.eq(len([m for m in H.mail.to(people[k][1]) if m["id"] not in seen[k] and "slot opened" in m["subject"].lower()]), 0,
                   f"offer emails to {k}")
        ctx.shared["W15_ben"] = (link, t_cancel)  # the offer was made during the cancellation request
    with ctx.step("someone else cannot accept the offer; when it expires the next FU30 waiter gets one"):
        t60, t30, d, w, p, pe, x1, x2, people = ctx.shared["W15"]
        link, t_offer = ctx.shared["W15_ben"]
        token_path = urllib.parse.urlsplit(link).path
        c, e = people["cy"]
        r = c.post_form(token_path + "/accept", tokens(c))
        if r.status in REDIRECTS and "/pay/" in (r.location or ""):
            ctx.breach("X1", "a patient accepted another patient's waitlist offer")
        ctx.eq(r.status, 403, "another patient accepting the offer")
        time.sleep(max(0.0, t_offer + H.offer_ttl + 2 - H.now()))
        link2, msg = offer_link("cy", ctx.shared["W15_seen"]["cy"])
        ctx.check(link2, f"Cy received no offer within {EFFECT_WAIT:.0f} s of Ben's offer expiring")
        ctx.shared["W15_cy"] = (link2, H.now())
        c, e = people["ben"]
        page = c.get(link, follow=True)
        node = page.dom.attr_one("data-offer-status")
        ctx.eq((node.attrs.get("data-offer-status") if node else None, sorted(actions(page) & {"accept-offer", "decline-offer"})),
               ("expired", []), "an expired offer's status and actions")
        r = c.post_form(token_path + "/accept", tokens(c))
        ctx.eq(r.status, 409, "accepting an expired offer")
        ctx.eq((entries("ana"), entries("ben"), entries("cy"), entries("dee")), (["waiting"], ["expired"], ["offered"], ["waiting"]),
               "entry statuses after the expiry")
    with ctx.step("Cy accepts the offer, pays, and the booking is confirmed"):
        t60, t30, d, w, p, pe, x1, x2, people = ctx.shared["W15"]
        link2, t_seen = ctx.shared["W15_cy"]
        c, e = people["cy"]
        page = c.get(link2, follow=True)
        r = submit_form(c, "", "accept-offer", {}, page=page)
        t_acc = H.now()
        mm = re.search(r"/pay/(cs_[A-Za-z0-9_]+)", r.location or "")
        ctx.check(r.status in REDIRECTS and mm, f"accepting answered {r.status} {r.location}")
        sid = mm.group(1)
        aid = int(H.pay.session(sid)["metadata"]["appointment_id"])
        H.sessions[aid] = sid
        H.created_appts.append(aid)
        H.model.add_booking(aid, w, t30, int(H.appt_marker(None, aid).get("data-room-id") or 0) or None, H.at(SEA, d, "10:00"),
                            "held", None, H.model.deposit_on(t30, d), expires=t_acc + H.hold_ttl)
        ctx.eq(H.appt_marker(None, aid).get("data-status"), "held", "status right after accepting")
        H.confirm(aid)
        ctx.eq(entries("cy"), ["booked"], "Cy's entry")
        page = c.get(link2, follow=True)
        node = page.dom.attr_one("data-offer-status")
        ctx.eq((node.attrs.get("data-offer-status") if node else None, sorted(actions(page) & {"accept-offer", "decline-offer"})),
               ("accepted", []), "an accepted offer's status and actions")
    with ctx.step("another cancellation offers to Ana first; her decline passes it to Dee, the next in line"):
        t60, t30, d, w, p, pe, x1, x2, people = ctx.shared["W15"]
        seen = {k: H.mail_ids(people[k][1]) for k in people}
        cancel(p, x1)
        H.model.set_status(x1, "cancelled")
        link_a, _ = offer_link("ana", seen["ana"])
        ctx.check(link_a, "Ana received no offer after the 09:00 cancellation")
        c, e = people["ana"]
        page = c.get(link_a, follow=True)
        ctx.eq(page.dom.attr_one("data-offer-status").attrs.get("data-type") if page.dom.attr_one("data-offer-status") else None, t60, "Ana's offer type")
        t_decline = H.now()
        submit_form(c, "", "decline-offer", {}, page=page, follow=True)
        t_made = H.now()  # Dee's offer is made by the decline request, so by now at the latest
        link_d, _ = offer_link("dee", seen["dee"])
        ctx.check(link_d, "Dee received no offer after Ana declined")
        page = people["dee"][0].get(link_d, follow=True)
        node = page.dom.attr_one("data-offer-status")
        ctx.check(node is not None and E.parse_instant(node.attrs.get("data-start", "1970-01-01T00:00:00Z")) == H.at(SEA, d, "09:00"),
                  f"Dee's offer is for {node.attrs.get('data-start') if node else None}, expected 09:00")
        ctx.eq((entries("ana"), entries("dee")), (["declined"], ["offered"]), "entry statuses after the decline")
        ctx.shared["W15_dee"] = (link_d, t_decline, t_made)
    with ctx.step("Dee accepts late in her offer; the hold then lasts HOLD_TTL_SECONDS from acceptance and the payment confirms"):
        t60, t30, d, w, p, pe, x1, x2, people = ctx.shared["W15"]
        link_d, t_decline, t_made = ctx.shared["W15_dee"]
        # accept well inside the offer's lifetime (counted from the decline that made it), late enough that the
        # offer's own expiry falls inside the hold that acceptance starts
        target = t_decline + H.offer_ttl - 12
        time.sleep(max(0.0, target - H.now()))
        ctx.check(H.now() < t_decline + H.offer_ttl - 4, "verifier fixture: too slow to accept inside the offer's lifetime")
        c, e = people["dee"]
        page = c.get(link_d, follow=True)
        t_acc0 = H.now()  # the hold starts during the accept request: no earlier than this
        r = submit_form(c, "", "accept-offer", {}, page=page)
        t_acc = H.now()
        mm = re.search(r"/pay/(cs_[A-Za-z0-9_]+)", r.location or "")
        ctx.check(r.status in REDIRECTS and mm, f"accepting answered {r.status} {r.location}")
        sid = mm.group(1)
        aid = int(H.pay.session(sid)["metadata"]["appointment_id"])
        H.sessions[aid] = sid
        H.created_appts.append(aid)
        H.model.add_booking(aid, w, t30, int(H.appt_marker(None, aid).get("data-room-id") or 0) or None, H.at(SEA, d, "09:00"),
                            "held", None, H.model.deposit_on(t30, d), expires=t_acc + H.hold_ttl)
        time.sleep(max(0.0, t_made + H.offer_ttl + 4 - H.now()))
        ctx.check(H.now() < t_acc0 + H.hold_ttl - 3, "verifier fixture: too slow to test the hold lifetime")
        ctx.eq(H.appt_marker(None, aid).get("data-status"), "held", "status after the offer's own expiry time passed")
        H.confirm(aid)
        ctx.eq(entries("dee"), ["booked"], "Dee's entry")
        time.sleep(3)
        counts = {k: len([m for m in H.mail.to(people[k][1]) if "slot opened" in m["subject"].lower()]) for k in people}
        ctx.eq(counts, {"ana": 1, "ben": 1, "cy": 1, "dee": 1}, "offer emails per patient")
    with ctx.step("offers accepted after a fee change pay the deposit in effect when the hold is created"):
        rnd = rnd_for("W15b")
        rt2 = H.room_type("W15b")
        for _ in range(4):
            H.create_room(SEA, rt2)
        D0 = H.quiet_block(SEA, 6, 3)
        fill, fe = H.register()
        bad, n, skipped, applied = [], 0, 0, 0
        for k, shift in enumerate((0, -2, 1)):  # new fee effective on the date, two days before it, the day after it
            D = D0 + timedelta(days=k)
            dn = E.DAY_NAMES[D.isoweekday() - 1]
            fee0, bp0 = tie_fees(rnd, 1)[0]
            code_k = H.create_type(30, 0, rt2, fee0, bp0)
            batch = []
            for _ in range(4):
                cl = H.create_clinician(SEA, 5)
                H.add_version(cl, D - timedelta(days=7), f"{dn} 09:00-09:30")
                batch.append(cl)
            fillers = H.book_many([(fill, fe, cl, code_k, H.at(SEA, D, "09:00")) for cl in batch])
            ctx.check(len(fillers) == len(batch), "verifier fixture: the one-slot days were not all booked")
            waiters = []
            for cl in batch:
                wc, we = H.register()
                seen = H.mail_ids(we)
                r = submit_form(wc, "/waitlist", "join-waitlist", {"clinician_id": str(cl), "type": code_k, "date": D.isoformat()})
                ctx.check(r.status < 400, f"joining the waitlist answered {r.status}")
                waiters.append((cl, wc, we, seen))
            t_made = []
            for aid in fillers:
                cancel(fill, aid)
                t_made.append(H.now())
                H.model.set_status(aid, "cancelled")
            links = []
            for cl, wc, we, seen in waiters:
                msgs = H.mail.wait_for(we, lambda m_: m_["id"] not in seen and "slot opened" in (m_["subject"] or "").lower(), 1, EFFECT_WAIT)
                link = next((l for m_ in msgs for l in body_links(m_) if "/offers/" in l), None)
                ctx.check(link, f"no offer email after the slot of clinician {cl} opened")
                links.append(link)
            for i, (link, (cl, wc, we, seen)) in enumerate(zip(links, waiters)):
                if i == 1:  # the first offer is accepted before the fee changes; the rest after
                    new_fee, new_bp = tie_fees(rnd, 1)[0]
                    H.add_fee(code_k, D + timedelta(days=shift), new_fee, new_bp)
                if H.now() - t_made[i] > H.offer_ttl - 8:
                    skipped += 1  # verifier too slow to accept well inside the offer's lifetime: not the app's fault
                    continue
                want = H.model.deposit_on(code_k, D)
                page = wc.get(link, follow=True)
                t = H.now()
                r = submit_form(wc, "", "accept-offer", {}, page=page)
                mm = re.search(r"/pay/(cs_[A-Za-z0-9_]+)", r.location or "")
                if not (r.status in REDIRECTS and mm):
                    bad.append(f"accepting the offer for clinician {cl} answered {r.status}")
                    continue
                sess = H.pay.session(mm.group(1))
                aid = int(sess["metadata"]["appointment_id"])
                H.record_hold(aid, cl, code_k, H.at(SEA, D, "09:00"), mm.group(1), we, t)
                got = (sess.get("amount_total"), H.appt_marker(None, aid).get("data-deposit-cents"))
                n += 1
                applied += 1 if (i >= 1 and shift <= 0) else 0
                if got != (want, str(want)):
                    bad.append(f"offer for clinician {cl} on {D} (new fee row {'added before' if i else 'not yet added at'} acceptance, effective "
                               f"{D + timedelta(days=shift)}): Checkout amount and deposit {got}, expected {want}")
        ctx.observe("fee_change_offers", n)
        ctx.observe("fee_change_offers_skipped", skipped)
        ctx.check(not bad, f"{len(bad)} of {n} accepted offers differ: " + " | ".join(bad[:4]))
        ctx.check(n >= 9 and applied >= 3, f"verifier fixture: only {n} offers accepted in time ({applied} with a new fee in effect)")


# ======================================================================================== W16
@workflow("W16", "Clinical notes and clinician scope: only the treating clinician and admins, authors from the session", 4)
def w16(ctx):
    with ctx.step("two clinicians, a patient with one appointment each, accounts for everyone"):
        H.ensure_staff()
        rt = H.room_type("W16")
        H.create_room(SEA, rt)
        code = H.create_type(30, 0, rt, 9000, 2000)
        c1, c2 = all_day_clinicians(SEA, 2)
        p, pe = H.register()
        q, qe = H.register()
        day = H.open_day(SEA, 5)
        a1 = H.booked(p, pe, c1, code, H.first_slot(c1, code, day))
        a2 = H.booked(p, pe, c2, code, H.first_slot(c2, code, day))
        k1, k2 = H.account(H.clinician_email(c1)), H.account(H.clinician_email(c2))
        ctx.shared["W16"] = (code, c1, c2, p, q, day, a1, a2, k1, k2)
    secret = f"Left knee effusion, confidential {secrets.token_hex(4)}"
    with ctx.step("the treating clinician adds a note; its author is the signed-in clinician whatever the form says"):
        code, c1, c2, p, q, day, a1, a2, k1, k2 = ctx.shared["W16"]
        r = submit_form(k1, f"/appointments/{a1}/notes", "add-note", {"body": secret}, follow=True,
                        override={"author_id": "1", "appointment_id": str(a2)})
        ctx.check(r.status < 400, f"adding a note answered {r.status}")
        rows = H.q("SELECT appointment_id, author_id FROM clinical_notes WHERE body = %s", (secret,))
        want_author = H.model.w.clinicians[c1].user_id
        if rows and (rows[0]["author_id"] != want_author or rows[0]["appointment_id"] != a1):
            ctx.breach("X4", f"a note's author or appointment came from the form: {rows[0]}")
        ctx.eq([(x["appointment_id"], x["author_id"]) for x in rows], [(a1, want_author)], "the note's appointment and author")
        page = k1.get(f"/appointments/{a1}/notes", follow=True)
        ctx.check(secret in visible_text(page.text) and page.dom.attr_one("data-note-id") is not None, "the note is not on its notes page")
    with ctx.step("other clinicians, front desk, and the patient get 403 and never see the note; an admin sees it"):
        code, c1, c2, p, q, day, a1, a2, k1, k2 = ctx.shared["W16"]
        for label, cl in (("another clinician", k2), ("front desk", H.desk), ("the patient", p), ("another patient", q)):
            r = cl.get(f"/appointments/{a1}/notes", follow=True)
            if secret in r.text:
                ctx.breach("X1", f"{label} read a clinical note")
            ctx.eq(r.status, 403, f"{label} reading the notes")
        r = H.admin.get(f"/appointments/{a1}/notes", follow=True)
        ctx.check(r.status == 200 and secret in visible_text(r.text), "an admin cannot read the note")
        ctx.eq(sorted(actions(r) & {"add-note"}), [], "actions offered to an admin on the notes page")
        ctx.eq(sorted(actions(k1.get(f"/appointments/{a1}/notes", follow=True)) & {"add-note"}), ["add-note"],
               "actions offered to the treating clinician on the notes page")
        n0 = H.q("SELECT count(*) AS n FROM clinical_notes")[0]["n"]
        for label, cl in (("another clinician", k2), ("an admin", H.admin), ("the patient", p)):
            r = cl.post_form(f"/appointments/{a1}/notes", tokens(cl) + [("body", "intrusion")])
            ctx.eq(r.status, 403, f"{label} adding a note")
        r = submit_form(k1, f"/appointments/{a1}/notes", "add-note", {"body": "token-less"}, drop_csrf=True)
        ctx.eq(r.status, 403, "a token-less note")
        n1 = H.q("SELECT count(*) AS n FROM clinical_notes")[0]["n"]
        if n1 != n0:
            ctx.breach("X2" if r.status < 400 else "X1", "a note was added by someone other than the treating clinician or without a token")
        ctx.eq(n1, n0, "notes after refused additions")
    with ctx.step("an old system's clinician reads exactly the old notes of their appointment"):
        notes = H.model.notes
        aid = next(a for a in sorted(notes) if len(notes[a]) >= 1 and H.model.w.appts[a].legacy)
        cid = H.model.w.appts[aid].clinician_id
        k = H.account(H.model.users[H.model.w.clinicians[cid].user_id]["email"])
        page = k.get(f"/appointments/{aid}/notes", follow=True)
        ids = sorted(int(n.attrs["data-note-id"]) for n in page.dom.attr_all("data-note-id"))
        ctx.eq(ids, sorted(n["id"] for n in notes[aid]), f"note ids on appointment {aid}")
        txt = visible_text(page.text)
        ctx.check(all(visible_text(n["body"].replace("<", "&lt;")) in txt for n in notes[aid]), "an old note's text is missing")
    with ctx.step("day sheets and appointment pages are scoped: each clinician sees only their own"):
        code, c1, c2, p, q, day, a1, a2, k1, k2 = ctx.shared["W16"]
        r = k1.get(f"/clinician/day?date={day.isoformat()}", follow=True)
        ctx.eq(sorted(page_appt_ids(r)), [a1], "clinician 1's day sheet")
        r = k1.get(f"/appointments/{a2}", follow=True)
        if r.dom.attr_one("data-appointment-id", str(a2)) is not None:
            ctx.breach("X1", "a clinician opened another clinician's appointment")
        ctx.eq(r.status, 403, "clinician 1 opening clinician 2's appointment")
        r = q.get(f"/appointments/{a1}", follow=True)
        if r.dom.attr_one("data-appointment-id", str(a1)) is not None:
            ctx.breach("X1", "a patient opened another patient's appointment")
        ctx.eq(r.status, 403, "another patient opening the appointment")


# ======================================================================================== W17
@workflow("W17", "Reminders: every booked appointment of a clinic's day once, quirky addresses normalized, never twice", 3)
def w17(ctx):
    m = H.model
    with ctx.step("pick a Honolulu day ahead with old bookings, including quirky and inactive patients; add new bookings"):
        H.ensure_staff()
        lo, hi = H.today(HNL) + timedelta(days=1), H.today(HNL) + timedelta(days=min(H.horizon, 120))
        best, score = None, -1
        days: dict = {}
        for a in m.w.appts.values():
            if a.legacy and a.status == "booked" and m.w.clinicians[a.clinician_id].clinic_id == HNL:
                dd = E.local_date(a.start, m.w.clinics[HNL])
                if lo <= dd <= hi:
                    u = m.users[a.patient_id]
                    days.setdefault(dd, [0, 0, 0])
                    days[dd][0] += 1
                    days[dd][1] += u["email"] != norm(u["email"])
                    days[dd][2] += not u["active"]
        for dd, (n, quirky, inactive) in days.items():
            s = quirky * 3 + inactive * 5 + min(n, 30) / 10
            if s > score:
                best, score = dd, s
        ctx.check(best is not None, "verifier fixture: no old Honolulu bookings ahead")
        rt = H.room_type("W17")
        H.create_room(HNL, rt)
        code = H.create_type(45, 0, rt, 9000, 2000)
        cid = all_day_clinicians(HNL, 1)[0]
        p, pe = H.register()
        q, qe = H.register()
        b1 = H.booked(p, pe, cid, code, H.first_slot(cid, code, best))
        b2 = H.booked(q, qe, cid, code, H.model.slots(cid, code, best, H.now())[3])
        held = H.hold(q, qe, cid, code, H.model.slots(cid, code, best, H.now())[6])
        gone = H.booked(p, pe, cid, code, H.model.slots(cid, code, best, H.now())[9])
        cancel(p, gone)
        H.model.set_status(gone, "cancelled")
        ctx.shared["W17"] = (best, cid, code, p, pe, q, qe)
        ctx.observe("day", best.isoformat())
    with ctx.step("a patient cannot send reminders; front desk sends one per booked appointment with an active patient"):
        best, cid, code, p, pe, q, qe = ctx.shared["W17"]
        before = {x["id"] for x in H.mail.summaries()}
        r = p.post_form("/desk/reminders", tokens(p) + [("clinic_id", str(HNL)), ("date", best.isoformat())])
        ctx.eq(r.status, 403, "a patient sending reminders")
        time.sleep(2)
        ctx.eq([x for x in H.mail.summaries() if x["id"] not in before and "reminder" in x["subject"].lower()], [], "reminders after a refused request")
        sheet = m.day_sheet(HNL, best)
        due = [a for a in sheet if a.status in ("booked", "confirmed") and m.users.get(a.patient_id, {"active": True})["active"]]
        r = submit_form(H.desk_for(HNL), "/desk/reminders", "send-reminders", {"clinic_id": str(HNL), "date": best.isoformat()}, follow=True)
        node = r.dom.attr_one("data-reminders-sent")
        ctx.eq(node.attrs.get("data-reminders-sent") if node else None, str(len(due)), "data-reminders-sent")
        addr = {}
        for a in due:
            e = H.patient_email.get(a.aid) or norm(m.users[a.patient_id]["email"])
            addr.setdefault(e, []).append(a)
        end = time.monotonic() + EFFECT_WAIT
        while time.monotonic() < end:
            got = [x for x in H.mail.summaries() if x["id"] not in before and "appointment reminder" in x["subject"].lower()]
            if len(got) >= len(due):
                break
            time.sleep(1)
        per = {}
        for x in got:
            for t in x["to"]:
                per[t] = per.get(t, 0) + 1
        ctx.eq(per, {e: len(v) for e, v in addr.items()}, "reminders per (normalized) address")
        one = next(a for a in due if not a.legacy)
        mail = [x for x in H.mail.to(pe) if "appointment reminder" in x["subject"].lower()][-1]
        loc = E.local_of(one.start, m.w.clinics[HNL])
        # R11.2: the reminder "names the clinic": its name (any case) or the city that identifies it
        names_clinic = m.clinic_names[HNL].lower() in body_of(mail).lower() or "honolulu" in body_of(mail).lower()
        ctx.check(loc.strftime("%Y-%m-%d") in body_of(mail) and loc.strftime("%H:%M") in body_of(mail) and names_clinic,
                  "the reminder does not name the clinic and the local date and time")
        ctx.shared["W17_before"] = before
    with ctx.step("sending again reaches nobody; a booking made since gets exactly one"):
        best, cid, code, p, pe, q, qe = ctx.shared["W17"]
        r = submit_form(H.desk_for(HNL), "/desk/reminders", "send-reminders", {"clinic_id": str(HNL), "date": best.isoformat()}, follow=True)
        node = r.dom.attr_one("data-reminders-sent")
        ctx.eq(node.attrs.get("data-reminders-sent") if node else None, "0", "reminders sent the second time")
        late = H.booked(q, qe, cid, code, H.model.slots(cid, code, best, H.now())[-1])
        r = submit_form(H.desk_for(HNL), "/desk/reminders", "send-reminders", {"clinic_id": str(HNL), "date": best.isoformat()}, follow=True)
        node = r.dom.attr_one("data-reminders-sent")
        ctx.eq(node.attrs.get("data-reminders-sent") if node else None, "1", "reminders after one new booking")
        H.wait_counts([(qe, "Appointment reminder", 2)])
        n = len([x for x in H.mail.to(qe) if "appointment reminder" in x["subject"].lower()])
        ctx.eq(n, 2, "reminders to the patient with two booked appointments")
    with ctx.step("the clinic's day sheet lists exactly the day's appointments in time order"):
        best, cid, code, p, pe, q, qe = ctx.shared["W17"]
        r = H.desk_for(HNL).get(f"/desk/day?clinic_id={HNL}&date={best.isoformat()}", follow=True)
        got = [(int(n.attrs["data-appointment-id"]), E.parse_instant(n.attrs.get("data-start", "1970-01-01T00:00:00Z")))
               for n in r.dom.attr_all("data-appointment-id")]
        want = [(a.aid, a.start) for a in m.day_sheet(HNL, best)]
        ctx.eq(sorted(got), sorted(want), "day sheet rows")
        ctx.check([s for _, s in got] == sorted(s for _, s in got), "the day sheet is not in time order")

# ======================================================================================== W18
REFLECT = "<img src=x onerror=\"window.__fk_xss&&__fk_xss('reflect')\">"
CHECKPOINTS = ["slot_list", "checkout_return", "appointment_confirmed", "cancel_after", "hold_expired", "offer_page",
               "clinician_day", "confirmation_email", "cancellation_email", "offer_email"]


@workflow("W18", "In a real browser: book and pay on the hosted page, layout at 1280 and 390 px, escaping everywhere", 3, timeout=1500)
def w18(ctx):
    br = None
    try:
        # the script payloads are created only just before the escaping step, so an app that stumbles on them
        # loses that step and not the judge evidence (common rule 12)
        with ctx.step("a clean booking setup: a clinician, a type and two rooms, a patient, and an expiring hold"):
            H.ensure_staff()
            rt = H.room_type("W18")
            H.create_room(SEA, rt)
            H.create_room(SEA, rt)  # two rooms: the day sheet's bookings and the waitlist's only slot overlap
            clean = H.create_type(45, 15, rt, 12550, 2000, name="Sports injury assessment")
            doc = H.create_clinician(SEA, 20, name="Rowan Ellis")
            day = H.quiet_block(SEA, 6, 1)
            dn = E.DAY_NAMES[day.isoweekday() - 1]
            H.add_version(doc, day - timedelta(days=7), "\n".join(f"{d} 08:00-16:00" for d in E.DAY_NAMES))
            offerer = H.create_clinician(SEA, 20, name="Kai Mahoe")
            H.add_version(offerer, day - timedelta(days=7), f"{dn} 09:00-09:45")
            pc, pe = H.register(name="Ana Lopez")
            exp = H.hold(pc, pe, doc, clean, H.first_slot(doc, clean, day + timedelta(days=1)))
            ctx.shared["W18"] = (clean, doc, offerer, day, pc, pe, exp)
        # judge evidence (common rule 12): each page is captured as soon as it is shown, before the strict checks on
        # it, and the second evidence step runs even when the first fails; the layout and escaping steps come after
        failed = None
        try:
            with ctx.step("a patient signs in through the form, picks a slot, and pays on the provider's hosted page"):
                clean, doc, offerer, day, pc, pe, exp = ctx.shared["W18"]
                br = B.Browser()
                cx = br.context()
                page = br.page(cx)
                page.goto(H.base + "/signin")
                page.fill("input[name=email]", pe)
                page.fill("input[name=password]", PASSWORD)
                page.click("[data-action=signin]")
                try:
                    page.wait_for_selector("[data-action=signout]", timeout=60000)
                except Exception:  # noqa: BLE001 - reported by the check below
                    pass
                signed_in = page.locator("[data-action=signout]").count() >= 1
                page.goto(H.base + f"/book?clinician_id={doc}&type={clean}&date={day.isoformat()}")
                page.wait_for_load_state()
                B.capture(page, "slot_list", {"clinic": H.model.clinic_names[SEA], "timezone": "America/Los_Angeles",
                                              "slots": len(H.model.slots(doc, clean, day, H.now()))})
                ctx.check(signed_in, "no sign-out button after browser sign-in")
                first = H.model.slots(doc, clean, day, H.now())[0]
                H.pay.hold(["checkout.session.completed"])
                try:
                    page.locator("[data-action=book-slot]").first.click()
                    try:  # an app may confirm first: wait for the navigation to the hosted page, not just a load event
                        page.wait_for_url(re.compile(r".*/pay/cs_.*"), timeout=60000)
                    except Exception:  # noqa: BLE001 - reported by the check below
                        pass
                    ctx.check("/pay/" in page.url, f"clicking a slot did not reach the hosted payment page: {page.url}")
                    sid = re.search(r"/pay/(cs_[A-Za-z0-9_]+)", page.url).group(1)
                    aid = int(H.pay.session(sid)["metadata"]["appointment_id"])
                    H.record_hold(aid, doc, clean, first, sid, pe, H.now())
                    page.fill("#card_number", CARD_OK)
                    page.click("#pay")
                    try:
                        page.wait_for_url(re.compile(rf".*/appointments/{aid}\b.*"), timeout=60000)
                    except Exception:  # noqa: BLE001 - reported by the check below
                        pass
                    B.capture(page, "checkout_return", {"status": "held", "waiting_for": "payment confirmation webhook",
                                                        "hold_ttl_seconds": H.hold_ttl})
                    returned_to = page.url  # asserted at the end of the step, after the later captures
                finally:
                    H.pay.release()
                m = H.wait_status(aid, "confirmed", 60)
                page.goto(H.base + f"/appointments/{aid}")
                page.wait_for_load_state()
                dep = H.model.appt(aid).deposit
                B.capture(page, "appointment_confirmed", {"status": "confirmed", "deposit_cents": dep, "hours_before": round((first - H.now()) / 3600, 1),
                                                          "refund_if_cancelled_now_cents": E.refund(dep, first - H.now())})
                ctx.eq(m.get("data-status"), "confirmed", "status after the hosted payment")
                H.model.set_status(aid, "confirmed")
                page.click("[data-action=cancel-appointment]")
                H.wait_status(aid, "cancelled", 60)  # an app may confirm first; judge by the server-side state, then reload
                page.goto(H.base + f"/appointments/{aid}")
                page.wait_for_load_state()
                mk = H.appt_marker(None, aid)
                B.capture(page, "cancel_after", {"status": "cancelled", "cancelled_by": "patient", "deposit_cents": dep,
                                                 "refund_cents": int(mk.get("data-refund-cents") or 0)})
                ctx.eq(page.locator(f"[data-appointment-id='{aid}']").first.get_attribute("data-status"), "cancelled", "status after clicking cancel")
                H.model.set_status(aid, "cancelled")
                ctx.shared["W18_page"] = (cx, page, aid)
                ctx.check(f"/appointments/{aid}" in returned_to, f"after paying the browser was at {returned_to}")
        except StepFailed as e:
            failed = e
        try:
            with ctx.step("an expired hold, a clinician's day sheet, and a waitlist offer as people see them"):
                clean, doc, offerer, day, pc, pe, exp = ctx.shared["W18"]
                br = br or B.Browser()
                pcx = br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in pc.cookies.items()])
                hp = br.page(pcx)
                time.sleep(max(0.0, H.model.appt(exp).expires + 3 - H.now()))
                H.model.set_status(exp, "expired")
                H.wait_status(exp, "expired", 15)
                hp.goto(H.base + f"/appointments/{exp}")
                hp.wait_for_load_state()
                B.capture(hp, "hold_expired", {"status": "expired", "hold_ttl_seconds": H.hold_ttl})
                exp_shown = hp.locator(f"[data-appointment-id='{exp}']").first.get_attribute("data-status")  # asserted after the captures
                qc, qe = H.register(name="Ben Okafor")
                kd = H.account(H.clinician_email(doc))
                H.booked(pc, pe, doc, clean, H.model.slots(doc, clean, day, H.now())[4])
                H.booked(qc, qe, doc, clean, H.model.slots(doc, clean, day, H.now())[8])
                kcx = br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in kd.cookies.items()])
                kp = br.page(kcx)
                kp.goto(H.base + f"/clinician/day?date={day.isoformat()}")
                kp.wait_for_load_state()
                sheet = [a for a in H.model.day_sheet(SEA, day) if a.clinician_id == doc]
                B.capture(kp, "clinician_day", {"date": day.isoformat(), "appointments": len(sheet),
                                                "local_times": [E.local_of(a.start, H.model.tz_of(doc)).strftime("%H:%M") for a in sheet]})
                only = H.booked(qc, qe, offerer, clean, H.first_slot(offerer, clean, day))
                wc, we = H.register(name="Mei Tanaka")
                submit_form(wc, "/waitlist", "join-waitlist", {"clinician_id": str(offerer), "type": clean, "date": day.isoformat()})
                seen = H.mail_ids(we)
                cancel(qc, only)
                H.model.set_status(only, "cancelled")
                msgs = H.mail.wait_for(we, lambda m: m["id"] not in seen and "slot opened" in m["subject"].lower(), 1, EFFECT_WAIT)
                link = next((l for m in msgs for l in body_links(m) if "/offers/" in l), None)
                ctx.check(link, "no waitlist offer email")
                wcx = br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in wc.cookies.items()])
                wp = br.page(wcx)
                wp.goto(link.rstrip(".,)"))
                wp.wait_for_load_state()
                B.capture(wp, "offer_page", {"offer_status": "offered", "offer_ttl_seconds": H.offer_ttl, "hold_ttl_seconds": H.hold_ttl})
                ctx.shared["W18_more"] = (wp, kp, kcx)
                ctx.eq(exp_shown, "expired", "status of the expired hold")
        except StepFailed as e:
            failed = failed or e
        if failed is not None:
            raise failed
        with ctx.step("every page stays within 1280 and 390 CSS pixels without horizontal scrolling"):
            clean, doc, offerer, day, pc, pe, exp = ctx.shared["W18"]
            cx, page, aid = ctx.shared["W18_page"]
            urls = [f"/book?clinician_id={doc}&type={clean}&date={day.isoformat()}", f"/appointments/{aid}", f"/appointments/{exp}",
                    "/appointments", "/waitlist", "/profile", "/book"]
            for u in urls:
                for wdt in (1280, 390):
                    page.set_viewport_size({"width": wdt, "height": 900})
                    page.goto(H.base + u)
                    page.wait_for_load_state()
                    ov = B.horizontal_overflow(page)
                    ctx.check(ov <= 2, f"{u} overflows by {ov}px at {wdt}px")
            wp, kp, kcx = ctx.shared["W18_more"]
            for p_ in (wp, kp):
                for wdt in (1280, 390):
                    p_.set_viewport_size({"width": wdt, "height": 900})
                    p_.reload()
                    p_.wait_for_load_state()
                    ov = B.horizontal_overflow(p_)
                    ctx.check(ov <= 2, f"{p_.url} overflows by {ov}px at {wdt}px")
        with ctx.step("a clinician, a type, a room, a patient and a note carrying script payloads, and a booking with them"):
            clean, doc, offerer, day, pc, pe, exp = ctx.shared["W18"]
            xrt = H.room_type("W18x")
            H.create_room(SEA, xrt, name="Treatment " + B.payload("room", 2))
            dirty = H.create_type(30, 0, xrt, 9000, 2000, name="Massage " + B.payload("type", 1))
            xdoc = H.create_clinician(SEA, 20, name=B.payload("clin", 0))
            H.add_version(xdoc, day - timedelta(days=7), "\n".join(f"{d} 08:00-16:00" for d in E.DAY_NAMES))
            xc, xe = H.register(name=B.payload("patient", 3))
            x1 = H.booked(xc, xe, xdoc, dirty, H.first_slot(xdoc, dirty, day))
            kx = H.account(H.clinician_email(xdoc))
            submit_form(kx, f"/appointments/{x1}/notes", "add-note", {"body": "Note " + B.payload("note", 0)}, follow=True)
            ctx.shared["W18_x"] = (dirty, xdoc, xc, xe, x1, kx)
        with ctx.step("names, type and room names, notes, old records, and echoed request text never run as script"):
            clean, doc, offerer, day, pc, pe, exp = ctx.shared["W18"]
            dirty, xdoc, xc, xe, x1, kx = ctx.shared["W18_x"]
            q = urllib.parse.quote(REFLECT)

            def ctxt(cl):
                return br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in cl.cookies.items()])

            leg_xss = next(a for a in H.model.w.appts.values() if a.legacy and "legacy-name" in H.model.users[a.patient_id]["full_name"]
                           and a.status != "cancelled")
            leg_day = E.local_date(leg_xss.start, H.model.tz_of(leg_xss.clinician_id))
            leg_clinic = H.model.w.clinicians[leg_xss.clinician_id].clinic_id
            leg_note = next(a for a, ns in H.model.notes.items() if any("legacy-note" in n["body"] for n in ns))
            visits = [
                (xc, [f"/book?clinician_id={xdoc}&type={dirty}&date={day.isoformat()}", f"/appointments/{x1}", "/appointments", "/profile",
                      f"/book?clinician_id={q}&type={q}&date={q}", f"/no-such-page/{q}", f"/appointments/{q}", f"/offers/{q}",
                      f"/api/availability?clinician_id={xdoc}&type={q}&date={day.isoformat()}", f"/appointments/{x1}?checkout={q}"]),
                (H.desk_for(SEA), [f"/desk/day?clinic_id={SEA}&date={day.isoformat()}", f"/appointments/{x1}", f"/desk/day?clinic_id={q}&date={q}",
                                   f"/desk/export.csv?clinic_id={q}&from={q}&to={q}"]),
                (H.desk_for(leg_clinic), [f"/desk/day?clinic_id={leg_clinic}&date={leg_day.isoformat()}", f"/appointments/{leg_xss.aid}"]),
                (kx, [f"/appointments/{x1}/notes", f"/clinician/day?date={day.isoformat()}", f"/clinician/day?date={q}"]),
                (H.admin, [f"/appointments/{leg_note}/notes", f"/admin/types/{urllib.parse.quote(dirty)}", f"/clinicians/{xdoc}/schedule",
                           f"/appointments/{x1}/notes", f"/admin/types/{q}"]),
                (None, [f"/signin?next={urllib.parse.quote(chr(39) + chr(34) + '><svg onload=window.__fk_xss&&__fk_xss(1)>')}", f"/password-reset/{q}",
                        f"/register?email={q}"]),
            ]
            for cl, paths in visits:
                vcx = ctxt(cl) if cl is not None else br.context()
                vp = br.page(vcx)
                for u in paths:
                    try:
                        vp.goto(H.base + u)
                        vp.wait_for_load_state()
                        vp.wait_for_timeout(200)
                    except Exception:
                        pass
                vcx.close()
            if br.xss_hits:
                ctx.breach("X3", f"stored or reflected text ran as script: {sorted(set(br.xss_hits))[:6]}")
            ctx.check(not br.xss_hits, f"script ran from typed or echoed text: {sorted(set(br.xss_hits))[:6]}")
    finally:
        B.mark_unreached(CHECKPOINTS)
        if br:
            br.close()


# ======================================================================================== W19
@workflow("W19", "Clinic closures: the old closures and new ones close every clinician; bookings stay; admins and the clinic's manager only", 3, critical=True)
def w19(ctx):
    m = H.model
    with ctx.step("the old system's closures within the horizon leave no slots for any clinician of that clinic"):
        H.ensure_staff()
        lo = {cl: H.today(cl) + timedelta(days=1) for cl in (SEA, HNL)}
        cases = []
        for (clinic, d) in sorted(m.w.closures):
            if not (lo[clinic] <= d <= H.today(clinic) + timedelta(days=H.horizon)):
                continue
            wd = d.isoweekday()
            workers = [c.cid for c in m.w.clinicians.values() if c.cid in m.legacy_clinicians and c.clinic_id == clinic and c.active
                       and (E.version_on(c, d) and any(w[0] == wd for w in E.version_on(c, d).windows) or any(x.on_date == d for x in c.exceptions))]
            for cid in workers[:12]:
                cases += [(cid, "FU30", d), (cid, "INIT60", d - timedelta(days=1)), (cid, "FU30", d + timedelta(days=1))]
        ctx.observe("legacy_closure_lists", len(cases))
        compare_all(ctx, cases, "w19_legacy")
    with ctx.step("an admin closes each clinic on a future day; booked appointments that day stay confirmed and listed"):
        rt = H.room_type("W19")
        for clinic in (SEA, HNL):
            H.create_room(clinic, rt)
            H.create_room(clinic, rt)
        code = H.create_type(30, 0, rt, 9000, 2000)
        sea, hnl = all_day_clinicians(SEA, 4), all_day_clinicians(HNL, 3)
        far = {SEA: H.quiet_block(SEA, 150, 3), HNL: H.quiet_block(HNL, 152, 3)}
        far[SEA] = min(far[SEA], H.today(SEA) + timedelta(days=H.horizon - 5))
        far[HNL] = min(far[HNL], H.today(HNL) + timedelta(days=H.horizon - 4))
        c, email = H.register()
        kept = [H.booked(c, email, cid, code, H.first_slot(cid, code, far[m.w.clinicians[cid].clinic_id])) for cid in (sea[0], hnl[0])]
        for clinic in (SEA, HNL):
            who = H.admin if clinic == SEA else H.manager_for(HNL)
            r = submit_form(who, f"/admin/clinics/{clinic}", "add-closure", {"on_date": far[clinic].isoformat(), "reason": "Flooring work"},
                            follow=True)
            ctx.check(r.status < 400, f"adding a closure answered {r.status}")
            m.add_closure(clinic, far[clinic])
            listing = who.get(f"/admin/clinics/{clinic}", follow=True)
            dates = {n.attrs.get("data-closure-date") for n in listing.dom.attr_all("data-closure-date")}
            every = {d.isoformat() for (cl, d) in m.w.closures if cl == clinic}
            upcoming = {d.isoformat() for (cl, d) in m.w.closures if cl == clinic and d >= H.today(clinic)}
            # R4.6 says the page shows "the clinic's closures": every upcoming one must be listed; past ones may be
            ctx.check(upcoming <= dates <= every, f"closures listed for clinic {clinic}: missing {sorted(upcoming - dates)[:5]}, "
                                                  f"not closures {sorted(dates - every)[:5]}")
        ctx.eq([H.appt_marker(None, a).get("data-status") for a in kept], ["confirmed", "confirmed"], "appointments on the closed days")
        for clinic in (SEA, HNL):
            r = H.desk_for(clinic).get(f"/desk/day?clinic_id={clinic}&date={far[clinic].isoformat()}", follow=True)
            ctx.check(set(kept) & set(page_appt_ids(r)), f"the closed day's sheet at clinic {clinic} lost its booked appointment")
        ctx.shared["W19"] = (code, sea, hnl, far)
    with ctx.step("on the closed days no clinician has a slot; the days around them are unaffected"):
        code, sea, hnl, far = ctx.shared["W19"]
        legacy = {cl: [c.cid for c in m.w.clinicians.values() if c.cid in m.legacy_clinicians and c.clinic_id == cl][:15] for cl in (SEA, HNL)}
        cases = []
        for cl, gen in ((SEA, sea), (HNL, hnl)):
            for cid in gen + legacy[cl]:
                for k in (-1, 0, 1):
                    cases.append((cid, code if cid in gen else "FU30", far[cl] + timedelta(days=k)))
        compare_all(ctx, cases, "w19_new")
        ctx.check(all(not H.model.slots(cid, code, far[SEA], H.now()) for cid in sea), "verifier fixture: the closure has no effect")
    with ctx.step("a second closure for the same day is refused; only admins and the clinic's manager add closures; a token-less closure is refused"):
        code, sea, hnl, far = ctx.shared["W19"]
        r = submit_form(H.admin, f"/admin/clinics/{SEA}", "add-closure", {"on_date": far[SEA].isoformat(), "reason": "Again"})
        ctx.eq(r.status, 409, "a duplicate closure")
        other = far[SEA] + timedelta(days=2)
        n0 = H.q("SELECT count(*) AS n FROM clinic_closures")[0]["n"]
        r1 = H.desk.post_form(f"/admin/clinics/{SEA}", tokens(H.desk) + [("on_date", other.isoformat()), ("reason", "Desk")])
        k = H.account(H.clinician_email(sea[1]))
        r2 = k.post_form(f"/admin/clinics/{SEA}", tokens(k) + [("on_date", other.isoformat()), ("reason", "Clinician")])
        r3 = submit_form(H.admin, f"/admin/clinics/{SEA}", "add-closure", {"on_date": other.isoformat(), "reason": "No token"}, drop_csrf=True)
        mh = H.manager_for(HNL)
        r4 = mh.post_form(f"/admin/clinics/{SEA}", tokens(mh) + [("on_date", other.isoformat()), ("reason", "Other clinic's manager")])
        n1 = H.q("SELECT count(*) AS n FROM clinic_closures")[0]["n"]
        if n1 != n0:
            ctx.breach("X1" if r3.status == 403 else "X2", "a closure was added by someone not allowed or without the session token")
        ctx.eq((r1.status, r2.status, r3.status, r4.status, n1), (403, 403, 403, 403, n0),
               "front desk, clinician, token-less, other clinic's manager closures and the row count")
    with ctx.step("the clinic page offers add-closure to admins and that clinic's manager only"):
        ms = H.manager_for(SEA)
        got = (sorted(actions(H.admin.get(f"/admin/clinics/{HNL}", follow=True)) & {"add-closure"}),
               sorted(actions(ms.get(f"/admin/clinics/{SEA}", follow=True)) & {"add-closure"}),
               ms.get(f"/admin/clinics/{HNL}").status, H.desk_for(SEA).get(f"/admin/clinics/{SEA}").status)
        ctx.eq(got, (["add-closure"], ["add-closure"], 403, 403), "add-closure for admin and manager; 403 for another clinic's manager and front desk")


# ======================================================================================== W20
EXPORT_HEADER = ["appointment_id", "local_date", "local_time", "utc_start", "clinician_id", "clinician_name", "patient_name",
                 "patient_email", "type", "status", "deposit_cents", "refund_cents"]
FORMULA = ("=", "+", "-", "@", "\t", "\r")


def export_expected(clinic: int, d0: date, d1: date) -> list[list[str]]:
    m = H.model
    tz = m.w.clinics[clinic]
    rows = []
    for a in m.w.appts.values():
        c = m.w.clinicians[a.clinician_id]
        if c.clinic_id != clinic:
            continue
        day = E.local_date(a.start, tz)
        if not (d0 <= day <= d1):
            continue
        if a.legacy:
            if a.status not in ("booked", "completed", "no_show", "cancelled"):
                continue
            u = m.users[a.patient_id]
            pname, pemail, refund = u["full_name"], norm(u["email"]), 0
        else:
            if a.status not in ("confirmed", "cancelled", "rescheduled"):
                continue
            pname, pemail, refund = a.patient_name, a.patient_email, a.refund
        loc = E.local_of(a.start, tz)
        cname = m.users[c.user_id]["full_name"] if c.user_id in m.users else c.name

        def safe(v):
            return "'" + v if v.startswith(FORMULA) else v
        rows.append((a.start, a.aid, [str(a.aid), loc.strftime("%Y-%m-%d"), loc.strftime("%H:%M"),
                                      datetime.fromtimestamp(a.start, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), str(a.clinician_id),
                                      safe(cname), safe(pname), pemail, a.type_code, a.status, str(a.deposit), str(refund)]))
    return [r for _, _, r in sorted(rows, key=lambda x: (x[0], x[1]))]


def export_check(ctx, clinic: int, d0: date, d1: date, label: str, only: set | None = None) -> None:
    import csv
    import io
    r = H.desk_for(clinic).get(f"/desk/export.csv?clinic_id={clinic}&from={d0.isoformat()}&to={d1.isoformat()}")
    ctx.eq(r.status, 200, f"{label}: export status")
    raw = r.body.decode("utf-8-sig", "replace")
    ctx.check(raw.startswith(",".join(EXPORT_HEADER) + "\r\n"), f"{label}: the header row differs or does not end in CRLF: {raw[:160]!r}")
    got = list(csv.reader(io.StringIO(raw, newline="")))
    want = export_expected(clinic, d0, d1)
    body = got[1:]
    if only is not None:
        body = [row for row in body if row and row[0].isdigit() and int(row[0]) in only]
        want = [row for row in want if int(row[0]) in only]
    for row in body:
        for v in row[5:7]:
            if v.startswith(FORMULA):
                ctx.breach("X15", f"{label}: the export wrote a live spreadsheet formula {v[:40]!r}")
    if body != want:
        for i, (g, w) in enumerate(zip(body, want)):
            if g != w:
                raise StepFailed(f"{label}: {len(body)} rows, expected {len(want)}; row {i + 1} is {g}, expected {w}")
        raise StepFailed(f"{label}: {len(body)} rows, expected {len(want)}")
    lines = raw.split("\r\n")
    ctx.check(raw.endswith("\r\n") and all("\n" not in ln for ln in lines if ln.count('"') % 2 == 0 and '"' not in ln),
              f"{label}: rows do not all end in CRLF")
    ctx.observe(f"{label}_rows", len(body))


@workflow("W20", "Appointment export: exact columns and order, local and UTC times with folds, quoting, spreadsheet safety", 4, critical=True)
def w20(ctx):
    m = H.model
    with ctx.step("the old system's past weeks export exactly, including the clock-change night"):
        H.ensure_staff()
        export_check(ctx, SEA, date(2026, 10, 18), date(2026, 11, 9), "seattle-legacy")
        export_check(ctx, HNL, date(2026, 10, 1), date(2026, 10, 31), "honolulu-legacy")
    with ctx.step("patients and clinicians with commas, quotes and formula-like names book, cancel and reschedule"):
        rt = H.room_type("W20")
        H.create_room(SEA, rt)
        H.create_room(SEA, rt)
        code = H.create_type(45, 15, rt, 12550, 1750)
        d0 = H.quiet_block(SEA, 40, 7)
        names = ['=SUM(1,2)', '+Plus Patient', '-Minus Patient', '@At Patient', 'Lee, Sunny', 'Quinn "Q" Akana', "Tab\tStart", "Plain Patient"]
        docs = []
        for nm in ("=Doctor Formula", "Ellis, Rowan"):
            cid = H.create_clinician(SEA, 20, name=nm)
            H.add_version(cid, d0 - timedelta(days=7), "\n".join(f"{d} 08:00-18:00" for d in E.DAY_NAMES))
            docs.append(cid)
        made = []
        for i, nm in enumerate(names):
            c, e = H.register(name=nm)
            cid = docs[i % 2]
            day = d0 + timedelta(days=i % 5)
            aid = H.booked(c, e, cid, code, H.model.slots(cid, code, day, H.now())[i % 3])
            made.append((aid, c, e))
        for aid, c, e in made[:2]:
            t0 = H.now()
            cancel(c, aid)
            mk = H.appt_marker(None, aid)
            a = H.model.appt(aid)
            a.status, a.refund = "cancelled", int(mk.get("data-refund-cents") or 0)
            ctx.check(a.refund in refund_bands(a.start, t0, H.now(), a.deposit, False), f"refund for {aid}")
        aid, c, e = made[2]
        cancel(H.desk, aid)
        a = H.model.appt(aid)
        a.status, a.refund = "cancelled", a.deposit
        aid, c, e = made[3]
        o = H.model.appt(aid)
        target = H.model.slots(o.clinician_id, code, d0 + timedelta(days=5), H.now(), ignore=aid)[0]
        page = H.appt_page(c, aid)
        r = c.post_form(f"/appointments/{aid}/reschedule", (csrf_from(page) or tokens(c)) +
                        [("clinician_id", str(o.clinician_id)), ("start", E.iso(target, H.model.tz_of(o.clinician_id)))])
        mm = re.search(r"/appointments/(\d+)", r.location or "")
        ctx.check(r.status in REDIRECTS and mm, f"rescheduling answered {r.status}")
        nid = int(mm.group(1))
        n = H.model.add_booking(nid, o.clinician_id, code, None, target, "confirmed", None, o.deposit)
        n.patient_email, n.patient_name = o.patient_email, o.patient_name
        o.status = "rescheduled"
        H.created_appts.append(nid)
        hold = H.hold(made[4][1], made[4][2], docs[0], code, H.model.slots(docs[0], code, d0 + timedelta(days=6), H.now())[0])
        ctx.shared["W20"] = (d0, docs, hold)
    with ctx.step("the export of those days lists exactly the right rows, quoted and spreadsheet-safe"):
        d0, docs, hold = ctx.shared["W20"]
        export_check(ctx, SEA, d0, d0 + timedelta(days=6), "seattle-new")
    with ctx.step("ranges over 31 days or backwards are refused; patients cannot export"):
        d0, docs, hold = ctx.shared["W20"]
        r = H.desk.get(f"/desk/export.csv?clinic_id={SEA}&from={d0.isoformat()}&to={(d0 + timedelta(days=40)).isoformat()}")
        ctx.eq(r.status, 400, "a 41-day range")
        # to = from + 31 is 32 days counted inclusively but 31 days apart: either reading of "more than 31 days" is accepted
        r = H.desk.get(f"/desk/export.csv?clinic_id={SEA}&from={d0.isoformat()}&to={(d0 + timedelta(days=31)).isoformat()}")
        ctx.check(r.status in (200, 400), f"to = from + 31 answered {r.status}")
        r = H.desk.get(f"/desk/export.csv?clinic_id={SEA}&from={d0.isoformat()}&to={(d0 - timedelta(days=1)).isoformat()}")
        ctx.eq(r.status, 400, "a backwards range")
        r = H.desk.get(f"/desk/export.csv?clinic_id={SEA}&from={d0.isoformat()}&to={(d0 + timedelta(days=30)).isoformat()}")
        ctx.eq(r.status, 200, "a 31-day range")
        pc, pe = H.register()
        r = pc.get(f"/desk/export.csv?clinic_id={SEA}&from={d0.isoformat()}&to={d0.isoformat()}")
        if r.status == 200 and b"appointment_id" in r.body:
            ctx.breach("X1", "a patient downloaded the appointment export")
        ctx.eq(r.status, 403, "a patient's export request")


# ======================================================================================== W21
@workflow("W21", "Front desk books for patients: holds for the account, payment-link emails, then the patient pays", 4, critical=True)
def w21(ctx):
    m = H.model
    with ctx.step("front desk holds slots for patients by address, including the old system's quirky addresses; the patients pay"):
        H.ensure_staff()
        rt = H.room_type("W21")
        for clinic in (SEA, HNL):
            H.create_room(clinic, rt)
            H.create_room(clinic, rt)
        fee, bp = tie_fees(rnd_for("W21"), 1)[0]
        code = H.create_type(60, 15, rt, fee, bp)
        clins = all_day_clinicians(SEA, 2) + all_day_clinicians(HNL, 1)
        legacy = [u for u in H.baseline["users"] if u["role"] == "patient" and m.account_for(u["email"]) == u["id"]
                  and u["email"] != norm(u["email"])][5:11]
        new = [H.register() for _ in range(5)]
        targets = [(u["email"], None) for u in legacy] + [(e, c) for c, e in new]
        dup = next(rows for rows in _dups() if not rows[0]["active"] and rows[1]["active"])
        targets.append((dup[0]["email"], None))
        day = H.quiet_block(SEA, 9, 2)
        made = []
        for i, (addr, cl) in enumerate(targets):
            cid = clins[i % 3]
            start = H.model.slots(cid, code, day, H.now())[i // 3]
            page = H.desk_for(H.clinic_of(cid)).get("/desk/book", follow=True)
            t = H.now()
            r = submit_form(H.desk_for(H.clinic_of(cid)), "/desk/book", "desk-book", {"patient_email": addr, "clinician_id": str(cid), "type": code,
                                                                 "start": E.iso(start, m.tz_of(cid))}, follow=True, page=page,
                            override={"deposit_cents": "1", "patient_id": "1"})
            node, link = r.dom.attr_one("data-appointment-id"), r.dom.attr_one("data-checkout-url")
            if r.status >= 400 or node is None or link is None:
                raise StepFailed(f"booking for {addr.strip()} answered {r.status} without data-appointment-id/data-checkout-url")
            aid, url = int(node.attrs["data-appointment-id"]), link.attrs["data-checkout-url"]
            sid = re.search(r"/pay/(cs_[A-Za-z0-9_]+)", url)
            ctx.check(sid, f"the checkout url {url!r} is not the provider's hosted page")
            H.record_hold(aid, cid, code, start, sid.group(1), addr, t)
            made.append((aid, norm(addr), url, cl))
            if i < len(targets) - 1:  # the patient pays at once (the last hold stays unpaid and expires)
                pay_now(aid)
        ctx.shared["W21"] = (code, clins, day, made, new, legacy, dup)
    with ctx.step("each session is for the account's address and the deposit; each patient gets one payment-link email"):
        code, clins, day, made, new, legacy, dup = ctx.shared["W21"]
        bad = []
        for aid, addr, url, cl in made:
            sess = H.pay.session(re.search(r"/pay/(cs_[A-Za-z0-9_]+)", url).group(1))
            a = H.model.appt(aid)
            if sess.get("amount_total") == 1:
                ctx.breach("X4", f"a desk booking's extra fields set the deposit of {aid}")
            got = (sess.get("amount_total"), norm(sess.get("customer_email") or ""), (sess.get("metadata") or {}).get("appointment_id"))
            want = (a.deposit, addr, str(aid))
            if got != want:
                bad.append(f"{aid}: {got} != {want}")
            mails = H.wait_mails(addr, "Pay your deposit", 1)
            if len(mails) != 1 or url not in body_links(mails[0]):
                bad.append(f"{aid}: {len(mails)} payment-link emails to {addr}" + ("" if not mails else f", link present={url in body_links(mails[0])}"))
        ctx.check(not bad, " | ".join(bad[:4]))
    with ctx.step("the paid bookings are confirmed and belong to the patients' accounts"):
        code, clins, day, made, new, legacy, dup = ctx.shared["W21"]
        for aid, addr, url, cl in made[:-1]:
            ctx.eq(H.wait_status(aid, "confirmed", 60).get("data-status"), "confirmed", f"desk booking {aid} after its patient paid")
            H.model.set_status(aid, "confirmed")
        for aid, addr, url, cl in made:
            if cl is not None:
                ids = page_appt_ids(cl.get("/appointments", follow=True))
                ctx.check(aid in ids, f"appointment {aid} is not on its patient's /appointments")
        acct = m.account_for(dup[0]["email"])
        c = H.account(dup[1]["email"])
        ctx.check(made[-1][0] in page_appt_ids(c.get("/appointments", follow=True)),
                  f"the booking for a shared address did not go to its account (user {acct})")
    with ctx.step("the last unpaid hold expires like any other"):
        code, clins, day, made, new, legacy, dup = ctx.shared["W21"]
        aid = made[-1][0]
        time.sleep(max(0.0, H.model.appt(aid).expires + 3 - H.now()))
        H.model.set_status(aid, "expired")
        ctx.eq(H.wait_status(aid, "expired", 15).get("data-status"), "expired", "status of the unpaid desk hold")
        compare_all(ctx, [(cid, code, day) for cid in clins], "w21_after")
    with ctx.step("refused: unknown or inactive addresses, non-patients, patients using the desk, no token"):
        code, clins, day, made, new, legacy, dup = ctx.shared["W21"]
        cid = clins[0]
        start = H.model.slots(cid, code, day, H.now())[-1]
        inactive = next(u for u in H.baseline["users"] if u["role"] == "patient" and m.account_for(u["email"]) is None)
        n0 = len(H.pay.state().get("checkout_sessions", {}))
        for addr, label in ((f"nobody.{H.nonce}@verify.example", "an unknown address"), (inactive["email"], "an inactive patient"),
                            (H.clinician_email(cid), "a clinician's address"), (ADMIN_EMAIL, "an admin's address")):
            r = submit_form(H.desk_for(H.clinic_of(cid)), "/desk/book", "desk-book", {"patient_email": addr, "clinician_id": str(cid), "type": code,
                                                                 "start": E.iso(start, m.tz_of(cid))})
            ctx.eq(r.status, 400, f"booking for {label}")
        pc, pe = new[0]
        r = pc.post_form("/desk/book", tokens(pc) + [("patient_email", pe), ("clinician_id", str(cid)), ("type", code),
                                                     ("start", E.iso(start, m.tz_of(cid)))])
        ctx.eq(r.status, 403, "a patient using /desk/book")
        r = submit_form(H.desk_for(H.clinic_of(cid)), "/desk/book", "desk-book", {"patient_email": pe, "clinician_id": str(cid), "type": code,
                                                             "start": E.iso(start, m.tz_of(cid))}, drop_csrf=True)
        ctx.eq(r.status, 403, "a token-less desk booking")
        n1 = len(H.pay.state().get("checkout_sessions", {}))
        if n1 != n0:
            ctx.breach("X1", "a refused desk booking created a Checkout Session")
        ctx.eq(n1, n0, "Checkout Sessions after refused desk bookings")
# ======================================================================================== W22
@workflow("W22", "Legacy Honolulu: the old system's schedules, appointments, closures and patients, served as they are", 5, timeout=1800,
          critical=True)
def w22(ctx):
    legacy_segment(ctx, HNL, "W22")


# ======================================================================================== W23
@workflow("W23", "Clinic staff: managers edit their clinic's schedules, front desk books and cancels only for their clinic", 4)
def w23(ctx):
    rnd = rnd_for("W23")
    with ctx.step("a Seattle and a Honolulu clinician; the Seattle manager sets the Seattle clinician's hours"):
        H.ensure_staff()
        rt = H.room_type("W23")
        for clinic in (SEA, HNL):
            H.create_room(clinic, rt)
            H.create_room(clinic, rt)
        fee, bp = tie_fees(rnd, 1)[0]
        code = H.create_type(45, 15, rt, fee, bp)
        cs, ch = H.create_clinician(SEA, 6), H.create_clinician(HNL, 6)
        ms, mh = H.manager_for(SEA), H.manager_for(HNL)
        d = H.quiet_block(SEA, 4, 4)
        H.add_version(cs, d - timedelta(days=3), edge_windows(rnd), client=ms)
        H.add_exception(cs, d + timedelta(days=1), "off", "10:00", "12:00", client=ms)
        H.add_version(ch, d - timedelta(days=3), edge_windows(rnd), client=mh)
        page = ms.get(f"/clinicians/{cs}/schedule", follow=True)
        ctx.eq(sorted(actions(page) & {"add-version", "add-exception"}), ["add-exception", "add-version"], "schedule actions for the manager")
        compare_all(ctx, [(cid, code, d + timedelta(days=k)) for cid in (cs, ch) for k in range(4)], "w23_hours")
        ctx.shared["W23"] = (code, cs, ch, ms, mh, d)
    with ctx.step("a manager cannot see or edit another clinic's schedules; front desk cannot edit schedules"):
        code, cs, ch, ms, mh, d = ctx.shared["W23"]
        before = H.availability(ch, code, d)[1]
        r0 = ms.get(f"/clinicians/{ch}/schedule")
        r1 = ms.post_form(f"/clinicians/{ch}/schedule/exceptions", tokens(ms) + [("on_date", d.isoformat()), ("kind", "off"),
                                                                                ("start_time", ""), ("end_time", "")])
        r2 = H.desk_for(SEA).post_form(f"/clinicians/{cs}/schedule/versions", tokens(H.desk_for(SEA)) +
                                       [("effective_from", d.isoformat()), ("windows", "")])
        if H.availability(ch, code, d)[1] != before:
            ctx.breach("X1", "a manager changed another clinic's clinician's schedule")
        ctx.eq((r0.status, r1.status, r2.status), (403, 403, 403), "another clinic's schedule (view, edit) and front desk editing")
        compare_all(ctx, [(cs, code, d), (ch, code, d)], "w23_untouched")
    with ctx.step("Seattle front desk books for a patient with a Seattle clinician; a Honolulu clinician is refused"):
        code, cs, ch, ms, mh, d = ctx.shared["W23"]
        c, email = H.register()
        desk = H.desk_for(SEA)
        # the clinicians' weekly hours come from edge_windows (some weekdays have none), so take the first day that has a
        # slot instead of a fixed date (a fixed date fails on some weekdays whatever the application does)
        start = first_open_slot(cs, code, [d + timedelta(days=k) for k in range(4)])
        t = H.now()
        r = submit_form(desk, "/desk/book", "desk-book", {"patient_email": email, "clinician_id": str(cs), "type": code,
                                                          "start": E.iso(start, H.model.tz_of(cs))}, follow=True)
        node, link = r.dom.attr_one("data-appointment-id"), r.dom.attr_one("data-checkout-url")
        ctx.check(r.status < 400 and node is not None and link is not None, f"booking for a patient answered {r.status}")
        aid = int(node.attrs["data-appointment-id"])
        H.record_hold(aid, cs, code, start, re.search(r"/pay/(cs_[A-Za-z0-9_]+)", link.attrs["data-checkout-url"]).group(1), email, t)
        n0 = len(H.pay.state().get("checkout_sessions", {}))
        r = desk.post_form("/desk/book", tokens(desk) + [("patient_email", email), ("clinician_id", str(ch)), ("type", code),
                                                         ("start", E.iso(first_open_slot(ch, code, [H.open_day(HNL, 5 + k) for k in range(14)]), H.model.tz_of(ch)))])
        if len(H.pay.state().get("checkout_sessions", {})) != n0:
            ctx.breach("X1", "Seattle front desk held a slot with a Honolulu clinician")
        ctx.eq(r.status, 403, "Seattle front desk booking a Honolulu clinician")
        H.confirm(aid)
        ctx.shared["W23_appt"] = (aid, c, email)
    with ctx.step("the appointment is visible to its clinic's staff only; other-clinic desk pages are refused"):
        code, cs, ch, ms, mh, d = ctx.shared["W23"]
        aid, c, email = ctx.shared["W23_appt"]
        dh = H.desk_for(HNL)
        r = dh.get(f"/appointments/{aid}", follow=True)
        if r.dom.attr_one("data-appointment-id", str(aid)) is not None:
            ctx.breach("X1", "Honolulu front desk opened a Seattle appointment")
        ctx.eq(r.status, 403, "Honolulu front desk opening a Seattle appointment")
        day = H.model.local_day(cs, H.model.appt(aid).start)
        r1 = dh.get(f"/desk/day?clinic_id={SEA}&date={day.isoformat()}")
        if r1.status == 200 and str(aid) in r1.text:
            ctx.breach("X1", "Honolulu front desk read the Seattle day sheet")
        r2 = dh.get(f"/desk/export.csv?clinic_id={SEA}&from={day.isoformat()}&to={day.isoformat()}")
        if r2.status == 200 and b"appointment_id" in r2.body:
            ctx.breach("X1", "Honolulu front desk exported Seattle appointments")
        r3 = dh.post_form("/desk/reminders", tokens(dh) + [("clinic_id", str(SEA)), ("date", day.isoformat())])
        ctx.eq((r1.status, r2.status, r3.status), (403, 403, 403), "Honolulu desk on Seattle's day sheet, export and reminders")
        r = H.desk_for(SEA).get(f"/desk/day?clinic_id={SEA}&date={day.isoformat()}", follow=True)
        ctx.check(aid in page_appt_ids(r), "the appointment is missing from its clinic's day sheet")
        ui_appt(ctx, H.desk_for(SEA), aid, {"cancel-appointment"}, "Seattle front desk")
        ui_appt(ctx, ms, aid, {"cancel-appointment"}, "the Seattle manager")
        ui_appt(ctx, c, aid, {"cancel-appointment", "reschedule"}, "the patient")
    with ctx.step("another clinic's manager cannot cancel; the clinic's manager cancels with a full refund and one email each"):
        code, cs, ch, ms, mh, d = ctx.shared["W23"]
        aid, c, email = ctx.shared["W23_appt"]
        r = mh.post_form(f"/appointments/{aid}/cancel", tokens(mh))
        if H.appt_marker(None, aid).get("data-status") == "cancelled":
            ctx.breach("X1", "a manager of another clinic cancelled an appointment")
            raise StepFailed("another clinic's manager cancelled the appointment")
        ctx.eq(r.status, 403, "Honolulu manager cancelling a Seattle appointment")
        cancel(ms, aid)
        a = H.model.appt(aid)
        a.status, a.refund = "cancelled", a.deposit
        ms_ = wait_refunds([aid])
        ctx.eq((ms_[aid].get("data-status"), ms_[aid].get("data-refund-cents"), ms_[aid].get("data-refund-status")),
               ("cancelled", str(a.deposit), "refunded"), "status and refund after the manager cancelled")
        H.wait_counts([(email, "Appointment cancelled", 1), (email, "Refund issued", 1), (email, "Pay your deposit", 1),
                       (email, "Appointment confirmed", 1)])
        ctx.eq((len(H.mails(email, "Appointment cancelled")), len(H.mails(email, "Refund issued")),
                len(H.mails(email, "Pay your deposit")), len(H.mails(email, "Appointment confirmed"))), (1, 1, 1, 1),
               "cancellation, refund, payment-link and confirmation emails")


# ======================================================================================== W24
@workflow("W24", "A patient's journey: browse, register, book and pay, waitlist, reschedule into an offer, cancel, export", 5, timeout=1200,
          critical=True)
def w24(ctx):
    rnd = rnd_for("W24")
    with ctx.step("a visitor sees a clinician's times without booking buttons, registers, and then sees the buttons"):
        H.ensure_staff()
        rt = H.room_type("W24")
        H.create_room(SEA, rt)
        H.create_room(SEA, rt)
        fee, bp = tie_fees(rnd, 1)[0]
        code = H.create_type(45, 15, rt, fee, bp)
        d = H.quiet_block(SEA, 5, 1)
        dn = E.DAY_NAMES[d.isoweekday() - 1]
        j = H.create_clinician(SEA, 6, name="Iolana Akana")
        H.add_version(j, d - timedelta(days=7), f"{dn} 10:00-12:00")
        k2 = all_day_clinicians(SEA, 1)[0]
        n = len(H.model.slots(j, code, d, H.now()))
        ctx.eq(book_buttons(H.client(), j, code, d), 0, "book-slot buttons for a visitor")
        c, email = H.register(name="Journey Patient")
        ctx.eq(book_buttons(c, j, code, d), n, "book-slot buttons for the signed-in patient")
        ctx.shared["W24"] = (code, d, j, k2, c, email)
    with ctx.step("the patient books 10:00 and pays; one confirmation; the list shows it"):
        code, d, j, k2, c, email = ctx.shared["W24"]
        a = H.booked(c, email, j, code, H.at(SEA, d, "10:00"))
        ctx.check(H.wait_mails(email, "Appointment confirmed", 1), "no confirmation email")
        r = c.get("/appointments", follow=True)
        node = r.dom.attr_one("data-appointment-id", str(a))
        ctx.check(node is not None and node.attrs.get("data-status") == "confirmed", "the appointment is not listed as confirmed")
        ctx.shared["W24_a"] = a
    with ctx.step("another patient takes 11:00, the day is full, and the patient joins its waitlist"):
        code, d, j, k2, c, email = ctx.shared["W24"]
        o, oe = H.register()
        H.booked(o, oe, j, code, H.at(SEA, d, "11:00"))
        ctx.eq(H.availability(j, code, d)[1], [], "slots on the full day")
        r = submit_form(c, "/waitlist", "join-waitlist", {"clinician_id": str(j), "type": code, "date": d.isoformat()}, follow=True)
        ctx.check(r.status < 400, f"joining the waitlist answered {r.status}")
        st = [n.attrs.get("data-entry-status") for n in c.get("/waitlist", follow=True).dom.attr_all("data-entry-status")]
        ctx.eq(st, ["waiting"], "waitlist entries")
    with ctx.step("moving the 10:00 appointment to a time soon after now frees the slot, and the waitlist offers it back"):
        code, d, j, k2, c, email = ctx.shared["W24"]
        a = ctx.shared["W24_a"]
        seen = H.mail_ids(email)
        s = slot_near(k2, code, 6, 3, 20, ignore=a, fallbacks=((1, 23), (25, 70)))
        page = H.appt_page(c, a)
        r = c.post_form(f"/appointments/{a}/reschedule", (csrf_from(page) or tokens(c)) + [("clinician_id", str(k2)),
                                                                                           ("start", E.iso(s, H.model.tz_of(k2)))])
        mm = re.search(r"/appointments/(\d+)", r.location or "")
        ctx.check(r.status in REDIRECTS and mm, f"rescheduling answered {r.status}")
        b = int(mm.group(1))
        old = H.model.appt(a)
        old.status = "rescheduled"
        nb = H.model.add_booking(b, k2, code, None, s, "confirmed", None, old.deposit)
        nb.patient_email, nb.patient_name = email, old.patient_name
        H.created_appts.append(b)
        H.sessions[b] = H.sessions[a]
        msgs = H.mail.wait_for(email, lambda m: m["id"] not in seen and "slot opened" in m["subject"].lower(), 1, EFFECT_WAIT)
        link = next((l for m in msgs for l in body_links(m) if "/offers/" in l), None)
        ctx.check(link, "no waitlist offer after the move freed the slot")
        ctx.check(H.wait_mails(email, "Appointment rescheduled", 1), "no reschedule email")
        ctx.shared["W24_b"] = (b, link.rstrip(".,)"))
    with ctx.step("the fee changes while the offer is open; accepting pays the deposit in effect on that date"):
        code, d, j, k2, c, email = ctx.shared["W24"]
        b, link = ctx.shared["W24_b"]
        new_fee, new_bp = tie_fees(rnd_for("W24b"), 1)[0]
        H.add_fee(code, d - timedelta(days=1), new_fee, new_bp)
        page = c.get(link, follow=True)
        node = page.dom.attr_one("data-offer-status")
        ctx.check(node is not None and E.parse_instant(node.attrs.get("data-start", "1970-01-01T00:00:00Z")) == H.at(SEA, d, "10:00"),
                  f"the offer is for {node.attrs.get('data-start') if node else None}")
        t = H.now()
        r = submit_form(c, "", "accept-offer", {}, page=page)
        mm = re.search(r"/pay/(cs_[A-Za-z0-9_]+)", r.location or "")
        ctx.check(r.status in REDIRECTS and mm, f"accepting answered {r.status}")
        sid = mm.group(1)
        sess = H.pay.session(sid)
        aid = int(sess["metadata"]["appointment_id"])
        ctx.eq(sess.get("amount_total"), H.model.deposit_on(code, d), "the offer's Checkout amount")
        H.record_hold(aid, j, code, H.at(SEA, d, "10:00"), sid, email, t)
        H.confirm(aid)
        ctx.shared["W24_c"] = aid
    with ctx.step("cancelling the moved appointment refunds the carried deposit by its band, once"):
        code, d, j, k2, c, email = ctx.shared["W24"]
        b, link = ctx.shared["W24_b"]
        a = H.model.appt(b)
        left = a.start - H.now()  # a few hours, unless a clinic closure moved the slot (see slot_near)
        if abs(left - 86400) > 120:
            ui_appt(ctx, c, b, {"cancel-appointment", "reschedule"} if left > 86400 else {"cancel-appointment"},
                    f"the patient, {left / 3600:.1f} h before")
        t0 = H.now()
        cancel(c, b)
        allowed = refund_bands(a.start, t0, H.now(), a.deposit, False)
        ms = wait_refunds([b])
        got = int(ms[b].get("data-refund-cents") or -1)
        ctx.check(got in allowed and ms[b].get("data-refund-status") == "refunded", f"refund {got} ({ms[b].get('data-refund-status')}), allowed {allowed}")
        a.status, a.refund = "cancelled", got
        ctx.eq([x["amount"] for x in H.pay.refunds_for(H.payment_intent(ctx.shared["W24_a"]))], [got], "refunds against the first payment")
        H.wait_counts([(email, "Appointment confirmed", 2), (email, "Refund issued", 1), (email, "Appointment cancelled", 1)])
        ctx.eq((len(H.mails(email, "Appointment confirmed")), len(H.mails(email, "Refund issued")), len(H.mails(email, "Appointment cancelled"))),
               (2, 1, 1), "confirmation, refund and cancellation emails")
    with ctx.step("the patient's list and the clinic's export show the whole history"):
        code, d, j, k2, c, email = ctx.shared["W24"]
        r = c.get("/appointments", follow=True)
        st = {int(n.attrs["data-appointment-id"]): n.attrs.get("data-status") for n in r.dom.attr_all("data-appointment-id")}
        want = {ctx.shared["W24_a"]: "rescheduled", ctx.shared["W24_b"][0]: "cancelled", ctx.shared["W24_c"]: "confirmed"}
        ctx.eq(st.get(ctx.shared["W24_c"]), "confirmed", "the confirmed appointment on /appointments")
        wrong = {k: st[k] for k in want if k in st and st[k] != want[k]}
        ctx.check(not wrong, f"statuses on /appointments {wrong}, expected {want}")
        lo = min(H.model.local_day(k2, H.model.appt(ctx.shared["W24_b"][0]).start), d)
        ids = {ctx.shared["W24_a"], ctx.shared["W24_b"][0], ctx.shared["W24_c"]}
        export_check(ctx, SEA, lo, d, "w24-export", only=ids)


# ======================================================================================== W25
ROTATED = "Rotated-Harbor-Pass-88"


def db_secret_hits(secrets_: list[str]) -> list[str]:
    pats = ["%" + x.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for x in secrets_ if x and len(x) >= 8]
    hits = []
    if not pats:
        return hits
    for t in H.q("SELECT table_schema, table_name, column_name FROM information_schema.columns WHERE table_schema NOT IN "
                 "('pg_catalog','information_schema') AND data_type IN ('text','character varying','jsonb','json','bytea','character')"):
        col = f'"{t["table_schema"]}"."{t["table_name"]}"'
        if H.q(f'SELECT 1 FROM {col} WHERE "{t["column_name"]}"::text LIKE ANY(%s) LIMIT 1', (pats,)):
            hits.append(f"{t['table_name']}.{t['column_name']}")
    return sorted(set(hits))


@workflow("W25", "Cross-cutting safety: secrets nowhere at rest or in logs, reset ends sessions, Host-proof links, headers, no stack traces", 3)
def w25(ctx):
    from kitlib import xcut
    from kitlib.mail import links as mail_links
    with ctx.step("setting a new password through a reset link signs out every existing session of that account"):
        H.ensure_staff()
        c_old, email = H.register()
        c_other, _ = H.signin(email)
        ctx.check(H.signed_in(c_old) and H.signed_in(c_other), "could not sign in twice")
        ctx.check(H.set_password(email, ROTATED), "the reset flow failed")
        survivors = [n for n, c in (("first", c_old), ("second", c_other)) if H.signed_in(c)]
        if survivors:
            ctx.breach("X13", f"{len(survivors)} session(s) still signed in after the password was reset")
        ctx.check(not survivors, f"sessions survived the password reset: {survivors}")
        c_new, _ = H.signin(email, ROTATED)
        ctx.check(H.signed_in(c_new), "the new password does not sign in")
        ctx.shared["W25"] = (email, c_new)
    with ctx.step("a reset link always starts with PUBLIC_BASE_URL, whatever Host header the request carried"):
        email, c_new = ctx.shared["W25"]
        before = H.mail_ids(email)
        c = H.client()
        page = c.get("/password-reset", headers={"Host": "evil.example"})
        try:
            fields = form_fields(find_form(page.dom, "request-reset")) + [("email", email)]
        except Exception:
            fields = [("email", email)]
        c.post_form("/password-reset", fields, headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"})
        msgs = H.mail.wait_for(email, lambda m: m["id"] not in before, 1, EFFECT_WAIT)
        ctx.check(msgs, "no reset email arrived")
        found = [l for m in msgs for l in body_links(m) if "/password-reset/" in l]
        bad = [l for l in found if not l.startswith(H.base + "/")]
        if bad:
            ctx.breach("X14", f"reset link built from the request's Host header: {bad[0][:80]}")
        ctx.check(found and not bad, f"reset links {found[:2]}")
        H.reset_tokens += [l.rstrip("/").rsplit("/", 1)[-1] for l in found]
    with ctx.step("signed-in responses carry Cache-Control: no-store and every page an anti-framing header"):
        email, c_new = ctx.shared["W25"]
        some = next((a for a in H.created_appts if H.model.appt(a).status == "confirmed"
                     and H.clinic_of(H.model.appt(a).clinician_id) == SEA), None)
        cid = next(iter(c.cid for c in H.model.w.clinicians.values() if c.clinic_id == SEA))
        d = H.today(SEA) + timedelta(days=3)
        checks = [(c_new, "/appointments"), (c_new, "/waitlist"), (c_new, "/profile"), (c_new, f"/book?clinician_id={cid}&type=FU30&date={d}"),
                  (H.desk_for(SEA), f"/desk/day?clinic_id={SEA}&date={d}"), (H.desk_for(SEA), "/desk/book"),
                  (H.desk_for(SEA), f"/desk/export.csv?clinic_id={SEA}&from={d}&to={d}"), (H.admin, "/admin/types/new"),
                  (H.admin, f"/clinicians/{cid}/schedule"), (H.admin, f"/admin/clinics/{SEA}"), (H.manager_for(SEA), "/desk/reminders")]
        if some:
            checks.append((H.admin, f"/appointments/{some}"))
        problems = []
        for cl, path in checks:
            r = cl.get(path)
            ctx.check(r.status == 200, f"{path} answered {r.status}")
            found = xcut.header_problems(r)
            if "text/html" not in (r.header("Content-Type") or "").lower():
                found = [x for x in found if "anti-framing" not in x]  # framing concerns pages; a CSV download needs only no-store
            problems += found
        for path in ("/signin", "/register", "/password-reset"):
            problems += xcut.header_problems(H.client().get(path), need_no_store=False)
        ctx.check(not problems, "; ".join(problems[:4]))
    with ctx.step("malformed requests get a plain error page without stack traces, SQL, or file paths"):
        email, c_new = ctx.shared["W25"]
        tok = csrf_from(c_new.get("/profile", follow=True))
        atok = csrf_from(H.admin.get("/admin/types/new", follow=True))
        big = "9" * 24
        probes = [
            H.client().get(f"/api/availability?clinician_id={big}&type=FU30&date=2026-12-01"),
            H.client().get("/api/availability?clinician_id=1&type=FU30&date=2026-02-30"),
            H.client().get("/api/availability?clinician_id=1%27%20OR%201%3D1--&type=FU30&date=2026-12-01"),
            c_new.get(f"/appointments/{big}"),
            H.admin.get(f"/clinicians/{big}/schedule"),
            H.admin.get(f"/appointments/{big}/notes"),
            c_new.post_form("/book", tok + [("clinician_id", "1;DROP TABLE users"), ("type", "FU30"), ("start", "' OR 1=1--")]),
            c_new.post_form("/book", tok + [("clinician_id", big), ("type", "FU30"), ("start", "2026-12-01T09:00:00Z")]),
            c_new.post_form("/waitlist", tok + [("clinician_id", "x"), ("type", "FU30"), ("date", "never")]),
            H.admin.post_form("/admin/types/new", atok + [("code", "ZZ"), ("name", "X"), ("duration_minutes", "1e99"), ("buffer_minutes", "-1"),
                                                          ("room_type", "treatment"), ("fee_cents", "abc"), ("deposit_bp", "99999"),
                                                          ("effective_from", "2026-02-30")]),
            H.desk_for(SEA).get(f"/desk/export.csv?clinic_id={big}&from=2026-01-01&to=2026-01-02"),
            H.client().get("/password-reset/" + "%00" * 3),
            H.client().request("POST", "/webhooks/payments", body=b"{not json", headers={"Content-Type": "application/json"}),
        ]
        problems = [p for r in probes for p in xcut.error_leaks(r)]
        ctx.check(not problems, "; ".join(problems[:4]))
    with ctx.step("a waitlist offer is made; its link reaches the patient"):
        rt = H.room_type("W25")
        H.create_room(SEA, rt)
        code = H.create_type(30, 0, rt, 9000, 2000)
        D = H.quiet_block(SEA, 5, 1)
        cl = H.create_clinician(SEA, 5)
        H.add_version(cl, D - timedelta(days=7), f"{E.DAY_NAMES[D.isoweekday() - 1]} 09:00-09:30")
        fc, fe = H.register()
        filler = H.booked(fc, fe, cl, code, H.at(SEA, D, "09:00"))
        wc, we = H.register()
        seen = H.mail_ids(we)
        submit_form(wc, "/waitlist", "join-waitlist", {"clinician_id": str(cl), "type": code, "date": D.isoformat()})
        cancel(fc, filler)
        H.model.set_status(filler, "cancelled")
        msgs = H.mail.wait_for(we, lambda m_: m_["id"] not in seen and "slot opened" in (m_["subject"] or "").lower(), 1, EFFECT_WAIT)
        ctx.check(any("/offers/" in l for m_ in msgs for l in body_links(m_)), "no offer email with an offer link")
    with ctx.step("no password, reset token, offer token, session identifier, or signing key is stored in plain text (database or workspace files)"):
        signing = ""
        try:
            signing = H.pay.signing_key()
        except OSError:
            pass
        tokens_in_mail = {"reset": [], "offer": []}
        for m_ in H.mail.all():
            for l in body_links(m_):
                for kind, marker in (("reset", "/password-reset/"), ("offer", "/offers/")):
                    if l.startswith(H.base + marker):
                        tok = l[len(H.base + marker):].split("/")[0].split("?")[0]
                        if len(tok) >= 16:
                            tokens_in_mail[kind].append(tok)
        secrets_ = ([PASSWORD, ROTATED, signing] + H.reset_tokens[-12:] + H.session_values[-12:]
                    + tokens_in_mail["reset"][-12:] + tokens_in_mail["offer"][-16:])
        ctx.observe("offer_tokens_scanned", len(tokens_in_mail["offer"][-16:]))
        hits = xcut.scan_files_for_secrets([H.workspace], secrets_)  # what the app stores; its stdout log is the platform's
        if hits:
            ctx.breach("X8", f"secrets stored in the application's files: {hits[:3]}")
        dbhits = db_secret_hits(secrets_)
        if dbhits:
            ctx.breach("X8", f"secrets stored in the database: {dbhits[:4]}")
        ctx.check(not hits and not dbhits, f"secrets stored in files {hits[:3]} or database {dbhits[:4]}")


# ======================================================================================== W26
@workflow("W26", "Deployment: README, restart persistence, holds and refunds survive, nothing re-sent, no secrets, records kept", 3, timeout=900)
def w26(ctx):
    with ctx.step("README.md and an executable start.sh are in the workspace"):
        ws = Path(H.workspace)
        ctx.check((ws / "README.md").is_file() and (ws / "README.md").stat().st_size > 40, "README.md missing")
        ctx.check(os.access(ws / "start.sh", os.X_OK), "start.sh is not executable")
    with ctx.step("before a restart: an unpaid hold, and a refund the provider has not answered yet"):
        H.ensure_staff()
        rt = H.room_type("W22")
        H.create_room(SEA, rt)
        code = H.create_type(30, 0, rt, 8888, 2000)
        cid = all_day_clinicians(SEA, 1)[0]
        c, email = H.register()
        day = H.open_day(SEA, 3)
        paid = H.booked(c, email, cid, code, H.first_slot(cid, code, day))
        H.pay.fault(refund_create="timeout", refund_create_delay=25)
        cancel(c, paid)
        H.model.set_status(paid, "cancelled")
        hold = H.hold(c, email, cid, code, H.model.slots(cid, code, day, H.now())[5])
        time.sleep(1)
        ctx.shared["W26"] = (code, cid, c, email, day, paid, hold)
        ctx.shared["W26_mail"] = {m["id"] for m in H.mail.summaries()}
    with ctx.step("after a restart: passwords, appointments and the hold's expiry are kept"):
        code, cid, c, email, day, paid, hold = ctx.shared["W26"]
        ctx.check(H.restart_app(), "the application did not answer /healthz after a restart")
        c2, _ = H.signin(ADMIN_EMAIL)
        ctx.check(H.signed_in(c2), "the admin password did not survive the restart")
        H.admin = c2
        H.desk = None
        H.desks, H.managers = {}, {}
        H.ensure_staff()
        kept = [a for a in H.created_appts if H.model.appt(a).status == "confirmed"][:20]
        sts = [H.appt_marker(None, a).get("data-status") for a in kept]
        ctx.eq(sts, ["confirmed"] * len(kept), "confirmed appointments after the restart")
        h = H.model.appt(hold)
        if H.now() < H.hold_lo[hold] - 2:
            ctx.eq(H.appt_marker(None, hold).get("data-status"), "held", "the hold right after the restart")
        time.sleep(max(0.0, h.expires + 3 - H.now()))
        h.status = "expired"
        err = H.compare_slots(cid, code, day)
        ctx.check(err is None, f"the hold did not expire on time after the restart: {err}")
    with ctx.step("the refund still pending at the restart completes once"):
        code, cid, c, email, day, paid, hold = ctx.shared["W26"]
        ms = wait_refunds([paid])
        ctx.eq(ms[paid].get("data-refund-status"), "refunded", "refund status after the restart")
        refunds = H.pay.refunds_for(H.payment_intent(paid))
        if len(refunds) > 1:
            ctx.breach("X6", f"a refund pending across the restart was paid {len(refunds)} times")
        ctx.eq([r["amount"] for r in refunds], [H.model.appt(paid).deposit], "refunds after the restart")
    with ctx.step("no email sent before the restart is sent again"):
        time.sleep(8)
        before = ctx.shared["W26_mail"]
        old = {}
        for m in H.mail.all():
            key = (tuple(m["to"]), m["subject"], body_of(m))
            if m["id"] in before:
                old[key] = old.get(key, 0) + 1
        again = [m for m in H.mail.all() if m["id"] not in before and (tuple(m["to"]), m["subject"], body_of(m)) in old]
        if again:
            ctx.breach("X6", f"{len(again)} emails were sent again after the restart: {[m['subject'] for m in again[:3]]}")
        ctx.eq(len(again), 0, "emails re-sent after the restart")
    with ctx.step("no password is stored in plain text"):
        hits = []
        for t in H.q("SELECT table_schema, table_name, column_name FROM information_schema.columns WHERE table_schema NOT IN "
                     "('pg_catalog','information_schema') AND data_type IN ('text','character varying','jsonb','json','bytea','character')"):
            col = f'"{t["table_schema"]}"."{t["table_name"]}"'
            if H.q(f'SELECT 1 FROM {col} WHERE position(%s in "{t["column_name"]}"::text) > 0 LIMIT 1', (PASSWORD,)):
                hits.append(f"{t['table_name']}.{t['column_name']}")
        if hits:
            ctx.breach("X8", f"plain-text password in {sorted(set(hits))}")
        ctx.check(not hits, f"plain-text password stored in {sorted(set(hits))}")
    with ctx.step("the old system's records are all still there, unchanged"):
        b = H.baseline
        changed = []
        from snapshot import TABLES
        for name, sql in TABLES.items():
            cur = {r["id"]: r for r in H.q(sql)} if b[name] and "id" in b[name][0] else None
            if cur is None:
                continue
            for row in b[name]:
                now = cur.get(row["id"])
                keys = [k for k in row if not (name == "users" and k == "full_name" and row["id"] in H.renamed_users)]
                if now is None or any(str(now[k]) != str(row[k]) for k in keys):
                    changed.append(f"{name} {row['id']}")
        if changed:
            ctx.breach("X9", f"pre-existing records changed or removed: {changed[:8]}")
        ctx.check(not changed, f"pre-existing records changed: {changed[:8]}")
    with ctx.step("every appointment the verifier made is a row in appointments with local_start and fold of its start"):
        bad = []
        ids = sorted(set(H.created_appts))
        rows = {r["id"]: r for r in H.q("SELECT id, clinician_id, to_char(local_start, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS ls, fold FROM appointments"
                                        " WHERE id = ANY(%s)", (ids,))}
        for aid in ids:
            a = H.model.appt(aid)
            loc = E.local_of(a.start, H.model.tz_of(a.clinician_id))
            r = rows.get(aid)
            if r is None or (r["ls"], r["fold"], r["clinician_id"]) != (loc.strftime("%Y-%m-%dT%H:%M:%S"), loc.fold, a.clinician_id):
                bad.append(f"{aid}: {r}")
        ctx.check(not bad, f"{len(bad)} of {len(ids)} appointments: " + " | ".join(bad[:4]))
        ctx.check(all(i > H.model.max_legacy_appt for i in ids), "new appointments do not continue the appointments id sequence")


# ======================================================================================== W27
@workflow("W27", "Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start", 2, timeout=400)
def w27(ctx):
    import subprocess
    stop, start = os.environ.get("FK_DB_STOP", ""), os.environ.get("FK_DB_START", "")

    def probe() -> int | None:
        try:
            return Client(H.base, timeout=5).get("/healthz").status
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



def _dups() -> list:
    by: dict = {}
    for u in H.baseline["users"]:
        by.setdefault(norm(u["email"]), []).append(u)
    return [sorted(v, key=lambda u: u["id"]) for v in by.values() if len(v) > 1]


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
