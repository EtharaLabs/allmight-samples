"""Pickpath verifier harness: drives the submission through its interface contract and keeps the reference world."""

from __future__ import annotations

import csv
import html
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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))  # Reward Kit imports every file standalone

import psycopg  # noqa: E402

from kitlib.mail import Mail, links  # noqa: E402
from kitlib.web import Client, FormNotFound, Response, find_form, form_fields, submit_form, visible_text  # noqa: E402
from kitlib.wf import InfraError, StepFailed  # noqa: E402
from paylib import Payments  # noqa: E402
from reference import rules as R  # noqa: E402
from reference.world import Cust, World  # noqa: E402

PASSWORD = "Correct-Horse-9"
CARD_OK, CARD_DECLINED = "4242424242424242", "4000000000000002"
FIN, FIN2 = "priya.raman@larderline.example", "tom.becker@larderline.example"
SUP, SUP2 = "grace.okafor@larderline.example", "luis.ortega@larderline.example"
ONHAND_HEADER = ["lot_id", "sku", "lot_code", "bin", "expiry_date", "on_hand", "reserved", "available", "status"]
ORDERS_HEADER = ["number", "customer_id", "customer_name", "placed_at", "status", "po_number", "ship_date", "total_cents"]
REDIRECTS = (301, 302, 303, 307, 308)  # "redirects to" in the contract names no status: any of these counts


def key_needles(key: str) -> list[str]:
    """What must never be stored or shown again of an API key (R2.6, R4.6): the full key and its secret part, i.e.
    the text after a short leading prefix such as `ppk_`, and the text after the key's last separator when that is
    long enough to be a secret. A shorter public lookup id or prefix, which an application may store or show, is not
    a needle."""
    out = [key]
    m = re.match(r"^[A-Za-z]{1,12}[_-](.+)$", key)
    if m and len(m.group(1)) >= 16:
        out.append(m.group(1))
    tail = re.split(r"[._-]", key)[-1]
    if len(tail) >= 16:
        out.append(tail)
    return list(dict.fromkeys(out))


def parse_csv(body: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body.decode("utf-8-sig", "replace"), newline="")))


def page_attr(dom, node, name: str, default=None):
    """A marker the contract says a page "carries": read from the element with the id marker when it has it,
    else from the first element of the page that has it."""
    if node is not None and name in node.attrs:
        return node.attrs[name]
    other = dom.attr_one(name)
    return other.attrs[name] if other is not None else default


