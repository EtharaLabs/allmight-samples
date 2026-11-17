"""Seatwise verifier harness: drives the submission through its interface contract and keeps the model."""

from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # Reward Kit imports every file standalone

import psycopg  # noqa: E402

from kitlib.mail import Mail, links  # noqa: E402
from kitlib.web import Client, FormNotFound, Response, find_form, form_fields, submit_form, visible_text  # noqa: E402
from kitlib.wf import InfraError, StepFailed  # noqa: E402
from paylib import Payments  # noqa: E402
from reference import money as M  # noqa: E402
from reference.model import (CSV_HEADER, Event, Order, Section, Model, inst, instant_to_wall, norm,  # noqa: E402
                             parse_inst, seat_parts, wall_to_instant)

UTC = timezone.utc
PASSWORD = "Correct-Horse-9-Seats"
CARD_OK, CARD_DECLINED = "4242424242424242", "4000000000000002"
REDIRECTS = (301, 302, 303, 307, 308)


class _Proxy:
    """`from harness import H` gives every workflow module the one Harness built by workflows.main()."""
    def __getattr__(self, k):
        if CURRENT[0] is None:
            raise InfraError("harness not initialised")
        return getattr(CURRENT[0], k)


CURRENT: list = [None]
H = _Proxy()


def now() -> datetime:
    return datetime.now(UTC)


def page_attr(dom, name: str, exclude: tuple = ()):
    """The value of `name` on the first element carrying it, skipping elements that carry any attribute in
    `exclude`. R19 says these pages carry the markers, not that one element carries them all."""
    for n in dom.attr_all(name):
        if not any(x in n.attrs for x in exclude):
            return n.attrs.get(name)
    return None


def exact_wall(t: datetime, tz: str) -> bool:
    """Whether the whole-minute instant t is what its own local wall-clock time names (R5.2: a repeated
    wall-clock time means its first occurrence, so instants in a fall-back's second pass are not)."""
    return wall_to_instant(instant_to_wall(t, tz), tz) == t


def next_transition(tz: str, kind: str, min_days: float = 2.0):
    """Local date of the zone's next DST change ('spring': clocks go forward, 'fall': clocks go back) at least
    `min_days` from now, read from the zone's own rules, so no calendar date in the grader goes stale."""
    from zoneinfo import ZoneInfo
    z = ZoneInfo(tz)
    t = (now() + timedelta(days=min_days)).replace(minute=0, second=0, microsecond=0)
    prev = t.astimezone(z).utcoffset()
    for _ in range(24 * 800):
        t += timedelta(hours=1)
        off = t.astimezone(z).utcoffset()
        if off != prev and (off > prev) == (kind == "spring"):
            return (t - timedelta(hours=1)).astimezone(z).date()
        prev = off
    raise InfraError(f"no {kind} DST transition found for {tz}")


def norm_scan(res):
    """One scan result as the verifier compares it. R13.4 lists the fields but gives ticket_id no JSON
    type, so an integer and a string of the same digits are the same id. Instants keep their text, which
    the spec fixes (RFC 3339 UTC to the second)."""
    if isinstance(res, dict):
        tid = res.get("ticket_id")
        if isinstance(tid, str) and tid.strip().isdigit():
            return dict(res, ticket_id=int(tid.strip()))
    return res


class ScanResponse(Response):
    """A /api/scan answer whose json() is normalized by norm_scan. R13.4 asks for a single result without
    naming a wrapper, so a one-element list, or an object without "result" holding exactly one one-element
    list, counts as that result too (the same leniency batch_results gives batch answers)."""

    def json(self):
        body = super().json()
        if isinstance(body, list) and len(body) == 1 and isinstance(body[0], dict):
            body = body[0]
        elif isinstance(body, dict) and "result" not in body:
            lists = [v for v in body.values() if isinstance(v, list)]
            if len(lists) == 1 and len(lists[0]) == 1 and isinstance(lists[0][0], dict):
                body = lists[0][0]
        return norm_scan(body)


def batch_results(resp) -> list:
    """The per-row results of a /api/scan/batch answer. R13.4 asks for one result per row in the same
    order but names no wrapper, so a bare JSON array and an object holding one array (for example
    {"results": [...]}) both count. Anything else yields no rows, which the count check then reports."""
    try:
        body = resp.json()
    except ValueError:
        return []
    rows = None
    if isinstance(body, list):
        rows = body
    elif isinstance(body, dict):
        if isinstance(body.get("results"), list):
            rows = body["results"]
        else:
            lists = [v for v in body.values() if isinstance(v, list)]
            rows = lists[0] if len(lists) == 1 else None
    return [norm_scan(x) if isinstance(x, dict) else {} for x in rows] if rows is not None else []


def rows_markers(dom, attr: str) -> list:
    return dom.attr_all(attr)


