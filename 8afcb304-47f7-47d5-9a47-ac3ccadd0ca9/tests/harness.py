"""Harborview verifier harness: drives the submission through its interface contract and keeps the model."""

from __future__ import annotations

import json
import os
import random
import re
import secrets
import subprocess
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # Reward Kit imports every file standalone

import psycopg  # noqa: E402

from kitlib.mail import Mail, links  # noqa: E402
from kitlib.web import Client, FormNotFound, Response, csrf_from, find_form, submit_form, visible_text  # noqa: E402
from kitlib.wf import InfraError, StepFailed  # noqa: E402
from paylib import Payments  # noqa: E402
from reference import engine as E  # noqa: E402
from reference.model import Model, norm  # noqa: E402

PASSWORD = "Harbor-View-Pass-77"
ADMIN_EMAIL = "leilani.kahale@harborviewphysio.example"
ADMIN2_EMAIL = "marcus.webb@harborviewphysio.example"
DESK_EMAIL = "jun.park@harborviewphysio.example"
DESK_EMAILS = {1: "jun.park@harborviewphysio.example", 2: "ikaika.long@harborviewphysio.example"}
MANAGER_EMAILS = {1: "tess.harlow@harborviewphysio.example", 2: "kalea.nakoa@harborviewphysio.example"}
SEA, HNL = 1, 2
CARD_OK, CARD_DECLINE = "4242424242424242", "4000000000000002"
REDIRECTS = (301, 302, 303, 307, 308)
ALL_DAY = "\n".join(f"{d} 00:00-24:00" for d in E.DAY_NAMES)
# The spec bounds no delay for emails, confirmations after a verified webhook, offers, session expiry, or refunds
# (only the first refund retry, R7.5). Effects are polled for up to these limits; polls return as soon as they hold.
EFFECT_WAIT = 90.0
REFUND_WAIT = 120.0


