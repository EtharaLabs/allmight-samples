#!/usr/bin/env python3
"""Pickpath workflows. Each workflow is strict: it passes only when every substep passes.

Run: python3 workflows.py --out /logs/verifier/results.json

Isolation: every rule workflow creates its own customers, SKUs and lots, and uses its own ship dates (waves are
taken per zone and ship date), so one defect fails the workflows that exercise it and not the others. Only W16 and
W26 allocate from the old system's lots. Ship-date offsets from today of the workflows that make waves: W08 210,
W09 100-139, W10 170, W11 180-181, W12 190, W14 0-1, W21 230, W22 240, W23 250-251 (W06's clock-change orders
use zone F, where no workflow makes waves).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import secrets
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (CARD_DECLINED, CARD_OK, FIN, FIN2, ONHAND_HEADER, ORDERS_HEADER, PASSWORD, REDIRECTS, SUP, SUP2,  # noqa: E402
                     Harness, key_needles, parse_csv)
from kitlib import browser as B  # noqa: E402
from kitlib.conc import simultaneously  # noqa: E402
from kitlib.web import Client, FormNotFound, find_form, form_fields, submit_form, visible_text  # noqa: E402
from kitlib.wf import InfraError, StepFailed, main_guard, run_all, workflow  # noqa: E402
from paylib import event_body  # noqa: E402
from reference import rules as R  # noqa: E402

H: Harness = None  # type: ignore[assignment]


def D(k: int) -> date:
    return H.today() + timedelta(days=k)


def norm_text(t: str, *remove: str) -> str:
    for r in remove:
        if r:
            t = t.replace(r, "").replace(r.lower(), "").replace(r.strip(), "")
    return re.sub(r"\s+", " ", t).strip().lower()


def legacy_users(pred, n: int = 1) -> list[dict]:
    out = [u for u in H.baseline["users"] if pred(u)]
    if len(out) < n:
        raise StepFailed("verifier fixture: not enough users in the old system's records for this check")
    return out[:n]


def tok(c: Client, path: str = "/profile") -> list[tuple[str, str]]:
    return H.token_of(c, path)


def status_of(key: str, oid: int) -> str | None:
    return H.order(key, oid).get("status")


def emails_to(addr: str, needle: str, since: set | None = None) -> list[dict]:
    return [m for m in H.mail.to(addr) if needle.lower() in m["subject"].lower() and (since is None or m["id"] not in since)]


def dollars(c: int) -> str:
    return ("-" if c < 0 else "") + f"${abs(c) // 100:,}.{abs(c) % 100:02d}"


def body_states(text: str, *needles: str) -> bool:
    """An email body states each needle. An HTML-only email is read through its visible text, where markup between
    a currency sign and its digits (`<b>$</b>12.50`) leaves a space, so the whitespace-free text counts too."""
    flat = re.sub(r"\s+", "", text or "")
    return all(n and (n in (text or "") or re.sub(r"\s+", "", n) in flat) for n in needles)


def mismatch(ctx, probs: list[str], what: str) -> None:
    ctx.check(not probs, f"{len(probs)} {what} differ: " + " | ".join(probs[:6]))


ORDER_ACTIONS = ("approve-order", "pay-order", "cancel-order", "ship-order")


def order_forms(c: Client, oid: int) -> tuple[set, dict]:
    """Which action forms an order page shows (top-level, and per line), as the interface contract states them."""
    page = c.get(f"/orders/{oid}", follow=True)
    top = {a for a in ORDER_ACTIONS if page.dom.attr_one("data-action", a) is not None}
    lines = {}
    for n in page.dom.attr_all("data-line-no"):
        lines[int(n.attrs.get("data-line-no") or 0)] = {a for a in ("cancel-backorder", "request-rma") if n.attr_one("data-action", a) is not None}
    return top, lines


def expect_forms(ctx, c: Client, oid: int, top, lines: dict | None = None, who: str = "") -> None:
    got_top, got_lines = order_forms(c, oid)
    ctx.eq(got_top, set(top), f"order {oid} action forms shown to {who}")
    for ln, acts in (lines or {}).items():
        ctx.eq(got_lines.get(ln, set()), set(acts), f"order {oid} line {ln} forms shown to {who}")


def customer_fields(cust, **over) -> dict:
    """Every field of the customer form (R3.5), so a refused submission is refused for the stated reason only."""
    f = {"name": cust.name, "tier": cust.tier, "ship_to_state": cust.state,
         "approval_limit_cents": "" if cust.limit is None else str(cust.limit), "min_shelf_life_days": str(cust.min_days),
         "allow_split_cases": "true" if cust.split else "false", "backorders_allowed": "true" if cust.backorders else "false"}
    f.update(over)
    return f


def has_action(c: Client, path: str, action: str) -> bool:
    return c.get(path, follow=True).dom.attr_one("data-action", action) is not None


def tie_qtys(price: int, rate: Decimal, pack: int = 1, limit: int = 2000) -> list[int]:
    """Quantities whose amount times the rate is exactly half a cent (R5.2 half-up; floats and banker's rounding differ)."""
    return [q for q in range(pack, limit, pack) if (Decimal(q * price) * rate) % 1 == Decimal("0.5")]


def clock_changes(lo: int, hi: int) -> list[date]:
    """Dates in [today+lo, today+hi) on which the warehouse's UTC offset changes."""
    out = []
    for k in range(lo, hi):
        d = D(k)
        if datetime(d.year, d.month, d.day, 0, 0, tzinfo=H.tz).utcoffset() != datetime(d.year, d.month, d.day, 12, 0, tzinfo=H.tz).utcoffset():
            out.append(d)
    return out


# ======================================================================================== W01
@workflow("W01", "Password links, sign-in, sessions and return paths (anti-enumeration, single use, expiry, fixation, open redirect)", 3)
def w01(ctx):
    quirky = legacy_users(lambda u: u["active"] and u["role"] == "buyer" and u["email"] != u["email"].strip().lower(), 2)
    e1, e2 = quirky[0]["email"].strip().lower(), quirky[1]["email"].strip().lower()
    with ctx.step("finance and a supervisor set passwords from emailed links and sign in"):
        H.account(FIN)
        H.account(SUP)
        ctx.surface("signin")
    with ctx.step("a known and an unknown address get the same status and message; the unknown one gets no email"):
        before = H.mail_ids(e1)
        rk = H.request_reset(e1.upper())
        ghost = f"nobody.{H.nonce}@verify.example"
        ru = H.request_reset(ghost)
        ctx.eq(ru.status, rk.status, "status for an unknown address")
        tk, tu = norm_text(visible_text(rk.text), e1.upper(), e1), norm_text(visible_text(ru.text), ghost)
        ctx.check(tk == tu, f"messages differ: known={tk[:160]!r} unknown={tu[:160]!r}")
        link = H.reset_link(e1, before)
        ctx.check(link is not None, "no link reached the address on file")
        time.sleep(2)
        ctx.eq(len(H.mail.to(ghost)), 0, "emails to an unknown address")
        ctx.shared["w01_link1"] = link
    with ctx.step("an inactive user's address receives no email"):
        inactive = legacy_users(lambda u: not u["active"])[0]["email"].strip().lower()
        before = H.mail_ids(inactive)
        H.request_reset(inactive)
        time.sleep(4)
        ctx.eq(len(H.mail_ids(inactive) - before), 0, "emails to an inactive user")
    with ctx.step("a newer link replaces the older one; the older one sets nothing; a used link changes nothing"):
        before = H.mail_ids(e1)
        H.request_reset(e1)
        link2 = H.reset_link(e1, before)
        ctx.check(link2 and link2 != ctx.shared["w01_link1"], "no second, different link")
        for lk, pw in ((ctx.shared["w01_link1"], "Old-Link-Pass-1"),):
            c = H.client()
            page = c.get(lk, follow=True)
            if page.status == 200:
                try:
                    submit_form(c, "", "set-password", {"password": pw}, follow=True, page=page)
                except FormNotFound:
                    pass
            c2, _ = H.signin(e1, pw)
            ctx.check(not H.signed_in(c2), "the replaced link set a working password")
        c = H.client()
        page = c.get(link2, follow=True)
        ctx.eq(page.status, 200, "newest link page status")
        submit_form(c, "", "set-password", {"password": PASSWORD}, follow=True, page=page)
        c3, _ = H.signin(e1)
        ctx.check(H.signed_in(c3), "the newest link did not set a working password")
        page = H.client().get(link2, follow=True)
        if page.status == 200:
            try:
                submit_form(H.client(), "", "set-password", {"password": "Second-Use-Pass-1"}, follow=True, page=page)
            except FormNotFound:
                pass
        c4, _ = H.signin(e1, "Second-Use-Pass-1")
        ctx.check(not H.signed_in(c4), "a used link set a password a second time")
    with ctx.step("an expired link changes nothing; an unknown link answers 404"):
        before = H.mail_ids(e2)
        H.request_reset(e2)
        link3 = H.reset_link(e2, before)
        ctx.check(link3, "no link for the second user")
        time.sleep(H.reset_ttl + 3)
        c = H.client()
        page = c.get(link3, follow=True)
        if page.status == 200:
            try:
                submit_form(c, "", "set-password", {"password": "Expired-Link-1"}, follow=True, page=page)
            except FormNotFound:
                pass
        c6, _ = H.signin(e2, "Expired-Link-1")
        ctx.check(not H.signed_in(c6), "an expired link set a password")
        ctx.eq(H.client().get("/password-reset/" + secrets.token_urlsafe(24), follow=False).status, 404, "unknown link status")
    with ctx.step("sign-in matches the address case-insensitively and padded; wrong password and unknown address are refused alike"):
        c7, _ = H.signin("  " + e1.upper() + "  ")
        ctx.check(H.signed_in(c7), "sign-in with a differently cased, padded address failed")
        c1, r1 = H.signin(e1, "Wrong-Password-1")
        ghost = f"ghost.{H.nonce}@verify.example"
        c2, r2 = H.signin(ghost, "Wrong-Password-1")
        ctx.check(not H.signed_in(c1) and not H.signed_in(c2), "a refused sign-in produced a session")
        p1 = r1 if r1.status not in REDIRECTS else c1.get(r1.location, follow=True)
        p2 = r2 if r2.status not in REDIRECTS else c2.get(r2.location, follow=True)
        t1, t2 = norm_text(visible_text(p1.text), e1), norm_text(visible_text(p2.text), ghost)
        ctx.check(t1 == t2, f"refusal messages differ: {t1[:160]!r} vs {t2[:160]!r}")
    with ctx.step("signing in starts a new session identifier; an identifier held before never becomes signed in"):
        c = H.client()
        c.get("/signin", follow=True)
        c.get("/password-reset", follow=True)
        planted = dict(c.cookies)
        _, r = H.signin(e1, client=c)
        ctx.check(H.signed_in(c), "sign-in failed")
        if planted:
            pre = H.client()
            pre.cookies = dict(planted)
            if H.signed_in(pre):
                ctx.breach("X9", "a session identifier held before sign-in became signed in")
                raise StepFailed("a session identifier held before sign-in became signed in")
        set_cookies = {h.split(";", 1)[0].split("=", 1)[0].strip(): h for h in r.header_all("Set-Cookie")
                       if "=" in h.split(";", 1)[0] and h.split(";", 1)[0].split("=", 1)[1].strip()}
        ctx.check(set_cookies, "sign-in set no cookie")
        # R2.5 constrains the session cookie(s): the ones whose removal signs the browser out. Other cookies (a
        # double-submit CSRF cookie, say) are not session cookies.
        session = H.session_cookie_names(c, list(set_cookies))
        ctx.observe("session_cookies", sorted(session))
        ctx.check(all("httponly" in set_cookies[n].lower() for n in session), f"a session cookie ({sorted(session)}) is not HttpOnly")
        ctx.check(all(re.search(r"samesite=(lax|strict)", set_cookies[n].lower()) for n in session),
                  f"a session cookie ({sorted(session)}) is not SameSite=Lax/Strict")
        ctx.shared["w01_client"] = c
    with ctx.step("signing out ends the session on the server"):
        c = ctx.shared["w01_client"]
        copy = c.copy()
        submit_form(c, "/", "signout", {}, follow=True)
        ctx.check(not H.signed_in(c), "still signed in after sign-out")
        if H.signed_in(copy):
            ctx.breach("X9", "a session cookie copied before sign-out still signs in")
            raise StepFailed("a cookie copied before sign-out still works")
    with ctx.step("a visitor sent to sign in returns to the page afterwards; sign-in never redirects to another site"):
        target = "/orders?from=wf"
        c = H.client()
        r = c.get(target, follow=False)
        ctx.check(r.status in REDIRECTS and "/signin" in (r.location or ""), f"visitor answered {r.status}")
        page = c.get(r.location, follow=True)
        r2 = submit_form(c, "", "signin", {"email": e1, "password": PASSWORD}, page=page)
        loc = urllib.parse.urlsplit(urllib.parse.urljoin(H.base + "/", r2.location or ""))
        ctx.eq(loc.path + ("?" + loc.query if loc.query else ""), target, "where sign-in returned the visitor")
        base_host = urllib.parse.urlsplit(H.base).netloc
        for evil in ("//evil.example/x", "/\\evil.example/x", "https://evil.example/x", "http:evil.example", "\\\\evil.example",
                     "///evil.example", "https:/evil.example", "/%2F%2Fevil.example", " //evil.example", "/\t/evil.example"):
            c = H.client()
            page = c.get("/signin?next=" + urllib.parse.quote(evil, safe=""), follow=True)
            r = submit_form(c, "", "signin", {"email": e1, "password": PASSWORD}, page=page)
            loc = (r.location or "").strip()
            norm = loc.replace("\\", "/").replace("\t", "")
            dest = urllib.parse.urlsplit(urllib.parse.urljoin(H.base + "/", norm))
            offsite = bool(loc) and (norm.startswith("//") or (re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", norm) and not norm.startswith(H.base + "/"))
                                     or (dest.netloc and dest.netloc != base_host))
            if offsite:
                ctx.breach("X11", f"sign-in redirected to {loc!r} for next={evil!r}")
                raise StepFailed(f"open redirect to {loc!r} for next={evil!r}")
            ctx.check(r.status in REDIRECTS, f"sign-in answered {r.status} for next={evil!r}")


# ======================================================================================== W02
@workflow("W02", "Setting up: customers, users, SKUs, receipts and prices with validation, lot identity and role checks", 3)
def w02(ctx):
    with ctx.step("finance adds a customer and a buyer; an address on file (any case and spacing) and a control-character name are refused"):
        cust = H.create_customer(tier="gold", state="NJ", limit=120000, min_days=9, split=False, backorders=False)
        row = H.q("SELECT name, tier, ship_to_state, approval_limit_cents, min_shelf_life_days, allow_split_cases, backorders_allowed"
                  " FROM customers WHERE id = %s", (cust.cid,))
        ctx.check(row and row[0]["tier"] == "gold" and row[0]["ship_to_state"] == "NJ" and row[0]["approval_limit_cents"] == 120000
                  and row[0]["min_shelf_life_days"] == 9 and row[0]["allow_split_cases"] is False and row[0]["backorders_allowed"] is False,
                  f"customers row differs: {row}")
        email = H.create_user("buyer", cust.cid)
        u = H.q("SELECT role, customer_id, zone, active FROM users WHERE lower(btrim(email)) = %s", (email,))
        ctx.check(u and u[0]["role"] == "buyer" and u[0]["customer_id"] == cust.cid and u[0]["active"], f"users row differs: {u}")
        n0 = H.q("SELECT count(*) AS n FROM users")[0]["n"]
        legacy = legacy_users(lambda x: x["role"] == "buyer" and x["email"] != x["email"].strip().lower())[0]["email"]
        for em, nm in (("  " + email.upper() + " ", "Dup Person"), (legacy, "Dup Legacy"), (f"ctl.{H.nonce}@verify.example", "Bell\x07Name"),
                       (f"ctl2.{H.nonce}@verify.example", "Two\nLines")):
            submit_form(H.fin, "/users/new", "create-user", {"email": em, "full_name": nm, "role": "buyer", "customer_id": str(cust.cid),
                                                             "zone": ""}, follow=True)
        ctx.eq(H.q("SELECT count(*) AS n FROM users")[0]["n"], n0, "user rows after refused additions")
        bad = submit_form(H.fin, "/customers/new", "create-customer", {"name": "Nowhere Deli", "tier": "gold", "ship_to_state": "ZZ",
                          "approval_limit_cents": "", "min_shelf_life_days": "0", "allow_split_cases": "true", "backorders_allowed": "true"}, follow=False)
        ctx.check(bad.status >= 400, f"a customer in a state with no tax rate answered {bad.status}")
        ctx.shared["w02"] = (cust, email)
    with ctx.step("a supervisor adds a SKU (code stored upper-cased); a duplicate code in other case and an invalid case pack are refused"):
        code = "w02-" + H.nonce
        sku = H.create_sku(pack=12, weight=250, dims=(30, 20, 15), storage="chilled", taxable=True, reorder=0, code=code)
        row = H.q("SELECT code, case_pack, storage, taxable, active FROM skus WHERE code = %s", (code.upper(),))
        ctx.check(row and row[0]["case_pack"] == 12 and row[0]["storage"] == "chilled" and row[0]["taxable"] and row[0]["active"],
                  f"skus row differs: {row}")
        n0 = H.q("SELECT count(*) AS n FROM skus")[0]["n"]
        for f in ({"code": code.lower(), "case_pack": "6"}, {"code": "W02B" + H.nonce, "case_pack": "0"},
                  {"code": "bad code!", "case_pack": "6"}, {"code": "W02C" + H.nonce, "case_pack": "6", "name": "x\r\nBcc: a@b.c"}):
            submit_form(H.sup, "/skus/new", "create-sku", {"name": f.get("name", "Thing"), "unit_weight_g": "10", "length_cm": "10",
                        "width_cm": "10", "height_cm": "10", "storage": "ambient", "taxable": "false", "reorder_point": "0", **f}, follow=True)
        ctx.eq(H.q("SELECT count(*) AS n FROM skus")[0]["n"], n0, "SKU rows after refused additions")
        ctx.shared["w02_sku"] = sku
    with ctx.step("receipts: a new lot, the same lot again (code in other case and spacing), conflicting lot data and wrong-zone bins are refused"):
        sku = ctx.shared["w02_sku"]
        lot = H.receive(sku, D(60), "C12-04-2", 40, "3.1250", lot_code="W2LOT-" + H.nonce)
        ctx.eq(H.lot_view(lot), {"on-hand": "40", "reserved": "0", "lot-version": "1", "lot-status": "available"}, "new lot page")
        r = submit_form(H.sup, "/receipts/new", "receive", {"sku": sku.lower(), "lot_code": "  w2lot-" + H.nonce + " ", "expiry_date": D(60).isoformat(),
                        "bin": "C12-04-2", "units": "12", "unit_cost": "3.1250"}, follow=False)
        ctx.check(r.status in REDIRECTS and f"/lots/{lot}" in (r.location or ""), f"a receipt for the same lot answered {r.status} {r.location}")
        H.world.receipt(lot, sku, "W2LOT-" + H.nonce, D(60), "C12-04-2", Decimal("3.1250"), 12)
        n_lots = H.q("SELECT count(*) AS n FROM lots")[0]["n"]
        for f, want in (({"expiry_date": D(61).isoformat()}, 409), ({"bin": "C12-05-2"}, 409), ({"unit_cost": "3.1300"}, 409),
                        ({"lot_code": "W2NEW-" + H.nonce, "bin": "A01-01-1"}, None), ({"lot_code": "W2NEW2-" + H.nonce, "bin": "QA-01"}, None),
                        ({"lot_code": "W2NEW3-" + H.nonce, "unit_cost": "1.23456"}, None), ({"lot_code": "W2NEW4-" + H.nonce, "units": "0"}, None)):
            fields = {"sku": sku, "lot_code": "W2LOT-" + H.nonce, "expiry_date": D(60).isoformat(), "bin": "C12-04-2", "units": "5",
                      "unit_cost": "3.1250", **f}
            r = submit_form(H.sup, "/receipts/new", "receive", fields, follow=False)
            ctx.check(r.status >= 400 and (want is None or r.status == want), f"refused receipt {f} answered {r.status}")
        ctx.eq(H.q("SELECT count(*) AS n FROM lots")[0]["n"], n_lots, "lot rows after refused receipts")
        ctx.eq(H.lot_view(lot), {"on-hand": "52", "reserved": "0", "lot-version": "2", "lot-status": "available"}, "lot page after two receipts")
    with ctx.step("finance adds prices; the same tier, SKU and date again is refused"):
        sku = ctx.shared["w02_sku"]
        H.add_price("gold", sku, 1234, D(-5))
        r = submit_form(H.fin, "/prices", "create-price", {"tier": "gold", "sku": sku, "unit_price_cents": "999",
                        "effective_from": D(-5).isoformat()}, follow=True)
        rows = H.q("SELECT p.unit_price_cents FROM price_list p JOIN skus s ON s.id = p.sku_id WHERE s.code = %s AND tier = 'gold'", (sku,))
        ctx.eq([x["unit_price_cents"] for x in rows], [1234], "gold prices of the SKU")
    with ctx.step("each role is refused other roles' pages and actions with 403, and nothing changes"):
        cust, email = ctx.shared["w02"]
        buyer = H.account(email)
        sku = ctx.shared["w02_sku"]
        for path in ("/skus/new", "/receipts/new", "/prices", "/customers/new", "/users/new", "/waves", "/reports/on-hand.csv",
                     "/reports/valuation.csv?as_of=2026-11-01T00:00:00Z"):
            ctx.eq(buyer.get(path).status, 403, f"buyer GET {path}")
        ctx.eq(H.sup.get("/prices").status, 403, "supervisor GET /prices")
        ctx.eq(H.sup.get("/customers/new").status, 403, "supervisor GET /customers/new")
        ctx.eq(H.fin.get("/receipts/new").status, 403, "finance GET /receipts/new")
        ctx.eq(H.fin.get("/waves").status, 403, "finance GET /waves")
        n0 = H.q("SELECT count(*) AS n FROM price_list")[0]["n"]
        r = buyer.post_form("/prices", tok(buyer) + [("tier", "gold"), ("sku", sku), ("unit_price_cents", "1"), ("effective_from", D(-1).isoformat())])
        r2 = H.sup.post_form("/prices", tok(H.sup) + [("tier", "gold"), ("sku", sku), ("unit_price_cents", "2"), ("effective_from", D(-2).isoformat())])
        n1 = H.q("SELECT count(*) AS n FROM price_list")[0]["n"]
        if n1 != n0:
            ctx.breach("X1", "a buyer or supervisor added a price")
        ctx.check(r.status == 403 and r2.status == 403 and n1 == n0, f"price by buyer {r.status}, by supervisor {r2.status}")
        nl = H.q("SELECT count(*) AS n FROM lots")[0]["n"]
        r = H.fin.post_form("/receipts/new", tok(H.fin) + [("sku", sku), ("lot_code", "FINLOT"), ("expiry_date", D(50).isoformat()),
                            ("bin", "C12-01-1"), ("units", "5"), ("unit_cost", "1.0000")])
        if H.q("SELECT count(*) AS n FROM lots")[0]["n"] != nl:
            ctx.breach("X1", "finance received stock")
        ctx.eq(r.status, 403, "finance POST /receipts/new")
        anon = H.client().get("/skus/new", follow=False)
        ctx.check(anon.status in REDIRECTS and "/signin" in (anon.location or ""), f"visitor answered {anon.status}")


# ======================================================================================== W03
@workflow("W03", "Order pricing rules: tier prices on the ship date, per-line tax, dimensional-weight freight, cold chain, free freight", 5, critical=True)
def w03(ctx):
    rnd = random.Random(303)
    specs = [(6, 1050, (30, 20, 32), "ambient", False), (24, 130, (30, 20, 12), "ambient", True), (12, 60, (25, 19, 9), "chilled", False),
             (1, 4000, (40, 30, 30), "ambient", True), (10, 90, (50, 40, 30), "frozen", False), (4, 700, (22, 22, 22), "ambient", False),
             (8, 300, (60, 40, 10), "ambient", True), (1, 120, (10, 10, 10), "chilled", True), (36, 25, (35, 25, 20), "ambient", False),
             (2, 2500, (30, 30, 30), "frozen", True), (12, 450, (45, 30, 25), "ambient", False), (6, 820, (30, 25, 30), "chilled", False)]
    with ctx.step("SKUs with tier prices that change on a ship date, plus a SKU priced only later"):
        skus = []
        for pack, w, dims, st, tx in specs:
            code = H.create_sku(pack, w, dims, st, tx)
            base = rnd.randint(150, 4000)
            for tier, f in (("standard", 1.0), ("gold", 0.93), ("platinum", 0.87)):
                H.add_price(tier, code, int(base * f), D(-30))
                H.add_price(tier, code, int(base * f) + rnd.randint(1, 90), D(40))
            skus.append(code)
        late = H.create_sku(1, 500, (20, 20, 20), "ambient", True)
        H.add_price("standard", late, 777, D(40))
        exact = H.create_sku(1, 100, (10, 10, 10), "ambient", False)
        H.price_all(exact, 1000, D(-30))
        ctx.shared["w03"] = (skus, late, exact)
    with ctx.step("customers in nine states and three tiers, with keys"):
        combos = [("standard", "NY", True), ("gold", "NJ", True), ("platinum", "PA", True), ("standard", "CT", False), ("gold", "MA", True),
                  ("platinum", "IL", True), ("standard", "CA", True), ("gold", "NH", False), ("platinum", "VT", True)]
        comps = [H.company(tier=t, state=s, split=sp) for t, s, sp in combos]
        ctx.shared["w03_comps"] = comps
    with ctx.step("130 orders through the API: every price, tax, freight and total matches the rules; refused ones answer 422"):
        skus, late, exact = ctx.shared["w03"]
        comps = ctx.shared["w03_comps"]
        legacy = [s["code"] for s in H.baseline["skus"] if s["active"]][7:400:9]
        probs, refused_ok, accepted = [], 0, 0
        for i in range(130):
            cust, c, key, _ = comps[i % len(comps)]
            ship = D(rnd.choice([39, 40, 41, 10, 100, 39, 40]))
            pool = skus + legacy[:30]
            lines = []
            for code in rnd.sample(pool, rnd.randint(1, 5)):
                pack = H.world.skus[code].info.case_pack
                q = pack * rnd.randint(1, 8) if not cust.split or rnd.random() < 0.5 else rnd.randint(1, 120)
                lines.append((code, q))
            if i % 13 == 0:
                lines = [(exact, rnd.choice([250, 249, 251]))]
            elif i % 17 == 0:
                lines.append((late, 3))
            elif i % 19 == 0:
                lines.append((next(s["code"] for s in H.baseline["skus"] if not s["active"]), 6))
            elif i % 23 == 0 and not cust.split:
                lines.append((skus[1], 25))
            if i % 11 == 0:
                lines = [(code.lower() + "  ", q) for code, q in lines]
            want = H.world.quote(cust, ship, lines)
            r = H.api_order(key, ship, lines, po=f"PO-{i}")
            if isinstance(want, str):
                if r.status != 422 or not isinstance(r.json().get("errors") if r.status == 422 else None, list):
                    probs.append(f"order {i} ({want}) answered {r.status}, want 422 with errors")
                else:
                    refused_ok += 1
                continue
            if r.status != 201:
                probs.append(f"order {i} answered {r.status}: {r.text[:150]}")
                continue
            j = r.json()
            accepted += 1
            for k in ("subtotal_cents", "tax_cents", "freight_cents", "freight_tax_cents", "total_cents"):
                if j.get(k) != want[k]:
                    probs.append(f"order {j.get('id')} ({cust.state}/{cust.tier}, ship {ship}) {k} {j.get(k)} want {want[k]}")
            for gl, wl in zip(j.get("lines", []), want["lines"]):
                for k in ("unit_price_cents", "amount_cents", "tax_cents", "qty"):
                    if gl.get(k) != wl[{"qty": "qty"}.get(k, k)]:
                        probs.append(f"order {j.get('id')} line {gl.get('line_no')} {k} {gl.get(k)} want {wl[k]}")
            if j.get("status") != "awaiting_payment" or j.get("number") != f"PP-{int(j.get('id', 0)):06d}":
                probs.append(f"order {j.get('id')} status {j.get('status')} number {j.get('number')}")
        ctx.observe("accepted", accepted)
        ctx.observe("refused", refused_ok)
        ctx.check(accepted > 80 and refused_ok >= 8, f"verifier fixture: {accepted} accepted, {refused_ok} refused")
        mismatch(ctx, probs, "orders")
    with ctx.step("hard cases: half-cent line and freight taxes, exact half-kilogram boundaries, per-line billable weight, split cases"):
        comps = ctx.shared["w03_comps"]
        heavy = H.create_sku(1, 500, (10, 10, 10), "ambient", False)      # 500 g actual beats 200 g dimensional
        bulky = H.create_sku(1, 10, (20, 20, 26), "ambient", False)       # 2080 g dimensional per case beats 10 g
        odd = H.create_sku(3, 5, (7, 7, 7), "ambient", True)              # 343 / 5 = 68.6 -> 69 g per case
        cold = H.create_sku(1, 100, (10, 10, 10), "chilled", True)
        unit = H.create_sku(1, 50, (10, 10, 10), "ambient", True)
        for code, p in ((heavy, 100), (bulky, 120), (odd, 333), (cold, 450), (unit, 1)):
            H.price_all(code, p, D(-30))
        ship = D(10)
        plans = []
        for cust, c, key, _ in comps:
            rate, ft = H.world.rates[cust.state]
            if rate:
                for q in tie_qtys(1, rate, 1, 4000)[:3]:
                    plans.append((cust, key, [(unit, q)], "line tax tie"))
                for code in (cold, odd):
                    p = H.world.price(cust.tier, code, ship)
                    pack = 1 if cust.split else H.world.skus[code].info.case_pack
                    for q in tie_qtys(p, rate, pack, 3000)[:2]:
                        plans.append((cust, key, [(code, q), (heavy, 3)], "line tax tie"))
            if rate and ft:
                for extra in ([], [(cold, 1)]):
                    found = 0
                    for k in range(1, 450):
                        q = H.world.quote(cust, ship, [(heavy, k)] + extra)
                        if isinstance(q, dict) and (Decimal(q["freight_cents"]) * rate) % 1 == Decimal("0.5"):
                            plans.append((cust, key, [(heavy, k)] + extra, "freight tax tie"))
                            found += 1
                            if found == 2:
                                break
            for lines in ([(bulky, 24)], [(bulky, 25)], [(bulky, 26)], [(heavy, 7), (bulky, 3)], [(heavy, 1), (bulky, 1), (odd, 3)],
                          [(heavy, 999)], [(heavy, 1000)], [(heavy, 1001)]):
                plans.append((cust, key, lines, "weight boundary"))
            if cust.split:
                plans.append((cust, key, [(odd, 4)], "split case"))
                plans.append((cust, key, [(odd, 1), (bulky, 2)], "split case"))
        probs, kinds = [], {}
        for cust, key, lines, kind in plans:
            want = H.world.quote(cust, ship, lines)
            r = H.api_order(key, ship, lines)
            if isinstance(want, str):
                if r.status != 422:
                    probs.append(f"{kind} {lines} answered {r.status}, want 422 ({want})")
                continue
            if r.status != 201:
                probs.append(f"{kind} {lines} answered {r.status}: {r.text[:120]}")
                continue
            kinds[kind] = kinds.get(kind, 0) + 1
            j = r.json()
            for k in ("subtotal_cents", "tax_cents", "freight_cents", "freight_tax_cents", "total_cents"):
                if j.get(k) != want[k]:
                    probs.append(f"{kind} ({cust.state}) {lines}: {k} {j.get(k)} want {want[k]}")
            for gl, wl in zip(j.get("lines", []), want["lines"]):
                if gl.get("tax_cents") != wl["tax_cents"] or gl.get("amount_cents") != wl["amount_cents"]:
                    probs.append(f"{kind} ({cust.state}) line {gl.get('line_no')}: tax {gl.get('tax_cents')} want {wl['tax_cents']}")
        ctx.observe("hard_cases", kinds)
        ctx.check(kinds.get("line tax tie", 0) >= 20 and kinds.get("freight tax tie", 0) >= 4, f"verifier fixture: {kinds}")
        mismatch(ctx, probs, "hard-case orders")


# ======================================================================================== W04
@workflow("W04", "Approval limits: the boundary, approvers only (never their own), other companies refused, emails to approvers", 3, critical=True)
def w04(ctx):
    with ctx.step("a company with an approval limit, a buyer, two approvers, and another company's approver"):
        cust, buyer, key, bmail = H.company(tier="standard", state="NH", limit=300000, split=True)
        a1, a2 = H.create_user("approver", cust.cid, name="Avery Approver"), H.create_user("approver", cust.cid, name="Blake Approver")
        other, _, _, _ = H.company(tier="standard", state="NH")
        x = H.create_user("approver", other.cid)
        sku = H.create_sku(1, 100, (10, 10, 10))
        H.price_all(sku, 1000, D(-10))
        ctx.shared["w04"] = (cust, buyer, key, bmail, a1, a2, x, sku)
    with ctx.step("a total equal to the limit needs no approval; one cent... one unit more does, and each other approver is emailed"):
        cust, buyer, key, bmail, a1, a2, x, sku = ctx.shared["w04"]
        before = {a: H.mail_ids(a) for a in (a1, a2, bmail)}
        eq = H.place(key, cust, D(25), [(sku, 300)])
        gt = H.place(key, cust, D(25), [(sku, 301)])
        ctx.eq(status_of(key, eq), "awaiting_payment", "order totalling exactly the limit")
        ctx.eq(status_of(key, gt), "awaiting_approval", "order above the limit")
        number = f"PP-{gt:06d}"
        for a in (a1, a2):
            got = H.mail.wait_for(a, lambda m, a=a: m["id"] not in before[a] and "approval needed" in m["subject"].lower() and number in m["subject"], 1, 35)
            ctx.eq(len(got), 1, f"approval emails to {a}")
        time.sleep(2)
        ctx.eq(len([m for m in H.mail.to(bmail) if m["id"] not in before[bmail] and "approval" in m["subject"].lower()]), 0,
               "approval emails to the buyer who created it")
        full = H.mail._get("/api/v1/message/" + str(H.mail.wait_for(a1, lambda m: number in m["subject"], 1, 5)[0]["id"]))
        ctx.check("Avery Approver" in [str(t.get("Name", "")) for t in (full.get("To") or [])], "the approval email's To has no display name")
        expect_forms(ctx, buyer, gt, {"cancel-order"}, who="the buyer of an order awaiting approval")
        expect_forms(ctx, H.account(a1), gt, {"approve-order", "cancel-order"}, who="an approver who did not create it")
        expect_forms(ctx, buyer, eq, {"pay-order", "cancel-order"}, who="the buyer of an order awaiting payment")
        expect_forms(ctx, H.fin, gt, set(), who="finance")
        ctx.shared["w04_orders"] = (eq, gt)
    with ctx.step("an approver's own order emails only the other approver"):
        cust, buyer, key, bmail, a1, a2, x, sku = ctx.shared["w04"]
        ac = H.account(a1)
        akey = H.create_key(ac)
        b2 = H.mail_ids(a2)
        b1 = H.mail_ids(a1)
        own = H.place(akey, cust, D(25), [(sku, 400)])
        number = f"PP-{own:06d}"
        ctx.eq(len(H.mail.wait_for(a2, lambda m: m["id"] not in b2 and number in m["subject"], 1, 35)), 1, "emails to the other approver")
        time.sleep(1)
        ctx.eq(len([m for m in H.mail.to(a1) if m["id"] not in b1 and number in m["subject"]]), 0, "emails to the approver who created it")
        expect_forms(ctx, ac, own, {"cancel-order"}, who="the approver who created it")
        expect_forms(ctx, H.account(a2), own, {"approve-order", "cancel-order"}, who="the other approver")
        ctx.shared["w04_own"] = own
    with ctx.step("the buyer cannot pay or approve; another company's approver and the creating approver cannot approve"):
        cust, buyer, key, bmail, a1, a2, x, sku = ctx.shared["w04"]
        eq, gt = ctx.shared["w04_orders"]
        own = ctx.shared["w04_own"]
        r = buyer.post_form(f"/orders/{gt}/pay", tok(buyer, f"/orders/{gt}"))
        ctx.check(r.status >= 400, f"paying an order awaiting approval answered {r.status}")
        ctx.eq(len(H.pay_api.sessions_for(gt)), 0, "checkout sessions for an order awaiting approval")
        attempts = [("buyer", buyer, gt), ("other company's approver", H.account(x), gt), ("creating approver", H.account(a1), own)]
        for label, c, oid in attempts:
            r = c.post_form(f"/orders/{oid}/approve", tok(c))
            st = status_of(key, oid)
            if st != "awaiting_approval":
                ctx.breach("X1", f"{label} approved order {oid}")
                raise StepFailed(f"{label} approved the order")
            # The buyer's role does not allow approving (R1.3: 403) and another company's order is 403/404; the
            # creator's own approval is a refused form submission, which the contract answers with any 4xx.
            ok = 400 <= r.status < 500 if label == "creating approver" else r.status in (403, 404, 409)
            ctx.check(ok, f"{label} approving answered {r.status}")
        ac2 = H.account(a2)
        r = submit_form(ac2, f"/orders/{gt}", "approve-order", {}, drop_csrf=True)
        if status_of(key, gt) != "awaiting_approval":
            ctx.breach("X2", "an approval without the session token took effect")
            raise StepFailed("token-less approval took effect")
        ctx.eq(r.status, 403, "token-less approval status")
    with ctx.step("the other approver approves; approving again or approving a cancelled order is refused with 409"):
        cust, buyer, key, bmail, a1, a2, x, sku = ctx.shared["w04"]
        eq, gt = ctx.shared["w04_orders"]
        ac2 = H.account(a2)
        r = submit_form(ac2, f"/orders/{gt}", "approve-order", {}, follow=False)
        ctx.check(r.status < 400, f"approving answered {r.status}")
        ctx.eq(status_of(key, gt), "awaiting_payment", "status after approval")
        expect_forms(ctx, ac2, gt, {"pay-order", "cancel-order"}, who="an approver once approved")
        r = ac2.post_form(f"/orders/{gt}/approve", tok(ac2))
        ctx.eq(r.status, 409, "approving an approved order")
        r = submit_form(buyer, f"/orders/{ctx.shared['w04_own']}", "cancel-order", {}, follow=False)
        ctx.check(r.status < 400, f"cancelling answered {r.status}")
        ctx.eq(status_of(key, ctx.shared["w04_own"]), "cancelled", "status after cancelling")
        expect_forms(ctx, ac2, ctx.shared["w04_own"], set(), who="an approver of a cancelled order")
        r = ac2.post_form(f"/orders/{ctx.shared['w04_own']}/approve", tok(ac2))
        ctx.eq(r.status, 409, "approving a cancelled order")


# ======================================================================================== W05
@workflow("W05", "Checkout and webhooks: session details, only signed events confirm, forged/stale refused, out of order, duplicates, late payment refunded", 5, critical=True)
def w05(ctx):
    with ctx.step("a company, a SKU with stock, and three orders"):
        cust, c, key, email = H.company(tier="gold", state="NY", split=True, backorders=True)
        sku = H.create_sku(1, 300, (20, 20, 20), "ambient", True)
        H.price_all(sku, 2350, D(-10))
        lot = H.receive(sku, D(200), "A04-06-1", 500)
        o1 = H.place(key, cust, D(20), [(sku, 7)])
        o2 = H.place(key, cust, D(20), [(sku, 3)])
        o3 = H.place(key, cust, D(20), [(sku, 5)])
        ctx.shared["w05"] = (cust, c, key, email, sku, lot, o1, o2, o3)
    with ctx.step("paying redirects to a Checkout Session with the stated amount, metadata, email and return URLs; paying again reuses it"):
        cust, c, key, email, sku, lot, o1, o2, o3 = ctx.shared["w05"]
        r, sid = H.start_checkout(c, o1)
        ctx.check(sid, f"paying answered {r.status} -> {r.location!r}")
        s = H.pay_api.session(sid)
        total = H.world.orders[o1].total
        ctx.eq(s.get("amount_total"), total, "session amount_total")
        ctx.eq(str((s.get("metadata") or {}).get("order_id")), str(o1), "session metadata[order_id]")
        ctx.eq(s.get("customer_email"), email, "session customer_email")
        ctx.eq(s.get("success_url"), f"{H.base}/orders/{o1}?checkout=success", "success_url")
        ctx.eq(s.get("cancel_url"), f"{H.base}/orders/{o1}?checkout=cancelled", "cancel_url")
        names = [((li.get("price_data") or {}).get("product_data") or {}).get("name", "") for li in s.get("line_items") or []]
        ctx.check(any(f"PP-{o1:06d}" in n for n in names), f"no line item names the order number: {names}")
        r2, sid2 = H.start_checkout(c, o1)
        ctx.eq(sid2, sid, "session after paying again")
        ctx.eq(len(H.pay_api.sessions_for(o1)), 1, "sessions created for the order")
        ctx.shared["w05_sid"] = sid
    with ctx.step("with deliveries held, the card is paid and the buyer returns to success_url: the order stays unpaid"):
        cust, c, key, email, sku, lot, o1, o2, o3 = ctx.shared["w05"]
        sid = ctx.shared["w05_sid"]
        H.pay_api.hold(["checkout.session.completed", "payment_intent.succeeded"])
        st, body = H.pay_api.pay(sid, CARD_OK)
        ctx.eq(st, 200, "provider payment")
        c.get(f"/orders/{o1}?checkout=success", follow=True)
        time.sleep(3)
        ctx.eq(status_of(key, o1), "awaiting_payment", "status before any webhook")
    with ctx.step("unsigned, mis-signed, stale and wrong-key deliveries are answered 400 and change nothing"):
        cust, c, key, email, sku, lot, o1, o2, o3 = ctx.shared["w05"]
        sid = ctx.shared["w05_sid"]
        evs = H.pay_api.events_of(sid, "checkout.session.completed")
        ctx.check(evs, "verifier fixture: no held checkout.session.completed")
        body = event_body(evs[0])
        cl = H.client()
        hook = lambda sig: cl.request("POST", "/webhooks/payments", body=body,  # noqa: E731
                                      headers={"Content-Type": "application/json", **({"Stripe-Signature": sig} if sig is not None else {})})
        t = int(time.time())
        cases = [("no header", None), ("garbage", "nonsense"), ("bad hmac", f"t={t},v1=" + "0" * 64),
                 ("stale", H.pay_api.sign(body, t=t - 400)), ("future", H.pay_api.sign(body, t=t + 400)),
                 ("wrong key", H.pay_api.sign(body, key="whsec_" + "1" * 48))]
        for label, sig in cases:
            r = hook(sig)
            if status_of(key, o1) != "awaiting_payment":
                ctx.breach("X7", f"a webhook with {label} signature marked the order paid")
                raise StepFailed(f"a {label} webhook marked the order paid")
            ctx.eq(r.status, 400, f"{label} webhook status")
        forged = json.loads(body)
        forged["id"] = "evt_forged_" + H.nonce
        r = cl.request("POST", "/webhooks/payments", body=event_body(forged), headers={"Content-Type": "application/json",
                       "Stripe-Signature": H.pay_api.sign(body)})
        if status_of(key, o1) != "awaiting_payment":
            ctx.breach("X7", "a body that does not match its signature marked the order paid")
            raise StepFailed("an altered body marked the order paid")
        ctx.eq(r.status, 400, "altered body status")
    with ctx.step("payment_intent.succeeded first changes nothing; the signed checkout.session.completed then confirms once"):
        cust, c, key, email, sku, lot, o1, o2, o3 = ctx.shared["w05"]
        sid = ctx.shared["w05_sid"]
        before = H.mail_ids(email)
        st = H.pay_api.state()
        pi_ev = [e for e in H.pay_api.events_of(sid, None, st) if e["type"] == "payment_intent.succeeded"]
        cs_ev = H.pay_api.events_of(sid, "checkout.session.completed", st)
        pi = cs_ev[0]["data"]["object"]["payment_intent"]
        pi_ev = [st["events"][e] for e in st["event_order"] if st["events"][e]["type"] == "payment_intent.succeeded"
                 and st["events"][e]["data"]["object"]["id"] == pi]
        held = set(st.get("held", []))
        ctx.check(pi_ev and pi_ev[0]["id"] in held and cs_ev[0]["id"] in held, "verifier fixture: events not held")
        H.pay_api.release([pi_ev[0]["id"]])
        time.sleep(3)
        ctx.eq(status_of(key, o1), "awaiting_payment", "status after payment_intent.succeeded alone")
        H.pay_api.release()
        H.wait_order(key, o1, lambda j: j["status"] == "in_fulfilment", 30, "in_fulfilment")
        H.world.pay(o1)
        H.pay_api.redeliver(cs_ev[0]["id"])
        cl = H.client()
        b = event_body(cs_ev[0])
        outs = simultaneously([lambda: cl.copy().request("POST", "/webhooks/payments", body=b, headers={
            "Content-Type": "application/json", "Stripe-Signature": H.pay_api.sign(b)}) for _ in range(4)])
        time.sleep(4)
        mismatch(ctx, H.compare(key, o1), "order fields")
        j = H.order(key, o1)
        if j["lines"][0]["reserved"] > 7:
            ctx.breach("X6", "a repeated payment event allocated the order twice")
        got = emails_to(email, "order confirmed", before)
        if len(got) > 1:
            ctx.breach("X6", "a repeated payment event sent the confirmation twice")
        ctx.eq(len([m for m in got if f"PP-{o1:06d}" in m["subject"]]), 1, "order confirmation emails")
        expect_forms(ctx, c, o1, set(), {1: set()}, "the buyer of a paid order")
        expect_forms(ctx, H.sup, o1, set(), who="the supervisor before any pick")
        ctx.check(body_states(got[0]["text"], dollars(H.world.orders[o1].total)), "the confirmation does not state the total")
    with ctx.step("a declined card leaves the order unpaid"):
        cust, c, key, email, sku, lot, o1, o2, o3 = ctx.shared["w05"]
        r, sid = H.start_checkout(c, o2)
        ctx.check(sid, "no checkout for the second order")
        st, _ = H.pay_api.pay(sid, CARD_DECLINED)
        ctx.check(st != 200, "the provider accepted a declined card")
        time.sleep(3)
        ctx.eq(status_of(key, o2), "awaiting_payment", "status after a declined card")
    with ctx.step("a payment that completes after the order was cancelled is refunded in full through the provider, once"):
        cust, c, key, email, sku, lot, o1, o2, o3 = ctx.shared["w05"]
        r, sid = H.start_checkout(c, o3)
        ctx.check(sid, "no checkout for the third order")
        r = submit_form(c, f"/orders/{o3}", "cancel-order", {}, follow=False)
        ctx.check(r.status < 400, f"cancelling answered {r.status}")
        end, sst = time.monotonic() + 10, None
        while time.monotonic() < end:
            sst = H.pay_api.session(sid).get("status")
            if sst == "expired":
                break
            time.sleep(0.5)
        ctx.eq(sst, "expired", "the session's status at the provider within 10 s of cancelling")
        before = H.mail_ids(email)
        H.pay_api.force_complete(sid)
        total = H.world.orders[o3].total
        end = time.monotonic() + 45
        refunds = []
        while time.monotonic() < end:
            pi = H.pay_api.session(sid).get("payment_intent")
            refunds = H.pay_api.refunds_for(pi) if pi else []
            if refunds:
                break
            time.sleep(0.5)
        ctx.eq([x["amount"] for x in refunds], [total], "provider refunds for the late payment")
        reqs = H.pay_api.refund_requests(pi)
        keys = {x.get("idempotency_key") for x in reqs}
        ctx.check(len(keys) == 1 and None not in keys, f"refund requests carried keys {keys}")
        ctx.check(all(str((x.get("metadata") or {}).get("order_id")) == str(o3) for x in refunds), "the refund's metadata[order_id]")
        H.world.cancel(o3)
        H.world.orders[o3].refunds.append(total)
        H.wait_refunds(key, o3, 45)
        ctx.eq(status_of(key, o3), "cancelled", "status of the late-paid cancelled order")
        got = H.mail.wait_for(email, lambda m: m["id"] not in before and "refund issued" in m["subject"].lower(), 1, 35)
        ctx.eq(len(got), 1, "refund emails")
        ctx.check(body_states(got[0]["text"], dollars(total)), "the refund email does not state the amount")


# ======================================================================================== W06
@workflow("W06", "FEFO allocation rules: shelf life on the ship date, expiry ties by lot id, whole and split cases, backorder or cancel, fills", 5, timeout=1500, critical=True)
def w06(ctx):
    rnd = random.Random(606)
    with ctx.step("six companies with different contracts; 24 SKUs with lots expiring around each shelf-life threshold"):
        combos = [(0, False, True), (7, True, True), (14, False, False), (30, True, False), (3, False, True), (21, True, True)]
        comps = [H.company(tier="standard", state=rnd.choice(["NY", "PA", "NH"]), min_days=n, split=sp, backorders=bo) for n, sp, bo in combos]
        skus = []
        for i in range(24):
            pack = [1, 6, 12, 24][i % 4]
            code = H.create_sku(pack, 150, (20, 15, 10), "ambient", i % 3 == 0)
            H.price_all(code, rnd.randint(200, 2500), D(-5))
            skus.append(code)
        ctx.shared["w06"] = (comps, skus)

    def receive_batch(rnd, skus, n_lo, n_hi):
        for code in skus:
            exps = []
            for _ in range(rnd.randint(n_lo, n_hi)):
                exp = D(30) + timedelta(days=rnd.choice([-2, -1, 0, 1, 3, 4, 6, 7, 8, 13, 14, 15, 20, 21, 22, 29, 30, 31, 33, 45, 60]))
                if exps and rnd.random() < 0.3:
                    exp = rnd.choice(exps)
                exps.append(exp)
                H.receive(code, exp, f"A{rnd.randint(1, 10):02d}-{rnd.randint(1, 20):02d}-{rnd.randint(1, 4)}",
                          rnd.choice([5, 7, 12, 13, 18, 24, 25, 30, 36, 40, 50, 60]))

    with ctx.step("lots received: 2 to 5 per SKU, with ties on expiry"):
        comps, skus = ctx.shared["w06"]
        receive_batch(rnd, skus, 2, 5)
    with ctx.step("130 orders paid one after another (a second batch of receipts arrives half way): every allocation matches FEFO"):
        comps, skus = ctx.shared["w06"]
        oids = []
        for i in range(130):
            cust, c, key, _ = comps[i % len(comps)]
            ship = D(rnd.choice([30, 31, 33, 36]))
            lines = []
            for code in rnd.sample(skus, rnd.randint(1, 3)):
                pack = H.world.skus[code].info.case_pack
                lines.append((code, pack * rnd.randint(1, 4) if not cust.split else rnd.randint(1, 40)))
            oid = H.place(key, cust, ship, lines)
            H.pay(c, key, oid)
            oids.append((key, oid))
            if i == 65:
                receive_batch(rnd, skus, 1, 3)
        probs = []
        for key, oid in oids:
            probs += H.compare(key, oid)
        ctx.observe("backordered_lines", sum(1 for o in H.world.orders.values() for l in o.lines if l.backordered and o.oid in {x for _, x in oids}))
        ctx.observe("cancelled_lines", sum(1 for o in H.world.orders.values() for l in o.lines if l.cancelled and o.oid in {x for _, x in oids}))
        mismatch(ctx, probs, "order fields")
    with ctx.step("across the next two clock changes, a lot exactly at the shelf-life floor is eligible and one a day short is not"):
        changes = clock_changes(25, 366)
        ctx.check(changes, "verifier fixture: no clock change within a year")
        probs, n_orders = [], 0
        comps = {}
        for x in changes[:2]:
            for n, back in ((7, 3), (14, 10), (1, 0), (2, 1), (30, 20)):
                ship = x - timedelta(days=back)
                if ship < D(1):
                    continue
                if n not in comps:
                    comps[n] = H.company(tier="standard", state="NH", min_days=n, split=True, backorders=False)
                cust, c, key, _ = comps[n]
                sku = H.create_sku(1, 100, (10, 10, 10), "frozen")  # zone F: no workflow makes waves there
                H.price_all(sku, 300, D(-5))
                for off, b in ((n - 1, "F15-02-1"), (n, "F15-03-1"), (n + 1, "F15-04-1")):
                    H.receive(sku, ship + timedelta(days=off), b, 20)
                oid = H.place(key, cust, ship, [(sku, 30)])
                H.pay(c, key, oid)
                probs += H.compare(key, oid)
                n_orders += 1
        ctx.observe("dst_threshold_orders", n_orders)
        ctx.check(n_orders >= 5 * len(changes[:2]), f"verifier fixture: {n_orders} orders across clock changes {changes[:2]}")
        mismatch(ctx, probs, "order fields")


# ======================================================================================== W07
@workflow("W07", "Backorders: whole cases, fills by order id with each order's own shelf life, cancelled units refunded exactly", 5, critical=True)
def w07(ctx):
    S = D(45)
    with ctx.step("two companies: backorders with whole cases and 10 days' shelf life; no backorders with split cases"):
        bo = H.company(tier="standard", state="NY", min_days=10, split=False, backorders=True)
        nb = H.company(tier="standard", state="NY", min_days=0, split=True, backorders=False)
        x = H.create_sku(6, 130, (30, 20, 12), "ambient", True)
        y = H.create_sku(1, 500, (20, 20, 20), "ambient", False)
        z = H.create_sku(6, 200, (25, 20, 15), "ambient", True)
        w = H.create_sku(1, 100, (10, 10, 10), "chilled", False)
        for code, p in ((x, 389), (y, 1450), (z, 600), (w, 990)):
            H.price_all(code, p, D(-5))
        H.receive(x, S + timedelta(days=30), "A06-10-1", 20)
        H.receive(y, S + timedelta(days=90), "A06-11-1", 100)
        ctx.shared["w07"] = (bo, nb, x, y, z, w)
    with ctx.step("payments allocate whole cases and backorder the rest, or cancel and refund it"):
        bo, nb, x, y, z, w = ctx.shared["w07"]
        (bc, bcl, bkey, bmail), (nc, ncl, nkey, nmail) = (bo[0], bo[1], bo[2], bo[3]), (nb[0], nb[1], nb[2], nb[3])
        o1 = H.place(bkey, bc, S, [(x, 30)])
        H.pay(bcl, bkey, o1)
        o2 = H.place(nkey, nc, S, [(x, 10)])
        H.pay(ncl, nkey, o2)
        o3 = H.place(bkey, bc, S, [(x, 12), (y, 6)])
        H.pay(bcl, bkey, o3)
        o4 = H.place(bkey, bc, S + timedelta(days=25), [(x, 6)])
        H.pay(bcl, bkey, o4)
        j1, j2 = H.order(bkey, o1), H.order(nkey, o2)
        ctx.eq((j1["lines"][0]["reserved"], j1["lines"][0]["backordered"]), (18, 12), "order 1 reserved/backordered (whole cases of 20 units)")
        ctx.eq((j2["lines"][0]["reserved"], j2["lines"][0]["cancelled"]), (2, 8), "order 2 reserved/cancelled")
        rate = Decimal("0.08875")
        want = 8 * 389 + R.tax_of(10 * 389, rate) - R.tax_of(2 * 389, rate)
        ctx.eq([r["amount_cents"] for r in j2["refunds"]], [want], "order 2 refund")
        for key, oid in ((bkey, o1), (nkey, o2), (bkey, o3), (bkey, o4)):
            mismatch(ctx, H.compare(key, oid), "order fields")
        ctx.shared["w07_o"] = (o1, o2, o3, o4)
    with ctx.step("receipts fill backorders by order id, each with its own ship date's shelf life"):
        bo, nb, x, y, z, w = ctx.shared["w07"]
        bkey = bo[2]
        o1, o2, o3, o4 = ctx.shared["w07_o"]
        H.receive(x, S + timedelta(days=5), "A06-12-1", 18)
        ctx.eq([H.order(bkey, o)["lines"][0]["backordered"] for o in (o1, o3, o4)], [12, 12, 6], "backorders after a short-dated receipt")
        H.receive(x, S + timedelta(days=34), "A06-13-1", 30)
        ctx.eq([H.order(bkey, o)["lines"][0]["backordered"] for o in (o1, o3, o4)], [0, 0, 6], "backorders after a receipt eligible for two")
        H.receive(x, S + timedelta(days=40), "A06-14-1", 6)
        ctx.eq([H.order(bkey, o)["lines"][0]["backordered"] for o in (o1, o3, o4)], [0, 0, 0], "backorders after a later receipt")
        for oid in (o1, o3, o4):
            mismatch(ctx, H.compare(bkey, oid), "order fields")
    with ctx.step("cancelling backordered units refunds exactly; out-of-range cancellations are refused"):
        bo, nb, x, y, z, w = ctx.shared["w07"]
        bc, bcl, bkey, bmail = bo
        o5 = H.place(bkey, bc, S, [(z, 24), (y, 1)])
        H.pay(bcl, bkey, o5)
        ctx.eq(H.order(bkey, o5)["lines"][0]["backordered"], 24, "backordered units with no stock")
        page = bcl.get(f"/orders/{o5}", follow=True)
        line = page.dom.attr_one("data-line-no", "1")
        ctx.check(line is not None and line.attrs.get("data-backordered") == "24", "the order page's line markers")
        expect_forms(ctx, bcl, o5, set(), {1: {"cancel-backorder"}, 2: set()}, "the buyer of a backordered line")
        expect_forms(ctx, H.fin, o5, set(), {1: set(), 2: set()}, "finance")
        for bad in ("25", "0", "-3", "x"):
            r = bcl.post_form(f"/orders/{o5}/lines/1/cancel-backorder", tok(bcl, f"/orders/{o5}") + [("units", bad)])
            ctx.check(r.status >= 400, f"cancelling {bad!r} units answered {r.status}")
        ctx.eq(H.order(bkey, o5)["lines"][0]["cancelled"], 0, "cancelled units after refused requests")
        before = H.mail_ids(bmail)
        r = submit_form(bcl, f"/orders/{o5}", "cancel-backorder", {"units": "6"}, follow=False)
        ctx.check(r.status < 400, f"cancelling 6 units answered {r.status}")
        H.world.cancel_backorder(o5, 1, 6)
        H.wait_refunds(bkey, o5)
        rate = Decimal("0.08875")
        ctx.eq(H.world.orders[o5].refunds, [6 * 600 + R.tax_of(24 * 600, rate) - R.tax_of(18 * 600, rate)], "verifier fixture: refund")
        mismatch(ctx, H.compare(bkey, o5), "order fields")
        got = H.mail.wait_for(bmail, lambda m: m["id"] not in before and "refund issued" in m["subject"].lower(), 1, 35)
        ctx.eq(len(got), 1, "refund emails")
        ctx.check(body_states(got[0]["text"], dollars(H.world.orders[o5].refunds[0])), "the refund email does not state the amount")
        ctx.shared["w07_o5"] = o5
    with ctx.step("every refund carries order and refund metadata and its own Idempotency-Key"):
        bo, nb, x, y, z, w = ctx.shared["w07"]
        bc, bcl, bkey, bmail = bo
        o5 = ctx.shared["w07_o5"]
        r = submit_form(bcl, f"/orders/{o5}", "cancel-backorder", {"units": "18"}, follow=False)
        ctx.check(r.status < 400, f"cancelling 18 units answered {r.status}")
        H.world.cancel_backorder(o5, 1, 18)
        H.wait_refunds(bkey, o5)
        mismatch(ctx, H.compare(bkey, o5), "order fields")
        expect_forms(ctx, bcl, o5, set(), {1: set(), 2: set()}, "the buyer once nothing is backordered")
        j = H.order(bkey, o5)
        st = H.pay_api.state()
        pi = next(s["payment_intent"] for s in H.pay_api.sessions_for(o5, st) if s.get("payment_intent"))
        reqs = H.pay_api.refund_requests(pi, st)
        ctx.eq(len({x.get("idempotency_key") for x in reqs}), 2, "distinct Idempotency-Keys for two refunds")
        prov = H.pay_api.refunds_for(pi, st)
        ctx.eq(sorted(x["amount"] for x in prov), sorted(r["amount_cents"] for r in j["refunds"]), "provider refunds")
        app_ids = {str(r["id"]) for r in j["refunds"]}
        ctx.check(all(str((x.get("metadata") or {}).get("order_id")) == str(o5) and str((x.get("metadata") or {}).get("refund_id")) in app_ids
                      for x in prov), "refund metadata[order_id]/metadata[refund_id]")
    with ctx.step("an order whose every unit is cancelled at payment refunds its whole total, freight included, and is cancelled"):
        bo, nb, x, y, z, w = ctx.shared["w07"]
        nc, ncl, nkey, nmail = nb
        o6 = H.place(nkey, nc, S, [(w, 10)])
        H.pay(ncl, nkey, o6)
        H.wait_refunds(nkey, o6)
        ctx.eq(H.world.orders[o6].refunds, [H.world.orders[o6].total], "verifier fixture: full refund")
        mismatch(ctx, H.compare(nkey, o6), "order fields")


# ======================================================================================== W09
@workflow("W09", "Pick paths: wave task selection and the exact shortest route with the dictionary-order tie-break", 5, timeout=1500, critical=True)
def w09(ctx):
    rnd = random.Random(909)
    with ctx.step("90 SKUs each with one lot in a chosen bin (shared bins, stacked levels, mirrored bays)"):
        cust, c, key, _ = H.company(tier="standard", state="NH", split=True, backorders=True)
        bays = [1, 2, 10, 11, 19, 20, 5, 16]
        pool = []
        for i in range(90):
            code = H.create_sku(1, 100, (10, 10, 10), "ambient", False)
            H.price_all(code, 300, D(-5))
            if i % 9 == 8 and pool:
                b = pool[-1][1]
            else:
                b = f"A{rnd.randint(1, 10):02d}-{rnd.choice(bays + [rnd.randint(1, 20)]):02d}-{rnd.randint(1, 4)}"
            H.receive(code, D(400), b, 5000)
            pool.append((code, b))
        chilled = H.create_sku(1, 100, (10, 10, 10), "chilled", False)
        H.price_all(chilled, 300, D(-5))
        H.receive(chilled, D(400), "C13-08-1", 500)
        ctx.shared["w09"] = (cust, c, key, pool, chilled)
    with ctx.step("40 ship dates, one or two paid orders each; each wave takes the first tasks and walks the shortest route"):
        cust, c, key, pool, chilled = ctx.shared["w09"]
        for i in range(40):
            ship = D(100 + i)
            for _ in range(1 if i % 3 else 2):
                lines = [(code, rnd.randint(1, 9)) for code, _ in rnd.sample(pool, rnd.randint(2, 9))]
                if i % 10 == 4:
                    lines.append((chilled, 2))
                oid = H.place(key, cust, ship, lines)
                H.pay(c, key, oid)
            k = rnd.choice([3, 5, 8, 12, 12, 12])
            H.wave("A", ship, k)
            if i % 4 == 0:
                left = [t for t in H.world.tasks if t.wave is None and H.world.orders[t.oid].ship == ship and H.world.lots[t.lot].bin[0] == "A"]
                if left:
                    H.wave("A", ship, 12)
            if i % 10 == 4:
                H.wave("C", ship, 12)
    with ctx.step("a wave with nothing to pick is refused with 409"):
        r, wid = H.create_wave("A", D(399), 12)
        ctx.check(wid is None and r.status == 409, f"an empty wave answered {r.status}")


# ======================================================================================== W10
@workflow("W10", "Picking: scans must match bin and lot, quantities validated, zones enforced, short picks hold the lot and re-allocate", 4, critical=True)
def w10(ctx):
    S = D(170)
    with ctx.step("pickers for zones A and C; a whole-case company; SKU lots in two bins"):
        cust, c, key, _ = H.company(tier="standard", state="NY", split=False, backorders=True)
        nb = H.company(tier="standard", state="NY", split=True, backorders=False)
        pa, pc = H.create_user("picker", zone="A"), H.create_user("picker", zone="C")
        p = H.create_sku(6, 200, (30, 20, 20), "ambient", True)
        q = H.create_sku(1, 200, (20, 20, 20), "ambient", False)
        for code in (p, q):
            H.price_all(code, 410, D(-5))
        l1 = H.receive(p, S + timedelta(days=10), "A02-03-1", 12, lot_code="w10-a " + H.nonce)
        l2 = H.receive(p, S + timedelta(days=20), "A05-11-2", 30)
        l3 = H.receive(q, S + timedelta(days=20), "A07-07-3", 12)
        o1 = H.place(key, cust, S, [(p, 18)])
        H.pay(c, key, o1)
        o2 = H.place(nb[2], nb[0], S, [(q, 12)])
        H.pay(nb[1], nb[2], o2)
        wid, view, chosen = H.wave("A", S, 12)
        ctx.shared["w10"] = (cust, c, key, nb, pa, pc, p, q, l1, l2, l3, o1, o2, wid, chosen)
    with ctx.step("a zone C picker gets 403 for the zone A wave and its picks, and nothing changes"):
        cust, c, key, nb, pa, pc, p, q, l1, l2, l3, o1, o2, wid, chosen = ctx.shared["w10"]
        cc = H.account(pc)
        r = cc.get(f"/waves/{wid}", follow=False)
        if r.status == 200 and r.dom.attr_one("data-task-id"):
            ctx.breach("X1", "a picker saw another zone's wave")
        ctx.eq(r.status, 403, "zone C picker viewing a zone A wave")
        t1 = next(t for t in chosen if t.lot == l1)
        r = H.confirm(t1.app_id, "A02-03-1", H.world.lots[l1].code, 12, client=cc)
        page = H.wave_page(wid)
        st = {t["task_id"]: t["status"] for s in H.wave_view(page)["stops"] for t in s["tasks"]}
        if st.get(t1.app_id) != "open":
            ctx.breach("X1", "a picker confirmed a pick in another zone")
            raise StepFailed("a zone C picker confirmed a zone A pick")
        ctx.eq(r.status, 403, "zone C picker confirming")
    with ctx.step("a wrong bin or lot is refused with 409, bad quantities with 422, a token-less confirm with 403; nothing changes"):
        cust, c, key, nb, pa, pc, p, q, l1, l2, l3, o1, o2, wid, chosen = ctx.shared["w10"]
        ca = H.account(pa)
        t1 = next(t for t in chosen if t.lot == l1)
        code = H.world.lots[l1].code
        for b, l, u, want in (("A02-03-2", code, 12, 409), ("A02-03-1", code + "X", 12, 409), ("A02-03-1", code, 13, 422),
                              ("A02-03-1", code, 5, 422), ("A02-03-1", code, -6, 422)):
            r = H.confirm(t1.app_id, b, l, u, client=ca)
            ctx.eq(r.status, want, f"confirm bin={b} lot={l} units={u}")
        r = H.confirm(t1.app_id, "A02-03-1", code, 12, client=ca, drop_csrf=True)
        ctx.eq(r.status, 403, "token-less confirm")
        st = {t["task_id"]: t["status"] for s in H.wave_view(H.wave_page(wid))["stops"] for t in s["tasks"]}
        if st.get(t1.app_id) != "open":
            ctx.breach("X2", "a refused or token-less pick confirmation took effect")
            raise StepFailed("the task changed after refused confirmations")
    with ctx.step("a short pick (codes typed in other case and spacing) holds the lot and re-allocates the rest from the next lot"):
        cust, c, key, nb, pa, pc, p, q, l1, l2, l3, o1, o2, wid, chosen = ctx.shared["w10"]
        ca = H.account(pa)
        t1 = next(t for t in chosen if t.lot == l1)
        r = H.confirm(t1.app_id, " a02-03-1 ", "  " + H.world.lots[l1].code.lower() + " ", 6, client=ca)
        ctx.check(r.status < 400, f"short pick answered {r.status}")
        H.world.confirm(t1, 6)
        mismatch(ctx, H.compare(key, o1), "order fields")
        ctx.eq(H.lot_view(l1)["lot-status"], "hold", "status of the short-picked lot")
        wp = H.wave_page(wid, ca)
        for n in wp.dom.attr_all("data-task-id"):
            want = n.attrs.get("data-task-status") == "open"
            ctx.eq(n.attr_one("data-action", "confirm-pick") is not None, want, f"confirm-pick form on task {n.attrs.get('data-task-id')} ({n.attrs.get('data-task-status')})")
        r = H.confirm(t1.app_id, "A02-03-1", H.world.lots[l1].code, 6, client=ca)
        ctx.eq(r.status, 409, "confirming the same task again")
        o3 = H.place(key, cust, S, [(p, 6)])
        H.pay(c, key, o3)
        mismatch(ctx, H.compare(key, o3), "order fields (a held lot is not allocated)")
        ctx.check(all(a["lot_id"] != l1 for a in H.order(key, o3)["lines"][0]["allocations"]), "a lot on count hold was allocated")
    with ctx.step("a short pick with nothing left to allocate cancels and refunds for a company without backorders"):
        cust, c, key, nb, pa, pc, p, q, l1, l2, l3, o1, o2, wid, chosen = ctx.shared["w10"]
        ca = H.account(pa)
        t2 = next(t for t in chosen if t.lot == l3)
        r = H.confirm(t2.app_id, "A07-07-3", H.world.lots[l3].code, 0, client=ca)
        ctx.check(r.status < 400, f"zero pick answered {r.status}")
        H.world.confirm(t2, 0)
        H.wait_refunds(nb[2], o2)
        mismatch(ctx, H.compare(nb[2], o2), "order fields")
        ctx.eq(H.order(nb[2], o2)["status"], "cancelled", "status of an order whose every unit was cancelled")


# ======================================================================================== W11
@workflow("W11", "Cycle counts: version checks, adjustments with picked units, releases newest first, re-allocation, holds cleared, fills", 4, critical=True)
def w11(ctx):
    S = D(180)
    with ctx.step("lots with known versions; a stale count is refused with 409 and changes nothing"):
        cust, c, key, _ = H.company(tier="standard", state="NH", split=True, backorders=True)
        v = H.create_sku(1, 100, (10, 10, 10))
        H.price_all(v, 500, D(-5))
        lv = H.receive(v, S + timedelta(days=30), "A08-02-1", 40)
        ctx.eq(H.lot_view(lv)["lot-version"], "1", "version after one receipt")
        ctx.check(has_action(H.sup, f"/lots/{lv}", "count-lot") and not has_action(H.fin, f"/lots/{lv}", "count-lot"),
                  "the count form is not shown exactly to supervisors")
        r = H.count(lv, 10, version=0)
        ctx.eq(r.status, 409, "count with a stale version")
        ctx.eq(H.lot_view(lv), {"on-hand": "40", "reserved": "0", "lot-version": "1", "lot-status": "available"}, "lot after a stale count")
        r = H.count(lv, 37)
        ctx.check(r.status < 400, f"count answered {r.status}")
        H.world.count(lv, 37)
        ctx.eq(H.lot_view(lv), {"on-hand": "37", "reserved": "0", "lot-version": "2", "lot-status": "available"}, "lot after counting 37")
        ctx.shared["w11"] = (cust, c, key, v, lv)
    with ctx.step("picked units waiting to ship are added to the count"):
        cust, c, key, v, lv = ctx.shared["w11"]
        o1 = H.place(key, cust, S, [(v, 10)])
        H.pay(c, key, o1)
        wid, view, chosen = H.wave("A", S, 12)
        H.pick_all(wid, chosen)
        r = H.count(lv, 27)
        ctx.check(r.status < 400, f"count answered {r.status}")
        H.world.count(lv, 27)
        lot = H.world.lots[lv]
        ctx.eq(H.lot_view(lv), {"on-hand": str(lot.on_hand), "reserved": "10", "lot-version": str(lot.version), "lot-status": "available"},
               "lot after a count with picked units")
        ctx.eq(lot.on_hand, 37, "verifier fixture: on-hand")
    with ctx.step("a count below the open reservations releases the newest order's units first and re-allocates them"):
        cust, c, key, v, lv = ctx.shared["w11"]
        w = H.create_sku(1, 100, (10, 10, 10))
        H.price_all(w, 500, D(-5))
        lw = H.receive(w, S + timedelta(days=10), "A08-05-1", 30)
        lw2 = H.receive(w, S + timedelta(days=50), "A08-06-1", 10)
        oa = H.place(key, cust, S + timedelta(days=1), [(w, 12)])
        H.pay(c, key, oa)
        ob = H.place(key, cust, S + timedelta(days=1), [(w, 12)])
        H.pay(c, key, ob)
        oc = H.place(key, cust, S + timedelta(days=1), [(w, 12)])
        H.pay(c, key, oc)
        r = H.count(lw, 15)
        ctx.check(r.status < 400, f"count answered {r.status}")
        # R7.7 fixes the release order (oc's 6 units, then 9 of ob's) but not the order the released units are
        # allocated anew in: the world tries each reading and keeps the one the application's orders match.
        probs, released = H.count_and_compare(key, lw, 15, (oa, ob, oc))
        ctx.check([(o, n) for o, _, n in released] == [(oc, 6), (ob, 9)], f"verifier fixture: released {released}")
        mismatch(ctx, probs, "order fields (no order of re-allocating the released units matches)")
        ctx.shared["w11_w"] = (w, lw, lw2, oa, ob, oc)
    with ctx.step("a count clears a short pick's hold and fills backorders from the counted lot"):
        cust, c, key, v, lv = ctx.shared["w11"]
        w, lw, lw2, oa, ob, oc = ctx.shared["w11_w"]
        wid, view, chosen = H.wave("A", S + timedelta(days=1), 12)
        t = next(t for t in chosen if t.lot == lw)
        r = H.confirm(t.app_id, H.world.lots[lw].bin, H.world.lots[lw].code, t.units - 3)
        ctx.check(r.status < 400, f"short pick answered {r.status}")
        H.world.confirm(t, t.units - 3)
        ctx.eq(H.lot_view(lw)["lot-status"], "hold", "status after a short pick")
        for oid in (oa, ob, oc):
            mismatch(ctx, H.compare(key, oid), "order fields")
        version = H.lot_view(lw)["lot-version"]
        r = H.count(lw, 9, version=version)
        ctx.check(r.status < 400, f"count answered {r.status}")
        H.world.count(lw, 9)
        lot = H.world.lots[lw]
        ctx.eq(H.lot_view(lw), {"on-hand": str(lot.on_hand), "reserved": str(H.world.reserved(lw)), "lot-version": str(lot.version),
                                "lot-status": "available"}, "lot after the count")
        for oid in (oa, ob, oc):
            mismatch(ctx, H.compare(key, oid), "order fields")


# ======================================================================================== W12
@workflow("W12", "Shipping and invoices: tracking, cumulative line tax across shipments, freight on the first shipment only, emails", 5, critical=True)
def w12(ctx):
    S = D(190)
    with ctx.step("a NY company orders taxable tins, oil and cheese; part of the tins are backordered"):
        cust, c, key, email = H.company(tier="standard", state="NY", split=False, backorders=True)
        tin = H.create_sku(6, 130, (30, 20, 12), "ambient", True)
        oil = H.create_sku(6, 1050, (30, 20, 32), "ambient", False)
        che = H.create_sku(6, 820, (30, 25, 30), "chilled", True)
        for code, p in ((tin, 100), (oil, 1450), (che, 2300)):
            H.price_all(code, p, D(-5))
        H.receive(tin, S + timedelta(days=200), "A09-04-1", 12)
        H.receive(oil, S + timedelta(days=200), "A09-17-3", 60)
        H.receive(che, S + timedelta(days=40), "C11-09-2", 30)
        oid = H.place(key, cust, S, [(tin, 30), (oil, 12), (che, 6)])
        H.pay(c, key, oid)
        ctx.eq([l["backordered"] for l in H.order(key, oid)["lines"]], [18, 0, 0], "backordered units")
        ctx.shared["w12"] = (cust, c, key, email, tin, oil, che, oid)
    with ctx.step("while picks are open there is no ship form and shipping is refused with 409"):
        cust, c, key, email, tin, oil, che, oid = ctx.shared["w12"]
        expect_forms(ctx, H.sup, oid, set(), who="the supervisor before picking")
        r = H.sup.post_form(f"/orders/{oid}/ship", tok(H.sup))
        ctx.eq(r.status, 409, "shipping before picking")
        ctx.eq(H.order(key, oid)["shipments"], [], "shipments after a refused ship")
        expect_forms(ctx, c, oid, set(), {1: {"cancel-backorder"}, 2: set(), 3: set()}, "the buyer before anything shipped")
    with ctx.step("the first shipment invoices its lines with tax and the order's freight; the buyer is emailed the tracking number"):
        cust, c, key, email, tin, oil, che, oid = ctx.shared["w12"]
        wid, view, chosen = H.wave("A", S, 12)
        H.pick_all(wid, chosen)
        expect_forms(ctx, H.sup, oid, set(), who="the supervisor with the chilled pick still open")
        wid, view, chosen = H.wave("C", S, 12)
        H.pick_all(wid, chosen)
        expect_forms(ctx, H.sup, oid, {"ship-order"}, who="the supervisor once every pick is confirmed")
        expect_forms(ctx, H.fin, oid, set(), who="finance")
        before = H.mail_ids(email)
        r, sid = H.ship(oid)
        ctx.check(sid, f"shipping answered {r.status}")
        inv = H.world.ship(oid)
        # The shipment email is judge evidence: it is kept before the strict checks below, so a defect they catch
        # costs W12 alone and the experience review still reads the email the application sent.
        got = H.mail.wait_for(email, lambda m: m["id"] not in before and "shipped" in m["subject"].lower(), 1, 35)
        v = None
        try:
            v = H.shipment_view(sid)
        finally:
            if got:
                B.capture_text("shipment_email", f"Subject: {got[0]['subject']}\n\n{got[0]['text']}",
                               {"tracking": v["tracking"] if v else None, "invoice": v["invoice"] if v else None,
                                "invoice_total_cents": inv["total"]})
        ctx.eq([{k: l[k] for k in ("line_no", "units", "amount_cents", "tax_cents")} for l in v["lines"]], inv["lines"], "invoice lines")
        ctx.eq((v["freight"], v["freight_tax"], v["total"]), (inv["freight"], inv["freight_tax"], inv["total"]), "invoice freight and total")
        ctx.check(re.fullmatch(r"PP\d{10}", v["tracking"] or ""), f"tracking number {v['tracking']!r}")
        ctx.eq(len(got), 1, "shipment emails")
        body = got[0]["text"]
        ctx.check(body_states(body, v["tracking"], v["invoice"], dollars(inv["total"])),
                  "the shipment email lacks the tracking number, invoice number or total")
        mismatch(ctx, H.compare(key, oid), "order fields")
        expect_forms(ctx, H.sup, oid, set(), who="the supervisor after shipping (the rest is backordered)")
        expect_forms(ctx, c, oid, set(), {1: {"cancel-backorder", "request-rma"}, 2: {"request-rma"}, 3: {"request-rma"}}, "the buyer after the first shipment")
        ctx.shared["w12_first"] = (sid, v)
    with ctx.step("the backorder fills, ships second: cumulative tax and no freight; the order completes and invoices plus refunds equal the charge"):
        cust, c, key, email, tin, oil, che, oid = ctx.shared["w12"]
        H.receive(tin, S + timedelta(days=210), "A09-05-1", 18)
        wid, view, chosen = H.wave("A", S, 12)
        H.pick_all(wid, chosen)
        r, sid = H.ship(oid)
        ctx.check(sid, f"second shipment answered {r.status}")
        inv = H.world.ship(oid)
        v = H.shipment_view(sid)
        ctx.eq([{k: l[k] for k in ("line_no", "units", "amount_cents", "tax_cents")} for l in v["lines"]], inv["lines"], "second invoice lines")
        ctx.eq((v["freight"], v["freight_tax"], v["total"]), (0, 0, inv["total"]), "second invoice freight and total")
        ctx.check(v["tracking"] != ctx.shared["w12_first"][1]["tracking"], "both shipments have the same tracking number")
        mismatch(ctx, H.compare(key, oid), "order fields")
        j = H.order(key, oid)
        ctx.eq(j["status"], "complete", "status after everything shipped")
        ctx.eq(sum(s["invoice_total_cents"] for s in j["shipments"]), j["total_cents"], "invoices of a fully shipped order")
        expect_forms(ctx, H.sup, oid, set(), who="the supervisor once complete")
        r = H.sup.post_form(f"/orders/{oid}/ship", tok(H.sup))
        ctx.check(r.status >= 400, f"shipping a complete order answered {r.status}")


# ======================================================================================== W13
@workflow("W13", "Refunds under provider faults: retries keep one Idempotency-Key, one provider refund each, complete only on a signed refund.created", 4, timeout=1200, critical=True)
def w13(ctx):
    S = D(200)
    with ctx.step("a company with five paid orders whose units are all backordered"):
        cust, c, key, email = H.company(tier="standard", state="NY", split=True, backorders=True)
        z = H.create_sku(1, 200, (20, 20, 20), "ambient", True)
        H.price_all(z, 733, D(-5))
        oids = []
        for _ in range(5):
            oid = H.place(key, cust, S, [(z, 24)])
            H.pay(c, key, oid)
            oids.append(oid)
        ctx.shared["w13"] = (cust, c, key, email, oids)
    for i, mode in enumerate(("error", "timeout", "lost_response", "slow")):
        with ctx.step(f"provider fault '{mode}' on the refund call: exactly one provider refund, one key, refunded, one email"):
            cust, c, key, email, oids = ctx.shared["w13"]
            oid = oids[i]
            before = H.mail_ids(email)
            H.pay_api.fault(refund_create=mode, refund_create_delay=8)
            r = submit_form(c, f"/orders/{oid}", "cancel-backorder", {"units": "6"}, follow=False)
            ctx.check(r.status < 400, f"cancelling answered {r.status}")
            H.world.cancel_backorder(oid, 1, 6)
            H.wait_refunds(key, oid, 120)
            time.sleep(3)
            st = H.pay_api.state()
            pi = next(s["payment_intent"] for s in H.pay_api.sessions_for(oid, st) if s.get("payment_intent"))
            prov = H.pay_api.refunds_for(pi, st)
            if len(prov) > 1:
                ctx.breach("X6", f"one refund became {len(prov)} provider refunds under fault {mode}")
            ctx.eq([x["amount"] for x in prov], H.world.orders[oid].refunds, "provider refunds")
            keys = {x.get("idempotency_key") for x in H.pay_api.refund_requests(pi, st)}
            ctx.check(len(keys) == 1 and None not in keys, f"refund attempts carried keys {keys}")
            mismatch(ctx, H.compare(key, oid), "order fields")
            got = H.mail.wait_for(email, lambda m: m["id"] not in before and "refund issued" in m["subject"].lower() and f"PP-{oid:06d}" in m["subject"], 1, 30)
            time.sleep(2)
            got = [m for m in H.mail.to(email) if m["id"] not in before and "refund issued" in m["subject"].lower()]
            ctx.eq(len(got), 1, "refund emails")
    with ctx.step("a refund stays pending until its refund.created verifies; a forged one changes nothing"):
        cust, c, key, email, oids = ctx.shared["w13"]
        oid = oids[4]
        H.pay_api.hold(["refund.created"])
        try:
            r = submit_form(c, f"/orders/{oid}", "cancel-backorder", {"units": "6"}, follow=False)
            ctx.check(r.status < 400, f"cancelling answered {r.status}")
            H.world.cancel_backorder(oid, 1, 6)
            end, ev = time.monotonic() + 40, None
            while time.monotonic() < end and ev is None:
                st = H.pay_api.state()
                pi = next((s["payment_intent"] for s in H.pay_api.sessions_for(oid, st) if s.get("payment_intent")), None)
                ev = next((st["events"][e] for e in st.get("held", []) if st["events"][e]["type"] == "refund.created"
                           and st["events"][e]["data"]["object"].get("payment_intent") == pi), None)
                time.sleep(0.5)
            ctx.check(ev is not None, "no refund reached the provider within 40 s")
            j = H.order(key, oid)
            ctx.eq([x["status"] for x in j["refunds"]], ["pending"], "refund status before refund.created")
            body = event_body(ev)
            forged = H.client().request("POST", "/webhooks/payments", body=body, headers={
                "Content-Type": "application/json", "Stripe-Signature": H.pay_api.sign(body, key="whsec_" + "2" * 48)})
            time.sleep(2)
            if H.order(key, oid)["refunds"][0]["status"] != "pending":
                ctx.breach("X7", "a forged refund.created completed a refund")
                raise StepFailed("a forged refund.created completed the refund")
            ctx.eq(forged.status, 400, "forged refund.created status")
        finally:
            H.pay_api.release()
        H.wait_refunds(key, oid, 45)
        mismatch(ctx, H.compare(key, oid), "order fields")


# ======================================================================================== W14
@workflow("W14", "Returns: limits, quarantine, exact credit notes, restock only when not short-dated, write-offs, refills", 4, critical=True)
def w14(ctx):
    S = D(0)
    with ctx.step("a NY company buys 30 tins from three lots expiring in 6, 7 and 30 days; they ship"):
        cust, c, key, email = H.company(tier="standard", state="NY", min_days=0, split=True, backorders=True)
        tin = H.create_sku(1, 130, (30, 20, 12), "ambient", True)
        H.price_all(tin, 200, D(-5))
        la = H.receive(tin, D(6), "A10-01-1", 10)
        lb = H.receive(tin, D(7), "A10-02-1", 10)
        lc = H.receive(tin, D(30), "A10-03-1", 50)
        o1 = H.place(key, cust, S, [(tin, 30)])
        H.pay(c, key, o1)
        wid, view, chosen = H.wave("A", S, 12)
        H.pick_all(wid, chosen)
        r, sid = H.ship(o1)
        ctx.check(sid, f"shipping answered {r.status}")
        H.world.ship(o1)
        ctx.shared["w14"] = (cust, c, key, email, tin, la, lb, lc, o1)
    with ctx.step("return requests beyond the shipped units, without a reason, or on another company's order are refused"):
        cust, c, key, email, tin, la, lb, lc, o1 = ctx.shared["w14"]
        r, rid = H.request_rma(c, o1, 1, 31)
        ctx.check(rid is None and r.status >= 400, f"returning 31 of 30 answered {r.status}")
        r, rid = H.request_rma(c, o1, 1, 3, reason="")
        ctx.check(rid is None and r.status >= 400, f"a return without a reason answered {r.status}")
        other = H.company(tier="standard", state="NY")
        r, rid = H.request_rma(other[1], o1, 1, 2)
        if rid is not None:
            ctx.breach("X1", "a buyer requested a return on another company's order")
        ctx.check(rid is None and r.status in (403, 404), f"another company's return request answered {r.status}")
    with ctx.step("receiving puts the units in quarantine and issues the exact credit note, emailed once; stock is unchanged"):
        cust, c, key, email, tin, la, lb, lc, o1 = ctx.shared["w14"]
        r, r1 = H.request_rma(c, o1, 1, 5, reason="Dented tins")
        ctx.check(r1, f"requesting a return answered {r.status}")
        ctx.eq(H.rma_view(r1, c), {"rma-status": "requested", "disposition": "", "credit-cents": ""}, "new return")
        ctx.check(has_action(H.sup, f"/rmas/{r1}", "receive-rma") and not has_action(H.sup, f"/rmas/{r1}", "inspect-rma")
                  and not has_action(c, f"/rmas/{r1}", "receive-rma"), "receive form not shown exactly to supervisors of a requested return")
        r = c.post_form(f"/rmas/{r1}/receive", tok(c, f"/rmas/{r1}"))
        ctx.eq(r.status, 403, "a buyer receiving a return")
        before = H.mail_ids(email)
        r = H.rma_action(r1, "receive-rma")
        ctx.check(r.status < 400, f"receiving answered {r.status}")
        credit = H.world.rma_receive(o1, 1, 5)
        ctx.eq(credit, 1000 + R.tax_of(6000, Decimal("0.08875")) - R.tax_of(5000, Decimal("0.08875")), "verifier fixture: credit (6000 taxes at exactly half a cent)")
        ctx.eq(H.rma_view(r1), {"rma-status": "quarantined", "disposition": "", "credit-cents": str(credit)}, "received return")
        ctx.check(has_action(H.sup, f"/rmas/{r1}", "inspect-rma") and not has_action(H.sup, f"/rmas/{r1}", "receive-rma"),
                  "a quarantined return does not offer exactly the inspection")
        mismatch(ctx, H.compare(key, o1), "order fields")
        got = H.mail.wait_for(email, lambda m: m["id"] not in before and "credit note" in m["subject"].lower(), 1, 35)
        ctx.eq(len(got), 1, "credit note emails")
        ctx.check(body_states(got[0]["text"], dollars(credit)), "the credit email does not state the amount")
        ctx.eq(H.lot_view(la)["on-hand"], "0", "lot on-hand while the return is in quarantine")
        r, r2 = H.request_rma(c, o1, 1, 25)
        ctx.check(r2, f"second return answered {r.status}")
        r, r3 = H.request_rma(c, o1, 1, 1)
        ctx.check(r3 is None and r.status >= 400, f"returning more than shipped across returns answered {r.status}")
        H.rma_action(r2, "receive-rma")
        credit2 = H.world.rma_receive(o1, 1, 25)
        ctx.eq(H.rma_view(r2)["credit-cents"], str(credit2), "second credit")
        expect_forms(ctx, c, o1, set(), {1: set()}, "the buyer once every shipped unit is on a return")
        mismatch(ctx, H.compare(key, o1), "order fields")
        ctx.shared["w14_rmas"] = (r1, r2)
    with ctx.step("restocking a lot that expires within 7 days writes it off; damaged goods are written off; nothing is added"):
        cust, c, key, email, tin, la, lb, lc, o1 = ctx.shared["w14"]
        r1, r2 = ctx.shared["w14_rmas"]
        r = H.rma_action(r1, "inspect-rma", {"outcome": "restock"})
        ctx.check(r.status < 400, f"inspecting answered {r.status}")
        disp = H.world.rma_inspect(o1, 1, 5, "restock", H.today())
        ctx.eq(disp, "written_off", "verifier fixture: short-dated")
        ctx.eq(H.rma_view(r1), {"rma-status": "closed", "disposition": "written_off", "credit-cents": str(H.world.orders[o1].credits[0])}, "inspected return")
        r = H.rma_action(r2, "inspect-rma", {"outcome": "damaged"})
        H.world.rma_inspect(o1, 1, 25, "damaged", H.today())
        ctx.eq(H.rma_view(r2)["disposition"], "written_off", "damaged return")
        ctx.check(not has_action(H.sup, f"/rmas/{r2}", "inspect-rma") and not has_action(H.sup, f"/rmas/{r2}", "receive-rma"),
                  "a closed return still offers an action")
        r = H.sup.post_form(f"/rmas/{r2}/inspect", tok(H.sup, f"/rmas/{r2}") + [("outcome", "restock")])
        ctx.eq(r.status, 409, "inspecting a closed return")
        for lot in (la, lb, lc):
            ctx.eq(H.lot_view(lot)["on-hand"], str(H.world.lots[lot].on_hand), f"lot {lot} on-hand after write-offs")
    with ctx.step("a return from a lot with 7 days left is restocked into that lot and fills a backorder"):
        cust, c, key, email, tin, la, lb, lc, o1 = ctx.shared["w14"]
        ld = H.receive(tin, D(7), "A10-04-1", 5)
        o2 = H.place(key, cust, S, [(tin, 15)])
        H.pay(c, key, o2)
        wid, view, chosen = H.wave("A", S, 12)
        H.pick_all(wid, chosen)
        r, sid = H.ship(o2)
        ctx.check(sid, f"shipping answered {r.status}")
        H.world.ship(o2)
        o3 = H.place(key, cust, D(1), [(tin, 50)])
        H.pay(c, key, o3)
        ctx.check(H.order(key, o3)["lines"][0]["backordered"] > 0, "verifier fixture: no backorder")
        r, r4 = H.request_rma(c, o2, 1, 5)
        H.rma_action(r4, "receive-rma")
        H.world.rma_receive(o2, 1, 5)
        H.rma_action(r4, "inspect-rma", {"outcome": "restock"})
        disp = H.world.rma_inspect(o2, 1, 5, "restock", H.today())
        ctx.eq(disp, "restocked", "verifier fixture: 7 days left")
        ctx.eq(H.rma_view(r4)["disposition"], "restocked", "return from a lot with 7 days left")
        lot = H.world.lots[ld]
        ctx.eq(H.lot_view(ld), {"on-hand": str(lot.on_hand), "reserved": str(H.world.reserved(ld)), "lot-version": str(lot.version),
                                "lot-status": "available"}, "restocked lot")
        for oid in (o2, o3):
            mismatch(ctx, H.compare(key, oid), "order fields")


# ======================================================================================== W08
@workflow("W08", "Concurrency: racing payments for the last units, double cancellations, double shipments and simultaneous counts", 4, critical=True)
def w08(ctx):
    S = D(210)
    with ctx.step("six orders for the last 12 units are paid while confirmations are held"):
        cust, c, key, email = H.company(tier="standard", state="NH", split=True, backorders=True)
        r_sku = H.create_sku(1, 100, (10, 10, 10))
        H.price_all(r_sku, 800, D(-5))
        lot = H.receive(r_sku, D(400), "A03-03-3", 12)
        oids = [H.place(key, cust, S, [(r_sku, 12)]) for _ in range(6)]
        H.pay_api.hold(["checkout.session.completed"])
        sids = []
        try:
            for oid in oids:
                sids.append(H.pay(c, key, oid, wait=False))
            st = H.pay_api.state()
            held = [st["events"][e] for e in st.get("held", []) if st["events"][e]["data"]["object"].get("id") in sids]
            ctx.check(len(held) == 6, f"verifier fixture: {len(held)} held confirmations")
            bodies = [event_body(e) for e in held]
            outs = simultaneously([lambda b=b: H.client().request("POST", "/webhooks/payments", body=b, headers={
                "Content-Type": "application/json", "Stripe-Signature": H.pay_api.sign(b)}) for b in bodies])
            ctx.check(all(k == "ok" and v.status < 300 for k, v in outs), f"racing deliveries answered {[getattr(v, 'status', v) for k, v in outs]}")
        finally:
            H.pay_api.release()
        js = {oid: H.wait_order(key, oid, lambda j: j["status"] != "awaiting_payment", 30, "paid") for oid in oids}
        time.sleep(3)
        js = {oid: H.order(key, oid) for oid in oids}
        reserved = {oid: j["lines"][0]["reserved"] for oid, j in js.items()}
        if sum(reserved.values()) > 12:
            ctx.breach("X13", f"{sum(reserved.values())} units reserved from a lot holding 12")
        winners = [o for o, v in reserved.items() if v == 12]
        ctx.check(len(winners) == 1 and sum(reserved.values()) == 12, f"reservations after the race: {reserved}")
        H.world.pay(winners[0])
        for oid in oids:
            if oid != winners[0]:
                H.world.pay(oid)
        for oid in oids:
            mismatch(ctx, H.compare(key, oid), "order fields")
        ctx.shared["w08"] = (cust, c, key, r_sku, lot, oids, winners[0])
    with ctx.step("the same cancellation submitted twice at once cancels and refunds once"):
        cust, c, key, r_sku, lot, oids, win = ctx.shared["w08"]
        loser = next(o for o in oids if o != win)
        page = c.get(f"/orders/{loser}", follow=True)
        form = find_form(page.dom, "cancel-backorder")
        fields = [(k, v) for k, v in form_fields(form) if k != "units"] + [("units", "12")]
        action = urllib.parse.urljoin(page.url, form.attrs.get("action") or "")
        outs = simultaneously([lambda: c.copy().post_form(action, fields) for _ in range(3)])
        H.world.cancel_backorder(loser, 1, 12)
        time.sleep(2)
        j = H.order(key, loser)
        if len(j["refunds"]) > 1 or j["lines"][0]["cancelled"] > 12:
            ctx.breach("X6", "one cancellation submitted three times refunded or cancelled more than once")
        mismatch(ctx, H.compare(key, loser), "order fields")
    with ctx.step("two simultaneous ship requests make one shipment and one stock movement"):
        cust, c, key, r_sku, lot, oids, win = ctx.shared["w08"]
        wid, view, chosen = H.wave("A", S, 12)
        H.pick_all(wid, chosen)
        clients = [H.sup, H.account(SUP2)]
        pages = [cl.get(f"/orders/{win}", follow=True) for cl in clients]
        outs = simultaneously([lambda cl=cl, p=p: submit_form(cl, "", "ship-order", {}, page=p) for cl, p in zip(clients, pages)])
        H.world.ship(win)
        time.sleep(1)
        j = H.order(key, win)
        lv = H.lot_view(lot)
        if len(j["shipments"]) > 1 or int(lv["on-hand"]) < 0:
            ctx.breach("X6", f"two ship requests made {len(j['shipments'])} shipments (lot on-hand {lv['on-hand']})")
        ctx.eq(len(j["shipments"]), 1, "shipments")
        ctx.eq(lv["on-hand"], "0", "lot on-hand after shipping 12 of 12")
        mismatch(ctx, H.compare(key, win), "order fields")
    with ctx.step("two counts with the same version at the same moment: exactly one succeeds"):
        lot2 = H.receive(H.create_sku(1, 100, (10, 10, 10)), D(300), "A03-04-1", 50)
        v = H.lot_view(lot2)["lot-version"]
        page = H.sup.get(f"/lots/{lot2}", follow=True)
        form = find_form(page.dom, "count-lot")
        action = urllib.parse.urljoin(page.url, form.attrs.get("action") or "")
        base = [(k, x) for k, x in form_fields(form) if k not in ("counted_units", "version")]
        outs = simultaneously([lambda n=n: H.sup.copy().post_form(action, base + [("counted_units", str(n)), ("version", v)]) for n in (44, 47)])
        codes = sorted(o[1].status for o in outs if o[0] == "ok")
        ok = [x for x in codes if x < 400]
        lv = H.lot_view(lot2)
        if len(ok) > 1 or int(lv["lot-version"]) > int(v) + 1:
            ctx.breach("X6", f"two counts with one version both applied (statuses {codes}, version {lv['lot-version']})")
        ctx.check(len(ok) == 1 and 409 in codes, f"count statuses {codes}")
        ctx.eq(lv["lot-version"], str(int(v) + 1), "version after the race")
        ctx.check(lv["on-hand"] in ("44", "47"), f"on-hand after the race {lv['on-hand']}")
        H.world.count(lot2, int(lv["on-hand"]))


# ======================================================================================== W16 / W26
def legacy_segment(ctx, zones: set, prefixes: tuple, tag: str, ship0: int) -> None:
    """The old system's lots of some zones: the on-hand report rows, then FEFO over them, then the rows again."""
    today = H.today()
    zone_of_bin = {b["code"]: b["zone"] for b in H.baseline["bins"]}
    legacy = sorted(l["id"] for l in H.baseline["lots"] if zone_of_bin[H.world.lots[l["id"]].bin] in zones)

    def rows_match(rows, lot_ids) -> list[str]:
        got = {row[0]: row for row in rows[1:] if row}
        probs = []
        for lid in lot_ids:
            lot = H.world.lots[lid]
            want = H.world.on_hand_row(lot, today) if lot.on_hand > 0 else None
            if want:
                want[1], want[2], want[3] = R.csv_safe(want[1]), R.csv_safe(want[2]), R.csv_safe(want[3])
            g = got.get(str(lid))
            if want is None and g is not None:
                probs.append(f"lot {lid} listed with no stock: {g}")
            elif want is not None and g != want:
                probs.append(f"lot {lid}: got {g} want {want}")
        return probs

    with ctx.step(f"the on-hand report lists every {tag} lot of the old system exactly, within 60 seconds"):
        t0 = time.monotonic()
        r, rows = H.csv(H.fin, "/reports/on-hand.csv")
        took = time.monotonic() - t0
        ctx.observe("seconds", round(took, 1))
        ctx.eq(r.status, 200, "report status")
        ctx.check(took <= 60, f"the report took {took:.0f} s")
        ctx.eq(rows[0] if rows else None, ONHAND_HEADER, "header")
        ctx.check(r.body.decode("utf-8", "replace").count("\r\n") >= len(rows) - 1, "rows do not end in CRLF")
        ids = [int(x[0]) for x in rows[1:] if x and x[0].isdigit()]
        ctx.check(ids == sorted(ids), "rows are not ordered by lot_id")
        ctx.observe("lots", len(legacy))
        mismatch(ctx, rows_match(rows, legacy), "lot rows")
    with ctx.step(f"orders on 30 {tag} SKUs of the old system allocate by FEFO over its lots (expired, held, quarantined, odd codes)"):
        rnd = random.Random(sum(map(ord, tag)))
        cands = []
        for code, ids in H.world.lots_of.items():
            s = H.world.skus[code]
            if not s.active or code in H.world.touched_skus or len(ids) < 3 or code[:3] not in prefixes:
                continue
            lots = [H.world.lots[i] for i in ids]
            score = sum(1 for l in lots if l.qa_hold or l.bin == "QA-01" or l.code != l.code.strip().upper() or l.expiry < today)
            score += len(lots) - len({l.expiry for l in lots})
            cands.append((-score, code))
        cands.sort()
        chosen = [c for _, c in cands[:30]]
        split = H.company(tier="gold", state="PA", min_days=5, split=True, backorders=True)
        whole = H.company(tier="platinum", state="NJ", min_days=0, split=False, backorders=False)
        probs, n = [], 0
        for i, code in enumerate(chosen):
            cust, c, key, _ = split if i % 3 else whole
            ship = D(ship0 + i % 15)
            stock = sum(max(0, H.world.lots[x].on_hand) for x in H.world.lots_of[code])
            pack = H.world.skus[code].info.case_pack
            if cust.split:
                qty = max(1, int(stock * rnd.choice([0.3, 0.6, 1.1])))
            else:
                qty = max(pack, int(stock * rnd.choice([0.2, 0.5, 0.8, 1.2])) // pack * pack)
            if not isinstance(H.world.quote(cust, ship, [(code, qty)]), dict):
                continue
            oid = H.place(key, cust, ship, [(code, qty)])
            H.pay(c, key, oid)
            probs += H.compare(key, oid)
            n += 1
        ctx.observe("orders", n)
        ctx.check(n >= 20, f"verifier fixture: only {n} orders on old SKUs")
        mismatch(ctx, probs, "order fields")
        ctx.shared[f"legacy_{tag}"] = chosen
    with ctx.step(f"the on-hand report shows those reservations on the {tag} lots"):
        r, rows = H.csv(H.fin, "/reports/on-hand.csv")
        mismatch(ctx, rows_match(rows, [lid for code in ctx.shared[f"legacy_{tag}"] for lid in H.world.lots_of[code]]), "lot rows")


@workflow("W16", "The old system's ambient stock: on-hand rows for zone A lots and FEFO over them", 5, timeout=1500, critical=True)
def w16(ctx):
    legacy_segment(ctx, {"A"}, ("OIL", "VIN", "TIN", "PAS", "CHO", "SPI", "HON", "BIS", "GFT", "KIT"), "ambient", 5)


@workflow("W26", "The old system's cold stock: on-hand rows for chilled, frozen and quarantined lots and FEFO over them", 5, timeout=1500, critical=True)
def w26(ctx):
    legacy_segment(ctx, {"C", "F", "Q"}, ("CHE", "CUR", "BUT", "BUR", "PST", "GEL", "DUM"), "cold", 2)


# ======================================================================================== W17 / W27
def valuation_at(at_s: str | None, codes: set | None) -> list[str]:
    """Compare /reports/valuation.csv at an instant (None: a few minutes from now) with FIFO over the world."""
    at = datetime.fromisoformat(at_s) if at_s else None
    q = at_s or (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t0 = time.monotonic()
    r, rows = H.csv(H.fin, "/reports/valuation.csv?as_of=" + urllib.parse.quote(q, safe=""))
    took = time.monotonic() - t0
    if r.status != 200 or not rows or rows[0] != ["sku", "on_hand", "fifo_value_cents"]:
        return [f"as_of={q}: status {r.status}, header {rows[0] if rows else None}"]
    if took > 60:
        return [f"as_of={q}: took {took:.0f} s"]
    got = {row[0]: row for row in rows[1:] if row}
    probs = []
    for code in sorted(codes if codes is not None else set(H.world.skus)):
        on, val = H.world.value(code, at)
        want = [R.csv_safe(code), str(on), str(val)] if on > 0 else None
        if got.get(R.csv_safe(code)) != want:
            probs.append(f"as_of={q} {code}: got {got.get(R.csv_safe(code))} want {want}")
    keys = [row[0] for row in rows[1:] if row]
    if keys != sorted(keys):
        probs.append(f"as_of={q}: rows not ordered by SKU code")
    return probs


def ledger_check(codes: list[str]) -> list[str]:
    probs = []
    for code in codes:
        r, rows = H.csv(H.sup, "/reports/ledger.csv?sku=" + urllib.parse.quote(code))
        if r.status != 200 or not rows or rows[0] != ["lot_id", "kind", "units", "at"]:
            probs.append(f"ledger {code}: status {r.status} header {rows[0] if rows else None}")
            continue
        got = [tuple(x) for x in rows[1:] if x]
        if sorted(got) != sorted(H.world.ledger_rows(code)):
            probs.append(f"ledger {code}: {sorted(set(got) ^ set(H.world.ledger_rows(code)))[:3]}")
        if [x[3] for x in got] != sorted(x[3] for x in got):
            probs.append(f"ledger {code}: rows not ordered by at")
    return probs


def legacy_skus_with(pred, n: int) -> list[str]:
    out = []
    for code, ids in H.world.lots_of.items():
        if code in H.world.touched_skus:
            continue
        if any(pred(m) for i in ids for m in H.world.lots[i].legacy_moves):
            out.append(code)
    return sorted(out)[:n]


@workflow("W17", "FIFO valuation and the ledger on the fall-back nights: instants inside the repeated hour, wall-clock order versus real order", 5, timeout=1500, critical=True)
def w17(ctx):
    for at in ("2026-11-01T01:30:00-04:00", "2026-11-01T01:30:00-05:00", "2026-11-01T01:05:00-05:00", "2025-11-02T06:20:00Z",
               "2025-11-02T01:59:59-04:00", "2026-11-09T23:59:59+00:00"):
        with ctx.step(f"valuation as of {at} matches FIFO over the old system's movements for every SKU"):
            mismatch(ctx, valuation_at(at, None), "SKU rows")
    with ctx.step("the ledger of SKUs with movements in a repeated hour lists them at the right UTC instants, in order"):
        fold = legacy_skus_with(lambda m: m[0].astimezone(H.tz).fold == 1 or (m[0].astimezone(H.tz).hour == 1 and m[0].astimezone(H.tz).date() in
                                                                            (date(2025, 11, 2), date(2026, 11, 1))), 120)
        ctx.check(len(fold) >= 40, f"verifier fixture: {len(fold)} SKUs")
        mismatch(ctx, ledger_check(fold), "ledgers")


@workflow("W27", "FIFO valuation across the skipped spring hour and same-instant receipts, and now: newest layers, half-cent totals", 5, timeout=1500, critical=True)
def w27(ctx):
    for at in ("2026-03-08T06:59:59Z", "2026-03-08T07:00:00Z", "2026-03-08T03:30:00-04:00", "2026-03-08T01:59:59-05:00",
               "2026-06-15T14:30:00Z", "2026-06-15T14:29:59Z"):
        with ctx.step(f"valuation as of {at} matches FIFO over the old system's movements for every SKU"):
            mismatch(ctx, valuation_at(at, None), "SKU rows")
    with ctx.step("the ledger of SKUs with movements stored in the skipped hour lists them at the stated instants"):
        gap = legacy_skus_with(lambda m: date(2026, 3, 8) == m[0].astimezone(H.tz).date() and m[0].astimezone(H.tz).hour in (1, 3), 60)
        ctx.check(len(gap) >= 10, f"verifier fixture: {len(gap)} SKUs")
        mismatch(ctx, ledger_check(gap), "ledgers")
    with ctx.step("new receipts at half-cent costs, negative and positive counts: valued now, newest layers first, half-up"):
        v1 = H.create_sku(1, 100, (10, 10, 10))
        l1 = H.receive(v1, D(300), "A01-09-1", 10, "2.0000")
        l2 = H.receive(v1, D(320), "A01-09-2", 5, "3.1250")
        H.count(l1, 2)
        H.world.count(l1, 2)
        H.count(l2, 8)
        H.world.count(l2, 8)
        v2 = H.create_sku(1, 100, (10, 10, 10))
        l3 = H.receive(v2, D(300), "A01-10-1", 7, "0.3335")
        H.receive(v2, D(300), "A01-10-2", 9, "1.0050")
        H.count(l3, 0)
        H.world.count(l3, 0)
        v3 = H.create_sku(1, 100, (10, 10, 10))
        H.receive(v3, D(300), "A01-11-1", 3, "1.0050")
        ctx.check(H.world.value(v3) == (3, 302) and H.world.value(v2) == (9, 905), f"verifier fixture: {H.world.value(v2)} {H.world.value(v3)}")
        untouched = {c for c in H.world.skus if c not in H.world.touched_skus and H.world.skus[c].sku_id <= len(H.baseline["skus"])}
        mismatch(ctx, valuation_at(None, untouched | {v1, v2, v3}), "SKU rows")
    with ctx.step("an as_of without an offset is answered 400; a supervisor gets 403"):
        for bad in ("2026-11-01T01:30:00", "yesterday", ""):
            ctx.eq(H.fin.get("/reports/valuation.csv?as_of=" + urllib.parse.quote(bad)).status, 400, f"as_of={bad!r}")
        ctx.eq(H.sup.get("/reports/valuation.csv?as_of=2026-11-01T00:00:00Z").status, 403, "supervisor valuation")


# ======================================================================================== W18
@workflow("W18", "Order history export: the old system's orders in UTC (folds), company scoping, quoting and spreadsheet safety", 3, critical=True)
def w18(ctx):
    def expected(rows_src, names):
        out = []
        for o in rows_src:
            at = R.legacy_instant(datetime.fromisoformat(o["placed_local"]), o["fold"], H.baseline["tz"])
            out.append([o["number"], str(o["customer_id"]), R.csv_safe(names[o["customer_id"]]), R.utc_z(at), o["status"],
                        R.csv_safe(o["po_number"] or ""), o["ship_date"], str(o["total_cents"])])
        return out

    names = {c["id"]: c["name"] for c in H.baseline["customers"]}
    with ctx.step("a buyer of a company with an odd name (from the old system) exports their history: exact rows in UTC"):
        def good(c):
            return c["name"][:1] in "=+-@" or "," in c["name"] or '"' in c["name"]
        bids = {}
        for u in H.baseline["users"]:
            if u["role"] == "buyer" and u["active"] and u["customer_id"]:
                bids.setdefault(u["customer_id"], u)
        cands = sorted((-sum(1 for o in H.baseline["legacy_orders"] if o["customer_id"] == c["id"] and o["fold"] == 1), c["id"])
                       for c in H.baseline["customers"] if good(c) and c["id"] in bids)
        ctx.check(cands, "verifier fixture: no company with an odd name and an active buyer")
        cid = cands[0][1]
        buyer = H.account(bids[cid]["email"].strip().lower())
        key = H.create_key(buyer)
        cust = H.world.customers[cid]
        sku = H.create_sku(1, 100, (10, 10, 10))
        H.price_all(sku, 450, D(-5))
        o1 = H.place(key, cust, D(15), [(sku, 2)], po="=1+2")
        o2 = H.place(key, cust, D(15), [(sku, 3)], po='a,"b"')
        r, rows = H.csv(buyer, "/orders/export.csv")
        ctx.eq(r.status, 200, "export status")
        raw = r.body.decode("utf-8-sig", "replace")
        ctx.check(raw.startswith(",".join(ORDERS_HEADER) + "\r\n"), "header row differs or does not end in CRLF")
        want = expected([o for o in H.baseline["legacy_orders"] if o["customer_id"] == cid], names)
        got = [row for row in rows[1:] if row]
        legacy_got = [row for row in got if row[0].startswith("SO-")]
        want.sort(key=lambda x: (x[3], x[0]))
        if legacy_got != want:
            diffs = [f"{g} != {w}" for g, w in zip(legacy_got, want) if g != w][:4]
            raise StepFailed(f"legacy rows differ ({len(legacy_got)} vs {len(want)}): {diffs}")
        new = {row[0]: row for row in got if row[0].startswith("PP-")}
        ctx.eq(sorted(new), sorted([f"PP-{o1:06d}", f"PP-{o2:06d}"]), "new orders in the export")
        ctx.eq(new[f"PP-{o1:06d}"][5], "'=1+2", "a PO number that looks like a formula")
        ctx.eq(new[f"PP-{o2:06d}"][5], 'a,"b"', "a PO number with a comma and quotes")
        ctx.eq(new[f"PP-{o1:06d}"][2], R.csv_safe(names[cid]), "customer_name of a new order")
        ctx.check(all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", row[3]) for row in got), "placed_at is not UTC YYYY-MM-DDTHH:MM:SSZ")
        keys = [(row[3], row[0]) for row in got]
        ctx.check(keys == sorted(keys), "rows are not ordered by placed_at, then number")
        others = [row[0] for row in got if row[1] != str(cid)]
        if others:
            ctx.breach("X1", f"a buyer's export listed other companies' orders: {others[:3]}")
        ctx.check(not others, "other companies' orders in a buyer's export")
    with ctx.step("finance exports every order, the old system's exactly; a supervisor gets 403"):
        r, rows = H.csv(H.fin, "/orders/export.csv")
        ctx.eq(r.status, 200, "finance export status")
        got = [row for row in rows[1:] if row and row[0].startswith("SO-")]
        want = sorted(expected(H.baseline["legacy_orders"], names), key=lambda x: (x[3], x[0]))
        if got != want:
            diffs = [f"{g} != {w}" for g, w in zip(got, want) if g != w][:4]
            raise StepFailed(f"{sum(1 for g, w in zip(got, want) if g != w)} of {len(want)} legacy rows differ: {diffs}")
        ctx.check(sum(1 for row in rows[1:] if row and row[0].startswith("PP-")) >= len(H.world.orders), "new orders missing from finance's export")
        ctx.eq(H.sup.get("/orders/export.csv").status, 403, "supervisor export")


# ======================================================================================== W19
@workflow("W19", "Low-stock email: below the reorder point, expired and held lots excluded, once per SKU per day to each active supervisor", 3)
def w19(ctx):
    S = D(220)
    with ctx.step("a deactivated supervisor, a SKU with a reorder point of 20 and 30 units: no email yet"):
        gone = H.create_user("supervisor")
        H.account(gone)
        uid = H.user_ids[gone]
        r = submit_form(H.fin, f"/users/{uid}", "deactivate-user", {}, follow=False)
        ctx.check(r.status < 400, f"deactivating answered {r.status}")
        sups = sorted({u["email"].strip().lower() for u in H.baseline["users"] if u["role"] == "supervisor" and u["active"]})
        cust, c, key, _ = H.company(tier="standard", state="NH", split=True, backorders=True)
        ls = H.create_sku(1, 100, (10, 10, 10), reorder=20, name="Smoked Anchovy Fillets")
        H.price_all(ls, 500, D(-5))
        before = {s: H.mail_ids(s) for s in sups + [gone]}
        H.receive(ls, D(300), "A02-15-1", 30)
        time.sleep(3)
        for s in sups:
            ctx.eq(len([m for m in H.mail.to(s) if m["id"] not in before[s] and ls in m["subject"]]), 0, f"low-stock emails to {s} at 30 units")
        ctx.shared["w19"] = (cust, c, key, ls, sups, gone, before)
    with ctx.step("an allocation takes it to 18: every active supervisor gets one email naming the SKU, 18 and 20"):
        cust, c, key, ls, sups, gone, before = ctx.shared["w19"]
        oid = H.place(key, cust, S, [(ls, 12)])
        H.pay(c, key, oid)
        for s in sups:
            got = H.mail.wait_for(s, lambda m, s=s: m["id"] not in before[s] and "low stock" in m["subject"].lower() and ls in m["subject"], 1, 35)
            ctx.eq(len(got), 1, f"low-stock emails to {s}")
            ctx.check(re.search(r"\b18\b", got[0]["text"]) and re.search(r"\b20\b", got[0]["text"]), "the email does not state 18 available and the reorder point 20")
    with ctx.step("further drops and a receipt the same day send nothing more; the deactivated supervisor got nothing"):
        cust, c, key, ls, sups, gone, before = ctx.shared["w19"]
        oid = H.place(key, cust, S, [(ls, 3)])
        H.pay(c, key, oid)
        H.receive(ls, D(300), "A02-15-2", 2)
        time.sleep(4)
        for s in sups:
            n = len([m for m in H.mail.to(s) if m["id"] not in before[s] and ls in m["subject"]])
            if n > 1:
                ctx.breach("X6", f"{s} got {n} low-stock emails for one SKU on one day")
            ctx.eq(n, 1, f"low-stock emails to {s} after more changes")
        ctx.eq(len([m for m in H.mail.to(gone) if ls in m["subject"]]), 0, "emails to a deactivated supervisor")
        ctx.check(not has_action(H.fin, f"/users/{H.user_ids[gone]}", "deactivate-user"), "an inactive user still has a deactivate form")
    with ctx.step("expired lots do not count: stock that is all expired triggers the email at once"):
        cust, c, key, ls, sups, gone, before = ctx.shared["w19"]
        ex = H.create_sku(1, 100, (10, 10, 10), reorder=10)
        b2 = {s: H.mail_ids(s) for s in sups}
        H.receive(ex, D(-1), "A02-16-1", 100)
        for s in sups:
            got = H.mail.wait_for(s, lambda m, s=s: m["id"] not in b2[s] and ex in m["subject"], 1, 35)
            ctx.eq(len(got), 1, f"low-stock emails to {s} for expired stock")
        H.receive(ex, D(100), "A02-16-2", 5)
        time.sleep(3)
        for s in sups:
            ctx.eq(len([m for m in H.mail.to(s) if m["id"] not in b2[s] and ex in m["subject"]]), 1, f"emails to {s} after a second receipt")


# ======================================================================================== W20
@workflow("W20", "API keys and the order API: shown once, scoped to the company, revocation, inactive users, validation, mass assignment", 3)
def w20(ctx):
    with ctx.step("a key is shown once and works; it is not shown again"):
        cust, c, key, email = H.company(tier="standard", state="NH", split=True)
        sku = H.create_sku(6, 100, (10, 10, 10))
        H.price_all(sku, 500, D(-5))
        page = c.get("/account/api-keys", follow=True)
        ctx.check(not any(n in page.text for n in key_needles(key)), "the key is shown again")
        oid = H.place(key, cust, D(30), [(sku, 6)])
        ctx.shared["w20"] = (cust, c, key, email, sku, oid)
    with ctx.step("malformed bodies answer 400; invalid orders answer 422 with errors and create nothing"):
        cust, c, key, email, sku, oid = ctx.shared["w20"]
        ctx.eq(H.api(key, "POST", "/api/v1/orders", b"{not json").status, 400, "malformed JSON")
        ctx.eq(H.api(key, "POST", "/api/v1/orders", b"[1, 2]").status, 400, "a JSON array")
        bad = [{"ship_date": D(-1).isoformat(), "lines": [{"sku": sku, "qty": 6}]}, {"ship_date": D(366).isoformat(), "lines": [{"sku": sku, "qty": 6}]},
               {"ship_date": "2026-13-01", "lines": [{"sku": sku, "qty": 6}]}, {"ship_date": D(5).isoformat(), "lines": []},
               {"ship_date": D(5).isoformat(), "lines": [{"sku": sku, "qty": 1}] * 13}, {"ship_date": D(5).isoformat(), "lines": [{"sku": sku, "qty": 6}, {"sku": sku.lower(), "qty": 6}]},
               {"ship_date": D(5).isoformat(), "lines": [{"sku": sku, "qty": 0}]}, {"ship_date": D(5).isoformat(), "lines": [{"sku": sku, "qty": 2.5}]},
               {"ship_date": D(5).isoformat(), "lines": [{"sku": sku, "qty": 100001}]}, {"ship_date": D(5).isoformat(), "lines": [{"sku": "NOPE-" + H.nonce, "qty": 6}]},
               {"ship_date": D(5).isoformat(), "po_number": "P" * 41, "lines": [{"sku": sku, "qty": 6}]},
               {"ship_date": D(5).isoformat(), "notes": "ring \x07 bell", "lines": [{"sku": sku, "qty": 6}]},
               {"ship_date": D(5).isoformat(), "lines": "everything"}]
        n0 = len(parse_csv(c.get("/orders/export.csv").body))
        for b in bad:
            r = H.api(key, "POST", "/api/v1/orders", b)
            ok = r.status == 422
            try:
                ok = ok and isinstance(r.json().get("errors"), list) and len(r.json()["errors"]) >= 1
            except Exception:
                ok = False
            ctx.check(ok, f"invalid order {json.dumps(b)[:120]} answered {r.status}: {r.text[:120]}")
        ctx.eq(len(parse_csv(c.get("/orders/export.csv").body)), n0, "orders in the company's export after refused orders")
    with ctx.step("extra JSON keys never change the company, prices or status"):
        cust, c, key, email, sku, oid = ctx.shared["w20"]
        other = H.company(tier="standard", state="NH")
        r = H.api_order(key, D(30), [(sku, 6)], extra={"customer_id": other[0].cid, "status": "complete", "total_cents": 1,
                                                        "lines": [{"sku": sku, "qty": 6, "unit_price_cents": 1, "amount_cents": 1}]})
        ctx.eq(r.status, 201, "order with extra keys")
        j = r.json()
        if j.get("customer_id") != cust.cid or j["lines"][0]["unit_price_cents"] != 500 or j.get("status") != "awaiting_payment":
            ctx.breach("X5", f"extra JSON keys changed the order: {j.get('customer_id')} {j['lines'][0]['unit_price_cents']} {j.get('status')}")
            raise StepFailed("extra JSON keys changed the order")
        r = H.api(other[2], "GET", f"/api/v1/orders/{oid}")
        if r.status == 200 and str(oid) in r.text:
            ctx.breach("X1", "a key of another company read an order")
        ctx.eq(r.status, 404, "another company's key reading the order")
        ctx.shared["w20_other"] = other
    with ctx.step("a revoked key, an unknown key and the key of a deactivated user answer 401"):
        cust, c, key, email, sku, oid = ctx.shared["w20"]
        listed_ids = lambda p: {n.attrs["data-api-key-id"] for n in p.dom.attr_all("data-api-key-id") if not n.attrs.get("data-api-key")}  # noqa: E731
        ids0 = listed_ids(c.get("/account/api-keys", follow=True))
        k2 = H.create_key(c, "to revoke")
        ctx.eq(H.api(k2, "GET", f"/api/v1/orders/{oid}").status, 200, "a new key")
        page = c.get("/account/api-keys", follow=True)
        # The new key is the one whose data-api-key-id the creation page showed (else the one id not listed before);
        # the contract fixes no list order, so its revoke form is found inside its own listed element.
        new_ids = listed_ids(page) - ids0
        revoked_id = H.api_key_ids.get(k2) or (next(iter(new_ids)) if len(new_ids) == 1 else None)
        node = next((n for n in page.dom.attr_all("data-api-key-id", str(revoked_id)) if not n.attrs.get("data-api-key")), None)
        ctx.check(revoked_id and node is not None, f"the new key (id {revoked_id}) is not listed")
        try:
            target = find_form(node, "revoke-api-key")
        except FormNotFound:
            raise StepFailed(f"the new key (id {revoked_id}) is listed without its revoke form")
        c.post_form(urllib.parse.urljoin(page.url, target.attrs.get("action") or page.url), form_fields(target), follow=True)
        r = H.api(k2, "GET", f"/api/v1/orders/{oid}")
        if r.status == 200:
            ctx.breach("X7", "a revoked API key still worked")
        ctx.eq(r.status, 401, "a revoked key")
        page = c.get("/account/api-keys", follow=True)
        listed = [n for n in page.dom.attr_all("data-api-key-id") if not n.attrs.get("data-api-key")]
        ctx.check(any(n.attrs["data-api-key-id"] == revoked_id for n in listed), "the revoked key is not listed")
        for n in listed:
            has = n.attr_one("data-action", "revoke-api-key") is not None
            ctx.eq(has, n.attrs["data-api-key-id"] != revoked_id, f"revoke form on key {n.attrs['data-api-key-id']}")
        ctx.eq(H.api(key, "GET", f"/api/v1/orders/{oid}").status, 200, "the other key after revoking one")
        ctx.eq(H.api("ppk_" + secrets.token_urlsafe(30), "GET", f"/api/v1/orders/{oid}").status, 401, "an unknown key")
        ctx.eq(H.api("", "GET", f"/api/v1/orders/{oid}").status, 401, "no key")
        r = submit_form(H.fin, f"/users/{H.user_ids[email]}", "deactivate-user", {}, follow=False)
        ctx.check(r.status < 400, f"deactivating answered {r.status}")
        r = H.api(key, "GET", f"/api/v1/orders/{oid}")
        if r.status == 200:
            ctx.breach("X7", "a deactivated user's API key still worked")
        ctx.eq(r.status, 401, "a deactivated user's key")
        if H.signed_in(c):
            ctx.breach("X9", "a deactivated user's session still worked")
            raise StepFailed("a deactivated user's session still worked")


# ======================================================================================== W21
@workflow("W21", "Tenancy and mass assignment: another company's orders, shipments, returns and actions; hidden fields change nothing", 4)
def w21(ctx):
    S = D(230)
    with ctx.step("company A has a shipped order with a received return; company B has a buyer and an approver"):
        a = H.company(tier="standard", state="NY", split=True, backorders=True)
        b = H.company(tier="standard", state="NJ", split=True, backorders=True)
        b_appr = H.create_user("approver", b[0].cid)
        sku = H.create_sku(1, 100, (10, 10, 10), "ambient", True)
        H.price_all(sku, 640, D(-5))
        H.receive(sku, D(300), "A04-12-1", 10)
        oa = H.place(a[2], a[0], S, [(sku, 14)])
        H.pay(a[1], a[2], oa)
        wid, view, chosen = H.wave("A", S, 12)
        H.pick_all(wid, chosen)
        r, sid = H.ship(oa)
        H.world.ship(oa)
        r, rid = H.request_rma(a[1], oa, 1, 2)
        H.rma_action(rid, "receive-rma")
        H.world.rma_receive(oa, 1, 2)
        ctx.check(sid and rid, "verifier fixture: shipment or return missing")
        ctx.shared["w21"] = (a, b, b_appr, sku, oa, sid, rid)
    with ctx.step("company B's buyer gets 404/403 and none of the data for A's order, shipment, invoice and return"):
        a, b, b_appr, sku, oa, sid, rid = ctx.shared["w21"]
        bc = b[1]
        num = f"PP-{oa:06d}"
        tracking = H.order(a[2], oa)["shipments"][0]["tracking_number"]
        for path in (f"/orders/{oa}", f"/shipments/{sid}", f"/rmas/{rid}"):
            r = bc.get(path, follow=True)
            if r.status == 200 or num in r.text or tracking in r.text:
                ctx.breach("X1", f"another company's buyer saw {path}")
            ctx.check(r.status in (403, 404), f"B's buyer GET {path} answered {r.status}")
        r, rows = H.csv(bc, "/orders/export.csv")
        if any(row and row[0] == num for row in rows):
            ctx.breach("X1", "another company's order is in a buyer's export")
            raise StepFailed("another company's order in the export")
        if num in bc.get("/orders", follow=True).text:
            ctx.breach("X1", "another company's order is in a buyer's order list")
            raise StepFailed("another company's order in the list")
    with ctx.step("company B's buyer and approver cannot act on A's order; nothing changes"):
        a, b, b_appr, sku, oa, sid, rid = ctx.shared["w21"]
        before = H.order(a[2], oa)
        for c in (b[1], H.account(b_appr)):
            t = tok(c)
            for path, fields in ((f"/orders/{oa}/lines/1/cancel-backorder", [("units", "1")]), (f"/orders/{oa}/rmas", [("line_no", "1"), ("units", "1"), ("reason", "x")]),
                                 (f"/orders/{oa}/cancel", []), (f"/orders/{oa}/pay", []), (f"/orders/{oa}/approve", []), (f"/rmas/{rid}/receive", [])):
                r = c.post_form(path, t + fields)
                ctx.check(r.status in (403, 404, 409), f"POST {path} by another company answered {r.status}")
        after = H.order(a[2], oa)
        if after != before:
            ctx.breach("X1", "another company's requests changed an order")
            raise StepFailed("another company's requests changed the order")
    with ctx.step("hidden order fields change nothing: the order belongs to the buyer's company at list prices"):
        a, b, b_appr, sku, oa, sid, rid = ctx.shared["w21"]
        bc = b[1]
        page = bc.get("/orders/new", follow=True)
        form = find_form(page.dom, "create-order")
        base = [(k, v) for k, v in form_fields(form)]
        fields = base + [("ship_date", D(31).isoformat()), ("po_number", "MA-1"), ("notes", ""), ("sku", sku), ("qty", "3")]
        fields += [("customer_id", str(a[0].cid)), ("unit_price_cents", "1"), ("unit_price", "1"), ("total_cents", "1"), ("status", "complete"),
                   ("created_by", "1")]
        r = bc.post_form("/orders/new", fields)
        m = re.search(r"/orders/(\d+)", r.location or "")
        ctx.check(m, f"creating an order with extra fields answered {r.status}")
        oid = int(m.group(1))
        j = H.order(b[2], oid) if H.api(b[2], "GET", f"/api/v1/orders/{oid}").status == 200 else None
        if j is None or j["customer_id"] != b[0].cid or j["lines"][0]["unit_price_cents"] != 640 or j["status"] != "awaiting_payment":
            ctx.breach("X5", f"hidden form fields changed an order: {j and (j['customer_id'], j['lines'][0]['unit_price_cents'], j['status'])}")
            raise StepFailed("hidden form fields changed the order")
    with ctx.step("the profile form changes only the display name; an approver cannot change the company's limit"):
        a, b, b_appr, sku, oa, sid, rid = ctx.shared["w21"]
        bc = b[1]
        email = [e for e, i in H.user_ids.items() if H.q("SELECT customer_id FROM users WHERE id = %s", (i,))[0]["customer_id"] == b[0].cid
                 and H.q("SELECT role FROM users WHERE id = %s", (i,))[0]["role"] == "buyer"][0]
        uid = H.user_ids[email]
        before = H.q("SELECT email, role, customer_id, zone, active FROM users WHERE id = %s", (uid,))[0]
        submit_form(bc, "/profile", "save-profile", {"full_name": "Renamed Buyer"}, follow=True,
                    override={"role": "finance", "customer_id": str(a[0].cid), "zone": "A", "active": "false", "email": f"x{H.nonce}@evil.example", "id": "1"})
        after = H.q("SELECT full_name, email, role, customer_id, zone, active FROM users WHERE id = %s", (uid,))[0]
        changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
        if changed:
            ctx.breach("X5", f"the profile form changed {changed}")
            raise StepFailed(f"the profile form changed {changed}")
        ctx.eq(after["full_name"], "Renamed Buyer", "display name")
        ac = H.account(b_appr)
        lim0 = H.q("SELECT approval_limit_cents FROM customers WHERE id = %s", (b[0].cid,))[0]["approval_limit_cents"]
        r = ac.post_form(f"/customers/{b[0].cid}", tok(ac) + [("name", "x"), ("tier", "platinum"), ("ship_to_state", "NH"), ("approval_limit_cents", "99999999"),
                         ("min_shelf_life_days", "0"), ("allow_split_cases", "true"), ("backorders_allowed", "true")])
        row = H.q("SELECT approval_limit_cents, tier FROM customers WHERE id = %s", (b[0].cid,))[0]
        if row["approval_limit_cents"] != lim0 or row["tier"] != "standard":
            ctx.breach("X5", "an approver changed the company's settings")
            raise StepFailed("an approver changed the company's settings")
        ctx.eq(r.status, 403, "approver POST to the customer page")


# ======================================================================================== W22
@workflow("W22", "Every state change needs the session token: orders, approvals, payments, returns, stock, prices, customers, users, keys", 3, timeout=900)
def w22(ctx):
    S = D(240)
    with ctx.step("fixtures: orders awaiting approval, awaiting payment, backordered, shipped; returns; a wave; keys"):
        cust, bc, key, bmail = H.company(tier="standard", state="NY", limit=5000, split=True, backorders=True)
        appr = H.create_user("approver", cust.cid)
        ac = H.account(appr)
        s1 = H.create_sku(1, 100, (10, 10, 10))
        s2 = H.create_sku(1, 100, (10, 10, 10))
        H.price_all(s1, 1000, D(-5))
        H.price_all(s2, 1000, D(-5))
        lot = H.receive(s1, D(300), "A07-15-1", 100)
        oa = H.place(key, cust, S, [(s1, 10)])
        op = H.place(key, cust, S, [(s1, 1)])
        ctx.check(H.order(key, op)["status"] == "awaiting_payment", "verifier fixture: small order needs approval")
        def approved_paid(lines):
            oid = H.place(key, cust, S, lines)
            r = submit_form(ac, f"/orders/{oid}", "approve-order", {}, follow=False)
            ctx.check(r.status < 400, f"approving answered {r.status}")
            H.world.approve(oid)
            H.pay(bc, key, oid)
            return oid
        of = approved_paid([(s1, 5), (s2, 3)])
        osh = approved_paid([(s1, 6)])
        osh2 = approved_paid([(s1, 4)])
        wid, view, chosen = H.wave("A", S, 12)
        for t in chosen:
            if t.oid in (osh, osh2):
                l = H.world.lots[t.lot]
                H.confirm(t.app_id, l.bin, l.code, t.units)
                H.world.confirm(t, t.units)
        r, sid = H.ship(osh)
        ctx.check(sid, f"shipping answered {r.status}")
        H.world.ship(osh)
        r, r_req = H.request_rma(bc, osh, 1, 1)
        r, r_q = H.request_rma(bc, osh, 1, 2)
        H.rma_action(r_q, "receive-rma")
        H.world.rma_receive(osh, 1, 2)
        open_task = next(t for t in chosen if t.oid == of and t.status == "open")
        victim = H.create_user("buyer", cust.cid)
        ctx.shared["w22"] = (cust, bc, key, ac, s1, lot, oa, op, of, osh, osh2, r_req, r_q, wid, open_task, victim)
    with ctx.step("each form submitted without its token is refused with 403 and changes nothing"):
        cust, bc, key, ac, s1, lot, oa, op, of, osh, osh2, r_req, r_q, wid, open_task, victim = ctx.shared["w22"]
        fails = []

        def attempt(label, client, path, action, fields, changed):
            try:
                r = submit_form(client, path, action, fields, drop_csrf=True)
            except FormNotFound:
                fails.append(f"{label}: no {action} form on {path}")
                return
            if changed():
                ctx.breach("X2", f"{label} without the session token took effect")
                fails.append(f"{label} took effect without the token")
            elif r.status != 403:
                fails.append(f"{label} without the token answered {r.status}")

        n_export = lambda: len(parse_csv(bc.get("/orders/export.csv").body))  # noqa: E731
        e0 = n_export()
        attempt("create-order", bc, "/orders/new", "create-order", {"ship_date": S.isoformat(), "po_number": "", "notes": "", "sku": s1, "qty": "1"},
                lambda: n_export() != e0)
        attempt("approve-order", ac, f"/orders/{oa}", "approve-order", {}, lambda: status_of(key, oa) != "awaiting_approval")
        attempt("cancel-order", bc, f"/orders/{op}", "cancel-order", {}, lambda: status_of(key, op) != "awaiting_payment")
        attempt("pay-order", bc, f"/orders/{op}", "pay-order", {}, lambda: len(H.pay_api.sessions_for(op)) > 0)
        attempt("cancel-backorder", bc, f"/orders/{of}", "cancel-backorder", {"units": "1"}, lambda: H.order(key, of)["lines"][1]["cancelled"] != 0)
        links0 = bc.get(f"/orders/{osh}", follow=True).text.count("/rmas/")
        attempt("request-rma", bc, f"/orders/{osh}", "request-rma", {"units": "1", "reason": "x"},
                lambda: bc.get(f"/orders/{osh}", follow=True).text.count("/rmas/") != links0)
        attempt("receive-rma", H.sup, f"/rmas/{r_req}", "receive-rma", {}, lambda: H.rma_view(r_req)["rma-status"] != "requested")
        attempt("inspect-rma", H.sup, f"/rmas/{r_q}", "inspect-rma", {"outcome": "damaged"}, lambda: H.rma_view(r_q)["rma-status"] != "quarantined")
        n_sku = lambda: H.q("SELECT count(*) AS n FROM skus")[0]["n"]  # noqa: E731
        k0 = n_sku()
        attempt("create-sku", H.sup, "/skus/new", "create-sku", {"code": "CSRF" + H.nonce, "name": "x", "case_pack": "1", "unit_weight_g": "1",
                "length_cm": "1", "width_cm": "1", "height_cm": "1", "storage": "ambient", "taxable": "false", "reorder_point": "0"}, lambda: n_sku() != k0)
        v0 = H.lot_view(lot)["lot-version"]
        l = H.world.lots[lot]
        attempt("receive", H.sup, "/receipts/new", "receive", {"sku": s1, "lot_code": l.code, "expiry_date": l.expiry.isoformat(), "bin": l.bin,
                "units": "5", "unit_cost": "1.2500"}, lambda: H.lot_view(lot)["lot-version"] != v0)
        attempt("count-lot", H.sup, f"/lots/{lot}", "count-lot", {"counted_units": "1", "version": v0}, lambda: H.lot_view(lot)["lot-version"] != v0)
        n_price = lambda: H.q("SELECT count(*) AS n FROM price_list")[0]["n"]  # noqa: E731
        p0 = n_price()
        attempt("create-price", H.fin, "/prices", "create-price", {"tier": "gold", "sku": s1, "unit_price_cents": "5",
                "effective_from": D(-3).isoformat()}, lambda: n_price() != p0)
        n_cust = lambda: H.q("SELECT count(*) AS n FROM customers")[0]["n"]  # noqa: E731
        c0 = n_cust()
        attempt("create-customer", H.fin, "/customers/new", "create-customer", {"name": "CSRF Deli", "tier": "gold", "ship_to_state": "NY",
                "approval_limit_cents": "", "min_shelf_life_days": "0", "allow_split_cases": "true", "backorders_allowed": "true"}, lambda: n_cust() != c0)
        lim = lambda: H.q("SELECT approval_limit_cents FROM customers WHERE id = %s", (cust.cid,))[0]["approval_limit_cents"]  # noqa: E731
        attempt("save-customer", H.fin, f"/customers/{cust.cid}", "save-customer", customer_fields(cust, approval_limit_cents="1"), lambda: lim() != 5000)
        n_user = lambda: H.q("SELECT count(*) AS n FROM users")[0]["n"]  # noqa: E731
        u0 = n_user()
        attempt("create-user", H.fin, "/users/new", "create-user", {"email": f"csrf.{H.nonce}@verify.example", "full_name": "X", "role": "finance",
                "customer_id": "", "zone": ""}, lambda: n_user() != u0)
        vid = H.user_ids[victim]
        attempt("deactivate-user", H.fin, f"/users/{vid}", "deactivate-user", {}, lambda: not H.q("SELECT active FROM users WHERE id = %s", (vid,))[0]["active"])
        n_waves = lambda: H.sup.get("/waves", follow=True).text.count("/waves/")  # noqa: E731
        w0 = n_waves()
        attempt("create-wave", H.sup, "/waves", "create-wave", {"zone": "A", "ship_date": S.isoformat(), "max_tasks": "12"}, lambda: n_waves() != w0)
        task_status = lambda: {t["task_id"]: t["status"] for s_ in H.wave_view(H.wave_page(wid))["stops"] for t in s_["tasks"]}.get(open_task.app_id)  # noqa: E731
        lt = H.world.lots[open_task.lot]
        try:
            page = H.wave_page(wid)
            node = page.dom.attr_one("data-task-id", str(open_task.app_id))
            form = node.find_all(lambda n: n.tag == "form")[0] if node else None
            if form is None:
                fails.append("confirm-pick: no form on the open task")
            else:
                csrf_names = {i.attrs.get("name") for i in form.find_all(lambda n: n.tag == "input" and "data-csrf" in n.attrs)}
                fields = [(k, v) for k, v in form_fields(form) if k not in csrf_names and k not in ("bin_code", "lot_code", "units")]
                r = H.sup.post_form(urllib.parse.urljoin(page.url, form.attrs.get("action") or ""), fields + [("bin_code", lt.bin), ("lot_code", lt.code), ("units", str(open_task.units))])
                if task_status() != "open":
                    ctx.breach("X2", "confirm-pick without the session token took effect")
                    fails.append("confirm-pick took effect without the token")
                elif r.status != 403:
                    fails.append(f"confirm-pick without the token answered {r.status}")
        except Exception as e:  # noqa: BLE001
            fails.append(f"confirm-pick: {e}")
        attempt("ship-order", H.sup, f"/orders/{osh2}", "ship-order", {}, lambda: len(H.order(key, osh2)["shipments"]) != 0)
        n_keys = lambda: len(bc.get("/account/api-keys", follow=True).dom.attr_all("data-api-key-id"))  # noqa: E731
        kk0 = n_keys()
        attempt("create-api-key", bc, "/account/api-keys", "create-api-key", {"label": "csrf"}, lambda: n_keys() != kk0)
        attempt("revoke-api-key", bc, "/account/api-keys", "revoke-api-key", {}, lambda: H.api(key, "GET", f"/api/v1/orders/{op}").status != 200)
        attempt("save-profile", bc, "/profile", "save-profile", {"full_name": "Token Less"},
                lambda: "Token Less" in [r["full_name"] for r in H.q("SELECT full_name FROM users WHERE customer_id = %s", (cust.cid,))])
        attempt("signout", bc, "/", "signout", {}, lambda: not H.signed_in(bc))
        ctx.check(not fails, f"{len(fails)} forms: " + " | ".join(fails[:8]))


# ======================================================================================== W23
SHORT_XSS = "<svg/onload=__fk_xss('{t}')>"


@workflow("W23", "In a real browser: layout at 1280 and 390 px, typed text never runs as script, paying through the hosted page", 3, timeout=1500)
def w23(ctx):
    S = D(250)
    br = None
    try:
        with ctx.step("records carrying script payloads in every typed field"):
            cust = H.create_customer(tier="standard", state="NY", split=True, backorders=True, name=B.payload("custname", 0))
            bmail = H.create_user("buyer", cust.cid, name=B.payload("buyername", 1))
            amail = H.create_user("approver", cust.cid, name="Avery Approver")
            bc = H.account(bmail)
            key = H.create_key(bc, B.payload("keylabel", 2)[:80])
            sku = H.create_sku(1, 100, (10, 10, 10), "ambient", True, name=B.payload("skuname", 3))
            nos = H.create_sku(1, 100, (10, 10, 10), "ambient", True)
            H.price_all(sku, 1250, D(-5))
            H.price_all(nos, 990, D(-5))
            lot = H.receive(sku, D(300), "A03-20-4", 40, lot_code=SHORT_XSS.format(t="lotcode"))
            r = H.api_order(key, S, [(sku, 6), (nos, 4)], po=SHORT_XSS.format(t="po"), notes=B.payload("notes", 0))
            ctx.check(r.status == 201, f"order with markup answered {r.status}: {r.text[:200]}")
            oid = r.json()["id"]
            H.world.add_order(oid, cust, S, [(sku, 6), (nos, 4)])
            H.pay(bc, key, oid)
            wid, view, chosen = H.wave_loose("A", S, 12)  # evidence fixture: W09 judges routes, not this step
            H.pick_view(view)
            r, sid = H.ship(oid)
            H.world.ship(oid)
            r, rid = H.request_rma(bc, oid, 1, 1, reason=B.payload("reason", 1))
            ctx.check(sid and rid, "verifier fixture: shipment or return missing")
            ctx.shared["w23"] = (cust, bmail, amail, key, sku, nos, lot, oid, wid, sid, rid)
        with ctx.step("signing in through the browser form reaches a signed-in page"):
            br = B.Browser()
            cust, bmail, amail, key, sku, nos, lot, oid, wid, sid, rid = ctx.shared["w23"]
            pages = {}
            for who, email in (("buyer", bmail), ("sup", SUP), ("fin", FIN)):
                cx = br.context()
                p = br.page(cx)
                p.goto(H.base + "/signin")
                p.fill("input[name=email]", email)
                p.fill("input[name=password]", PASSWORD)
                p.click("[data-action=signin]")
                p.wait_for_load_state()
                ctx.check(p.locator("[data-action=signout]").count() >= 1, f"no sign-out button after the {who}'s browser sign-in")
                pages[who] = p
            ctx.shared["w23_pages"] = pages
        cust, bmail, amail, key, sku, nos, lot, oid, wid, sid, rid = ctx.shared["w23"]
        pages = ctx.shared["w23_pages"]
        # Judge evidence first: a failure of the strict browser checks below costs W23 alone and never hides the
        # pages the experience review grades. A failure here still fails W23, after those checks have run.
        evidence_failed = None
        try:
            with ctx.step("evidence pages for the experience review"):
                # Each page is captured on its own: one that fails is recorded and the rest are still captured.
                problems: list[str] = []

                @contextmanager
                def piece(name: str):
                    try:
                        yield
                    except InfraError:
                        raise
                    except Exception as e:  # noqa: BLE001 - recorded, raised after the last capture
                        problems.append(f"{name}: {type(e).__name__}: {e}"[:400])

                p, sp = pages["buyer"], pages["sup"]
                bo = None
                with piece("order_backorder"):
                    bo = H.place(key, cust, S, [(nos, 3), (sku, 30)])
                    H.pay(H.account(bmail), key, bo)
                    p.goto(H.base + f"/orders/{bo}")
                    p.wait_for_load_state()
                    B.capture(p, "order_backorder", {"order": bo, "lines": H.world.order_view(bo)["lines"]},
                              focus=["[data-action=cancel-backorder]"])
                with piece("order_refund"):
                    ln = next(l for l in H.world.orders[bo].lines if l.backordered)
                    # Hold refund.created while the page is captured, so the refund is still pending (R5.9) then.
                    H.pay_api.hold(["refund.created"])
                    try:
                        p.goto(H.base + f"/orders/{bo}")
                        p.wait_for_load_state()
                        p.fill("form:has([data-action=cancel-backorder]) input[name=units]", str(ln.backordered))
                        p.click("[data-action=cancel-backorder]")
                        p.wait_for_load_state()
                        H.world.cancel_backorder(bo, ln.no, ln.backordered)
                        p.goto(H.base + f"/orders/{bo}")
                        p.wait_for_load_state()
                        B.capture(p, "order_refund", {"refunds": [{"amount_cents": a, "status": "pending"} for a in H.world.orders[bo].refunds],
                                                      "why_pending": "the provider's refund.created event had not been delivered when this page was captured"},
                                  focus=["[data-line-no]"])
                    finally:
                        H.pay_api.release()
                with piece("approve_before"):
                    try:
                        r = submit_form(H.fin, f"/customers/{cust.cid}", "save-customer", customer_fields(cust, approval_limit_cents="1000"),
                                        follow=False)
                        ctx.check(r.status < 400, f"saving the customer answered {r.status}")
                        cust.limit = 1000
                        ao = H.place(key, cust, S, [(nos, 3)])
                        acx = br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in H.account(amail).cookies.items()])
                        ap = br.page(acx)
                        ap.goto(H.base + f"/orders/{ao}")
                        ap.wait_for_load_state()
                        B.capture(ap, "approve_before", {"total_cents": H.world.orders[ao].total, "approval_limit_cents": 1000},
                                  focus=["[data-action=approve-order]"])
                    finally:  # the checks after this step expect the company without a limit again
                        cust.limit = None
                        try:
                            submit_form(H.fin, f"/customers/{cust.cid}", "save-customer", customer_fields(cust, approval_limit_cents=""),
                                        follow=False)
                        except Exception:  # noqa: BLE001 - a missing form is already recorded above
                            pass
                with piece("wave_page"):
                    sp.goto(H.base + f"/waves/{wid}")
                    sp.wait_for_load_state()
                    _, dist, route = H.world.route_of(wid)
                    B.capture(sp, "wave_page", {"route": route, "route_distance": dist}, focus=["[data-stop-seq='1']"])
                with piece("pick_refused"):
                    po = H.place(key, cust, S + timedelta(days=1), [(sku, 2)])
                    H.pay(H.account(bmail), key, po)
                    w2, v2, _ = H.wave_loose("A", S + timedelta(days=1), 12)
                    t2 = next(t for s_ in v2["stops"] for t in s_["tasks"] if t["status"] == "open")
                    l2 = H.world.lots[t2["lot_id"]]
                    sp.goto(H.base + f"/waves/{w2}")
                    sp.wait_for_load_state()
                    sel = f"[data-task-id='{t2['task_id']}']"
                    sp.fill(sel + " input[name=bin_code]", "A01-01-1")
                    sp.fill(sel + " input[name=lot_code]", l2.code)
                    sp.click(sel + " [data-action=confirm-pick]")
                    sp.wait_for_load_state()
                    B.capture(sp, "pick_refused", {"refused": True, "reason": f"wrong bin: the pick is in {l2.bin}"})
                with piece("count_refused"):
                    sp.goto(H.base + f"/lots/{lot}")
                    sp.wait_for_load_state()
                    seen = sp.locator("[data-lot-version]").first.get_attribute("data-lot-version")
                    r = H.count(lot, H.world.lots[lot].on_hand - H.world.reserved(lot, ("picked",)))
                    if r.status < 400:
                        H.world.count(lot, H.world.lots[lot].on_hand - H.world.reserved(lot, ("picked",)))
                    # The supervisor submits the version they saw (R7.7): a form that does not pre-fill it gets it typed in.
                    sp.evaluate("(v) => { const b = document.querySelector('[data-action=count-lot]'); const f = b && b.closest('form');"
                                " const i = f && f.querySelector('input[name=version]'); if (i && !i.value) i.value = v; }", seen)
                    sp.fill("input[name=counted_units]", "5")
                    sp.click("[data-action=count-lot]")
                    sp.wait_for_load_state()
                    B.capture(sp, "count_refused", {"refused": True, "reason": "the lot was counted by someone else after this page loaded"})
                with piece("invoice"):
                    sp.goto(H.base + f"/shipments/{sid}")
                    sp.wait_for_load_state()
                    B.capture(sp, "invoice", {"invoice": H.world.orders[oid].shipments[0]}, focus=["[data-invoice-total-cents]"])
                ctx.check(not problems, f"{len(problems)} evidence pages failed: " + " | ".join(problems))
        except StepFailed as e:
            evidence_failed = e
        views = [("buyer", f"/orders/{oid}"), ("buyer", "/orders"), ("buyer", "/orders/new"), ("buyer", "/account/api-keys"),
                 ("buyer", f"/rmas/{rid}"), ("buyer", f"/shipments/{sid}"), ("sup", f"/waves/{wid}"), ("sup", f"/lots/{lot}"),
                 ("sup", f"/orders/{oid}"), ("fin", f"/customers/{cust.cid}"), ("fin", "/orders")]
        with ctx.step("every page stays within 1280 and 390 CSS pixels without horizontal scrolling"):
            for who, u in views:
                p = pages[who]
                for w in (1280, 390):
                    p.set_viewport_size({"width": w, "height": 900})
                    p.goto(H.base + u)
                    p.wait_for_load_state()
                    ov = B.horizontal_overflow(p)
                    ctx.check(ov <= 2, f"{u} overflows by {ov}px at {w}px")
            for p in pages.values():
                p.set_viewport_size({"width": 1280, "height": 900})
        with ctx.step("names, codes, PO numbers, notes, reasons and labels are shown as text everywhere, error pages included"):
            q = urllib.parse.quote
            reflected = [("buyer", "/orders/" + q(B.payload("path", 0))), ("buyer", "/signin?next=" + q(B.payload("next", 3))),
                         ("fin", "/prices?sku=" + q(B.payload("query", 2))), ("sup", "/waves?x=" + q(B.payload("query", 1))),
                         ("buyer", "/password-reset/" + q(B.payload("token", 0))), ("buyer", "/" + q(B.payload("root", 1))),
                         ("fin", f"/skus/{q(B.payload('id', 2))}"), ("sup", f"/skus/{H.world.skus[sku].sku_id}"),
                         ("fin", "/users/" + str(H.user_ids[bmail]))]
            for who, u in views + reflected:
                pages[who].goto(H.base + u)
                pages[who].wait_for_load_state()
                pages[who].wait_for_timeout(250)
            if br.xss_hits:
                ctx.breach("X3", f"typed text ran as script: {sorted(set(br.xss_hits))[:5]}")
            ctx.check(not br.xss_hits, f"script ran from typed text: {sorted(set(br.xss_hits))[:5]}")
        with ctx.step("the buyer pays by clicking through the hosted page and returns to an order awaiting confirmation, then paid"):
            p = pages["buyer"]
            oid2 = H.place(key, cust, S, [(nos, 2)])
            p.goto(H.base + f"/orders/{oid2}")
            p.wait_for_load_state()
            p.click("[data-action=pay-order]")
            p.wait_for_load_state()
            ctx.check(p.url.startswith(H.pay_api.api), f"the pay button led to {p.url}")
            p.fill("#card_number", CARD_OK)
            p.click("#pay")
            p.wait_for_load_state()
            ctx.check(p.url.startswith(f"{H.base}/orders/{oid2}"), f"the provider returned to {p.url}")
            end, st = time.monotonic() + 30, None
            while time.monotonic() < end:
                p.goto(H.base + f"/orders/{oid2}")
                st = p.locator("[data-order-status]").first.get_attribute("data-order-status")
                if st == "in_fulfilment":
                    break
                time.sleep(1)
            ctx.eq(st, "in_fulfilment", "status shown after paying in the browser")
            H.world.pay(oid2)
        if evidence_failed is not None:
            raise evidence_failed
    finally:
        B.mark_unreached(["order_backorder", "order_refund", "approve_before", "wave_page", "pick_refused", "count_refused", "invoice",
                          "shipment_email"])
        if br:
            br.close()


# ======================================================================================== W24
@workflow("W24", "Deployment: README, conservation, restart persistence, a pending refund survives a restart, nothing re-sent, no secrets at rest, records kept", 3, timeout=900)
def w24(ctx):
    with ctx.step("README.md and an executable start.sh are in the workspace"):
        ws = Path(H.workspace)
        ctx.check((ws / "README.md").is_file() and (ws / "README.md").stat().st_size > 40, "README.md missing")
        ctx.check(os.access(ws / "start.sh", os.X_OK), "start.sh is not executable")
    with ctx.step("for every SKU the verifier touched, each lot's ledger adds up to its on-hand and nothing is negative"):
        r, rows = H.csv(H.sup, "/reports/on-hand.csv")
        onhand = {row[0]: int(row[5]) for row in rows[1:] if row and row[0].isdigit()}
        probs = []
        for code in sorted(H.world.touched_skus):
            r, led = H.csv(H.sup, "/reports/ledger.csv?sku=" + urllib.parse.quote(code))
            sums: dict[str, int] = {}
            for row in led[1:]:
                if row:
                    sums[row[0]] = sums.get(row[0], 0) + int(row[2])
            for lid in H.world.lots_of.get(code, []):
                s_ = sums.get(str(lid), 0)
                if s_ < 0:
                    ctx.breach("X13", f"lot {lid} has negative stock ({s_})")
                if s_ != onhand.get(str(lid), 0):
                    probs.append(f"lot {lid}: ledger sum {s_}, on-hand report {onhand.get(str(lid), 0)}")
        mismatch(ctx, probs, "lots")
    with ctx.step("a refund whose provider call hangs when the application restarts is completed afterwards, exactly once"):
        cust, c, key, email = H.company(tier="standard", state="NY", split=True, backorders=True)
        z = H.create_sku(1, 100, (10, 10, 10), "ambient", True)
        H.price_all(z, 1999, D(-5))
        oid = H.place(key, cust, D(260), [(z, 5)])
        H.pay(c, key, oid)
        before_mail = H.mail_ids(email)
        H.pay_api.fault(refund_create="timeout", refund_create_delay=20)
        r = submit_form(c, f"/orders/{oid}", "cancel-backorder", {"units": "5"}, follow=False)
        ctx.check(r.status < 400, f"cancelling answered {r.status}")
        H.world.cancel_backorder(oid, 1, 5)
        time.sleep(3)
        ctx.check(H.restart_app(), "the application did not answer /healthz after a restart")
        H.wait_refunds(key, oid, 120)
        time.sleep(3)
        st = H.pay_api.state()
        pi = next(s["payment_intent"] for s in H.pay_api.sessions_for(oid, st) if s.get("payment_intent"))
        prov = H.pay_api.refunds_for(pi, st)
        if len(prov) > 1:
            ctx.breach("X6", "a refund was made twice across a restart")
        ctx.eq([x["amount"] for x in prov], H.world.orders[oid].refunds, "provider refunds")
        got = [m for m in H.mail.to(email) if m["id"] not in before_mail and "refund issued" in m["subject"].lower()]
        ctx.eq(len(got), 1, "refund emails")
        ctx.shared["w24"] = (key, oid)
    with ctx.step("after the restart: passwords, keys, orders and stock are kept and no email is sent again"):
        H.clients.clear()
        c, _ = H.signin(FIN)
        ctx.check(H.signed_in(c), "the finance password did not survive the restart")
        key, oid = ctx.shared["w24"]
        mismatch(ctx, H.compare(key, oid), "order fields after the restart")
        time.sleep(8)
        allowed = {}
        for o in H.world.orders.values():
            n_ = f"PP-{o.oid:06d}"
            allowed.update({("shipped", n_): len(o.shipments), ("credit note", n_): len(o.credits), ("refund issued", n_): len(o.refunds)})
        seen: dict = {}
        for m in H.mail.summaries():
            sub = m["subject"].lower()
            hits = [(sub.find(k), k) for k in ("order confirmed", "shipped", "refund issued", "credit note", "approval needed", "low stock") if k in sub]
            kind = min(hits)[1] if hits else None
            if kind:
                num_ = (re.search(r"PP-\d{6}", m["subject"]) or [None])[0]
                for t in m["to"]:
                    k_ = (t, kind, num_ or m["subject"])
                    seen[k_] = seen.get(k_, 0) + 1
        dups = {k: v for k, v in seen.items() if v > max(1, allowed.get((k[1], k[2]), 1))}
        if dups:
            ctx.breach("X6", f"{len(dups)} emails were sent more than once: {list(dups)[:3]}")
        ctx.eq(len(dups), 0, "emails sent more than once")
    with ctx.step("no password, API key, reset token or session identifier is stored in plain text"):
        # Session identifiers are the values of the session cookie(s) only (R2.5); API keys are the full key and its
        # secret part (a public lookup id may be stored); reset tokens are every link's token (R2.6).
        names = H.session_names
        if names is None:
            c_, r_ = H.signin(FIN)
            names = H.session_cookie_names(c_, list(H.set_cookies(r_))) if H.signed_in(c_) else {n for n, _ in H.session_values}
        needles = [PASSWORD] + [n for k in H.api_keys for n in key_needles(k)]
        needles += [t for t in H.reset_tokens if len(t) >= 16][-6:] + [v for n, v in H.session_values if n in names and len(v) >= 16][-6:]
        hits = []
        for t in H.q("SELECT table_schema, table_name, column_name FROM information_schema.columns WHERE table_schema NOT IN "
                     "('pg_catalog','information_schema') AND data_type IN ('text','character varying','jsonb','json','bytea','character')"):
            col = f'"{t["table_schema"]}"."{t["table_name"]}"'
            for s_ in needles:
                if H.q(f'SELECT 1 FROM {col} WHERE position(%s in "{t["column_name"]}"::text) > 0 LIMIT 1', (s_,)):
                    hits.append(f"{t['table_name']}.{t['column_name']}")
        if hits:
            ctx.breach("X8", f"plain-text secret in {sorted(set(hits))}")
        ctx.check(not hits, f"plain-text secret stored in {sorted(set(hits))}")
    with ctx.step("the old system's records are all still there, unchanged"):
        b = H.baseline
        changed = []
        checks = [("skus", "SELECT id, code, name, case_pack, unit_weight_g, length_cm, width_cm, height_cm, storage, taxable, reorder_point, active FROM skus"),
                  ("lots", "SELECT id, sku_id, lot_code, expiry_date::text AS expiry_date, bin_id, unit_cost::text AS unit_cost, qa_hold FROM lots"),
                  ("price_list", "SELECT id, tier, sku_id, unit_price_cents, effective_from::text AS effective_from FROM price_list"),
                  ("customers", "SELECT id, name, tier, ship_to_state, approval_limit_cents, min_shelf_life_days, allow_split_cases, backorders_allowed FROM customers"),
                  ("bins", "SELECT id, code, zone, aisle, bay, level FROM bins")]
        for name, sql in checks:
            cur = {r["id"]: r for r in H.q(sql)}
            for r in b[name]:
                now = cur.get(r["id"])
                if now is None or any(str(now[k]) != str(r[k]) for k in r):
                    changed.append(f"{name} {r['id']}")
        cur = {r["id"]: r for r in H.q("SELECT id, email, role, customer_id, zone, active FROM users")}
        for r in b["users"]:
            now = cur.get(r["id"])
            if now is None or any(str(now[k]) != str(r[k]) for k in ("email", "role", "customer_id", "zone", "active")):
                changed.append(f"users {r['id']}")
        n = H.q("SELECT count(*) AS n, coalesce(sum(hashtext(id::text||'/'||lot_id::text||'/'||kind||'/'||units::text||'/'||local_time::text||'/'||fold::text)),0) AS h FROM legacy_movements")[0]
        if n["n"] != len(b["legacy_movements"]) or int(n["h"]) != int(b["legacy_hashes"]["mv"]):
            changed.append("legacy_movements")
        o_ = H.q("SELECT coalesce(sum(hashtext(id::text||'/'||number||'/'||customer_id::text||'/'||placed_local::text||'/'||fold::text||'/'||coalesce(po_number,'')||'/'||status||'/'||total_cents::text)),0) AS h FROM legacy_orders")[0]
        if int(o_["h"]) != int(b["legacy_hashes"]["orders"]):
            changed.append("legacy_orders")
        lo = H.q("SELECT count(*) AS n FROM legacy_orders")[0]["n"]
        ll = H.q("SELECT count(*) AS n, coalesce(sum(hashtext(order_id::text||'/'||line_no::text||'/'||sku_id::text||'/'||qty_units::text||'/'||unit_price_cents::text)), 0) AS h FROM legacy_order_lines")[0]
        if lo != len(b["legacy_orders"]) or ll["n"] != b["legacy_lines_hash"]["n"] or int(ll["h"]) != int(b["legacy_lines_hash"]["h"]):
            changed.append("legacy_orders/legacy_order_lines")
        if changed:
            ctx.breach("X10", f"pre-existing records changed: {changed[:8]}")
        ctx.check(not changed, f"pre-existing records changed: {changed[:8]}")


# ======================================================================================== W25
@workflow("W25", "Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start", 2, timeout=400)
def w25(ctx):
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


# ======================================================================================== main
def main() -> None:
    global H
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    H = Harness()
    shared: dict = {}
    day0 = H.today()
    run_all(a.out, shared, set(x for x in a.only.split(",") if x) or None)
    if H.today() != day0:
        raise InfraError(f"run crossed midnight {H.tz.key} ({day0} -> {H.today()}): dates the checks computed moved")


if __name__ == "__main__":
    main_guard(main)