class Harness:
    def __init__(self):
        e = os.environ
        self.base = e["PUBLIC_BASE_URL"].rstrip("/")
        self.db_url = e["DATABASE_URL"]
        self.mail = Mail(e["MAILSINK_HTTP_URL"])
        self.pay = Payments()
        self.hold_ttl = int(e.get("HOLD_TTL_SECONDS", "30"))
        self.offer_ttl = int(e.get("WAITLIST_OFFER_TTL_SECONDS", "30"))
        self.link_ttl = int(e.get("ACCOUNT_LINK_TTL_SECONDS", "20"))
        self.restart_hook = e.get("FK_RESTART_APP", "")
        self.workspace = e.get("FK_WORKSPACE", "/app")
        self.nonce = secrets.token_hex(3)
        self.rnd = random.Random(int(e.get("FK_SEED", "20261110")))
        self.baseline = self._baseline()
        self.model = Model(self.baseline)
        self.clients: dict[str, Client] = {}
        self.names: dict[str, str] = {}
        self.secrets: list[str] = [PASSWORD]       # plain secrets that must never be stored (R2.8)
        self.session_values: list[str] = []
        self.n = 0

    # -- infrastructure ------------------------------------------------------------------------------
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

    def db_secret_hits(self, needles: list[str], skip=()) -> list[str]:
        """table.column of every text-like column holding any needle, over one connection, one query per column."""
        needles = [n for n in dict.fromkeys(needles) if n]
        hits = []
        if not needles:
            return hits
        with psycopg.connect(self.db_url, connect_timeout=10) as c:
            cols = c.execute("SELECT table_schema, table_name, column_name FROM information_schema.columns WHERE table_schema NOT IN "
                             "('pg_catalog','information_schema') AND data_type IN ('text','character varying','jsonb','json','bytea',"
                             "'character', 'ARRAY')").fetchall()
            for schema, table, column in cols:
                if table in skip:
                    continue
                q = (f'SELECT 1 FROM "{schema}"."{table}" t WHERE EXISTS (SELECT 1 FROM unnest(%s::text[]) AS n '
                     f'WHERE position(n in t."{column}"::text) > 0) LIMIT 1')
                if c.execute(q, (needles,)).fetchone():
                    hits.append(f"{table}.{column}")
        return hits

    def client(self) -> Client:
        return Client(self.base)

    def uid(self, tag: str = "p") -> str:
        self.n += 1
        return f"{tag}{self.n}.{self.nonce}"

    def restart_app(self) -> bool:
        if not self.restart_hook:
            return False
        # The hook is the verifier's own root-owned script (tests/test.sh writes it); no user input reaches it.
        return subprocess.run([self.restart_hook], check=False).returncode == 0

    # -- mail -----------------------------------------------------------------------------------------
    def mail_ids(self, email: str) -> set:
        return {m["id"] for m in self.mail.to(norm(email))}

    def wait_mail(self, email: str, subject_word: str, since: set, count: int = 1, timeout: float = 35.0) -> list[dict]:
        w = subject_word.lower()
        return self.mail.wait_for(norm(email), lambda m: m["id"] not in since and w in m["subject"].lower(), count, timeout)

    def new_mail(self, email: str, subject_word: str, since: set) -> list[dict]:
        w = subject_word.lower()
        return [m for m in self.mail.to(norm(email)) if m["id"] not in since and w in m["subject"].lower()]

    def link_in(self, msgs: list[dict], fragment: str) -> str | None:
        for m in reversed(msgs):
            for l in links(m["text"] or m["html"]):
                if l.startswith(self.base) and fragment in l:
                    return l
        return None

    # -- accounts -------------------------------------------------------------------------------------
    def signin(self, email: str, password: str = PASSWORD, client: Client | None = None) -> tuple[Client, Response]:
        c = client or self.client()
        r = submit_form(c, "/signin", "signin", {"email": email, "password": password}, follow=False)
        for h in r.header_all("Set-Cookie"):
            name, _, v = h.split(";", 1)[0].partition("=")
            if len(v.strip()) >= 16:
                self.session_values.append((name.strip(), v.strip()))
        return c, r

    def session_cookie_names(self, c: Client) -> set[str]:
        """Names of the cookies that alone keep `c` signed in (the session identifiers of R2.6)."""
        out = set()
        for k, v in c.cookies.items():
            probe = self.client()
            probe.cookies = {k: v}
            if self.signed_in(probe):
                out.add(k)
        return out

    def signed_in(self, c: Client) -> bool:
        """Signed in when /tickets, after any redirects, is not the sign-in page and shows the sign-out button."""
        r = c.get("/tickets", follow=True)
        path = urllib.parse.urlsplit(r.url).path
        return r.status == 200 and not path.startswith("/signin") and r.dom.attr_one("data-action", "signout") is not None

    def signup(self, email: str, name: str, password: str = PASSWORD) -> Response:
        return submit_form(self.client(), "/signup", "signup", {"email": email, "full_name": name, "password": password},
                           follow=True)

    def account(self, email: str | None = None, name: str | None = None, tag: str = "buyer") -> tuple[str, Client]:
        """A verified, signed-in attendee account (sign-up, verification link, sign-in)."""
        email = email or f"{self.uid(tag)}@verify.example"
        key = norm(email)
        if key in self.clients:
            return key, self.clients[key]
        name = name or f"{tag.title()} {self.n}"
        before = self.mail_ids(email)
        r = self.signup(email, name)
        if r.status >= 400:
            raise StepFailed(f"sign-up for {email} answered {r.status}")
        msgs = self.wait_mail(email, "verify", before)
        link = self.link_in(msgs, "/verify/")
        if not link:
            raise StepFailed(f"no verification link reached {email}")
        self.secrets.append(link.rstrip("/").rsplit("/", 1)[-1])
        v = self.client().get(link, follow=True)
        if v.status >= 400:
            raise StepFailed(f"the verification link answered {v.status}")
        c, _ = self.signin(email.strip())
        if not self.signed_in(c):
            raise StepFailed(f"{email} could not sign in after verifying")
        self.clients[key] = c
        self.names[key] = name
        return key, c

    def reset_password(self, email: str, password: str = PASSWORD) -> bool:
        before = self.mail_ids(email)
        submit_form(self.client(), "/password-reset", "request-reset", {"email": email}, follow=True)
        msgs = self.wait_mail(email, "reset", before)
        link = self.link_in(msgs, "/password-reset/")
        if not link:
            return False
        self.secrets.append(link.rstrip("/").rsplit("/", 1)[-1])
        c = self.client()
        page = c.get(link, follow=True)
        if page.status != 200:
            return False
        r = submit_form(c, link, "set-password", {"password": password}, follow=True, page=page)
        return r.status < 400

    def staff(self, email: str) -> Client:
        """A member of an existing organization: password via R2.3, then sign in."""
        key = norm(email)
        if key in self.clients:
            return self.clients[key]
        if not self.reset_password(key):
            raise StepFailed(f"could not set a password for {email} through /password-reset")
        c, r = self.signin(key)
        if not self.signed_in(c):
            raise StepFailed(f"sign-in for {email} did not produce a signed-in session (status {r.status})")
        self.clients[key] = c
        return c

    # -- organizations ----------------------------------------------------------------------------------
    def create_org(self, c: Client, name: str) -> int:
        r = submit_form(c, "/orgs/new", "create-org", {"name": name}, follow=False)
        m = re.search(r"/orgs/(\d+)", r.location or "")
        if r.status not in REDIRECTS or not m:
            raise StepFailed(f"creating an organization answered {r.status} without redirecting to /orgs/<id>")
        return int(m.group(1))

    def invite(self, owner: Client, org: int, email: str, role: str) -> Response:
        return submit_form(owner, f"/orgs/{org}/members", "invite-member", {"email": email, "role": role}, follow=True)

    def add_member(self, owner: Client, org: int, role: str, email: str | None = None, name: str | None = None) -> tuple[str, Client]:
        email, c = self.account(email, name, tag=role.replace("_", ""))
        before = self.mail_ids(email)
        r = self.invite(owner, org, email, role)
        if r.status >= 400:
            raise StepFailed(f"inviting {email} as {role} answered {r.status}")
        link = self.link_in(self.wait_mail(email, "invitation", before), "/invites/")
        if not link:
            raise StepFailed(f"no invitation link reached {email}")
        self.secrets.append(link.rstrip("/").rsplit("/", 1)[-1])
        page = c.get(link, follow=True)
        r = submit_form(c, link, "accept-invite", {}, follow=True, page=page)
        if r.status >= 400:
            raise StepFailed(f"accepting the invitation answered {r.status}")
        return email, c

    # -- venues and events ---------------------------------------------------------------------------------
    def create_venue(self, c: Client, org: int, name: str, tz: str, sections: list[tuple], address: str = "1 Main St") -> int:
        r = submit_form(c, f"/orgs/{org}/venues/new", "create-venue", {"name": name, "address": address, "timezone": tz},
                        follow=True)
        node = r.dom.attr_one("data-venue-id")
        if r.status >= 400 or node is None:
            raise StepFailed(f"creating venue {name!r} answered {r.status} without data-venue-id")
        vid = int(node.attrs["data-venue-id"])
        for code, sname, rows, per_row, acc in sections:
            rr = submit_form(c, f"/orgs/{org}/venues/{vid}", "add-section",
                             {"code": code, "name": sname, "rows": rows, "seats_per_row": str(per_row), "accessible": " ".join(acc)},
                             follow=True)
            if rr.status >= 400:
                raise StepFailed(f"adding section {code} answered {rr.status}")
        self.model.venue_tz[vid] = tz
        self._venue_sections = getattr(self, "_venue_sections", {})
        self._venue_sections[vid] = sections
        return vid

    @staticmethod
    def section_seats(code: str, rows: str, per_row: int) -> list[str]:
        a, b = rows.split("-")
        letters = [chr(x) for x in range(ord(a), ord(b) + 1)]
        return [f"{code}-{r}-{n}" for r in letters for n in range(1, per_row + 1)]

    def create_event(self, c: Client, org: int, venue: int, name: str, start_wall: str, on_sale_wall: str, fee: int,
                     prices: dict[str, int], off_sale_wall: str = "", presale_wall: str = "", presale_code: str = "",
                     description: str = "An evening of music.", follow: bool = True) -> tuple[Response, int | None]:
        fields = {"venue_id": str(venue), "name": name, "description": description, "starts_at": start_wall,
                  "on_sale_at": on_sale_wall, "off_sale_at": off_sale_wall, "presale_at": presale_wall,
                  "presale_code": presale_code, "facility_fee_cents": str(fee)}
        for code, p in prices.items():
            fields[f"price_{code}"] = str(p)
        page = c.get(f"/orgs/{org}/events/new?venue_id={venue}", follow=True)
        r = submit_form(c, f"/orgs/{org}/events/new", "create-event", fields, follow=follow, page=page)
        node = r.dom.attr_one("data-event-id")
        if r.status >= 400 or node is None:
            return r, None
        eid = int(node.attrs["data-event-id"])
        tz = self.model.venue_tz[venue]
        sections = {}
        for code, sname, rows, per_row, acc in self._venue_sections[venue]:
            sections[code] = Section(code, sname, self.section_seats(code, rows, per_row),
                                     {f"{code}-{a[0]}-{a[1:]}" for a in acc}, prices[code])
        self.model.add_event(Event(eid, org, venue, tz, wall_to_instant(start_wall, tz), fee, sections, name, False, "draft"))
        return r, eid

    def manage(self, c: Client, org: int, event: int) -> dict:
        """The event manage page's markers (R19), each read from the page: present, status, starts_at."""
        r = c.get(f"/orgs/{org}/events/{event}", follow=True)
        present = r.dom.attr_one("data-event-id", str(event)) is not None
        return {"page": r, "present": present, "status": page_attr(r.dom, "data-event-status") if present else None,
                "starts_at": page_attr(r.dom, "data-starts-at") if present else None}

    def publish(self, c: Client, org: int, event: int) -> Response:
        r = submit_form(c, f"/orgs/{org}/events/{event}", "publish-event", {}, follow=True)
        if r.status < 400:
            self.model.events[event].status = "published"
        return r

    def setup_event(self, owner: Client, org: int, venue: int, name: str, start: datetime, prices: dict[str, int],
                    fee: int = 200, on_sale: datetime | None = None, **kw) -> int:
        tz = self.model.venue_tz[venue]
        on_sale = on_sale or (now() - timedelta(minutes=5))
        r, eid = self.create_event(owner, org, venue, name, instant_to_wall(start, tz), instant_to_wall(on_sale, tz), fee,
                                   prices, **kw)
        if eid is None:
            raise StepFailed(f"creating event {name!r} answered {r.status}: {visible_text(r.text)[:200]}")
        p = self.publish(owner, org, eid)
        if p.status >= 400:
            raise StepFailed(f"publishing event {eid} answered {p.status}")
        return eid

    def quick_world(self, tag: str, tz: str = "America/New_York", sections=None) -> dict:
        """A fresh organization (owner account) with one venue."""
        email, owner = self.account(tag=f"{tag}owner")
        org = self.create_org(owner, f"{tag.title()} Presents {self.nonce}")
        sections = sections or [("BALC", "Balcony", "A-B", 10, []), ("FLOOR", "Floor", "A-D", 12, ["A1", "A2"])]
        venue = self.create_venue(owner, org, f"{tag.title()} Hall", tz, sections)
        return {"owner": owner, "owner_email": email, "org": org, "venue": venue, "tz": tz}

    def start_in(self, tz: str, minutes: float) -> datetime:
        """The first whole minute at least `minutes` from now whose local wall-clock time names it exactly. In the
        hour a fall-back repeats, a typed wall-clock time means its first occurrence (R5.2), so an instant in the
        second pass cannot be typed; such minutes are skipped (by at most an hour)."""
        t = now() + timedelta(minutes=minutes)
        t = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
        while not exact_wall(t, tz):
            t += timedelta(minutes=1)
        return t

    # -- event pages -----------------------------------------------------------------------------------------
    def event_page(self, c: Client, event: int) -> Response:
        return c.get(f"/events/{event}", follow=True)

    def seat_states(self, c: Client, event: int) -> dict[str, str]:
        r = self.event_page(c, event)
        return {n.attrs["data-seat"]: n.attrs.get("data-seat-status", "") for n in r.dom.attr_all("data-seat")
                if "data-seat-status" in n.attrs}

    # -- holds, orders, payment -----------------------------------------------------------------------------
    def hold(self, c: Client, event: int, seats: list[str], presale_code: str | None = None,
             accessible: bool = False, buyer: str | None = None) -> tuple[Response, int | None]:
        fields: dict = {"seats": list(seats)}
        if presale_code is not None:
            fields["presale_code"] = presale_code
        if accessible:
            fields["accessible"] = "1"
        page = self.event_page(c, event)
        try:
            r = submit_form(c, f"/events/{event}", "hold-seats", fields, follow=False, page=page)
        except FormNotFound:
            tok = [(i.attrs.get("name"), i.attrs.get("value", "")) for i in page.dom.find_all(
                lambda n: n.tag == "input" and "data-csrf" in n.attrs)]
            body = tok + [("seats", s) for s in seats] + ([("presale_code", presale_code)] if presale_code is not None else []) \
                + ([("accessible", "1")] if accessible else [])
            r = c.post_form(f"/events/{event}/hold", body)
        m = re.search(r"/orders/(\d+)", r.location or "")
        oid = int(m.group(1)) if r.status in REDIRECTS and m else None
        if oid and buyer:
            ex = parse_inst(self.order(c, oid).get("hold_expires_at"))
            self.model.add_order(Order(oid, event, norm(buyer), self.names.get(norm(buyer), ""), list(seats),
                                       self.model.quote_for(event, seats), "pending", ex))
        return r, oid

    def order(self, c: Client, oid: int) -> dict:
        r = c.get(f"/orders/{oid}", follow=True)
        out: dict = {"status_code": r.status, "page": r}
        if r.dom.attr_one("data-order-id", str(oid)) is None:
            return out
        dom = r.dom
        a = {k: page_attr(dom, k, ("data-seat",)) for k in (
            "data-order-status", "data-hold-expires-at", "data-subtotal-cents", "data-discount-cents", "data-service-fee-cents",
            "data-facility-fee-cents", "data-tax-cents", "data-total-cents")}   # order-level: not on a ticket line
        out.update({"status": a.get("data-order-status"), "hold_expires_at": a.get("data-hold-expires-at"),
                    "subtotal": a.get("data-subtotal-cents"), "discount": a.get("data-discount-cents"),
                    "service": a.get("data-service-fee-cents"), "facility": a.get("data-facility-fee-cents"),
                    "tax": a.get("data-tax-cents"), "total": a.get("data-total-cents")})
        lines = {}
        for n in r.dom.attr_all("data-seat"):
            if "data-face-cents" in n.attrs:
                lines[n.attrs["data-seat"]] = {k: n.attrs.get(f"data-{k}") for k in
                                               ("face-cents", "discount-cents", "service-fee-cents", "facility-fee-cents",
                                                "tax-cents", "total-cents", "ticket-id")}
        out["lines"] = lines
        out["codes"] = sorted(n.attrs.get("data-applied-code", "") for n in r.dom.attr_all("data-applied-code"))
        out["has_pay"] = r.dom.attr_one("data-action", "pay") is not None
        out["has_release"] = r.dom.attr_one("data-action", "release-hold") is not None
        out["has_apply"] = r.dom.attr_one("data-action", "apply-code") is not None
        return out

    @staticmethod
    def quote_diff(o: dict, q: M.Quote) -> list[str]:
        probs = []
        want_tot = {"subtotal": q.subtotal, "discount": q.discount, "service": q.service, "facility": q.facility,
                    "tax": q.tax, "total": q.total}
        per_ticket_facility = q.lines[0].facility if q.lines else 0
        for k, v in want_tot.items():
            if k == "facility" and o.get(k) == str(per_ticket_facility):
                continue   # R19 names the order's data-facility-fee-cents without saying total or per ticket (R5.1: per ticket)
            if o.get(k) != str(v):
                probs.append(f"{k} {o.get(k)} want {v}")
        for l in q.lines:
            got = (o.get("lines") or {}).get(l.key)
            if got is None:
                probs.append(f"no line for {l.key}")
                continue
            want = {"face-cents": l.face, "discount-cents": l.discount, "service-fee-cents": l.service,
                    "facility-fee-cents": l.facility, "tax-cents": l.tax, "total-cents": l.total}
            d = [f"{k} {got.get(k)} want {v}" for k, v in want.items() if got.get(k) != str(v)]
            if d:
                probs.append(f"{l.key}: " + ", ".join(d))
        return probs

    def apply_code(self, c: Client, oid: int, code: str) -> Response:
        page = c.get(f"/orders/{oid}", follow=True)
        try:
            return submit_form(c, f"/orders/{oid}", "apply-code", {"code": code}, follow=True, page=page)
        except FormNotFound:
            tok = [(i.attrs.get("name"), i.attrs.get("value", "")) for i in page.dom.find_all(
                lambda n: n.tag == "input" and "data-csrf" in n.attrs)]
            return c.post_form(f"/orders/{oid}/codes", tok + [("code", code)], follow=True)

    def remove_code(self, c: Client, oid: int, code: str) -> Response:
        page = c.get(f"/orders/{oid}", follow=True)
        for form in page.dom.find_all(lambda n: n.tag == "form" and f"/codes/{code}/remove" in (n.attrs.get("action") or "")):
            return c.post_form(f"/orders/{oid}/codes/{code}/remove", form_fields(form), follow=True)
        raise StepFailed(f"no remove form for code {code} on order {oid}")

    def csrf(self, c: Client, path: str = "/tickets") -> list[tuple[str, str]]:
        page = c.get(path, follow=True)
        return [(i.attrs.get("name"), i.attrs.get("value", "")) for i in page.dom.find_all(
            lambda n: n.tag == "input" and "data-csrf" in n.attrs)]

    def post_with_token(self, c: Client, path: str, fields: list | None = None, token_from: str = "/tickets",
                        follow: bool = False) -> Response:
        return c.post_form(path, self.csrf(c, token_from) + list(fields or []), follow=follow)

    def release(self, c: Client, oid: int) -> Response:
        page = c.get(f"/orders/{oid}", follow=True)
        try:
            r = submit_form(c, f"/orders/{oid}", "release-hold", {}, follow=True, page=page)
        except FormNotFound:
            r = self.post_with_token(c, f"/orders/{oid}/release", token_from=f"/orders/{oid}", follow=True)
        if r.status < 400 and oid in self.model.orders:
            self.model.orders[oid].status = "released"
        return r

    def pay_click(self, c: Client, oid: int) -> tuple[Response, str | None]:
        page = c.get(f"/orders/{oid}", follow=True)
        try:
            r = submit_form(c, f"/orders/{oid}", "pay", {}, follow=False, page=page)
        except FormNotFound:
            r = self.post_with_token(c, f"/orders/{oid}/pay", token_from=f"/orders/{oid}")
        loc = r.location or ""
        m = re.search(r"/pay/([A-Za-z0-9_]+)", loc)
        sid = m.group(1) if r.status in REDIRECTS and m and loc.startswith(self.pay.api) else None
        if sid and oid in self.model.orders:
            self.model.orders[oid].session = sid
        return r, sid

    def wait_status(self, c: Client, oid: int, statuses, timeout: float = 90.0) -> dict:
        if isinstance(statuses, str):
            statuses = (statuses,)
        end = time.monotonic() + timeout
        o = {}
        while time.monotonic() < end:
            o = self.order(c, oid)
            if o.get("status") in statuses:
                return o
            time.sleep(0.5)
        return o

    def settle_paid(self, c: Client, oid: int, window: tuple, timeout: float = 90.0) -> dict[str, int]:
        o = self.wait_status(c, oid, "paid", timeout)
        if o.get("status") != "paid":
            raise StepFailed(f"order {oid} is {o.get('status')!r}, not paid, {timeout:.0f} s after a completed payment")
        ids = {}
        for seat, line in (o.get("lines") or {}).items():
            if not (line.get("ticket-id") or "").isdigit():
                raise StepFailed(f"paid order {oid} line {seat} has no data-ticket-id")
            ids[seat] = int(line["ticket-id"])
        if oid in self.model.orders:
            self.model.issue(oid, ids, window)
        return ids

    def buy(self, c: Client, buyer: str, event: int, seats: list[str], codes: list[str] = (),
            presale_code: str | None = None, accessible: bool = False) -> tuple[int, dict[str, int]]:
        """Hold, apply codes, pay with a good card, wait until paid. Returns (order id, {seat: ticket id})."""
        r, oid = self.hold(c, event, seats, presale_code, accessible, buyer=buyer)
        if not oid:
            raise StepFailed(f"holding {seats} answered {r.status}: {visible_text(r.text)[:200]}")
        ev = self.model.events[event]
        cobjs = []
        for code in codes:
            rr = self.apply_code(c, oid, code)
            if rr.status >= 400:
                raise StepFailed(f"applying {code} answered {rr.status}")
            cobjs.append(self.codes[(event, code.upper())])
        self.model.orders[oid].quote = self.model.quote_for(event, seats, cobjs)
        pr, sid = self.pay_click(c, oid)
        if not sid:
            raise StepFailed(f"Pay on order {oid} answered {pr.status} {pr.location!r}, not a redirect to the provider")
        t0 = now()
        st, body = self.pay.pay(sid, CARD_OK)
        if st != 200:
            raise StepFailed(f"the provider refused the payment for order {oid}: {st} {body}")
        ids = self.settle_paid(c, oid, (t0 - timedelta(seconds=2), now() + timedelta(seconds=2)))
        _ = ev
        return oid, ids

    codes: dict = {}

    def create_code(self, c: Client, org: int, event: int, code: str, kind: str, value: int, min_sub: int = 0,
                    max_red: int | None = None) -> Response:
        r = submit_form(c, f"/orgs/{org}/events/{event}/codes", "create-code",
                        {"code": code, "kind": kind, "value": str(value), "min_subtotal_cents": str(min_sub),
                         "max_redemptions": "" if max_red is None else str(max_red)}, follow=True)
        if r.status < 400:
            self.codes[(event, code.upper())] = M.Code(code.upper(), kind, value, min_sub, max_red)
        return r

    # -- tickets ------------------------------------------------------------------------------------------
    def ticket(self, c: Client, tid: int) -> dict:
        r = c.get(f"/tickets/{tid}", follow=True)
        out = {"status_code": r.status, "page": r}
        if r.dom.attr_one("data-ticket-id", str(tid)) is None:
            return out
        a = {k: page_attr(r.dom, k) for k in ("data-ticket-status", "data-seat", "data-holder-email", "data-admitted-at")}
        tok = r.dom.attr_one("data-qr-token")
        out.update({"status": a.get("data-ticket-status"), "seat": a.get("data-seat"), "holder": a.get("data-holder-email"),
                    "admitted_at": a.get("data-admitted-at"), "token": tok.attrs.get("data-qr-token") if tok else None})
        for k in ("data-full-refund-until", "data-half-refund-until", "data-refund-percent"):
            node = r.dom.attr_one(k)
            out[k[5:]] = node.attrs.get(k) if node else None
        out["has_refund"] = r.dom.attr_one("data-action", "request-refund") is not None
        out["has_transfer"] = r.dom.attr_one("data-action", "transfer-ticket") is not None
        return out

    def token(self, c: Client, tid: int) -> str:
        t = self.ticket(c, tid)
        if not t.get("token"):
            raise StepFailed(f"ticket {tid} page shows its holder no data-qr-token")
        mt = self.model.tickets.get(tid)
        if mt is not None and t["token"] not in mt.tokens:
            mt.tokens.append(t["token"])
        return t["token"]

    def request_refund(self, c: Client, tid: int) -> Response:
        page = c.get(f"/tickets/{tid}", follow=True)
        try:
            return submit_form(c, f"/tickets/{tid}", "request-refund", {}, follow=True, page=page)
        except FormNotFound:
            return self.post_with_token(c, f"/tickets/{tid}/refund", token_from=f"/tickets/{tid}", follow=True)

    def transfer(self, c: Client, tid: int, to_email: str, to_name: str) -> Response:
        page = c.get(f"/tickets/{tid}", follow=True)
        try:
            return submit_form(c, f"/tickets/{tid}", "transfer-ticket", {"to_email": to_email, "to_name": to_name},
                               follow=True, page=page)
        except FormNotFound:
            return self.post_with_token(c, f"/tickets/{tid}/transfer", [("to_email", to_email), ("to_name", to_name)],
                                        token_from=f"/tickets/{tid}", follow=True)

    def org_refund(self, c: Client, org: int, event: int, tid: int) -> Response:
        page = c.get(f"/orgs/{org}/events/{event}/orders", follow=True)
        for form in page.dom.find_all(lambda n: n.tag == "form" and f"/tickets/{tid}/refund" in (n.attrs.get("action") or "")):
            return c.post_form(f"/orgs/{org}/tickets/{tid}/refund", form_fields(form), follow=True)
        return self.post_with_token(c, f"/orgs/{org}/tickets/{tid}/refund", token_from=f"/orgs/{org}/events/{event}/orders",
                                    follow=True)

    def wait_ticket(self, c: Client, tid: int, status: str, timeout: float = 90.0) -> dict:
        end = time.monotonic() + timeout
        t = {}
        while time.monotonic() < end:
            t = self.ticket(c, tid)
            if t.get("status") == status:
                return t
            time.sleep(0.5)
        return t

    def refunds_for_ticket(self, tid: int, st: dict | None = None) -> list[dict]:
        st = st or self.pay.state()
        return [r for r in st.get("refunds", {}).values() if str((r.get("metadata") or {}).get("ticket_id")) == str(tid)]

    # -- scanner devices and scans ---------------------------------------------------------------------
    def create_device(self, c: Client, org: int, event: int, label: str) -> tuple[int, str]:
        r = submit_form(c, f"/orgs/{org}/events/{event}/devices", "create-device", {"label": label}, follow=True)
        did = r.dom.attr_one("data-device-id")
        key = r.dom.attr_one("data-device-key")
        if r.status >= 400 or did is None or key is None:
            raise StepFailed(f"creating a scanner device answered {r.status} without data-device-id/data-device-key")
        k = key.attrs["data-device-key"]
        self.secrets.append(k)
        return int(did.attrs["data-device-id"]), k

    def scan(self, key: str, token: str, gate: str) -> Response:
        r = self.client().post_json("/api/scan", {"token": token, "gate": gate}, headers={"Authorization": f"Bearer {key}"})
        return ScanResponse(r.status, r.headers, r.body, r.url)

    def scan_batch(self, key: str, rows: list[dict]) -> Response:
        return self.client().post_json("/api/scan/batch", {"scans": rows}, headers={"Authorization": f"Bearer {key}"})

    # -- dashboard and export ----------------------------------------------------------------------------
    FIGS = {"capacity": "data-capacity", "sold": "data-sold", "held": "data-held", "available": "data-available",
            "admitted": "data-admitted", "gross": "data-gross-cents", "refunded": "data-refunded-cents", "net": "data-net-cents"}

    def dashboard(self, c: Client, org: int, event: int) -> tuple[Response, dict | None]:
        r = c.get(f"/orgs/{org}/events/{event}/dashboard", follow=False)
        root = r.dom.attr_one("data-dashboard-event-id", str(event)) if r.status == 200 else None
        if root is None:
            return r, None
        tot = {k: root.attrs.get(a) for k, a in self.FIGS.items()}
        secs = {}
        for n in r.dom.attr_all("data-section"):
            if "data-capacity" in n.attrs:
                secs[n.attrs["data-section"]] = {k: n.attrs.get(a) for k, a in self.FIGS.items()}
        return r, {"total": tot, "sections": secs}

    @staticmethod
    def dash_diff(got: dict | None, want: dict) -> list[str]:
        if got is None:
            return ["no dashboard markers"]
        probs = [f"total {k} {got['total'].get(k)} want {v}" for k, v in want["total"].items() if got["total"].get(k) != str(v)]
        for code, figs in want["sections"].items():
            g = got["sections"].get(code)
            if g is None:
                probs.append(f"no section {code}")
                continue
            probs += [f"{code} {k} {g.get(k)} want {v}" for k, v in figs.items() if g.get(k) != str(v)]
        return probs

    def export(self, c: Client, org: int, event: int) -> tuple[Response, list[dict] | None, str]:
        r = c.get(f"/orgs/{org}/events/{event}/attendees.csv", follow=False)
        if r.status != 200:
            return r, None, ""
        raw = r.body.decode("utf-8-sig", "replace")
        data = list(csv.reader(io.StringIO(raw, newline="")))
        if not data or data[0] != CSV_HEADER:
            return r, None, raw
        return r, [dict(zip(CSV_HEADER, row)) for row in data[1:] if len(row) == len(CSV_HEADER)], raw

    def export_diff(self, got: list[dict] | None, want: list[dict]) -> list[str]:
        if got is None:
            return ["no CSV with the exact header"]
        probs = []
        if [g["ticket_id"] for g in got] != [w["ticket_id"] for w in want]:
            gi, wi = [g["ticket_id"] for g in got], [w["ticket_id"] for w in want]
            first = next((i for i, (a, b) in enumerate(zip(gi, wi)) if a != b), min(len(gi), len(wi)))
            probs.append(f"rows/order differ: {len(gi)} rows vs {len(wi)} expected; first difference at row {first + 1}: "
                         f"got {gi[first:first + 3]} want {wi[first:first + 3]}")
            return probs
        for g, w in zip(got, want):
            for k in CSV_HEADER:
                if k == "purchased_at" and w[k] is None:
                    lo, hi = w["_window"]
                    gt = parse_inst(g[k])
                    if gt is None or not (lo - timedelta(seconds=60) <= gt <= hi + timedelta(seconds=60)) \
                            or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", g[k]):
                        probs.append(f"ticket {w['ticket_id']} purchased_at {g[k]!r} not within {inst(lo)}..{inst(hi)}")
                elif g[k] != w[k]:
                    probs.append(f"ticket {w['ticket_id']} {k} {g[k]!r} want {w[k]!r}")
        return probs