class Harness:
    def __init__(self):
        e = os.environ
        self.base = e["PUBLIC_BASE_URL"].rstrip("/")
        self.db_url = e["DATABASE_URL"]
        self.mail = Mail(e["MAILSINK_HTTP_URL"])
        self.pay = Payments()
        self.reset_ttl = int(e.get("RESET_LINK_TTL_SECONDS", "20"))
        self.hold_ttl = int(e.get("HOLD_TTL_SECONDS", "30"))
        self.offer_ttl = int(e.get("OFFER_TTL_SECONDS", "30"))
        self.notice = int(e.get("MIN_NOTICE_MINUTES", "50"))
        self.horizon = int(e.get("BOOKING_HORIZON_DAYS", "240"))
        self.restart_hook = e.get("FK_RESTART_APP", "")
        self.workspace = e.get("FK_WORKSPACE", "/app")
        self.nonce = secrets.token_hex(3)
        self.baseline = self._baseline()
        self.model = Model(self.baseline, self.notice, self.horizon)
        self.admin: Client | None = None
        self.desk: Client | None = None
        self.desks: dict[int, Client] = {}
        self.managers: dict[int, Client] = {}
        self.n = 0
        self.created_appts: list[int] = []
        self.renamed_users: set[int] = set()
        self.sessions: dict[int, str] = {}  # appointment id -> checkout session id
        self.patient_email: dict[int, str] = {}  # appointment id -> patient address
        self.names: dict[str, str] = {}  # normalized address -> display name of verifier-registered patients
        self.reset_tokens: list[str] = []  # plain reset tokens seen in emails (R14.5 at-rest scan)
        self.hold_lo: dict[int, float] = {}  # appointment id -> earliest moment its hold can expire
        self.session_values: list[str] = []  # cookie values set at sign-in (R14.5 at-rest scan)

    # -- plumbing -----------------------------------------------------------------------------------
    def _baseline(self) -> dict:
        path = os.environ.get("FK_BASELINE")
        if path and os.path.exists(path):
            with open(path) as fh:
                return json.load(fh)
        raise InfraError("baseline snapshot missing")

    def q(self, sql: str, args=()):
        with psycopg.connect(self.db_url, connect_timeout=10) as c:
            cur = c.execute(sql, args)
            cols = [d.name for d in cur.description] if cur.description else []
            return [dict(zip(cols, r)) for r in cur.fetchall()] if cols else []

    def client(self) -> Client:
        return Client(self.base)

    def uid(self) -> str:
        self.n += 1
        return f"{self.nonce}{self.n}"

    @staticmethod
    def now() -> float:
        return time.time()

    # -- accounts -----------------------------------------------------------------------------------
    def request_reset(self, email: str, client: Client | None = None) -> Response:
        return submit_form(client or self.client(), "/password-reset", "request-reset", {"email": email}, follow=True)

    def mail_ids(self, email: str) -> set:
        return {m["id"] for m in self.mail.to(norm(email))}

    def reset_link(self, email: str, since: set, timeout: float = EFFECT_WAIT) -> str | None:
        msgs = self.mail.wait_for(norm(email), lambda m: m["id"] not in since, 1, timeout)
        for m in reversed(msgs):
            for l in links((m["text"] or "") + "\n" + (m["html"] or "")):
                l = l.replace("&amp;", "&")
                if l.startswith(self.base) and "/password-reset/" in l:
                    self.reset_tokens.append(l.rstrip("/").rsplit("/", 1)[-1])
                    return l
        return None

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
        self.note_session(c)
        return c, r

    def signed_in(self, c: Client) -> bool:
        """Signed in when /profile, after any redirects, is a page with the sign-out button (not the sign-in page)."""
        r = c.get("/profile", follow=True)
        path = urllib.parse.urlsplit(r.url).path
        return r.status == 200 and not path.startswith("/signin") and r.dom.attr_one("data-action", "signout") is not None

    session_cookie_names: list[str] | None = None

    def note_session(self, c: Client) -> None:
        """Record the session identifier values of a client (R14.5 at-rest scan). The session cookie(s) are found once:
        the cookie that alone keeps a client signed in, else those whose removal signs it out. Other cookies (a CSRF
        cookie, say) are not session identifiers."""
        if not c.cookies:
            return
        if self.session_cookie_names is None:
            if not self.signed_in(c):
                return
            names = []
            for k in list(c.cookies):
                probe = self.client()
                probe.cookies = {k: c.cookies[k]}
                if self.signed_in(probe):
                    names.append(k)
            if not names:
                for k in list(c.cookies):
                    probe = self.client()
                    probe.cookies = {n: v for n, v in c.cookies.items() if n != k}
                    if not self.signed_in(probe):
                        names.append(k)
            Harness.session_cookie_names = names
        for k in self.session_cookie_names or []:
            v = c.cookies.get(k, "")
            if len(v) >= 8:
                self.session_values.append(v)

    def account(self, email: str) -> Client:
        if not self.set_password(email):
            raise StepFailed(f"could not set a password for {email.strip()} through /password-reset")
        c, r = self.signin(norm(email))
        if not self.signed_in(c):
            raise StepFailed(f"sign-in for {email.strip()} did not produce a signed-in session (status {r.status})")
        return c

    def ensure_staff(self) -> None:
        if self.admin is None:
            self.admin = self.account(ADMIN_EMAIL)
        if self.desk is None:
            self.desks = {}
            self.desk = self.desk_for(SEA)

    def desk_for(self, clinic: int) -> Client:
        if clinic not in self.desks:
            self.desks[clinic] = self.account(DESK_EMAILS[clinic])
        return self.desks[clinic]

    def manager_for(self, clinic: int) -> Client:
        if clinic not in self.managers:
            self.managers[clinic] = self.account(MANAGER_EMAILS[clinic])
        return self.managers[clinic]

    def clinic_of(self, cid: int) -> int:
        return self.model.w.clinicians[cid].clinic_id

    _ties: list | None = None

    def ties(self) -> list[tuple[int, int]]:
        """(fee, bp) pairs whose deposits and half refunds land on exact half cents (reference/ties.json)."""
        if Harness._ties is None:
            with open(Path(__file__).resolve().parent / "reference" / "ties.json") as fh:
                Harness._ties = [(p["fee"], p["bp"]) for p in json.load(fh)["pairs"]]
        return list(Harness._ties)

    def register(self, name: str | None = None, email: str | None = None) -> tuple[Client, str]:
        tag = self.uid()
        email = email or f"p{tag}@verify.example"
        name = name or f"Verifier Patient {tag}"
        c = self.client()
        r = submit_form(c, "/register", "register", {"email": email, "full_name": name, "password": PASSWORD}, follow=True)
        self.note_session(c)
        if r.status >= 400 or not self.signed_in(c):
            raise StepFailed(f"registering {email} answered {r.status} without a signed-in session")
        self.names[norm(email)] = name
        return c, email

    def token(self, c: Client, path: str = "/profile") -> list[tuple[str, str]]:
        return csrf_from(c.get(path, follow=True))

    # -- reference data through the app ---------------------------------------------------------------
    def create_clinician(self, clinic: int, max_per_day: int = 20, name: str | None = None) -> int:
        self.ensure_staff()
        tag = self.uid()
        email = f"c{tag}@verify.example"
        name = name or f"Verifier Clinician {tag}"
        r = submit_form(self.admin, "/admin/clinicians/new", "create-clinician",
                        {"email": email, "full_name": name, "clinic_id": str(clinic), "max_per_day": str(max_per_day)}, follow=True)
        node, unode = r.dom.attr_one("data-clinician-id"), r.dom.attr_one("data-user-id")  # the page carries both
        if r.status >= 400 or node is None or unode is None:
            raise StepFailed(f"creating a clinician answered {r.status} without data-clinician-id/data-user-id")
        cid, uid = int(node.attrs["data-clinician-id"]), int(unode.attrs["data-user-id"])
        self.model.add_clinician(cid, uid, clinic, max_per_day, email, name)
        return cid

    def clinician_email(self, cid: int) -> str:
        return self.model.users[self.model.w.clinicians[cid].user_id]["email"]

    def add_version(self, cid: int, eff: date, windows: str, client: Client | None = None, record: bool = True) -> Response:
        c = client or self.admin
        r = submit_form(c, f"/clinicians/{cid}/schedule", "add-version", {"effective_from": eff.isoformat(), "windows": windows},
                        follow=True)
        if record:
            if r.status >= 400:
                raise StepFailed(f"adding weekly hours for clinician {cid} answered {r.status}")
            self.model.add_version(cid, eff, windows)
        return r

    def add_exception(self, cid: int, on: date, kind: str, start: str | None = None, end: str | None = None,
                      client: Client | None = None, record: bool = True) -> Response:
        c = client or self.admin
        r = submit_form(c, f"/clinicians/{cid}/schedule", "add-exception",
                        {"on_date": on.isoformat(), "kind": kind, "start_time": start or "", "end_time": end or ""}, follow=True)
        if record:
            if r.status >= 400:
                raise StepFailed(f"adding an exception for clinician {cid} answered {r.status}")
            self.model.add_exception(cid, on, kind, start, end)
        return r

    def create_type(self, dur: int, buf: int, room_type: str, fee: int, bp: int, eff: date = date(2024, 1, 1),
                    name: str | None = None) -> str:
        self.ensure_staff()
        code = f"V{self.uid()}".upper()
        name = name or f"Verifier visit {dur}+{buf}"
        r = submit_form(self.admin, "/admin/types/new", "create-type", {
            "code": code, "name": name, "duration_minutes": str(dur), "buffer_minutes": str(buf), "room_type": room_type,
            "fee_cents": str(fee), "deposit_bp": str(bp), "effective_from": eff.isoformat()}, follow=True)
        node = r.dom.attr_one("data-type-code")
        if r.status >= 400 or node is None or node.attrs.get("data-type-code") != code:
            raise StepFailed(f"creating appointment type {code} answered {r.status} without data-type-code")
        self.model.add_type(-len(self.model.w.types) - 1, code, name, dur, buf, room_type, fee, bp, eff)
        return code

    def add_fee(self, code: str, eff: date, fee: int, bp: int) -> Response:
        r = submit_form(self.admin, f"/admin/types/{urllib.parse.quote(code)}", "add-fee",
                        {"effective_from": eff.isoformat(), "fee_cents": str(fee), "deposit_bp": str(bp)}, follow=True)
        if r.status >= 400:
            raise StepFailed(f"adding a fee to {code} answered {r.status}")
        self.model.add_fee(code, eff, fee, bp)
        return r

    def create_room(self, clinic: int, room_type: str, name: str | None = None) -> int:
        self.ensure_staff()
        name = name or f"Verifier room {self.uid()}"
        r = submit_form(self.admin, "/admin/rooms/new", "create-room", {"clinic_id": str(clinic), "name": name, "room_type": room_type},
                        follow=True)
        node = r.dom.attr_one("data-room-id")
        if r.status >= 400 or node is None:
            raise StepFailed(f"creating a room answered {r.status} without data-room-id")
        rid = int(node.attrs["data-room-id"])
        self.model.add_room(rid, clinic, room_type, name)
        return rid

    def room_type(self, wid: str) -> str:
        return f"rt-{wid.lower()}-{self.uid()}"

    # -- dates --------------------------------------------------------------------------------------
    def today(self, clinic: int) -> date:
        return E.local_date(int(time.time()), self.model.w.clinics[clinic])

    def quiet_block(self, clinic: int, first: int, length: int) -> date:
        """The first date >= today+first such that no UTC-offset change happens in [d, d+length] and neither clinic is
        closed on any of those dates (workflows book clinicians of both clinics on a block)."""
        tz = self.model.w.clinics[clinic]
        d = self.today(clinic) + timedelta(days=first)
        for _ in range(60):
            offs = {E.local_of(E.wall_to_epoch(d + timedelta(days=k), 720, tz), tz).utcoffset() for k in range(length + 1)}
            closed = any((cl, d + timedelta(days=k)) in self.model.w.closures for cl in self.model.w.clinics for k in range(length + 1))
            if len(offs) == 1 and not closed:
                return d
            d += timedelta(days=1)
        return d

    def open_day(self, clinic: int, first: int, length: int = 0, also: tuple = ()) -> date:
        """The first date d >= today+first such that no day of [d, d+length] is a closure at the clinic or at `also`
        (old closures fall on fixed dates, so a fixed offset from today could land on one)."""
        d = self.today(clinic) + timedelta(days=first)
        for _ in range(60):
            if not any((cl, d + timedelta(days=k)) in self.model.w.closures for cl in (clinic, *also) for k in range(length + 1)):
                return d
            d += timedelta(days=1)
        return d

    def at(self, clinic: int, day: date, hhmm: str) -> int:
        return E.wall_to_epoch(day, E.hm(hhmm), self.model.w.clinics[clinic])

    # -- availability -------------------------------------------------------------------------------
    def availability(self, cid: int, code: str, day: date) -> tuple[int, list[int] | None, dict]:
        r = self.client().get(f"/api/availability?clinician_id={cid}&type={urllib.parse.quote(code)}&date={day.isoformat()}")
        if r.status != 200:
            return r.status, None, {}
        try:
            data = r.json()
            return 200, [E.parse_instant(s["start"]) for s in data["slots"]], data
        except Exception:
            return r.status, None, {}

    def compare_slots(self, cid: int, code: str, day: date, ignore: int | None = None) -> str | None:
        """None when the app's slot list equals the model's at some moment of the request; else a description."""
        t0 = self.now()
        status, got, data = self.availability(cid, code, day)
        t1 = self.now()
        if status != 200 or got is None:
            return f"clinician {cid} {code} {day}: availability answered {status}"
        want0 = self.model.slots(cid, code, day, t0, ignore=ignore)
        if got == want0:
            return self._check_ends(cid, code, data)
        want1 = self.model.slots(cid, code, day, t1, ignore=ignore)
        if got == want1:
            return self._check_ends(cid, code, data)
        tz = self.model.tz_of(cid)
        miss = [E.iso(s, tz) for s in want0 if s not in got][:4]
        extra = [E.iso(s, tz) for s in got if s not in want0][:4]
        return f"clinician {cid} {code} {day}: {len(got)} slots, expected {len(want0)}; missing {miss} extra {extra}"

    def _check_ends(self, cid: int, code: str, data: dict) -> str | None:
        dur = self.model.w.types[code].duration * 60
        for s in data.get("slots", []):
            if E.parse_instant(s["end"]) - E.parse_instant(s["start"]) != dur:
                return f"clinician {cid} {code}: slot {s} does not end duration after its start"
        return None

    # -- booking ---------------------------------------------------------------------------------------
    def book(self, c: Client, cid: int, code: str, start: int, extra: dict | None = None, drop_csrf: bool = False) -> tuple[Response, int | None, str | None]:
        """POST /book. Returns (response, appointment id, session id) when it redirected to a Checkout Session."""
        tz = self.model.tz_of(cid)
        page = c.get(f"/book?clinician_id={cid}&type={urllib.parse.quote(code)}&date={E.local_date(start, tz).isoformat()}", follow=True)
        tok = [] if drop_csrf else csrf_from(page)
        if not tok and not drop_csrf:
            tok = self.token(c)
        fields = tok + [("clinician_id", str(cid)), ("type", code), ("start", E.iso(start, tz))] + list((extra or {}).items())
        r = c.post_form("/book", fields)
        loc = r.location or ""
        m = re.search(r"/pay/(cs_[A-Za-z0-9_]+)", loc)
        if r.status in REDIRECTS and m:
            sid = m.group(1)
            sess = self.pay.session(sid)
            aid = (sess.get("metadata") or {}).get("appointment_id")
            if aid is None:
                raise StepFailed("the Checkout Session carries no metadata[appointment_id]")
            return r, int(aid), sid
        return r, None, None

    def record_hold(self, aid: int, cid: int, code: str, start: int, sid: str | None, email: str, t_req: float) -> None:
        """t_req is a moment before the request that created the hold; it is called once that request has returned.
        The hold therefore expires between t_req + ttl and now + ttl: the model keeps it until the later bound (waits
        for expiry are safe), and hold_lo keeps the earlier bound (checks that it still holds are safe)."""
        t_hi = self.now()
        self.hold_lo[aid] = t_req + self.hold_ttl
        room = self.appt_marker(self.admin, aid).get("data-room-id") or None
        dep = self.model.deposit_on(code, self.model.local_day(cid, start))
        a = self.model.add_booking(aid, cid, code, int(room) if room else None, start, "held", None, dep, expires=t_hi + self.hold_ttl)
        a.patient_email = norm(email)
        acct = self.model.account_for(email)
        a.patient_name = self.names.get(norm(email)) or (self.model.users[acct]["full_name"] if acct else "")
        self.created_appts.append(aid)
        if sid:
            self.sessions[aid] = sid
        self.patient_email[aid] = norm(email)

    def hold(self, c: Client, email: str, cid: int, code: str, start: int) -> int:
        t = self.now()
        r, aid, sid = self.book(c, cid, code, start)
        if aid is None:
            raise StepFailed(f"booking clinician {cid} {code} at {E.iso(start, self.model.tz_of(cid))} answered {r.status}: "
                             f"{visible_text(r.text)[:200]}")
        self.record_hold(aid, cid, code, start, sid, email, t)
        return aid

    def appt_page(self, c: Client, aid: int) -> Response:
        return c.get(f"/appointments/{aid}", follow=True)

    def appt_marker(self, c: Client | None, aid: int) -> dict:
        r = self.appt_page(c or self.admin, aid)
        node = r.dom.attr_one("data-appointment-id", str(aid))
        return dict(node.attrs) if node is not None else {}

    def wait_status(self, aid: int, status: str, timeout: float = 60.0, c: Client | None = None) -> dict:
        end = time.monotonic() + timeout
        m = {}
        while time.monotonic() < end:
            m = self.appt_marker(c, aid)
            if m.get("data-status") == status:
                return m
            time.sleep(0.5)
        return m

    def confirm(self, aid: int, card: str = CARD_OK) -> dict:
        sid = self.sessions[aid]
        st, body = self.pay.pay(sid, card)
        if st != 200 or not body.get("ok"):
            raise StepFailed(f"paying session {sid} answered {st}: {body.get('message')}")
        m = self.wait_status(aid, "confirmed", 60)
        if m.get("data-status") != "confirmed":
            raise StepFailed(f"appointment {aid} was not confirmed within 60 s of payment (status {m.get('data-status')})")
        a = self.model.appt(aid)
        a.status, a.expires = "confirmed", None
        return m

    def booked(self, c: Client, email: str, cid: int, code: str, start: int) -> int:
        aid = self.hold(c, email, cid, code, start)
        self.confirm(aid)
        return aid

    def book_many(self, plan: list, chunk: int = 15) -> list[int]:
        """plan: [(client, email, clinician, code, start)]. Holds a chunk, pays each, waits for all confirmations."""
        out = []
        for i in range(0, len(plan), chunk):
            part = []
            for c, email, cid, code, start in plan[i:i + chunk]:
                if start not in self.model.slots(cid, code, self.model.local_day(cid, start), self.now()):
                    continue  # an earlier booking of the plan took the room or the time
                aid = self.hold(c, email, cid, code, start)
                st, body = self.pay.pay(self.sessions[aid])
                if st != 200 or not body.get("ok"):
                    raise StepFailed(f"paying session {self.sessions[aid]} answered {st}")
                part.append(aid)
            end = time.monotonic() + 60
            left = list(part)
            while left and time.monotonic() < end:
                left = [a for a in left if self.appt_marker(None, a).get("data-status") != "confirmed"]
                if left:
                    time.sleep(0.5)
            if left:
                raise StepFailed(f"{len(left)} paid appointments were not confirmed within 60 s: {left[:5]}")
            for a in part:
                self.model.set_status(a, "confirmed")
            out += part
        return out

    def first_slot(self, cid: int, code: str, day: date, after: float | None = None) -> int:
        slots = self.model.slots(cid, code, day, self.now())
        slots = [s for s in slots if after is None or s >= after]
        if not slots:
            raise StepFailed(f"verifier fixture: clinician {cid} has no {code} slot on {day}")
        return slots[0]

    def payment_intent(self, aid: int) -> str | None:
        sid = self.sessions.get(aid)
        return self.pay.session(sid).get("payment_intent") if sid else None

    # -- mail -------------------------------------------------------------------------------------------
    def mails(self, email: str, subject_part: str) -> list[dict]:
        return [m for m in self.mail.to(norm(email)) if subject_part.lower() in (m["subject"] or "").lower()]

    def wait_mails(self, email: str, subject_part: str, count: int = 1, timeout: float = EFFECT_WAIT) -> list[dict]:
        return self.mail.wait_for(norm(email), lambda m: subject_part.lower() in (m["subject"] or "").lower(), count, timeout)

    def wait_counts(self, pairs: list, timeout: float = EFFECT_WAIT) -> None:
        """pairs: [(address, subject part, at least)]. Poll until every count is reached, then allow 3 s for extras."""
        end = time.monotonic() + timeout
        while time.monotonic() < end and any(len(self.mails(a, sp)) < n for a, sp, n in pairs):
            time.sleep(1)
        time.sleep(3)

    def mail_raw(self, mid: str) -> dict:
        import urllib.request
        with urllib.request.urlopen(self.mail.url + "/api/v1/message/" + str(mid), timeout=20) as r:
            return json.loads(r.read().decode())

    def all_mail_recipients(self) -> set[str]:
        return {t for m in self.mail.summaries() for t in m["to"]}

    def restart_app(self) -> bool:
        if not self.restart_hook:
            return False
        # The hook is the verifier's own root-owned script (tests/test.sh writes it); no user input reaches it.
        return subprocess.run([self.restart_hook], check=False).returncode == 0


def actions(resp: Response) -> set[str]:
    """data-action values of buttons inside forms on the page (the interface contract's offered actions)."""
    out = set()
    for n in resp.dom.attr_all("data-action"):
        if n.ancestor("form") is not None:
            out.add(n.attrs["data-action"])
    return out


APPT_ACTIONS = {"release-hold", "cancel-appointment", "reschedule"}


def rnd_for(wid: str) -> random.Random:
    return random.Random(sum(ord(ch) * 31 ** i for i, ch in enumerate(wid)))