class Harness:
    def __init__(self):
        e = os.environ
        self.base = e["PUBLIC_BASE_URL"].rstrip("/")
        self.db_url = e["DATABASE_URL"]
        self.mail = Mail(e["MAILSINK_HTTP_URL"])
        self.pay_api = Payments()
        self.reset_ttl = int(e.get("RESET_LINK_TTL_SECONDS", "90"))
        self.restart_hook = e.get("FK_RESTART_APP", "")
        self.workspace = e.get("FK_WORKSPACE", "/app")
        self.nonce = secrets.token_hex(3)
        self.rnd = random.Random(int(e.get("FK_SEED", "20261110")))
        self.baseline = self._baseline()
        self.world = World(self.baseline)
        self.tz = ZoneInfo(self.baseline["tz"])
        self.clients: dict[str, Client] = {}
        self.reset_tokens: list[str] = []
        self.session_values: list[tuple[str, str]] = []   # (cookie name, value) of every cookie set at sign-in
        self.session_names: set[str] | None = None        # the session cookie(s), once identified
        self.api_keys: list[str] = []
        self.api_key_ids: dict[str, str] = {}             # key -> its data-api-key-id, from the page shown at creation
        self.user_ids: dict[str, int] = {}
        self.n = 0

    # -- basics --------------------------------------------------------------------------------------------
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

    def today(self) -> date:
        return datetime.now(self.tz).date()

    def uid(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.nonce.upper()}{self.n:03d}"

    def client(self) -> Client:
        return Client(self.base)

    # -- accounts ------------------------------------------------------------------------------------------
    def request_reset(self, email: str, client: Client | None = None) -> Response:
        return submit_form(client or self.client(), "/password-reset", "request-reset", {"email": email}, follow=True)

    def mail_ids(self, email: str) -> set:
        return {m["id"] for m in self.mail.to(email.strip().lower())}

    def reset_link(self, email: str, since: set, timeout: float = 35.0) -> str | None:
        msgs = self.mail.wait_for(email.strip().lower(),
                                  lambda m: m["id"] not in since and "/password-reset/" in ((m["text"] or "") + (m["html"] or "")), 1, timeout)
        for m in reversed(msgs):
            # The text part first; an HTML-only email's visible text has no hrefs, so its HTML is read too.
            for l in links(m["text"] or "") + links(html.unescape(m["html"] or "")):
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
        for name, v in self.set_cookies(r).items():
            if len(v) >= 16:
                self.session_values.append((name, v))
        return c, r

    @staticmethod
    def set_cookies(r: Response) -> dict[str, str]:
        """Cookies a response sets (name -> value), deletions left out."""
        out = {}
        for h in r.header_all("Set-Cookie"):
            first = h.split(";", 1)[0]
            if "=" in first and first.split("=", 1)[1].strip():
                out[first.split("=", 1)[0].strip()] = first.split("=", 1)[1].strip()
        return out

    def session_cookie_names(self, c: Client, names) -> set[str]:
        """The session cookie(s) among `names` (cookies set at sign-in) of the signed-in client `c`: those whose
        removal signs the client out; failing that, those that sign a client in on their own; failing that, all of
        them. Only these are what R2.5 and R2.6 call session cookies and session identifiers."""
        names = [n for n in names if n in c.cookies]
        found = set()
        for n in names:
            probe = c.copy()
            probe.cookies.pop(n, None)
            if not self.signed_in(probe):
                found.add(n)
        if not found:
            for n in names:
                probe = c.copy()
                probe.cookies = {n: c.cookies[n]}
                if self.signed_in(probe):
                    found.add(n)
        found = found or set(names)
        if found and self.session_names is None:
            self.session_names = set(found)
        return found

    def signed_in(self, c: Client) -> bool:
        """Signed in when the home page, after any redirects, is not the sign-in page and has the sign-out button."""
        r = c.get("/", follow=True)
        path = urllib.parse.urlsplit(r.url).path.rstrip("/")
        return r.status == 200 and path != "/signin" and r.dom.attr_one("data-action", "signout") is not None

    def account(self, email: str) -> Client:
        key = email.strip().lower()
        if key in self.clients and self.signed_in(self.clients[key]):
            return self.clients[key]
        if not self.set_password(key):
            raise StepFailed(f"could not set a password for {key} through /password-reset")
        c, r = self.signin(key)
        if not self.signed_in(c):
            raise StepFailed(f"sign-in for {key} did not produce a signed-in session (status {r.status})")
        self.clients[key] = c
        return c

    @property
    def fin(self) -> Client:
        return self.account(FIN)

    @property
    def sup(self) -> Client:
        return self.account(SUP)

    # -- setup through the application's forms (R3) --------------------------------------------------------------
    def _created(self, r: Response, pattern: str, what: str) -> int:
        m = re.search(pattern, r.location or "")
        if r.status not in REDIRECTS or not m:
            raise StepFailed(f"creating {what} answered {r.status} {visible_text(r.text)[:200]!r}")
        return int(m.group(1))

    def create_customer(self, tier="standard", state="NH", limit: int | None = None, min_days=0, split=True, backorders=True,
                        name: str | None = None) -> Cust:
        name = name or f"Verifier Foods {self.uid('C')}"
        r = submit_form(self.fin, "/customers/new", "create-customer", {
            "name": name, "tier": tier, "ship_to_state": state, "approval_limit_cents": "" if limit is None else str(limit),
            "min_shelf_life_days": str(min_days), "allow_split_cases": "true" if split else "false",
            "backorders_allowed": "true" if backorders else "false"}, follow=False)
        cid = self._created(r, r"/customers/(\d+)", "a customer")
        c = Cust(cid, tier, state, limit, min_days, split, backorders, name)
        self.world.customers[cid] = c
        return c

    def create_user(self, role: str, customer_id: int | None = None, zone: str | None = None, name: str | None = None) -> str:
        email = f"{role}.{self.uid('u').lower()}@verify.example"
        r = submit_form(self.fin, "/users/new", "create-user", {
            "email": email, "full_name": name or f"Verifier {role.title()} {self.n}", "role": role,
            "customer_id": "" if customer_id is None else str(customer_id), "zone": zone or ""}, follow=False)
        self.user_ids[email] = self._created(r, r"/users/(\d+)", f"a {role}")
        return email

    def create_key(self, c: Client, label: str = "integration") -> str:
        r = submit_form(c, "/account/api-keys", "create-api-key", {"label": label}, follow=True)
        node = r.dom.attr_one("data-api-key")
        if r.status >= 400 or node is None or not node.attrs.get("data-api-key"):
            raise StepFailed(f"creating an API key answered {r.status} without data-api-key")
        self.api_keys.append(node.attrs["data-api-key"])
        if node.attrs.get("data-api-key-id"):
            self.api_key_ids[node.attrs["data-api-key"]] = node.attrs["data-api-key-id"]
        return node.attrs["data-api-key"]

    def company(self, **kw) -> tuple[Cust, Client, str, str]:
        """A customer with one buyer (signed in) and the buyer's API key."""
        cust = self.create_customer(**kw)
        email = self.create_user("buyer", cust.cid)
        c = self.account(email)
        return cust, c, self.create_key(c), email

    def create_sku(self, pack=1, weight=200, dims=(20, 15, 10), storage="ambient", taxable=False, reorder=0,
                   name: str | None = None, code: str | None = None) -> str:
        code = code or self.uid("V")
        name = name or f"Verifier item {code}"
        r = submit_form(self.sup, "/skus/new", "create-sku", {
            "code": code, "name": name, "case_pack": str(pack), "unit_weight_g": str(weight), "length_cm": str(dims[0]),
            "width_cm": str(dims[1]), "height_cm": str(dims[2]), "storage": storage, "taxable": "true" if taxable else "false",
            "reorder_point": str(reorder)}, follow=False)
        sid = self._created(r, r"/skus/(\d+)", f"SKU {code}")
        self.world.add_sku(sid, code.upper(), name, pack, weight, dims[0], dims[1], dims[2], storage, taxable, reorder)
        return code.upper()

    def add_price(self, tier: str, code: str, cents: int, eff: date) -> Response:
        r = submit_form(self.fin, "/prices", "create-price", {"tier": tier, "sku": code, "unit_price_cents": str(cents),
                                                              "effective_from": eff.isoformat()}, follow=False)
        if r.status >= 400:
            raise StepFailed(f"adding a {tier} price for {code} answered {r.status}")
        self.world.add_price(tier, code, cents, eff)
        return r

    def price_all(self, code: str, cents: int, eff: date = date(2026, 1, 1)) -> None:
        for tier in ("standard", "gold", "platinum"):
            self.add_price(tier, code, cents, eff)

    def receive(self, code: str, expiry: date, bin_code: str, units: int, cost: str = "1.2500", lot_code: str | None = None,
                client: Client | None = None) -> int:
        lot_code = lot_code or self.uid("L")
        r = submit_form(client or self.sup, "/receipts/new", "receive", {
            "sku": code, "lot_code": lot_code, "expiry_date": expiry.isoformat(), "bin": bin_code, "units": str(units),
            "unit_cost": cost}, follow=False)
        lot_id = self._created(r, r"/lots/(\d+)", f"a receipt of {code}")
        self.world.receipt(lot_id, code, lot_code, expiry, bin_code, Decimal(cost), units)
        return lot_id

    # -- orders --------------------------------------------------------------------------------------------------
    def api(self, key: str, method: str, path: str, body=None) -> Response:
        c = self.client()
        h = {"Authorization": f"Bearer {key}"}
        if method == "GET":
            return c.get(path, headers=h)
        if isinstance(body, (bytes, str)):
            return c.request("POST", path, body=body if isinstance(body, bytes) else body.encode(),
                             headers={**h, "Content-Type": "application/json"})
        return c.post_json(path, body, headers=h)

    def api_order(self, key: str, ship: date, lines: list[tuple[str, int]], po: str = "", notes: str = "", extra: dict | None = None) -> Response:
        body = {"ship_date": ship.isoformat(), "po_number": po, "notes": notes, "lines": [{"sku": s, "qty": q} for s, q in lines]}
        body.update(extra or {})
        return self.api(key, "POST", "/api/v1/orders", body)

    def place(self, key: str, cust: Cust, ship: date, lines: list[tuple[str, int]], po: str = "", created_by: int = 0) -> int:
        r = self.api_order(key, ship, lines, po)
        if r.status != 201:
            raise StepFailed(f"POST /api/v1/orders answered {r.status}: {r.text[:300]}")
        try:
            oid = int(r.json()["id"])
        except Exception:
            raise StepFailed(f"order API answered without an id: {r.text[:300]}")
        self.world.add_order(oid, cust, ship, lines, created_by)
        return oid

    def order(self, key: str, oid: int) -> dict:
        r = self.api(key, "GET", f"/api/v1/orders/{oid}")
        if r.status != 200:
            raise StepFailed(f"GET /api/v1/orders/{oid} answered {r.status}")
        return r.json()

    def wait_order(self, key: str, oid: int, pred, timeout: float = 30.0, what: str = "the expected state") -> dict:
        end = time.monotonic() + timeout
        j = {}
        while time.monotonic() < end:
            j = self.order(key, oid)
            if pred(j):
                return j
            time.sleep(0.3)
        raise StepFailed(f"order {oid} did not reach {what} within {timeout:.0f} s (status {j.get('status')})")

    def start_checkout(self, c: Client, oid: int) -> tuple[Response, str | None]:
        page = c.get(f"/orders/{oid}", follow=True)
        try:
            r = submit_form(c, "", "pay-order", {}, page=page, follow=False)
        except FormNotFound:
            raise StepFailed(f"order {oid} page ({page.status}) has no pay-order form")
        loc = r.location or ""
        m = re.search(r"/pay/([A-Za-z0-9_]+)", loc)
        return r, (m.group(1) if (r.status in (302, 303) and loc.startswith(self.pay_api.api) and m) else None)

    def pay(self, c: Client, key: str, oid: int, card: str = CARD_OK, wait: bool = True) -> str:
        r, sid = self.start_checkout(c, oid)
        if not sid:
            raise StepFailed(f"paying order {oid} answered {r.status} -> {r.location!r}, not a redirect to the provider")
        st, body = self.pay_api.pay(sid, card)
        if card == CARD_OK and st != 200:
            raise StepFailed(f"the provider refused the payment of order {oid}: {st} {body}")
        if wait and card == CARD_OK:
            self.wait_order(key, oid, lambda j: j.get("status") != "awaiting_payment", 30, "a paid state")
            self.world.pay(oid)
        return sid

    def compare(self, key: str, oid: int, refunds_done: bool = True) -> list[str]:
        """Order JSON vs the world: status, per-line quantities and allocations, refund amounts, shipments, credits."""
        return self.diff_order(self.order(key, oid), oid)

    def count_and_compare(self, key: str, lot_id: int, counted: int, oids) -> tuple[list[str], list]:
        """Record an accepted count (R7.7) in the world and compare the given orders with it. R7.7 fixes the order
        released units leave their tasks in, not the order they are allocated anew in, so every order the text
        allows is tried (World.count_matching) and the one the application's orders match is kept. Returns the
        differences (from the release-order reading when no reading matches) and the released entries."""
        got = {oid: self.order(key, oid) for oid in oids}
        ok, released = self.world.count_matching(lot_id, counted, lambda: not any(self.diff_order(got[o], o) for o in oids))
        probs = [] if ok else [p for o in oids for p in self.diff_order(got[o], o)]
        return probs, released

    def diff_order(self, got: dict, oid: int) -> list[str]:
        want = self.world.order_view(oid)
        probs = []
        if got.get("status") != want["status"]:
            probs.append(f"order {oid} status {got.get('status')} want {want['status']}")
        gl = {l.get("line_no"): l for l in got.get("lines", [])}
        for wl in want["lines"]:
            g = gl.get(wl["line_no"])
            if g is None:
                probs.append(f"order {oid} line {wl['line_no']} missing")
                continue
            for k in ("qty", "reserved", "shipped", "backordered", "cancelled", "returned", "allocations"):
                if g.get(k) != wl[k]:
                    probs.append(f"order {oid} line {wl['line_no']} {k} {g.get(k)} want {wl[k]}")
        ga = [r.get("amount_cents") for r in got.get("refunds", [])]
        if ga != want["refunds"]:
            probs.append(f"order {oid} refunds {ga} want {want['refunds']}")
        gi = [s.get("invoice_total_cents") for s in got.get("shipments", [])]
        if gi != want["invoice_totals"]:
            probs.append(f"order {oid} invoice totals {gi} want {want['invoice_totals']}")
        gc = [c.get("amount_cents") for c in got.get("credit_notes", [])]
        if gc != want["credits"]:
            probs.append(f"order {oid} credit notes {gc} want {want['credits']}")
        return probs

    def wait_refunds(self, key: str, oid: int, timeout: float = 60.0) -> dict:
        n = len(self.world.orders[oid].refunds)
        return self.wait_order(key, oid, lambda j: len(j.get("refunds", [])) >= n and all(r.get("status") == "refunded" for r in j["refunds"]),
                               timeout, f"{n} refunded refunds")

    # -- warehouse ------------------------------------------------------------------------------------------------
    def create_wave(self, zone: str, ship: date, k: int, client: Client | None = None) -> tuple[Response, int | None]:
        r = submit_form(client or self.sup, "/waves", "create-wave", {"zone": zone, "ship_date": ship.isoformat(), "max_tasks": str(k)},
                        follow=False)
        m = re.search(r"/waves/(\d+)", r.location or "")
        return r, (int(m.group(1)) if r.status in REDIRECTS and m else None)

    def wave_page(self, wid: int, client: Client | None = None) -> Response:
        return (client or self.sup).get(f"/waves/{wid}", follow=True)

    @staticmethod
    def wave_view(page: Response) -> dict:
        root = page.dom.attr_one("data-wave-id")
        stops = []
        for s in page.dom.attr_all("data-stop-seq"):
            tasks = [{"task_id": int(t.attrs.get("data-task-id", "0") or 0), "order_id": int(t.attrs.get("data-order-id", "0") or 0),
                      "line_no": int(t.attrs.get("data-line-no", "0") or 0), "lot_id": int(t.attrs.get("data-lot-id", "0") or 0),
                      "units": int(t.attrs.get("data-units", "0") or 0), "status": t.attrs.get("data-task-status")}
                     for t in s.attr_all("data-task-id")]
            stops.append({"seq": s.attrs.get("data-stop-seq"), "bin": s.attrs.get("data-bin"), "tasks": tasks})
        return {"distance": root.attrs.get("data-route-distance") if root else None, "zone": root.attrs.get("data-zone") if root else None,
                "stops": stops}

    def wave(self, zone: str, ship: date, k: int) -> tuple[int, dict, list]:
        """Create a wave, compare it with the world (tasks, route, distance), and bind task ids."""
        r, wid = self.create_wave(zone, ship, k)
        if wid is None:
            raise StepFailed(f"creating a wave for zone {zone} {ship} answered {r.status}")
        view = self.wave_view(self.wave_page(wid))
        chosen, dist, seq = self.world.wave(wid, zone, ship, k)
        got_tasks = sorted((t["order_id"], t["line_no"], t["lot_id"], t["units"]) for s in view["stops"] for t in s["tasks"])
        want_tasks = sorted((t.oid, t.line, t.lot, t.units) for t in chosen)
        if got_tasks != want_tasks:
            raise StepFailed(f"wave {wid} tasks differ: got {got_tasks[:12]} want {want_tasks[:12]}")
        got_seq = [s["bin"] for s in view["stops"]]
        if got_seq != seq or str(view["distance"]) != str(dist):
            raise StepFailed(f"wave {wid} route got {got_seq} ({view['distance']}) want {seq} ({dist})")
        for s in view["stops"]:
            for t in s["tasks"]:
                mt = next(x for x in chosen if (x.oid, x.line, x.lot) == (t["order_id"], t["line_no"], t["lot_id"]))
                mt.app_id = t["task_id"]
        return wid, view, chosen

    def wave_loose(self, zone: str, ship: date, k: int, client: Client | None = None) -> tuple[int, dict, list]:
        """Create a wave for judge-evidence fixtures: record it in the world and bind the task ids the page shows,
        without asserting its tasks or route (W09 checks waves strictly), so a route defect never hides evidence."""
        r, wid = self.create_wave(zone, ship, k, client)
        if wid is None:
            raise StepFailed(f"creating a wave for zone {zone} {ship} answered {r.status}")
        view = self.wave_view(self.wave_page(wid, client))
        chosen, _, _ = self.world.wave(wid, zone, ship, k)
        for s in view["stops"]:
            for t in s["tasks"]:
                mt = next((x for x in chosen if (x.oid, x.line, x.lot) == (t["order_id"], t["line_no"], t["lot_id"])), None)
                if mt is not None:
                    mt.app_id = t["task_id"]
        return wid, view, chosen

    def pick_view(self, view: dict) -> None:
        """Confirm every task a wave page shows, in full, with its lot's bin and code (fixtures only)."""
        for s in view["stops"]:
            for t in s["tasks"]:
                lot = self.world.lots.get(t["lot_id"])
                if lot is None or t["status"] != "open":
                    continue
                r = self.confirm(t["task_id"], lot.bin, lot.code, t["units"])
                if r.status >= 400:
                    raise StepFailed(f"confirming task {t['task_id']} answered {r.status}: {visible_text(r.text)[:200]}")
                mt = next((x for x in self.world.tasks if x.app_id == t["task_id"]), None)
                if mt is not None and mt.status == "open":
                    self.world.confirm(mt, mt.units)

    def confirm(self, task_id: int, bin_code: str, lot_code: str, units: int, client: Client | None = None,
                drop_csrf: bool = False) -> Response:
        c = client or self.sup
        wid = next((t.wave for t in self.world.tasks if t.app_id == task_id), None)
        page = c.get(f"/waves/{wid}", follow=True) if wid else None
        form = None
        if page is not None and page.status == 200:
            for n in page.dom.attr_all("data-task-id", str(task_id)):
                for f in n.find_all(lambda x: x.tag == "form"):
                    form = f
        if form is None:
            tok = [(i.attrs.get("name"), i.attrs.get("value", "")) for i in c.get("/profile", follow=True).dom.find_all(
                lambda n: n.tag == "input" and "data-csrf" in n.attrs)]
            fields = tok
        else:
            fields = form_fields(form)
        if drop_csrf:
            fields = [(k, v) for k, v in fields if k not in {i.attrs.get("name") for i in (form or page.dom).find_all(
                lambda n: n.tag == "input" and "data-csrf" in n.attrs)}]
        fields = [(k, v) for k, v in fields if k not in ("bin_code", "lot_code", "units")]
        fields += [("bin_code", bin_code), ("lot_code", lot_code), ("units", str(units))]
        return c.post_form(f"/pick-tasks/{task_id}/confirm", fields, follow=False)

    def pick_all(self, wid: int, chosen: list) -> None:
        for t in chosen:
            lot = self.world.lots[t.lot]
            r = self.confirm(t.app_id, lot.bin, lot.code, t.units)
            if r.status >= 400:
                raise StepFailed(f"confirming task {t.app_id} answered {r.status}: {visible_text(r.text)[:200]}")
            self.world.confirm(t, t.units)

    def lot_view(self, lot_id: int, client: Client | None = None) -> dict:
        r = (client or self.sup).get(f"/lots/{lot_id}", follow=True)
        n = r.dom.attr_one("data-lot-id", str(lot_id))
        if r.status != 200 or n is None:
            raise StepFailed(f"/lots/{lot_id} answered {r.status} without its markers")
        return {k: n.attrs.get(f"data-{k}") for k in ("on-hand", "reserved", "lot-version", "lot-status")}

    def count(self, lot_id: int, counted: int, version: str | int | None = None, client: Client | None = None) -> Response:
        """Submit the count form. Without an explicit version, the version the supervisor sees is the lot page's
        data-lot-version marker (R7.7: "the version they saw"), whether or not the form pre-fills it."""
        c = client or self.sup
        page = c.get(f"/lots/{lot_id}", follow=True)
        if version is None:
            n = page.dom.attr_one("data-lot-id", str(lot_id))
            version = n.attrs.get("data-lot-version") if n is not None else None
        fields = {"counted_units": str(counted)}
        if version is not None:
            fields["version"] = str(version)
        return submit_form(c, "", "count-lot", fields, page=page, follow=False)

    def ship(self, oid: int, client: Client | None = None) -> tuple[Response, int | None]:
        c = client or self.sup
        page = c.get(f"/orders/{oid}", follow=True)
        try:
            r = submit_form(c, "", "ship-order", {}, page=page, follow=False)
        except FormNotFound:
            raise StepFailed(f"order {oid} page has no ship-order form for the supervisor ({page.status})")
        m = re.search(r"/shipments/(\d+)", r.location or "")
        return r, (int(m.group(1)) if r.status in REDIRECTS and m else None)

    def shipment_view(self, sid: int, c: Client | None = None) -> dict:
        r = (c or self.sup).get(f"/shipments/{sid}", follow=True)
        n = r.dom.attr_one("data-shipment-id", str(sid))
        if r.status != 200 or n is None:
            raise StepFailed(f"/shipments/{sid} answered {r.status} without its markers")
        lines = [{"line_no": int(x.attrs.get("data-invoice-line-no")), "units": int(x.attrs.get("data-units", "0")),
                  "amount_cents": int(x.attrs.get("data-amount-cents", "0")), "tax_cents": int(x.attrs.get("data-tax-cents", "0"))}
                 for x in r.dom.attr_all("data-invoice-line-no")]
        a = lambda k, d=None: page_attr(r.dom, n, k, d)  # noqa: E731 - the page "carries" these markers
        return {"tracking": a("data-tracking-number"), "invoice": a("data-invoice-number"),
                "total": int(a("data-invoice-total-cents", "-1")), "freight": int(a("data-freight-cents", "-1")),
                "freight_tax": int(a("data-freight-tax-cents", "-1")), "lines": sorted(lines, key=lambda l: l["line_no"])}

    def request_rma(self, c: Client, oid: int, line_no: int, units: int, reason: str = "Damaged in transit") -> tuple[Response, int | None]:
        page = c.get(f"/orders/{oid}", follow=True)
        tok = [(i.attrs.get("name"), i.attrs.get("value", "")) for i in page.dom.find_all(lambda n: n.tag == "input" and "data-csrf" in n.attrs)][:1]
        r = c.post_form(f"/orders/{oid}/rmas", tok + [("line_no", str(line_no)), ("units", str(units)), ("reason", reason)])
        m = re.search(r"/rmas/(\d+)", r.location or "")
        return r, (int(m.group(1)) if r.status in REDIRECTS and m else None)

    def rma_view(self, rid: int, c: Client | None = None) -> dict:
        r = (c or self.sup).get(f"/rmas/{rid}", follow=True)
        n = r.dom.attr_one("data-rma-id", str(rid))
        if r.status != 200 or n is None:
            raise StepFailed(f"/rmas/{rid} answered {r.status} without its markers")
        return {k: page_attr(r.dom, n, f"data-{k}") for k in ("rma-status", "disposition", "credit-cents")}

    def rma_action(self, rid: int, action: str, fields: dict | None = None, c: Client | None = None) -> Response:
        c = c or self.sup
        page = c.get(f"/rmas/{rid}", follow=True)
        return submit_form(c, "", action, fields or {}, page=page, follow=False)

    # -- reports -------------------------------------------------------------------------------------------------
    def csv(self, c: Client, path: str) -> tuple[Response, list[list[str]]]:
        r = c.get(path, follow=False)
        return r, (parse_csv(r.body) if r.status == 200 else [])

    def token_of(self, c: Client, path: str = "/profile") -> list[tuple[str, str]]:
        return [(i.attrs.get("name"), i.attrs.get("value", "")) for i in c.get(path, follow=True).dom.find_all(
            lambda n: n.tag == "input" and "data-csrf" in n.attrs)][:1]

    def restart_app(self) -> bool:
        if not self.restart_hook:
            return False
        # The hook is the verifier's own root-owned script (tests/test.sh writes it); no user input reaches it.
        return subprocess.run([self.restart_hook], check=False).returncode == 0