# -- waiting lists (W13, W17) -----------------------------------------------------------------------
def section_node(page, code):
    """The element of a section that carries data-available (R19)."""
    for n in page.dom.attr_all("data-section", code):
        if "data-available" in n.attrs:
            return n
    return None


def join(c, event: int, code: str, qty: int):
    """Join a section's waiting list through its form (R14.1)."""
    page = H.event_page(c, event)
    for form in page.dom.find_all(lambda n: n.tag == "form" and f"/sections/{code}/waitlist" in (n.attrs.get("action") or "")):
        return c.post_form(f"/events/{event}/sections/{code}/waitlist",
                           [x for x in form_fields(form) if x[0] != "quantity"] + [("quantity", str(qty))], follow=True)
    return H.post_with_token(c, f"/events/{event}/sections/{code}/waitlist", [("quantity", str(qty))], token_from=f"/events/{event}", follow=True)


def offer_page(c, link: str) -> dict:
    """Markers and controls of a waitlist offer page (R19)."""
    r = c.get(link, follow=True)
    n = r.dom.attr_one("data-offer-status")
    s = r.dom.attr_one("data-offer-seats")
    return {"code": r.status, "page": r, "status": n.attrs.get("data-offer-status") if n else None,
            "seats": s.attrs.get("data-offer-seats") if s else None,
            "accept": r.dom.attr_one("data-action", "accept-offer") is not None,
            "decline": r.dom.attr_one("data-action", "decline-offer") is not None}
