#!/usr/bin/env python3
"""Lotbook workflows. Each workflow is strict: it passes only when every substep passes.

Run: python3 workflows.py --out /logs/verifier/results.json

Ordering and isolation: every rule workflow (W13-W20) creates its own accounts and securities (symbols unique
to this run) in the Raman-Okafor household, so its rows in the household's reports are its own. The legacy
workflows W09-W12 compare the legacy securities of households 1 and 2; they run right after the import
workflows and before W22 locks 2024 for household 2. W21 owns the Castellano household (no legacy data), where
it locks 2024 and 2025. The corporate-action workflows W28, W29, W31 and W30 (in that order: the critical ones
first) also use their own accounts and securities in the Raman-Okafor household.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction as F
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import H1, H2, H3, PASSWORD, PEOPLE, REDIRECTS, Harness, d, diff_rows  # noqa: E402
from kitlib import browser as B  # noqa: E402
from kitlib.conc import simultaneously  # noqa: E402
from kitlib.web import Client, FormNotFound, csrf_from, find_form, form_fields, submit_form, visible_text  # noqa: E402
from kitlib.wf import StepFailed, main_guard, run_all, workflow  # noqa: E402
from reference import engine as E  # noqa: E402
from reference import model as M  # noqa: E402
from reference import statements as ST  # noqa: E402

H: Harness = None  # type: ignore[assignment]
EVIL = "evil.example"


def norm_text(t: str, *remove: str) -> str:
    for r in remove:
        if r:
            t = t.replace(r, "").replace(r.lower(), "").replace(r.upper(), "")
    return re.sub(r"\s+", " ", t).strip().lower()


def tokens(c: Client, path: str = "/profile") -> list[tuple[str, str]]:
    return csrf_from(c.get(path, follow=True))


def money_of(cents: int) -> str:
    return M.money(cents)


class Book:
    """One workflow's own accounts and securities in a household, and the statement rows it imports."""

    def __init__(self, wid: str, hid: int = H1, who: str = "priya"):
        self.wid, self.hid, self.who = wid, hid, who
        self.c = H.who(who)
        self.secs: list[dict] = []
        self.accts: dict[int, str] = {}
        self.rows: dict[int, list[dict]] = {}
        self.n = 0

    @property
    def sids(self) -> set:
        return {s["sid"] for s in self.secs}

    def account(self, broker: str, method: str = "FIFO", name: str | None = None, kind: str = "taxable") -> int:
        k = len(self.accts) + 1
        number = (f"NW-{self.wid}-{H.nonce}-{k}" if broker == "northwind" else f"HD {self.wid} {H.nonce} {k:02d}")
        aid = H.create_account(self.c, self.hid, name or f"{self.wid} {broker} {method} {k}", broker, number, method, kind)
        self.accts[aid] = broker
        self.rows[aid] = []
        return aid

    def security(self, cur: str, name: str | None = None) -> dict:
        self.n += 1
        sym = f"{self.wid}{H.nonce}{self.n:02d}" if self.n < 100 else f"{self.wid}{H.nonce}{self.n:03d}"
        isin = "XS" + f"{self.wid[1:]}{H.nonce}{self.n:03d}"[:9].ljust(9, "0") + str(self.n % 10)
        nm = name or f"{self.wid} Holdings {self.n} {cur}"
        sid = H.create_security(self.c, self.hid, sym, isin, nm, cur)
        s = {"sid": sid, "symbol": sym, "isin": isin, "name": nm, "currency": cur}
        self.secs.append(s)
        return s

    def row(self, aid: int, sec: dict, kind: str, day: date, qty=None, amount: int | None = None, ratio=None, desc=None) -> dict:
        r = {"kind": kind, "day": day, "symbol": sec["symbol"], "isin": sec["isin"], "name": sec["name"], "currency": sec["currency"],
             "quantity": F(qty) if qty is not None else None, "amount": amount, "ratio": ratio, "ref": H.ref(self.wid)}
        if desc:
            r["description"] = desc
        self.rows[aid].append(r)
        return r

    def buy(self, aid, sec, day, qty, price, tie: bool = False, kind: str = "buy") -> dict:
        amt = E.rhu(F(qty) * F(price) * 100)
        if tie:
            amt = tie_amount(amt, sec["currency"], day)
        return self.row(aid, sec, kind, day, qty, amt)

    def sell(self, aid, sec, day, qty, price, tie: bool = False) -> dict:
        amt = E.rhu(F(qty) * F(price) * 100)
        if tie:
            amt = tie_amount(amt, sec["currency"], day)
        return self.row(aid, sec, "sell", day, qty, amt)

    def div(self, aid, sec, day, amount_cents: int) -> dict:
        return self.row(aid, sec, "dividend", day, None, amount_cents)

    def split(self, aid, sec, day, ratio) -> dict:
        return self.row(aid, sec, "split", day, None, None, ratio)

    def flush(self, aids=None) -> None:
        for aid in (aids or list(self.rows)):
            rows = sorted(self.rows[aid], key=lambda r: (r["day"], E.KIND_RANK[r["kind"]]))
            if rows:
                H.import_rows(self.c, aid, rows, tag=f"{self.wid.lower()}-{aid}", rnd=random.Random(aid))
            self.rows[aid] = []

    def compare(self, years=(2025, 2026), lots: bool = True, summary: bool = True) -> list[str]:
        problems = []
        for y in years:
            problems += H.compare_realized(self.c, self.hid, y, self.sids)
            if summary:
                problems += H.compare_summary(self.c, self.hid, y, self.sids)
        if lots:
            problems += H.compare_lots(self.c, self.hid, list(self.accts), self.sids)
        return problems


def tie_amount(cents: int, cur: str, day: date) -> int:
    """Nudge an amount so that its USD value is exactly half a cent (R5.2), when the day's rate allows."""
    if cur == "USD":
        return cents if cents % 2 else cents + 1
    r = H.rates.rate(cur, day)
    for k in range(400):
        for c in (cents + k, cents - k):
            if c > 0 and (F(c) * r).denominator == 2:
                return c
    return cents


def missing_rate_days(cur: str, lo: date, hi: date, n: int, rnd: random.Random) -> list[date]:
    """Days without an fx_rates row (weekends, holidays, gaps) between lo and hi."""
    out, dd = [], lo
    while dd <= hi:
        if (cur, dd) not in H.fx_days:
            out.append(dd)
        dd += timedelta(days=1)
    rnd.shuffle(out)
    return sorted(out[:n])


PAGE_TIMEOUT_MS = 70000  # above the 60 s every page may take (R9.6)
# Workflow alarms allow >= 3x the oracle's request count at 2.5 s per request (W13-W20 make 206-283 requests,
# W28-W31 120-424). W31 (critical, ~120 requests) runs before W30 (not critical, ~316), so a run short of time
# loses the non-critical one first.


def bpage(br, cx):
    """A browser page whose waits allow for the 60 s a page may take."""
    p = br.page(cx)
    p.set_default_timeout(PAGE_TIMEOUT_MS)
    p.set_default_navigation_timeout(PAGE_TIMEOUT_MS)
    return p


def form_of(page, action: str):
    """The form that holds the data-action button (pages may carry other forms with the same field names)."""
    f = page.locator("form", has=page.locator(f"[data-action={action}]")).first
    f.wait_for(state="attached")
    return f


def try_capture(page, path: str, checkpoint: str, truth: dict, focus=None) -> None:
    """Judge evidence, captured once and never failing the workflow (shared rule 12)."""
    try:
        if (B.UX_DIR / checkpoint / "truth.json").exists():
            return
        page.goto(H.base + path)
        page.wait_for_load_state()
        B.capture(page, checkpoint, truth, focus=focus)
    except Exception:  # noqa: BLE001 - evidence is best effort
        pass


def report(ctx, problems: list[str], what: str) -> None:
    ctx.check(not problems, f"{what}: " + " | ".join(problems[:10]))


def legacy_pick(hid: int, n: int = 1):
    return [s for s in H.hh[hid].securities.values()][:n]


# ======================================================================================== W01
@workflow("W01", "Password links: anti-enumeration, single use, replacement, expiry, address matching", 2)
def w01(ctx):
    with ctx.step("members and the advisor set passwords from emailed links and sign in"):
        for who in ("priya", "daniel", "morgan"):
            H.who(who)
        ctx.surface("signin")
    addr = PEOPLE["johan"]  # stored upper-cased in users
    with ctx.step("a known and an unknown address get the same status and message; the unknown one gets no email"):
        before = H.mail_ids(addr)
        rk = H.request_reset("  " + addr.upper() + " ")
        ghost = f"nobody.{H.nonce.lower()}@verify.example"
        ru = H.request_reset(ghost)
        ctx.eq(ru.status, rk.status, "status for an unknown address")
        tk, tu = norm_text(visible_text(rk.text), addr), norm_text(visible_text(ru.text), ghost)
        ctx.check(tk == tu, f"messages differ: known={tk[:200]!r} unknown={tu[:200]!r}")
        link = H.reset_link(addr, before)
        ctx.check(link is not None, "no reset email reached the address on file (matched ignoring case and spaces)")
        time.sleep(2)
        ctx.eq(len(H.mail.to(ghost)), 0, "emails to an unknown address")
        ctx.shared["pw_link1"] = link
    with ctx.step("an inactive user's address receives no email"):
        a = PEOPLE["ebba"]
        before = H.mail_ids(a)
        H.request_reset(a)
        time.sleep(4)
        ctx.eq(len(H.mail_ids(a) - before), 0, "emails to an inactive user")
    with ctx.step("a newer link replaces the older one; the older one sets nothing"):
        before = H.mail_ids(addr)
        H.request_reset(addr)
        link2 = H.reset_link(addr, before)
        ctx.check(link2 and link2 != ctx.shared["pw_link1"], "no second, different link")
        c = H.client()
        page = c.get(ctx.shared["pw_link1"], follow=True)
        if page.status == 200:
            try:
                submit_form(c, "", "set-password", {"password": "Old-Link-Pass-1"}, follow=True, page=page)
            except FormNotFound:
                pass
        c2, _ = H.signin(addr, "Old-Link-Pass-1")
        ctx.check(not H.signed_in(c2), "the replaced link set a working password")
        c = H.client()
        page = c.get(link2, follow=True)
        ctx.eq(page.status, 200, "newest link page status")
        submit_form(c, "", "set-password", {"password": PASSWORD}, follow=True, page=page)
        c3, _ = H.signin(addr)
        ctx.check(H.signed_in(c3), "the newest link did not set a working password")
        H.sessions["johan"] = c3
        ctx.shared["pw_link2"] = link2
    with ctx.step("a used link changes nothing"):
        c = H.client()
        page = c.get(ctx.shared["pw_link2"], follow=True)
        if page.status == 200:
            try:
                submit_form(c, "", "set-password", {"password": "Second-Use-Pass-1"}, follow=True, page=page)
            except FormNotFound:
                pass
        c4, _ = H.signin(addr, "Second-Use-Pass-1")
        ctx.check(not H.signed_in(c4), "a used link set a password a second time")
    with ctx.step("an expired link changes nothing"):
        a2 = PEOPLE["astrid"]
        before = H.mail_ids(a2)
        H.request_reset(a2)
        link3 = H.reset_link(a2, before)
        ctx.check(link3, "no link for a second member")
        time.sleep(H.reset_ttl + 3)
        c = H.client()
        page = c.get(link3, follow=True)
        if page.status == 200:
            try:
                submit_form(c, "", "set-password", {"password": "Expired-Link-1"}, follow=True, page=page)
            except FormNotFound:
                pass
        c6, _ = H.signin(a2, "Expired-Link-1")
        ctx.check(not H.signed_in(c6), "an expired link set a password")
    with ctx.step("an unknown link answers 404; sign-in matches the address ignoring case and spaces"):
        ctx.eq(H.client().get("/password-reset/" + secrets.token_urlsafe(24), follow=False).status, 404, "unknown link status")
        c7, _ = H.signin("  " + PEOPLE["daniel"].upper() + " ")
        ctx.check(H.signed_in(c7), "sign-in with a differently cased, padded address failed")


# ======================================================================================== W02
@workflow("W02", "Sessions: refusals, rotation, sign-out, cookie attributes, safe next= redirects", 3)
def w02(ctx):
    addr = PEOPLE["daniel"]
    with ctx.step("a wrong password, an unknown address and an inactive user are refused alike, with no session"):
        H.who("daniel")
        c1, r1 = H.signin(addr, "Wrong-Password-1")
        ghost = f"ghost.{H.nonce.lower()}@verify.example"
        c2, r2 = H.signin(ghost, "Wrong-Password-1")
        c3, r3 = H.signin(PEOPLE["ebba"], "Wrong-Password-1")
        ctx.check(not any(H.signed_in(c) for c in (c1, c2, c3)), "a refused sign-in produced a session")
        texts = []
        for c, r, rm in ((c1, r1, addr), (c2, r2, ghost), (c3, r3, PEOPLE["ebba"])):
            p = r if r.status not in REDIRECTS else c.get(r.location, follow=True)
            texts.append(norm_text(visible_text(p.text), rm))
        ctx.check(len(set(texts)) == 1, f"refusal messages differ: {[t[:120] for t in texts]}")
    with ctx.step("signing in starts a new session identifier; one held before never becomes signed in"):
        c = H.client()
        c.get("/signin", follow=True)
        c.get("/password-reset", follow=True)
        planted = dict(c.cookies)
        _, r = H.signin(addr, client=c)
        ctx.check(H.signed_in(c), "sign-in failed")
        if planted:
            pre = H.client()
            pre.cookies = dict(planted)
            if H.signed_in(pre):
                ctx.breach("X9", "a session identifier held before sign-in became signed in")
                raise StepFailed("a session identifier held before sign-in became signed in")
        names = H.find_session_names(c)
        ctx.check(names, "no cookie set at sign-in is needed to stay signed in")
        set_cookies = [h for h in r.header_all("Set-Cookie") if h.split(";")[0].partition("=")[0].strip() in names
                       and h.split(";")[0].partition("=")[2]]
        ctx.check(set_cookies, "sign-in set no session cookie")
        ctx.check(all("httponly" in h.lower() for h in set_cookies), "a session cookie set at sign-in is not HttpOnly")
        ctx.check(all(re.search(r"samesite\s*=\s*(lax|strict)", h, re.I) for h in set_cookies),
                  f"a session cookie lacks SameSite=Lax or Strict: {set_cookies[:2]}")
        ctx.shared["sess_client"] = c
    with ctx.step("signing out ends the session on the server"):
        c = ctx.shared["sess_client"]
        copy = c.copy()
        submit_form(c, "/", "signout", {}, follow=True)
        ctx.check(not H.signed_in(c), "still signed in after sign-out")
        if H.signed_in(copy):
            ctx.breach("X9", "a session cookie copied before sign-out still signs in")
            raise StepFailed("a cookie copied before sign-out still works")
    with ctx.step("a visitor is sent to /signin with next=, and lands on the page after signing in"):
        target = "/households/1/reports/2025?view=1"
        r = H.client().get(target, follow=False)
        loc = r.location or ""
        ctx.check(r.status in REDIRECTS and "/signin" in loc, f"visitor answered {r.status} {loc}")
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)
        ctx.eq(q.get("next", [""])[0], target, "next= carried to /signin")
        c, r = H.signin(addr, next_=target)
        ctx.check(r.status in REDIRECTS and urllib.parse.urljoin(H.base + "/", r.location or "") == H.base + target,
                  f"after sign-in with next={target} the answer was {r.status} {r.location}")
    with ctx.step("next= never leaves the site (//host, /\\host, scheme:host, full URLs)"):
        for bad in ("//" + EVIL + "/x", "/\\" + EVIL + "/x", "https:" + EVIL, "https://" + EVIL + "/", "\\\\" + EVIL, "/\t/" + EVIL):
            c, r = H.signin(addr, next_=bad)
            loc = (r.location or "").strip()
            u = urllib.parse.urlsplit(urllib.parse.urljoin(H.base + "/signin", loc.replace("\\", "/")))
            off = loc.startswith("//") or loc.startswith("/\\") or loc.startswith("\\") or (u.netloc and u.netloc != urllib.parse.urlsplit(H.base).netloc) \
                or (re.match(r"^[a-z][a-z0-9+.-]*:", loc, re.I) and not loc.startswith(H.base))
            if r.status in REDIRECTS and off and EVIL in loc:
                ctx.breach("X11", f"sign-in redirected off-site to {loc!r} (next={bad!r})")
                raise StepFailed(f"open redirect to {loc!r}")
            ctx.check(r.status in REDIRECTS and not off, f"next={bad!r} answered {r.status} {loc!r}")


# ======================================================================================== W03
@workflow("W03", "Household isolation: pages, exports and form ids of other households", 3)
def w03(ctx):
    with ctx.step("the advisor sees the households listed for them, and gets 403 on the third"):
        adv = H.who("morgan")
        home = adv.get("/", follow=True)
        seen = {n.attrs.get("data-household-id") for n in home.dom.attr_all("data-household-id")}
        ctx.eq(seen, {"1", "2"}, "households on the advisor's home page")
        for path in ("/households/1", "/households/2/reports/2024/summary.csv", "/accounts/1/lots", "/households/1/unrealized.csv"):
            ctx.eq(adv.get(path).status, 200, f"advisor GET {path}")
        for path in ("/households/3", "/households/3/reports/2025/summary.csv", "/households/3/unrealized.csv"):
            r = adv.get(path)
            if r.status == 200 and ("symbol," in r.text or "data-account-id" in r.text):
                ctx.breach("X1", f"the advisor read an unassigned household: {path}")
            ctx.eq(r.status, 403, f"advisor GET {path}")
    with ctx.step("a member gets 403 for another household's pages, exports and accounts"):
        el = H.who("elena")
        for path in ("/households/1", "/households/1/reports/2025/realized.csv", "/households/2/unrealized.csv", "/accounts/1",
                     "/accounts/1/lots", "/accounts/6/transactions/new", "/households/1/securities", "/households/1/transfers"):
            r = el.get(path)
            if r.status == 200 and ("data-account-id" in r.text or "sale_date," in r.text or "data-lot-id" in r.text
                                    or "account,symbol" in r.text or "data-security-id" in r.text):
                ctx.breach("X1", f"a member read another household's data: {path}")
            ctx.eq(r.status, 403, f"Castellano member GET {path}")
        r = H.client().get("/households/1/unrealized.csv", follow=False)
        ctx.check(r.status in REDIRECTS and "/signin" in (r.location or ""), f"visitor answered {r.status}")
    with ctx.step("ids of another household in a member's forms are refused and change nothing"):
        pr = H.who("priya")
        el = H.who("elena")
        bk = Book("W03", H3, "elena")
        a3 = bk.account("northwind")
        s3 = bk.security("USD")
        tok = tokens(pr, "/households/1")
        h1sec = legacy_pick(H1)[0]
        n_tx = H.q("SELECT count(*) AS n FROM accounts")[0]["n"]
        probes = [
            (pr, "/households/1/transfers", {"from_account_id": "1", "to_account_id": str(a3), "security_id": str(h1sec.sid), "trade_date": "2026-03-02"}),
            (pr, "/accounts/1/transactions", {"kind": "buy", "trade_date": "2026-03-02", "security_id": str(s3["sid"]), "quantity": "1", "price": "5", "fee": "0"}),
            (pr, f"/accounts/{a3}/transactions", {"kind": "buy", "trade_date": "2026-03-02", "security_id": str(s3["sid"]), "quantity": "1", "price": "5", "fee": "0"}),
            (pr, f"/accounts/{a3}/method", {"lot_method": "HIFO", "effective_from": "2026-01-01"}),
            (pr, "/households/3/accounts", {"name": "Intruder", "broker": "harbor", "account_number": f"INT-{H.nonce}", "lot_method": "FIFO",
                                            "account_type": ""}),
        ]
        bad = []
        for c, path, fields in probes:
            r = c.post_form(path, tok + list(fields.items()))
            if r.status < 400:
                bad.append(f"{path} answered {r.status}")
        lots3 = H.lots(el, a3)
        moved = H.q("SELECT count(*) AS n FROM accounts WHERE household_id = 3 AND name = 'Intruder'")[0]["n"]
        ch3 = H.q("SELECT count(*) AS n FROM lot_method_changes WHERE account_id = %s", (a3,))[0]["n"]
        if lots3 or moved or ch3:
            ctx.breach("X1", f"a member changed another household's data (lots={len(lots3)}, accounts={moved}, changes={ch3})")
        ctx.check(not bad and not lots3 and not moved and not ch3, f"cross-household writes: {bad}")
        r, _h, rows = H.year_csv(pr, H1, 2026, "realized")
        ctx.check(not any(row[2:3] == [s3["symbol"]] for row in rows), "another household's security was recorded in account 1")


# ======================================================================================== W04
WRITE_ACTIONS = ("create-account", "create-security", "record-transfer", "import-statement", "record-transaction", "change-method",
                 "lock-year", "save-profile", "record-corporate-action")


@workflow("W04", "The advisor's read-only views: same figures as members, no write controls, every write refused", 3)
def w04(ctx):
    with ctx.step("the advisor reads the same accounts, lots, year totals and exports as the household's members"):
        adv, pr, ast = H.who("morgan"), H.who("priya"), H.who("astrid")
        acc = lambda c: sorted(n.attrs.get("data-account-id") for n in c.get("/households/1", follow=True).dom.attr_all("data-account-id"))  # noqa: E731
        ctx.eq(acc(adv), acc(pr), "accounts listed on the household page")
        ctx.eq(sorted(H.lots(adv, 2)), sorted(H.lots(pr, 2)), "open lots of account 2")
        ctx.eq(H.year_page(adv, H1, 2025), H.year_page(pr, H1, 2025), "the 2025 year page's totals")
        for c_member, path in ((pr, "/households/1/reports/2025/summary.csv"), (pr, "/households/1/reports/2024/realized.csv"),
                               (ast, "/households/2/unrealized.csv"), (ast, "/households/2/reports/2025/summary.csv")):
            a, m = adv.get(path), c_member.get(path)
            ctx.check(a.status == 200 and m.status == 200 and a.body == m.body, f"{path}: advisor {a.status}, member {m.status}, "
                      f"same bytes={a.body == m.body}")
    with ctx.step("no page offers the advisor a form or button for a change"):
        adv = H.who("morgan")
        offered = []
        for path in ("/", "/households/1", "/households/2", "/households/1/securities", "/households/1/transfers",
                     "/households/1/corporate-actions", "/accounts/1",
                     "/accounts/6", "/accounts/1/lots", "/accounts/1/transactions/new", f"/accounts/1/transactions/new?kind=sell&security_id={legacy_pick(H1)[0].sid}",
                     "/households/1/reports/2024", "/households/2/reports/2024", "/profile"):
            page = adv.get(path, follow=True)
            for act in WRITE_ACTIONS:
                if page.dom.attr_one("data-action", act) is not None:
                    offered.append(f"{path}: {act}")
        ctx.check(not offered, f"write controls shown to the advisor: {offered[:8]}")
    with ctx.step("every change the advisor attempts is refused with 403 and changes nothing"):
        adv = H.who("morgan")
        n_acct = H.q("SELECT count(*) AS n FROM accounts")[0]["n"]
        n_sec = H.q("SELECT count(*) AS n FROM securities")[0]["n"]
        n_ch = H.q("SELECT count(*) AS n FROM lot_method_changes")[0]["n"]
        name0 = H.q("SELECT full_name FROM users WHERE id = 8")[0]["full_name"]
        n_ca0 = n_actions(H.who("priya"), H1)
        tok = tokens(adv, "/households/1") or tokens(adv, "/")
        sec = legacy_pick(H1)[0]
        attempts = [
            ("/households/1/accounts", {"name": "Advisor acct", "broker": "northwind", "account_number": f"ADV-{H.nonce}", "lot_method": "FIFO",
                                        "account_type": ""}),
            ("/households/1/securities", {"symbol": f"ADV{H.nonce}", "isin": f"XSADV{H.nonce}0001"[:11] + "1", "name": "Adv", "currency": "USD"}),
            ("/accounts/1/transactions", {"kind": "buy", "trade_date": "2026-03-02", "security_id": str(sec.sid), "quantity": "1",
                                          "price": "10", "fee": "0"}),
            ("/accounts/1/method", {"lot_method": "HIFO", "effective_from": "2026-01-05"}),
            ("/households/1/transfers", {"from_account_id": "1", "to_account_id": "2", "security_id": str(sec.sid), "trade_date": "2026-03-02"}),
            ("/households/1/reports/2024/lock", {}),
            ("/households/1/corporate-actions", {"kind": "return_of_capital", "security_id": str(sec.sid), "action_date": "2026-03-02",
                                                 "amount_per_share": "0.01"}),
            ("/profile", {"full_name": "Advisor Renamed"}),
        ]
        bad = []
        for path, fields in attempts:
            r = adv.post_form(path, tok + list(fields.items()))
            if r.status != 403:
                bad.append(f"{path} answered {r.status}")
        data = ST.northwind(H.hh[H1].accounts[1].number, [{"kind": "buy", "day": date(2026, 3, 2), "symbol": sec.symbol, "isin": sec.isin,
                            "name": sec.name, "currency": sec.currency, "quantity": F(1), "amount": 1000, "ref": H.ref("adv")}])[0]
        page = H.who("priya").get("/accounts/1", follow=True)
        try:
            r = H.upload(adv, 1, "advisor.csv", data, page=adv.get("/accounts/1", follow=True))
        except FormNotFound:
            r = H.upload(adv, 1, "advisor.csv", data, page=page)
        if r.status != 403:
            bad.append(f"advisor upload answered {r.status}")
        changed = []
        if H.q("SELECT count(*) AS n FROM accounts")[0]["n"] != n_acct:
            changed.append("accounts")
        if H.q("SELECT count(*) AS n FROM securities")[0]["n"] != n_sec:
            changed.append("securities")
        if H.q("SELECT count(*) AS n FROM lot_method_changes")[0]["n"] != n_ch:
            changed.append("lot_method_changes")
        if H.q("SELECT full_name FROM users WHERE id = 8")[0]["full_name"] != name0:
            changed.append("users.full_name")
        txt = H.who("priya").get("/accounts/1", follow=True).text
        if "advisor.csv" in txt or "Advisor acct" in txt:
            changed.append("account 1 page")
        if n_actions(H.who("priya"), H1) != n_ca0:
            changed.append("corporate actions")
        yp = H.year_page(H.who("priya"), H1, 2024)
        if yp and yp["locked"] == "true":
            changed.append("2024 locked")
        r, _h, rows = H.year_csv(H.who("priya"), H1, 2026, "realized")
        if changed:
            ctx.breach("X1", f"an advisor request changed data: {changed}")
        ctx.check(not bad and not changed, f"advisor writes: {bad} changed={changed}")


# ======================================================================================== W05
@workflow("W05", "Roles: a read-only viewer, a member who cannot lock, a preparer who reads and locks only, an inactive preparer", 3)
def w05(ctx):
    with ctx.step("the viewer reads the household's pages and exports exactly as the owner does"):
        asha, pr = H.who("asha"), H.who("priya")
        for path in ("/households/1/reports/2025/summary.csv", "/households/1/unrealized.csv", "/households/1/reports/2024/realized.csv"):
            a, o = asha.get(path), pr.get(path)
            ctx.check(a.status == 200 and o.status == 200 and a.body == o.body, f"{path}: viewer {a.status}, owner {o.status}, "
                      f"same bytes={a.body == o.body}")
        ctx.eq(sorted(H.lots(asha, 2)), sorted(H.lots(pr, 2)), "open lots of account 2 (markers)")
        ctx.eq(H.year_page(asha, H1, 2025), H.year_page(pr, H1, 2025), "the 2025 year page's totals")
        ctx.eq({n.attrs.get("data-household-id") for n in asha.get("/", follow=True).dom.attr_all("data-household-id")}, {"1"},
               "households on the viewer's home page")
    with ctx.step("every change the viewer attempts is refused, and no page offers one other than their display name"):
        asha = H.who("asha")
        changed = write_attempts(asha, H1, "viewer")
        if changed:
            ctx.breach("X1", f"a viewer's request changed data: {changed}")
        ctx.check(not changed, f"viewer writes: {changed}")
        offered = []
        for path in ("/households/1", "/households/1/securities", "/households/1/transfers", "/households/1/corporate-actions",
                     "/accounts/3", "/accounts/3/lots",
                     "/accounts/3/transactions/new", "/households/1/reports/2024"):
            page = asha.get(path, follow=True)
            for act in WRITE_ACTIONS:
                if page.dom.attr_one("data-action", act) is not None:
                    offered.append(f"{path}: {act}")
        ctx.check(not offered, f"write controls shown to the viewer: {offered[:6]}")
        ctx.check(asha.get("/profile", follow=True).dom.attr_one("data-action", "save-profile") is not None,
                  "the viewer's profile page has no save-profile form")
        r = submit_form(asha, "/profile", "save-profile", {"full_name": "Asha O."}, follow=True)
        ctx.eq(H.q("SELECT full_name FROM users WHERE id = 9")[0]["full_name"], "Asha O.", "the viewer's display name")
    with ctx.step("a member sees no lock button and a lock request is refused with 403; the owner sees the button"):
        dn, pr = H.who("daniel"), H.who("priya")
        ctx.check(not lock_button(dn, H1, 2024), "the member is offered lock-year on 2024")
        ctx.check(lock_button(pr, H1, 2024), "the owner is not offered lock-year on 2024 (2023 is locked, 2024 has ended)")
        r = dn.post_form("/households/1/reports/2024/lock", tokens(dn, "/households/1"))
        if (H.year_page(pr, H1, 2024) or {}).get("locked") == "true":
            ctx.breach("X1", "a member locked a tax year")
            raise StepFailed("a member locked 2024")
        ctx.eq(r.status, 403, "member POST to lock 2024")
    with ctx.step("the preparer reads assigned households only, may lock their year, and changes nothing else"):
        hb = H.who("hannah")
        ctx.eq({n.attrs.get("data-household-id") for n in hb.get("/", follow=True).dom.attr_all("data-household-id")}, {"2", "3"},
               "households on the preparer's home page")
        for path in ("/households/1", "/households/1/reports/2025/summary.csv", "/accounts/1/lots"):
            r = hb.get(path)
            if r.status == 200 and ("symbol," in r.text or "data-lot-id" in r.text or "data-account-id" in r.text):
                ctx.breach("X1", f"a preparer read an unassigned household: {path}")
            ctx.eq(r.status, 403, f"preparer GET {path}")
        ctx.check(lock_button(hb, H2, 2024), "the preparer is not offered lock-year on household 2's 2024")
        for path in ("/households/2", "/accounts/6", "/households/2/securities", "/households/2/corporate-actions"):
            page = hb.get(path, follow=True)
            shown = [act for act in WRITE_ACTIONS if act != "lock-year" and page.dom.attr_one("data-action", act) is not None]
            ctx.check(not shown, f"write controls shown to the preparer on {path}: {shown}")
        changed = write_attempts(hb, H2, "preparer")
        r = hb.post_form("/profile", tokens(hb, "/households/2/reports/2024") + [("full_name", "Hannah Renamed")])
        if H.q("SELECT full_name FROM users WHERE id = 10")[0]["full_name"] != "Hannah Brooks":
            changed.append("display name")
        ctx.eq(r.status, 403, "preparer POST /profile")
        if changed:
            ctx.breach("X1", f"a preparer's request changed data: {changed}")
        ctx.check(not changed, f"preparer writes: {changed}")
    with ctx.step("an inactive preparer gets no password email and cannot sign in"):
        a = "old.preparer@brooks-tax.example"
        before = H.mail_ids(a)
        H.request_reset(a)
        time.sleep(3)
        ctx.eq(len(H.mail_ids(a) - before), 0, "emails to an inactive preparer")
        c, _ = H.signin(a, PASSWORD)
        ctx.check(not H.signed_in(c), "an inactive preparer signed in")


def n_actions(c: Client, hid: int) -> int:
    """Corporate actions listed on the household's corporate-actions page (R13.11)."""
    return len(c.get(f"/households/{hid}/corporate-actions", follow=True).dom.attr_all("data-action-id"))


def write_attempts(c: Client, hid: int, label: str) -> list[str]:
    """Every change of R3/R4 attempted by a user who may not make it; returns what changed."""
    aid = min(a for a in H.hh[hid].accounts)
    sec = legacy_pick(hid)[0]
    n_acct = H.q("SELECT count(*) AS n FROM accounts")[0]["n"]
    n_sec = H.q("SELECT count(*) AS n FROM securities")[0]["n"]
    n_ch = H.q("SELECT count(*) AS n FROM lot_method_changes")[0]["n"]
    tok = tokens(c, f"/households/{hid}") or tokens(c, "/")
    lots0 = H.lots(H.who("priya" if hid == H1 else "astrid"), aid)
    n_ca0 = n_actions(H.who("priya" if hid == H1 else "astrid"), hid)
    attempts = [
        (f"/households/{hid}/accounts", {"name": f"{label} acct", "broker": "northwind", "account_number": f"{label[:3].upper()}-{H.nonce}",
                                         "lot_method": "FIFO", "account_type": ""}),
        (f"/households/{hid}/securities", {"symbol": f"{label[:3].upper()}{H.nonce}", "isin": f"XS{label[:3].upper()}{H.nonce}0001"[:11] + "1",
                                           "name": "x", "currency": "USD"}),
        (f"/accounts/{aid}/transactions", {"kind": "buy", "trade_date": "2026-03-02", "security_id": str(sec.sid), "quantity": "1",
                                           "price": "10", "fee": "0"}),
        (f"/accounts/{aid}/method", {"lot_method": "HIFO", "effective_from": "2026-01-05"}),
        (f"/households/{hid}/transfers", {"from_account_id": str(aid), "to_account_id": str(max(H.hh[hid].accounts)),
                                          "security_id": str(sec.sid), "trade_date": "2026-03-02"}),
        (f"/households/{hid}/corporate-actions", {"kind": "return_of_capital", "security_id": str(sec.sid), "action_date": "2026-03-02",
                                                  "amount_per_share": "0.01"}),
    ]
    bad = []
    for path, fields in attempts:
        r = c.post_form(path, tok + list(fields.items()))
        if r.status != 403:
            bad.append(f"{path} answered {r.status}")
    owner = H.who("priya" if hid == H1 else "astrid")
    data = ST.build(H.hh[hid].accounts[aid].broker, H.hh[hid].accounts[aid].number,
                    [{"kind": "buy", "day": date(2026, 3, 2), "symbol": sec.symbol, "isin": sec.isin, "name": sec.name,
                      "currency": sec.currency, "quantity": F(1), "amount": 1000, "ref": H.ref(label[:3])}])[0]
    try:
        r = H.upload(c, aid, f"{label}.csv", data, page=c.get(f"/accounts/{aid}", follow=True))
    except FormNotFound:
        r = H.upload(c, aid, f"{label}.csv", data, page=owner.get(f"/accounts/{aid}", follow=True))
    if r.status != 403:
        bad.append(f"upload answered {r.status}")
    changed = list(bad and [] or [])
    if H.q("SELECT count(*) AS n FROM accounts")[0]["n"] != n_acct:
        changed.append("accounts")
    if H.q("SELECT count(*) AS n FROM securities")[0]["n"] != n_sec:
        changed.append("securities")
    if H.q("SELECT count(*) AS n FROM lot_method_changes")[0]["n"] != n_ch:
        changed.append("lot_method_changes")
    if H.lots(owner, aid) != lots0 or f"{label}.csv" in owner.get(f"/accounts/{aid}", follow=True).text:
        changed.append(f"account {aid}")
    if n_actions(owner, hid) != n_ca0:
        changed.append("corporate actions")
    if bad and not changed:
        raise StepFailed(f"{label} writes were not refused with 403: {bad}")
    return changed

# ======================================================================================== W06
@workflow("W06", "CSRF on every change, mass assignment, profile edits change only the name", 3)
def w06(ctx):
    pr = H.who("priya")
    with ctx.step("every state-changing form without the session token is refused with 403 and changes nothing"):
        bk = Book("W06", H1, "priya")
        aid = bk.account("northwind")
        sec = bk.security("USD")
        bk.buy(aid, sec, date(2026, 2, 2), 10, "50.00")
        bk.flush()
        n_acct = H.q("SELECT count(*) AS n FROM accounts")[0]["n"]
        n_sec = H.q("SELECT count(*) AS n FROM securities")[0]["n"]
        n_ch = H.q("SELECT count(*) AS n FROM lot_method_changes")[0]["n"]
        lots0 = H.lots(pr, aid)
        name0 = H.q("SELECT full_name FROM users WHERE id = 1")[0]["full_name"]
        n_ca0 = n_actions(pr, H1)
        bad = []

        def tryform(path, action, fields, page=None):
            try:
                r = submit_form(pr, path, action, fields, drop_csrf=True, page=page)
            except FormNotFound:
                bad.append(f"no {action} form on {path}")
                return
            if r.status != 403:
                bad.append(f"{action} without token answered {r.status}")
        tryform("/households/1", "create-account", {"name": "No token", "broker": "harbor", "account_number": f"NT-{H.nonce}", "lot_method": "FIFO",
                                                    "account_type": ""})
        tryform("/households/1/securities", "create-security", {"symbol": f"NT{H.nonce}", "isin": f"XSNT{H.nonce}00001"[:11] + "2",
                                                                "name": "No token", "currency": "USD"})
        tryform(f"/accounts/{aid}/transactions/new", "record-transaction", {"kind": "buy", "trade_date": "2026-03-03",
                "security_id": str(sec["sid"]), "quantity": "3", "price": "10", "fee": "0"})
        tryform(f"/accounts/{aid}", "change-method", {"lot_method": "HIFO", "effective_from": "2026-01-01"})
        tryform("/households/1/transfers", "record-transfer", {"from_account_id": str(aid), "to_account_id": "1",
                "security_id": str(sec["sid"]), "trade_date": "2026-03-04"})
        tryform("/profile", "save-profile", {"full_name": "Token Less"})
        tryform("/households/1/corporate-actions", "record-corporate-action", {"kind": "return_of_capital", "security_id": str(sec["sid"]),
                "action_date": "2026-03-06", "amount_per_share": "0.01"})
        tryform("/households/1/reports/2024", "lock-year", {})
        data = ST.northwind(H.hh[H1].accounts[aid].number, [{"kind": "buy", "day": date(2026, 3, 5), "symbol": sec["symbol"],
                            "isin": sec["isin"], "name": sec["name"], "currency": "USD", "quantity": F(7), "amount": 7000, "ref": H.ref("nt")}])[0]
        r = H.upload(pr, aid, "no-token.csv", data, drop_csrf=True)
        if r.status != 403:
            bad.append(f"upload without token answered {r.status}")
        changed = []
        if H.q("SELECT count(*) AS n FROM accounts")[0]["n"] != n_acct:
            changed.append("accounts")
        if H.q("SELECT count(*) AS n FROM securities")[0]["n"] != n_sec:
            changed.append("securities")
        if H.q("SELECT count(*) AS n FROM lot_method_changes")[0]["n"] != n_ch:
            changed.append("method changes")
        if H.lots(pr, aid) != lots0:
            changed.append("lots")
        if H.q("SELECT full_name FROM users WHERE id = 1")[0]["full_name"] != name0:
            changed.append("display name")
        yp = H.year_page(pr, H1, 2024)
        if yp and yp["locked"] == "true":
            changed.append("2024 locked")
        if n_actions(pr, H1) != n_ca0:
            changed.append("corporate actions")
        if changed:
            ctx.breach("X2", f"a request without the session token changed {changed}")
        ctx.check(not bad and not changed, f"{bad} changed={changed}")
    with ctx.step("the profile form changes only the display name, whatever else it carries"):
        before = H.q("SELECT email, household_id, role, active FROM users WHERE id = 1")[0]
        r = submit_form(pr, "/profile", "save-profile", {"full_name": "Priya R. Raman"}, follow=True,
                        override={"role": "advisor", "household_id": "2", "active": "false", "email": f"hijack.{H.nonce.lower()}@verify.example",
                                  "id": "2", "user_id": "2"})
        after = H.q("SELECT full_name, email, household_id, role, active FROM users WHERE id = 1")[0]
        other = H.q("SELECT full_name FROM users WHERE id = 2")[0]["full_name"]
        changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
        if changed or other != "Daniel Okafor":
            ctx.breach("X5", f"the profile form changed {changed or 'another user'}")
        ctx.check(not changed and other == "Daniel Okafor", f"extra profile fields changed {changed}")
        ctx.eq(after["full_name"], "Priya R. Raman", "display name after the profile form")
    with ctx.step("extra household_id fields on account and security forms are ignored"):
        number = f"MA-{H.nonce}"
        r = submit_form(pr, "/households/1", "create-account", {"name": "Mass assign", "broker": "northwind", "account_number": number,
                        "lot_method": "FIFO", "account_type": ""}, override={"household_id": "3", "id": "1"}, follow=False)
        rows = H.q("SELECT id, household_id FROM accounts WHERE account_number = %s", (number,))
        if rows and rows[0]["household_id"] != 1:
            ctx.breach("X5", "an extra household_id field moved a new account to another household")
        ctx.check(rows and rows[0]["household_id"] == 1, f"account created as {rows}")
        H.hh[H1].accounts[rows[0]["id"]] = M.Account(rows[0]["id"], 1, "Mass assign", "northwind", number, "FIFO")
        H.acct_hh[rows[0]["id"]] = 1
        sym = f"MA{H.nonce}"
        submit_form(pr, "/households/1/securities", "create-security", {"symbol": sym, "isin": f"XSMA{H.nonce}00009"[:11] + "9",
                    "name": "Mass assign sec", "currency": "USD"}, override={"household_id": "3"}, follow=True)
        rows = H.q("SELECT id, household_id FROM securities WHERE symbol = %s", (sym,))
        if rows and rows[0]["household_id"] != 1:
            ctx.breach("X5", "an extra household_id field moved a new security to another household")
        ctx.check(rows and rows[0]["household_id"] == 1, f"security created as {rows}")
        H.hh[H1].securities[rows[0]["id"]] = M.Sec(rows[0]["id"], 1, sym, f"XSMA{H.nonce}00009"[:11] + "9", "Mass assign sec", "USD")
    with ctx.step("an account number already on file is refused with 409; a duplicate symbol too"):
        n0 = H.q("SELECT count(*) AS n FROM accounts")[0]["n"]
        r = submit_form(pr, "/households/1", "create-account", {"name": "Dup", "broker": "northwind", "account_number": "  NW-4821-3377 ",
                        "lot_method": "FIFO", "account_type": ""}, follow=False)
        ctx.eq(r.status, 409, "duplicate broker account number")
        ctx.eq(H.q("SELECT count(*) AS n FROM accounts")[0]["n"], n0, "accounts after a duplicate")
        r = submit_form(pr, "/households/1/securities", "create-security", {"symbol": " ntmx ", "isin": "XS9999999990", "name": "Dup",
                        "currency": "USD"}, follow=False)
        ctx.eq(r.status, 409, "duplicate symbol in the household")


# ======================================================================================== W07
@workflow("W07", "Northwind statements: quirks, junk lines, natural keys, overlaps, legacy references, concurrent uploads", 4, critical=True)
def w07(ctx):
    rnd = random.Random(606)
    bk = None
    with ctx.step("a Northwind account and 14 USD/CAD securities; a quirky first statement is recorded exactly"):
        bk = Book("W07", H1, "priya")
        aid = bk.account("northwind", "FIFO")
        secs = [bk.security("CAD" if i % 3 == 0 else "USD") for i in range(14)]
        rows = []
        day = date(2026, 1, 5)
        for i, s in enumerate(secs):
            q1 = rnd.choice([1200, 2500, 15, 3, F("0.4375"), 1000])
            p = F(rnd.randrange(1000, 90000), 100)
            rows.append(bk.buy(aid, s, day + timedelta(days=i % 5), q1, p, tie=i % 2 == 0))
            rows.append(bk.buy(aid, s, day + timedelta(days=9 + i % 4), rnd.choice([7, 1100, 2]), p * F(rnd.randrange(80, 120), 100)))
            rows.append(bk.div(aid, s, day + timedelta(days=20 + i % 3), rnd.randrange(101, 99999)))
            rows.append(bk.sell(aid, s, day + timedelta(days=40 + i % 6), q1, p * F(rnd.randrange(90, 140), 100)))
            if i % 4 == 1:
                rows.append(bk.split(aid, s, day + timedelta(days=80), (2, 1)))
        rows.sort(key=lambda r: (r["day"], E.KIND_RANK[r["kind"]]))
        ctx.shared["nw"] = (bk, aid, rows)
        data, _ = ST.northwind(H.hh[H1].accounts[aid].number, rows[:40], rnd, crlf=False)
        want_new, _ = H.model_rows(aid, rows[:40])
        r = H.upload(bk.c, aid, "nw-jan-feb.csv", data)
        st, n, dup, lines = H.import_status(r)
        ctx.check(st == "imported" and n == len(want_new) and dup == 0, f"first statement: {r.status} {st} {n}/{dup} lines={lines}")
        H.commit_rows(aid, want_new)
        bk.rows[aid] = []
        ctx.shared["nw_first"] = data
    with ctx.step("re-importing the same file records nothing and counts every row as a duplicate"):
        bk, aid, rows = ctx.shared["nw"]
        r = H.upload(bk.c, aid, "nw-jan-feb (copy).csv", ctx.shared["nw_first"])
        st, n, dup, _ = H.import_status(r)
        ctx.check(st == "imported" and n == 0 and dup == 40, f"re-import: {r.status} {st} recorded={n} duplicates={dup}")
    with ctx.step("an overlapping statement (one shared row) records only its new rows"):
        bk, aid, rows = ctx.shared["nw"]
        second = rows[39:]
        data, _ = ST.northwind(H.hh[H1].accounts[aid].number, second, rnd)
        want_new, want_dup = H.model_rows(aid, second)
        r = H.upload(bk.c, aid, "nw-feb-apr.csv", data)
        st, n, dup, lines = H.import_status(r)
        ctx.check(st == "imported" and (n, dup) == (len(want_new), 1), f"overlap: {r.status} {st} {n}/{dup} want {len(want_new)}/1 {lines}")
        H.commit_rows(aid, want_new)
    with ctx.step("a known reference with different content rejects the whole file, naming that line"):
        bk, aid, rows = ctx.shared["nw"]
        s = bk.secs[0]
        fresh = [{"kind": "buy", "day": date(2026, 5, 4), "symbol": s["symbol"], "isin": s["isin"], "name": s["name"], "currency": s["currency"],
                  "quantity": F(5), "amount": 50000, "ref": H.ref("w05x")} for _ in range(3)]
        changed = dict(rows[5])
        changed["amount"] = changed["amount"] + 1 if changed.get("amount") else 1
        data, nums = ST.northwind(H.hh[H1].accounts[aid].number, fresh[:2] + [changed] + fresh[2:], rnd)
        lots0 = H.lots(bk.c, aid)
        r = H.upload(bk.c, aid, "conflict.csv", data)
        st, n, dup, lines = H.import_status(r)
        ctx.check(r.status == 422 and st == "rejected" and lines == [nums[2]], f"conflict: {r.status} {st} lines={lines} want [{nums[2]}]")
        ctx.eq(H.lots(bk.c, aid), lots0, "lots after a rejected file")
    with ctx.step("inside one file, a repeated row is a duplicate and a repeated reference with other content rejects the file"):
        bk, aid, rows = ctx.shared["nw"]
        s = bk.secs[2]
        mk = lambda day, q, amt, ref: {"kind": "buy", "day": day, "symbol": s["symbol"], "isin": s["isin"], "name": s["name"],  # noqa: E731
                                       "currency": s["currency"], "quantity": F(q), "amount": amt, "ref": ref}
        r1, r2 = mk(date(2026, 5, 11), 4, 40404, H.ref("w07d")), mk(date(2026, 5, 12), 6, 60606, H.ref("w07d"))
        data, _ = ST.northwind(H.hh[H1].accounts[aid].number, [r1, r2, dict(r1)], rnd)  # R4.5: the third row repeats the first
        want_new, _ = H.model_rows(aid, [r1, r2])
        r = H.upload(bk.c, aid, "repeat-inside.csv", data)
        st, n, dup, lines = H.import_status(r)
        ctx.check(st == "imported" and (n, dup) == (2, 1), f"a file repeating a row: {r.status} {st} recorded={n} duplicates={dup} {lines}")
        H.commit_rows(aid, want_new)
        ref3 = H.ref("w07c")
        a3, b3 = mk(date(2026, 5, 13), 3, 30303, ref3), mk(date(2026, 5, 14), 3, 30304, ref3)
        data, nums = ST.northwind(H.hh[H1].accounts[aid].number, [a3, b3], rnd)
        lots0 = H.lots(bk.c, aid)
        r = H.upload(bk.c, aid, "conflict-inside.csv", data)
        st, n, dup, lines = H.import_status(r)
        ctx.check(r.status == 422 and st == "rejected" and nums[1] in lines and set(lines) <= {nums[0], nums[1]},
                  f"a reference repeated with other content: {r.status} {st} lines={lines} want {nums[1]}")
        ctx.eq(H.lots(bk.c, aid), lots0, "lots after a rejected file")
    with ctx.step("a statement for another account number is rejected as a whole-file problem (line 0)"):
        bk, aid, rows = ctx.shared["nw"]
        data, _ = ST.northwind("NW-0000-0000", rows[:3], rnd)
        r = H.upload(bk.c, aid, "wrong-account.csv", data)
        st, n, dup, lines = H.import_status(r)
        ctx.check(r.status == 422 and st == "rejected" and lines == [0], f"wrong account: {r.status} {st} lines={lines}")
    with ctx.step("a statement repeating the old tool's last rows for account 1 is all duplicates (legacy references count)"):
        h = H.hh[H1]
        leg = [t for t in h.txns if t.account == 1 and t.ref and t.kind in ("buy", "sell", "dividend", "reinvest") and t.seq < 10 ** 6]
        leg = sorted(leg, key=lambda t: (t.day, t.tid))[-45:]
        rows = []
        for t in leg:
            s = h.securities[t.security]
            rows.append({"kind": t.kind, "day": t.day, "symbol": s.symbol, "isin": s.isin, "name": s.name, "currency": s.currency,
                         "quantity": t.quantity, "amount": t.amount, "ratio": t.ratio, "ref": t.ref.strip()})
        data, _ = ST.northwind(h.accounts[1].number, sorted(rows, key=lambda r: (r["day"], E.KIND_RANK[r["kind"]])), rnd)
        r = H.upload(H.who("priya"), 1, "northwind-dec-2025.csv", data)
        st, n, dup, lines = H.import_status(r)
        ctx.check(st == "imported" and n == 0 and dup == len(rows), f"legacy overlap: {r.status} {st} recorded={n} duplicates={dup} of {len(rows)} {lines}")
    with ctx.step("two simultaneous uploads of one new statement record each row once"):
        bk, aid, rows = ctx.shared["nw"]
        s = bk.secs[1]
        new_rows = [bk.buy(aid, s, date(2026, 6, 1) + timedelta(days=i), 3 + i, "41.37") for i in range(12)]
        bk.rows[aid] = []
        data, _ = ST.northwind(H.hh[H1].accounts[aid].number, new_rows, rnd)
        want_new, _ = H.model_rows(aid, new_rows)
        pages = [bk.c.get(f"/accounts/{aid}", follow=True) for _ in range(2)]
        c2 = bk.c.copy()
        outs = simultaneously([lambda: H.upload(bk.c, aid, "race-a.csv", data, page=pages[0]),
                               lambda: H.upload(c2, aid, "race-b.csv", data, page=pages[1])])
        ctx.check(all(k == "ok" for k, _v in outs), f"a racing upload got no answer: {outs}")
        res = [(v.status,) + H.import_status(v)[:3] for _k, v in outs]
        imported = [x for x in res if x[1] == "imported"]
        refused = [x for x in res if x[1] != "imported" and x[0] >= 400]
        H.commit_rows(aid, want_new)
        # R4.5: each row is recorded once; the other upload may count them as duplicates or be refused
        ctx.check(imported and len(imported) + len(refused) == 2 and sum(x[2] or 0 for x in imported) == 12,
                  f"racing uploads (status, result, recorded, duplicates): {res}")
        refs = Counter(x["ref"] for x in H.app_txn_ids(bk.c, aid))
        ctx.eq(sorted(refs[r["ref"]] for r in new_rows), [1] * 12, "times each racing row is listed on the account page")
    with ctx.step("the account's reports and lots match the rules"):
        bk, aid, rows = ctx.shared["nw"]
        report(ctx, bk.compare(years=(2026,)), "W07 securities")


# ======================================================================================== W08
@workflow("W08", "Harbor Direct statements: number formats, row errors, atomicity, short checks, size limit, file names", 4, critical=True)
def w08(ctx):
    rnd = random.Random(707)
    with ctx.step("a Harbor account and 12 EUR/USD/CAD securities; a statement with BOM, 1.200-style numbers and reinvestments"):
        bk = Book("W08", H1, "daniel")
        aid = bk.account("harbor", "FIFO")
        secs = [bk.security(["EUR", "USD", "CAD"][i % 3]) for i in range(12)]
        for i, s in enumerate(secs):
            p = F(rnd.randrange(500, 30000), 100)
            bk.buy(aid, s, date(2026, 1, 12) + timedelta(days=i), rnd.choice([1200, 15000, 2300, 7]), p, tie=True)
            bk.buy(aid, s, date(2026, 2, 2) + timedelta(days=i), rnd.choice([F("12.5"), 1001, 40]), p * F(rnd.randrange(85, 115), 100))
            amt = rnd.randrange(1999, 49999)
            bk.div(aid, s, date(2026, 3, 1) + timedelta(days=i % 4), amt)
            bk.row(aid, s, "reinvest", date(2026, 3, 1) + timedelta(days=i % 4), F(rnd.randrange(1, 9999), 10000), amt)
            if i % 3 == 0:
                bk.split(aid, s, date(2026, 3, 20), (1, 4))
            bk.sell(aid, s, date(2026, 5, 4) + timedelta(days=i), 7, p * F(rnd.randrange(70, 150), 100), tie=i % 2 == 1)
        bk.flush()
        ctx.shared["hd"] = (bk, aid)
    with ctx.step("a file with bad rows is rejected whole and every bad line is listed"):
        bk, aid = ctx.shared["hd"]
        s = bk.secs[0]
        good = lambda day, q, amt: {"kind": "buy", "day": day, "symbol": s["symbol"], "isin": s["isin"], "name": s["name"],  # noqa: E731
                                    "currency": s["currency"], "quantity": F(q), "amount": amt, "ref": H.ref("w06g")}
        rows = [good(date(2026, 6, 1), 10, 12345), good(date(2026, 6, 2), 11, 22345), good(date(2026, 6, 3), 12, 32345),
                good(date(2026, 6, 4), 13, 42345), good(date(2026, 6, 5), 14, 52345), good(date(2026, 6, 8), 15, 62345),
                good(date(2026, 6, 9), 16, 72345), good(date(2026, 6, 10), 17, 82345)]
        data, nums = ST.harbor(H.hh[H1].accounts[aid].number, rows, rnd)
        lines = data.decode("utf-8-sig").split("\r\n")
        bad_idx = {}

        def corrupt(i, fn):
            ln = nums[i] - 1
            f = lines[ln].split(";")
            fn(f)
            lines[ln] = ";".join(f)
            bad_idx[i] = nums[i]
        corrupt(0, lambda f: f.__setitem__(0, "31.02.2026"))                   # impossible date
        corrupt(2, lambda f: f.__setitem__(3, "XS0000000000"))                 # unknown ISIN
        corrupt(3, lambda f: f.__setitem__(9, "SEK" if s["currency"] != "SEK" else "USD"))  # wrong currency
        corrupt(5, lambda f: f.__setitem__(8, f[8].lstrip("-")))               # purchase with a positive amount
        corrupt(6, lambda f: f.__setitem__(8, "-123,456"))                     # three decimals
        corrupt(7, lambda f: f.__setitem__(2, "Gift"))                         # unknown activity
        bad_file = ("﻿" + "\r\n".join(lines)).encode("utf-8")
        lots0 = H.lots(bk.c, aid)
        n_imp = len(H.app_txn_ids(bk.c, aid))
        r = H.upload(bk.c, aid, "harbor-bad.csv", bad_file)
        st, n, dup, got = H.import_status(r)
        ctx.check(r.status == 422 and st == "rejected", f"bad file answered {r.status} {st}")
        ctx.eq(got, sorted(bad_idx.values()), "error lines")
        ctx.eq(H.lots(bk.c, aid), lots0, "lots after a rejected file")
        ctx.eq(len(H.app_txn_ids(bk.c, aid)), n_imp, "transactions after a rejected file")
        B.capture_text("import_rejected_http", visible_text(r.text)[:6000], {"error_lines": sorted(bad_idx.values())})
    with ctx.step("a sale beyond the holding is listed by its line; an older sale that starves an existing one is listed as line 0"):
        bk, aid = ctx.shared["hd"]
        s = bk.secs[1]
        mk = lambda kind, day, q, amt: {"kind": kind, "day": day, "symbol": s["symbol"], "isin": s["isin"], "name": s["name"],  # noqa: E731
                                        "currency": s["currency"], "quantity": F(q), "amount": amt, "ref": H.ref("w06s")}
        lots = [l for l in H.hh[H1].open_lots(H.rates, aid=aid, sids={s["sid"]})]
        have = sum((l.qty for l in lots), F(0))
        rows = [mk("buy", date(2026, 6, 15), 5, 5000), mk("sell", date(2026, 6, 16), have + 6, 9000)]
        data, nums = ST.harbor(H.hh[H1].accounts[aid].number, rows, rnd)
        r = H.upload(bk.c, aid, "too-many.csv", data)
        st, n, dup, got = H.import_status(r)
        ctx.check(r.status == 422 and st == "rejected" and got == [nums[1]], f"short sale: {r.status} {st} lines={got} want [{nums[1]}]")
        early = sum((t.quantity for t in H.hh[H1].txns if t.account == aid and t.security == s["sid"] and t.kind in ("buy", "reinvest")
                     and t.day < date(2026, 2, 20)), F(0))
        rows = [mk("sell", date(2026, 2, 20), early - F(1, 2), 9000)]  # fine on its own date, but the May sale is then short
        data, nums = ST.harbor(H.hh[H1].accounts[aid].number, rows, rnd)
        r = H.upload(bk.c, aid, "starves-may.csv", data)
        st, n, dup, got = H.import_status(r)
        ctx.check(r.status == 422 and st == "rejected" and got == [0], f"older sale starving a later one: {r.status} {st} lines={got} want [0]")
    with ctx.step("a file of exactly IMPORT_MAX_BYTES is imported; one byte more is refused with 413 and records nothing"):
        bk, aid = ctx.shared["hd"]
        s = bk.secs[2]
        rows = [bk.buy(aid, s, date(2026, 7, 1) + timedelta(days=i), 2, "15.15") for i in range(6)]
        bk.rows[aid] = []
        data, _ = ST.harbor(H.hh[H1].accounts[aid].number, rows, rnd, pad_to=H.import_max + 1)
        lots0 = H.lots(bk.c, aid)
        r = H.upload(bk.c, aid, "big.csv", data)
        ctx.eq(r.status, 413, f"a file of {H.import_max + 1} bytes")
        ctx.eq(H.lots(bk.c, aid), lots0, "lots after a refused large file")
        data, _ = ST.harbor(H.hh[H1].accounts[aid].number, rows, rnd, pad_to=H.import_max)
        want_new, _ = H.model_rows(aid, rows)
        r = H.upload(bk.c, aid, "exact.csv", data)
        st, n, dup, got = H.import_status(r)
        ctx.check(st == "imported" and n == len(rows), f"a file of exactly {H.import_max} bytes: {r.status} {st} {n} {got}")
        H.commit_rows(aid, want_new)
    with ctx.step("file names are never paths: traversal names write nothing outside, and names are listed as text"):
        bk, aid = ctx.shared["hd"]
        s = bk.secs[3]
        ws = Path(H.workspace)
        h0 = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ws / "start.sh", ws / "README.md") if p.exists()}
        tag = f"lb-{H.nonce.lower()}-pwned"
        names = [f"../../../../../../../../tmp/{tag}.csv", f"..\\..\\..\\..\\{tag}-win.csv", f"../{tag}-up.csv", "../start.sh",
                 f"/tmp/{tag}-abs.csv"]
        for i, nm in enumerate(names):
            row = bk.buy(aid, s, date(2026, 8, 3) + timedelta(days=i), 1, "10.00")
            bk.rows[aid] = []
            data, _ = ST.harbor(H.hh[H1].accounts[aid].number, [row], rnd)
            want_new, _ = H.model_rows(aid, [row])
            r = H.upload(bk.c, aid, nm, data)
            if H.import_status(r)[0] == "imported":
                H.commit_rows(aid, want_new)
        found = []
        for base in {Path("/tmp"), ws.parent, ws, ws.parent.parent, Path("/")}:
            for nm in (f"{tag}.csv", f"{tag}-win.csv", f"{tag}-up.csv", f"{tag}-abs.csv"):
                if (base / nm).exists():
                    found.append(str(base / nm))
        h1 = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in h0 if p.exists()}
        if found or h1 != h0:
            ctx.breach("X12", f"an upload's file name reached the file system: {found or 'start.sh/README.md changed'}")
        ctx.check(not found and h1 == h0, f"files written from upload names: {found}")
        nice = "Harbor Ümsätze März 2026 (final).csv"
        row = bk.buy(aid, s, date(2026, 8, 12), 1, "10.00")
        bk.rows[aid] = []
        data, _ = ST.harbor(H.hh[H1].accounts[aid].number, [row], rnd)
        want_new, _ = H.model_rows(aid, [row])
        r = H.upload(bk.c, aid, nice, data)
        ctx.check(H.import_status(r)[0] == "imported", f"upload named {nice!r}: {r.status}")
        H.commit_rows(aid, want_new)
        page = bk.c.get(f"/accounts/{aid}", follow=True)
        listed = [n.attrs.get("data-import-filename") for n in page.dom.attr_all("data-import-filename")]
        ctx.check(nice in listed, f"the account page does not list the original file name; it lists {listed[:6]}")
    with ctx.step("the account's reports and lots match the rules"):
        bk, aid = ctx.shared["hd"]
        report(ctx, bk.compare(years=(2026,)), "W08 securities")


# ======================================================================================== legacy helpers
def legacy_sids(hid: int) -> set:
    base = {r["id"] for r in H.baseline["securities"]}
    return {s for s in H.hh[hid].securities if s in base}


def lock_button(c: Client, hid: int, year: int) -> bool:
    return c.get(f"/households/{hid}/reports/{year}", follow=True).dom.attr_one("data-action", "lock-year") is not None


def year_page_step(ctx, hid: int, year: int, who: str) -> None:
    c = H.who(who)
    h = H.hh[hid]
    t = h.totals(h.summary_rows(year, H.rates))
    yp = H.year_page(c, hid, year)
    ctx.check(yp is not None, f"no data-year={year} element on the year page")
    got = {k: yp[k] for k in ("short", "long", "disallowed", "dividends")} | {"adjustments": yp["adjustment"]}
    ctx.eq(got, t, f"household {hid} {year} page totals (data-total-*)")
    ctx.eq(yp["locked"], "true" if h.is_locked(year) else "false", f"household {hid} {year} data-locked")


def legacy_year(ctx, hid: int, year: int, who: str) -> None:
    sids = legacy_sids(hid)
    with ctx.step(f"summary.csv for {year} answers within 60 s and matches every legacy security, 2023 corrections included"):
        c = H.who(who)
        t0 = time.monotonic()
        r, header, _rows = H.year_csv(c, hid, year, "summary")
        took = time.monotonic() - t0
        ctx.observe(f"summary_{year}_s", round(took, 1))
        ctx.check(r.status == 200 and took <= 60, f"summary.csv answered {r.status} after {took:.0f} s")
        problems = H.compare_summary(c, hid, year, sids)
        ctx.observe(f"summary_{year}_problems", len(problems))
        report(ctx, problems, f"household {hid} {year} summary")
    with ctx.step(f"every realized row of {year} matches, in order"):
        c = H.who(who)
        problems = H.compare_realized(c, hid, year, sids, check_order=True)
        ctx.observe(f"realized_{year}_rows", len(H.hh[hid].realized_rows(year, H.rates, sids=sids)))
        ctx.observe(f"realized_{year}_problems", len(problems))
        report(ctx, problems, f"household {hid} {year} realized")
    with ctx.step(f"the {year} page shows its summary's totals, the lock state, and a lock button only to those who may lock"):
        year_page_step(ctx, hid, year, who)
        h = H.hh[hid]
        lockable = not h.is_locked(year) and h.lock_through is not None and year == h.lock_through + 1
        roles = [("priya", True), ("daniel", False), ("asha", False), ("morgan", False)] if hid == H1 else \
                [("astrid", True), ("johan", False), ("hannah", True), ("morgan", False)]
        for w, may in roles:
            ctx.eq(lock_button(H.who(w), hid, year), lockable and may, f"lock-year button on {year} for {w}")


@workflow("W09", "Legacy 2024 of the Raman-Okafor household: realized rows, summary with 2023 corrections, year page", 4,
          timeout=1200, critical=True)
def w09(ctx):
    legacy_year(ctx, H1, 2024, "priya")


@workflow("W10", "Legacy 2025 of the Raman-Okafor household: realized rows, summary, year page", 4, timeout=1200, critical=True)
def w10(ctx):
    legacy_year(ctx, H1, 2025, "daniel")


@workflow("W11", "Legacy 2024 and 2025 of the Lindqvist household, read by a member and by the advisor", 4, timeout=1200,
          critical=True)
def w11(ctx):
    legacy_year(ctx, H2, 2024, "johan")
    legacy_year(ctx, H2, 2025, "astrid")
    with ctx.step("the advisor's exports of both years are byte-identical to the members'"):
        for y in (2024, 2025):
            for kind in ("summary", "realized"):
                a = H.who("morgan").get(f"/households/2/reports/{y}/{kind}.csv")
                m = H.who("astrid").get(f"/households/2/reports/{y}/{kind}.csv")
                ctx.check(a.status == 200 and a.body == m.body, f"{y} {kind}.csv: advisor {a.status}, same bytes={a.body == m.body}")


@workflow("W12", "Legacy open lots account by account, and unrealized gains at the stored prices", 4, timeout=1200, critical=True)
def w12(ctx):
    for hid, who, aids in ((H1, "priya", (1, 2, 3)), (H1, "asha", (4, 5, 8)), (H2, "johan", (6, 7, 9))):
        with ctx.step(f"lots pages of accounts {aids}: every open lot's quantity, dates, basis and wash-sale marker"):
            c = H.who(who)
            problems = H.compare_lots(c, hid, list(aids), legacy_sids(hid))
            ctx.observe(f"lots_{'_'.join(map(str, aids))}", sum(len(H.want_lots(hid, a, legacy_sids(hid))) for a in aids))
            report(ctx, problems, f"open lots of accounts {aids}")
    for hid, who in ((H1, "daniel"), (H2, "morgan")):
        with ctx.step(f"household {hid}: unrealized.csv values every open lot at the latest price and rate"):
            c = H.who(who)
            h = H.hh[hid]
            sids = legacy_sids(hid)
            want = M.render(h.unrealized_rows(H.rates, H.prices, sids=sids), M.UNREALIZED_TEXT)
            r, header, rows = H.csv_get(c, f"/households/{hid}/unrealized.csv")
            ctx.check(r.status == 200, f"unrealized.csv answered {r.status}")
            ctx.eq(header, M.UNREALIZED_HEADER, "header")
            syms = {h.securities[s].symbol for s in sids}
            got = [x for x in rows if len(x) > 1 and x[1].lstrip("'") in syms]
            report(ctx, diff_rows(want, got, f"unrealized household {hid}", True), "unrealized")

# ======================================================================================== rule-workflow helpers
def rday(rnd: random.Random, lo: date, hi: date) -> date:
    return lo + timedelta(days=rnd.randrange((hi - lo).days + 1))


def price(rnd: random.Random, lo: int = 800, hi: int = 40000) -> F:
    return F(rnd.randrange(lo, hi), 100)


def fixture_check(bk: Book) -> None:
    """The verifier's own rows must be valid (no short sale) before they are sent."""
    h = H.hh[bk.hid]
    trial = []
    for aid, rows in bk.rows.items():
        for r in rows:
            sid = H.sid_of(bk.hid, r)
            trial.append(E.Txn(10 ** 12 + len(trial), 10 ** 12 + len(trial), aid, sid, r["kind"], r["day"],
                               F(r["quantity"]) if r.get("quantity") is not None else None, r.get("amount"), r.get("ratio")))
    saved = list(h.txns)
    try:
        h.txns = saved + trial
        h.touch()
        errs = h.result(H.rates, bk.sids).errors
    finally:
        h.txns = saved
        h.touch()
        h._cache = {k: v for k, v in h._cache.items() if k not in bk.sids}
    if errs:
        raise StepFailed(f"verifier fixture: {len(errs)} invalid rows, first {errs[0][0].kind} {errs[0][0].day}: {errs[0][1]}")


TIES_PATH = Path(__file__).resolve().parent / "reference" / "ties.json"
TIES: dict = {}


def ties(cat: str) -> list[dict]:
    if not TIES:
        import json
        TIES.update(json.loads(TIES_PATH.read_text()))
    return TIES.get(cat, [])


def plant_ties(bk: Book, cat: str, lo: int, hi: int) -> list:
    """Add the rows of ties.json scenarios [lo:hi] of a category to the workflow (three accounts of its own: FIFO
    Northwind, FIFO Harbor, HIFO Northwind). Rows after a transfer are returned, to import once it is recorded."""
    if not getattr(bk, "tie_accts", None):
        bk.tie_accts = [bk.account("northwind", "FIFO", name=f"{bk.wid} ties FIFO"), bk.account("harbor", "FIFO", name=f"{bk.wid} ties FIFO 2"),
                        bk.account("northwind", "HIFO", name=f"{bk.wid} ties HIFO")]
    pending = []
    for sc in ties(cat)[lo:hi]:
        sec = bk.security(sc["currency"])
        rows = sc["rows"]
        tr = [r for r in rows if r["kind"] == "transfer"]
        cut = (tr[0]["day"], E.KIND_RANK["transfer"]) if tr else None
        post = []
        for r in rows:
            if r["kind"] == "transfer":
                continue
            key = (r["day"], E.KIND_RANK[r["kind"]])
            target = post if cut and key > cut else None
            spec = (bk.tie_accts[r["a"]], sec, r["kind"], d(r["day"]), F(r["qty"]) if r.get("qty") else None, r.get("amount"),
                    tuple(r["ratio"]) if r.get("ratio") else None)
            if target is not None:
                target.append(spec)
            else:
                bk.row(*spec)
        if tr:
            pending.append((bk.tie_accts[tr[0]["a"]], bk.tie_accts[tr[0]["to"]], sec, d(tr[0]["day"]), post))
    return pending


def finish_ties(bk: Book, pending: list) -> None:
    if not pending:
        return
    for frm, to, sec, day, _post in pending:
        H.transfer(bk.c, bk.hid, frm, to, sec["sid"], day)
    for *_x, post in pending:
        for spec in post:
            bk.row(*spec)
    fixture_check(bk)
    bk.flush()


NOT_LISTED = ("transfer", "spinoff", "merger", "return_of_capital")  # not compared on account pages


def ui_contract(ctx, bk: Book) -> None:
    with ctx.step("each account page lists every recorded transaction with its markers; the year page offers lock-year only to the owner"):
        h = H.hh[bk.hid]
        for aid in bk.accts:
            got = Counter((x["kind"], x["day"], x["symbol"], x["ref"]) for x in H.app_txn_ids(bk.c, aid) if x["kind"] not in NOT_LISTED)
            want = Counter((t.kind, t.day.isoformat(), h.securities[t.security].symbol, t.ref or "") for t in h.txns
                           if t.account == aid and t.kind not in NOT_LISTED)
            ctx.check(got == want, f"account {aid} lists {sum(got.values())} transactions, expected {sum(want.values())}; "
                      f"missing {list((want - got).elements())[:3]}, unexpected {list((got - want).elements())[:3]}")
        ctx.eq(lock_button(bk.c, bk.hid, 2024), bk.who in ("priya", "astrid"), f"lock-year button on 2024 for {bk.who}")


def compare_step(ctx, bk: Book, years=(2024, 2025, 2026), what: str | None = None) -> None:
    ui_contract(ctx, bk)
    with ctx.step("every realized row, year summary and open lot of these securities matches the rules"):
        h = H.hh[bk.hid]
        n_rows = sum(len(h.realized_rows(y, H.rates, sids=bk.sids)) for y in years)
        n_lots = sum(len(h.open_lots(H.rates, aid=a, sids=bk.sids)) for a in bk.accts)
        n_wash = sum(1 for p in h.result(H.rates, bk.sids).portions if p.disallowed)
        ctx.observe("compared", {"realized_rows": n_rows, "open_lots": n_lots, "wash_portions": n_wash, "securities": len(bk.secs)})
        ctx.check(n_rows >= 10 and n_lots >= 5, f"verifier fixture: only {n_rows} realized rows and {n_lots} lots to compare")
        report(ctx, bk.compare(years=years), what or bk.wid)


# ======================================================================================== W13
@workflow("W13", "Exchange rates and rounding: fallback dates, quote directions, half-cent ties, the share rule", 5, timeout=2400, critical=True)
def w13(ctx):
    rnd = random.Random(808)
    with ctx.step("36 CAD, EUR and USD securities with trades on days without a rate, under both quote directions, at half-cent ties"):
        bk = Book("W13", H1, "priya")
        nw, hd = bk.account("northwind"), bk.account("harbor")
        for i in range(36):
            cur = ["CAD", "EUR", "USD", "CAD", "EUR"][i % 5]
            s = bk.security(cur)
            aid = hd if cur == "EUR" or i % 7 == 0 else nw
            early = cur == "CAD" and i % 2 == 0          # CAD rows before 2024-07-01 are quoted units_per_usd
            lo, hi = (date(2024, 2, 1), date(2024, 6, 20)) if early else (date(2025, 1, 6), date(2025, 6, 30))
            gaps = missing_rate_days(cur, lo, hi, 3, rnd) if cur != "USD" else []
            d1 = gaps[0] if gaps else rday(rnd, lo, hi)
            d2 = gaps[1] if len(gaps) > 1 else rday(rnd, d1, hi)
            d2 = max(d2, d1)
            p = price(rnd)
            q1 = rnd.choice([2, 4, 6, 10, 14, 1000, 1200])
            bk.buy(aid, s, d1, q1, p, tie=True)
            bk.buy(aid, s, d2, rnd.choice([3, 5, F("0.5"), 7, 1250]), p * F(rnd.randrange(90, 115), 100), tie=True)
            dv = missing_rate_days(cur, date(2025, 8, 1), date(2025, 12, 31), 1, rnd) if cur != "USD" else [date(2025, 9, 13)]
            bk.div(aid, s, dv[0], tie_amount(rnd.randrange(1001, 50001), cur, dv[0]))
            sd = max(d2 + timedelta(days=45), date(2025, 9, 1))
            sgap = missing_rate_days(cur, sd, sd + timedelta(days=60), 1, rnd) if cur != "USD" else []
            s1 = sgap[0] if sgap and rnd.random() < 0.5 else sd + timedelta(days=rnd.randrange(60))
            bk.sell(aid, s, s1, F(q1, 2), p * F(rnd.randrange(105, 160), 100), tie=True)
            if i % 3 == 0:
                bk.sell(aid, s, s1 + timedelta(days=rnd.randrange(1, 300)), F(q1, 2), p * F(rnd.randrange(105, 170), 100), tie=True)
        pend = plant_ties(bk, "fx", 0, 30)
        fixture_check(bk)
        bk.flush()
        finish_ties(bk, pend)
        ctx.shared["fx"] = bk
    with ctx.step("hand-entered buys and sales round quantity x price +/- fee to the cent first, then convert once"):
        bk = ctx.shared["fx"]
        nw, hd = list(bk.accts)[:2]
        cases = [("3", "10.005", "0"), ("7", "1.235", "0.00"), ("1", "99.995", "0.01"), ("12", "4.125417", "1.99"), ("0.5", "33.333",
                 "0"), ("2", "0.005", "0"), ("9", "70.0005", "4.95"), ("11", "13.1313", "0.05"), ("5", "2.501", "0.00"), ("4", "8.125", "0")]
        for k, (q, pr_, fee) in enumerate(cases):
            s = bk.secs[k]
            aid = hd if s["currency"] == "EUR" or k % 7 == 0 else nw
            day = date(2026, 9, 1) + timedelta(days=k)
            H.manual(bk.c, aid, {"kind": "buy", "trade_date": day.isoformat(), "security_id": s["sid"], "quantity": q, "price": pr_, "fee": fee,
                                 "memo": f"hand entry {k}"})
            H.manual(bk.c, aid, {"kind": "sell", "trade_date": (day + timedelta(days=40)).isoformat(), "security_id": s["sid"],
                                 "quantity": q, "price": E.qty_str(F(pr_) * 2 if k % 2 else F(pr_) + F(1, 1000)), "fee": fee})
    with ctx.step("a row dated before the first exchange rate (2022-12-01) rejects its file, naming that line"):
        bk = ctx.shared["fx"]
        nw = list(bk.accts)[0]
        cad = next(s for s in bk.secs if s["currency"] == "CAD")
        usd = next(s for s in bk.secs if s["currency"] == "USD")
        rows = [{"kind": "buy", "day": date(2022, 11, 30), "symbol": usd["symbol"], "isin": usd["isin"], "name": usd["name"], "currency": "USD",
                 "quantity": F(1), "amount": 1000, "ref": H.ref("w08x")},
                {"kind": "buy", "day": date(2022, 11, 30), "symbol": cad["symbol"], "isin": cad["isin"], "name": cad["name"], "currency": "CAD",
                 "quantity": F(1), "amount": 1000, "ref": H.ref("w08x")}]
        data, nums = ST.northwind(H.hh[H1].accounts[nw].number, rows, rnd)
        r = H.upload(bk.c, nw, "too-early.csv", data)
        st, n, dup, lines = H.import_status(r)
        ctx.check(r.status == 422 and st == "rejected" and lines == [nums[1]], f"no-rate row: {r.status} {st} {lines} want [{nums[1]}]")
        lots0 = H.lots(bk.c, nw)
        for kind, extra in (("buy", {"quantity": "2", "price": "10.00", "fee": "0"}), ("dividend", {"amount": "12.34"})):
            r = H.manual(bk.c, nw, {"kind": kind, "trade_date": "2022-11-30", "security_id": cad["sid"], **extra}, expect_ok=False)
            ctx.eq(r.status, 422, f"a hand-entered {kind} dated before the first CAD rate (R3.4, R5.1)")
        ctx.eq(H.lots(bk.c, nw), lots0, "lots after refused entries")
    compare_step(ctx, ctx.shared["fx"])


# ======================================================================================== W14
@workflow("W14", "Lot methods: FIFO, HIFO with ties, per-account effective-dated method changes", 4, timeout=2400, critical=True)
def w14(ctx):
    rnd = random.Random(909)
    with ctx.step("three accounts (FIFO, HIFO, and one switching methods four times) with 36 securities"):
        bk = Book("W14", H1, "daniel")
        a, b, c = bk.account("northwind", "FIFO"), bk.account("harbor", "HIFO"), bk.account("northwind", "FIFO")
        ctx.shared["methods"] = (bk, a, b, c)
        sale_days = [date(2025, 5, 30), date(2025, 5, 31), date(2025, 6, 1), date(2025, 9, 12), date(2025, 9, 15), date(2026, 2, 1),
                     date(2026, 2, 2), date(2026, 3, 10)]
        for i in range(36):
            aid = [a, b, c][i % 3]
            s = bk.security(["USD", "EUR", "CAD"][i % 3] if aid == b else ["USD", "CAD"][i % 2])
            buys = sorted(rday(rnd, date(2025, 1, 6), date(2025, 4, 15)) for _ in range(5))
            base = price(rnd, 2000, 20000)
            qtys = [rnd.choice([4, 6, 8, 10, 12, 20]) for _ in buys]
            for k, (dd, q) in enumerate(zip(buys, qtys)):
                pk = base * F(rnd.randrange(70, 130), 100)
                if k == 3:  # the same basis per share as the first lot: HIFO falls back to FIFO order
                    pk = F(bk.rows[aid][-3]["amount"], 100) / bk.rows[aid][-3]["quantity"]
                bk.buy(aid, s, dd, q, pk)
            held = sum(qtys)
            for dd in sorted(rnd.sample(sale_days, 3)):
                q = rnd.choice([3, 5, 7, 9])
                if q > held:
                    break
                bk.sell(aid, s, dd, q, base * F(rnd.randrange(140, 200), 100), tie=rnd.random() < 0.3)
                held -= q
        pend = plant_ties(bk, "share", 0, 20)
        fixture_check(bk)
        bk.flush()
        finish_ties(bk, pend)
    with ctx.step("account 3 records HIFO from 2025-06-01, FIFO from 2025-09-15, then FIFO and HIFO both from 2026-02-02"):
        bk, a, b, c = ctx.shared["methods"]
        for m, eff in (("HIFO", date(2025, 6, 1)), ("FIFO", date(2025, 9, 15)), ("FIFO", date(2026, 2, 2)), ("HIFO", date(2026, 2, 2))):
            r = H.change_method(bk.c, c, m, eff)
            ctx.check(r.status < 400, f"recording {m} from {eff} answered {r.status}")
        n = H.q("SELECT count(*) AS n FROM lot_method_changes WHERE account_id = %s", (c,))[0]["n"]
        ctx.eq(n, 4, "lot_method_changes rows for the account")
    with ctx.step("a change effective before existing sales re-matches them: account 1 switches to HIFO from 2025-09-13"):
        bk, a, b, c = ctx.shared["methods"]
        r = H.change_method(bk.c, a, "HIFO", date(2025, 9, 13))
        ctx.check(r.status < 400, f"recording HIFO for account 1 answered {r.status}")
    compare_step(ctx, ctx.shared["methods"][0])


# ======================================================================================== W15
@workflow("W15", "Specific-lot sales: chosen lots by form and in the browser, portion order, refusals", 4, timeout=2400, critical=True)
def w15(ctx):
    rnd = random.Random(1010)
    br = None
    try:
        with ctx.step("two accounts with 20 securities of four or five lots each"):
            bk = Book("W15", H1, "priya")
            a, b = bk.account("northwind", "FIFO"), bk.account("harbor", "HIFO")
            for i in range(20):
                aid = a if i % 2 == 0 else b
                s = bk.security(["USD", "EUR", "CAD", "USD"][i % 4])
                for k in range(rnd.choice([4, 5])):
                    bk.buy(aid, s, date(2025, 2, 3) + timedelta(days=17 * k + i % 5), rnd.choice([5, 8, 10, 12, F("7.5")]),
                           price(rnd, 3000, 9000), tie=k % 2 == 0)
            pend = plant_ties(bk, "share", 20, 40)
            fixture_check(bk)
            bk.flush()
            finish_ties(bk, pend)
            ctx.shared["speclots"] = (bk, a, b)
        with ctx.step("16 sales name lots and partial quantities; each takes exactly the named shares, whatever the method"):
            bk, a, b = ctx.shared["speclots"]
            ids = {**H.map_ids(bk.c, a), **H.map_ids(bk.c, b)}
            h = H.hh[H1]
            for i, s in enumerate(bk.secs[:16]):
                aid = a if i % 2 == 0 else b
                lots = [l for l in h.open_lots(H.rates, aid=aid, sids={s["sid"]})]
                ctx.check(all(l.acq in ids for l in lots), "could not find the application's ids for the lots (data-txn-id/data-ref)")
                chosen = rnd.sample(lots, min(len(lots), rnd.choice([1, 2, 3])))
                fields = {"kind": "sell", "trade_date": (date(2025, 9, 1) + timedelta(days=i * 3)).isoformat(), "security_id": s["sid"],
                          "price": E.qty_str(price(rnd, 6000, 15000)), "fee": rnd.choice(["0", "4.95"])}
                tot = F(0)
                for l in chosen:
                    q = l.qty if rnd.random() < 0.5 else l.qty / 2
                    fields[f"lot_{ids[l.acq]}"] = E.qty_str(q)
                    tot += q
                fields["quantity"] = E.qty_str(tot)
                H.manual(bk.c, aid, fields, query=f"?kind=sell&security_id={s['sid']}")
        with ctx.step("named lots that do not add up, belong to another account or household, or lack the shares are refused"):
            bk, a, b = ctx.shared["speclots"]
            ids = {**H.map_ids(bk.c, a), **H.map_ids(bk.c, b)}
            h = H.hh[H1]
            s = bk.secs[16]
            lots = h.open_lots(H.rates, aid=a, sids={s["sid"]})
            other = h.open_lots(H.rates, aid=b, sids={bk.secs[17]["sid"]})
            h2_buy = next(t for t in H.hh[H2].txns if t.kind == "buy")
            base = {"kind": "sell", "trade_date": "2025-12-01", "security_id": s["sid"], "price": "50", "fee": "0"}
            bad_cases = [  # R3.4/R6.6: a named lot of another account or household, or without the shares, is refused with 422
                {**base, "quantity": "2", f"lot_{ids[other[0].acq]}": "2"},
                {**base, "quantity": E.qty_str(lots[0].qty + 1), f"lot_{ids[lots[0].acq]}": E.qty_str(lots[0].qty + 1)},
                {**base, "quantity": "1", f"lot_{h2_buy.tid}": "1"},
            ]
            before = H.lots(bk.c, a)
            for fields in bad_cases:
                r = H.manual(bk.c, a, fields, query=f"?kind=sell&security_id={s['sid']}", expect_ok=False)
                ctx.check(r.status == 422, f"a refused lot choice answered {r.status}: {fields}")
            after = H.lots(bk.c, a)
            if any(x[0] == H.hh[H2].securities[h2_buy.security].symbol for x in after):
                ctx.breach("X1", "a sale took a lot of another household")
            ctx.eq(sorted(after), sorted(before), "lots after refused sales")
        with ctx.step("in a real browser, the sale form lists the lots; choosing two of them records exactly that sale"):
            bk, a, b = ctx.shared["speclots"]
            s = bk.secs[18]
            ids = H.map_ids(bk.c, a)
            lots = H.hh[H1].open_lots(H.rates, aid=a, sids={s["sid"]})
            br = B.Browser()
            cx = br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in bk.c.cookies.items()])
            page = bpage(br, cx)
            page.goto(H.base + f"/accounts/{a}/transactions/new?kind=sell&security_id={s['sid']}")
            page.wait_for_load_state()
            B.capture(page, "sell_form", {"lots_listed": len(lots), "symbol": s["symbol"]}, focus=["form"])
            form = form_of(page, "record-transaction")
            for l in lots:
                ctx.check(form.locator(f"input[name='lot_{ids[l.acq]}']").count() >= 1, f"no input lot_{ids[l.acq]} in the sale form")
            pick = [lots[-1], lots[1]]
            qs = [lots[-1].qty, lots[1].qty / 2]
            form.locator("[name=trade_date]").first.fill("2025-12-15")
            sel = form.locator("select[name=kind]")
            if sel.count():
                sel.first.select_option("sell")
            ss = form.locator("select[name=security_id]")
            if ss.count():
                ss.first.select_option(str(s["sid"]))
            form.locator("[name=quantity]").first.fill(E.qty_str(sum(qs, F(0))))
            form.locator("[name=price]").first.fill("88.80")
            form.locator("[name=fee]").first.fill("1.25")
            for l, q in zip(pick, qs):
                form.locator(f"input[name='lot_{ids[l.acq]}']").first.fill(E.qty_str(q))
            form.locator("[data-action=record-transaction]").first.click()
            page.wait_for_load_state()
            fields = {"kind": "sell", "trade_date": "2025-12-15", "security_id": s["sid"], "quantity": E.qty_str(sum(qs, F(0))),
                      "price": "88.80", "fee": "1.25"}
            t = E.Txn(0, 0, a, s["sid"], "sell", date(2025, 12, 15), sum(qs, F(0)), E.rhu((sum(qs, F(0)) * F("88.80") - F("1.25")) * 100))
            t.selection = [(l.acq, q) for l, q in zip(pick, qs)]
            t.tid = t.seq = H.next_seq()
            H.hh[H1].txns.append(t)
            H.hh[H1].touch()
            del fields
        compare_step(ctx, ctx.shared["speclots"][0])
    finally:
        sl = ctx.shared.get("speclots")
        if sl and not (B.UX_DIR / "sell_form" / "truth.json").exists():  # evidence even when an earlier step failed
            try:
                bk, a, _b = sl
                s = bk.secs[18]
                br = br or B.Browser()
                p = bpage(br, br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in bk.c.cookies.items()]))
                try_capture(p, f"/accounts/{a}/transactions/new?kind=sell&security_id={s['sid']}", "sell_form",
                            {"symbol": s["symbol"]}, ["form"])
            except Exception:  # noqa: BLE001 - evidence is best effort
                pass
        B.mark_unreached(["sell_form"])
        if br:
            br.close()


# ======================================================================================== W16
@workflow("W16", "Holding periods and splits: anniversaries, February 29, forward and reverse splits", 4, timeout=2400, critical=True)
def w16(ctx):
    rnd = random.Random(1111)
    with ctx.step("32 securities: sales on, before and after anniversaries, leap days, and splits from 10:1 to 1:25"):
        bk = Book("W16", H1, "priya")
        nw, hd = bk.account("northwind"), bk.account("harbor", "HIFO")
        for i in range(12):  # anniversaries
            aid = nw if i % 2 else hd
            s = bk.security(["USD", "EUR", "CAD"][i % 3])
            d0 = rday(rnd, date(2024, 3, 1), date(2025, 4, 30))
            ann = date(d0.year + 1, d0.month, d0.day) if not (d0.month == 2 and d0.day == 29) else date(d0.year + 1, 2, 28)
            q = rnd.choice([6, 10, 12])
            p = price(rnd, 2000, 9000)
            bk.buy(aid, s, d0, q, p, tie=True)
            bk.buy(aid, s, d0 + timedelta(days=rnd.choice([1, 2, 7])), 4, p * F(11, 10))
            for k, delta in enumerate((-1, 0, 1)):
                bk.sell(aid, s, ann + timedelta(days=delta), [3, 3, q - 6 + 4][k], p * F(rnd.randrange(120, 180), 100))
        for i, (bday, sdays) in enumerate([(date(2024, 2, 29), [date(2025, 2, 28), date(2025, 3, 1)]),
                                           (date(2024, 2, 29), [date(2025, 3, 1), date(2025, 2, 27)]),
                                           (date(2024, 2, 28), [date(2025, 2, 28), date(2025, 3, 1)]),
                                           (date(2025, 2, 28), [date(2026, 2, 28), date(2026, 3, 1)])]):
            s = bk.security("USD" if i % 2 else "EUR")
            aid = nw if s["currency"] == "USD" else hd
            bk.buy(aid, s, bday, 10, "41.25")
            for k, sd in enumerate(sorted(sdays)):
                bk.sell(aid, s, sd, 5, F("55.5") + k)
        ratios = [(2, 1), (3, 1), (10, 1), (1, 2), (1, 4), (1, 5), (1, 8), (1, 20), (1, 25), (1, 16), (4, 1), (20, 1), (1, 10), (5, 1),
                  (1, 50), (1, 8)]
        for i, ratio in enumerate(ratios):
            aid = nw if i % 2 == 0 else hd
            s = bk.security(["USD", "CAD", "EUR"][i % 3])
            d0 = rday(rnd, date(2024, 9, 2), date(2025, 2, 28))
            p = price(rnd, 1000, 40000)
            q1, q2 = rnd.choice([7, 15, 33, F("12.25"), 101]), rnd.choice([3, F("0.5"), 9])
            bk.buy(aid, s, d0, q1, p, tie=True)
            bk.buy(aid, s, d0 + timedelta(days=20), q2, p * F(rnd.randrange(80, 120), 100))
            sp = d0 + timedelta(days=rnd.randrange(60, 120))
            bk.split(aid, s, sp, ratio)
            mult = F(ratio[0], ratio[1])
            bk.sell(aid, s, sp + timedelta(days=rnd.choice([35, 200, 400])), (F(q1) + F(q2)) * mult * rnd.choice([F(1, 2), F(1, 4), F(3, 4)]),
                    p / mult * F(rnd.randrange(130, 160), 100))
            if i % 4 == 0:  # a second split on the same lots
                bk.split(aid, s, sp + timedelta(days=450), (2, 1))
        pend = plant_ties(bk, "split", 0, 24)
        fixture_check(bk)
        bk.flush()
        finish_ties(bk, pend)
        ctx.shared["splits"] = bk
    compare_step(ctx, ctx.shared["splits"])


# ======================================================================================== W17
@workflow("W17", "Transfers between accounts: whole positions, carried basis and dates, same-day order, refusals", 3, timeout=2400, critical=True)
def w17(ctx):
    rnd = random.Random(1212)
    with ctx.step("24 securities bought in two Northwind accounts, then moved whole to Harbor or the other account"):
        bk = Book("W17", H1, "daniel")
        n1, n2, hd = bk.account("northwind", "FIFO"), bk.account("northwind", "HIFO"), bk.account("harbor", "FIFO")
        plan = []
        for i in range(24):
            src = n1 if i % 2 == 0 else n2
            dst = hd if i % 3 else (n2 if src == n1 else n1)
            s = bk.security(["USD", "CAD", "USD", "EUR"][i % 4])
            T = rday(rnd, date(2025, 6, 2), date(2025, 9, 30))
            p = price(rnd, 2000, 30000)
            for k in range(rnd.choice([2, 3, 4])):
                bk.buy(src, s, rday(rnd, date(2024, 6, 3), T - timedelta(days=40)), rnd.choice([3, 7, 10, F("2.5")]), p * F(rnd.randrange(80, 120), 100))
            if i % 4 == 0:
                bk.buy(src, s, T, 2, p)  # bought on the transfer day: it moves too (buys come before transfers)
            plan.append((s, src, dst, T, p))
        pend = plant_ties(bk, "transfer", 0, 20)
        fixture_check(bk)
        bk.flush()
        finish_ties(bk, pend)
        ctx.shared["transfers"] = (bk, plan)
    with ctx.step("each transfer is recorded; the destination then sells, some on the transfer day itself"):
        bk, plan = ctx.shared["transfers"]
        for s, src, dst, T, p in plan:
            H.transfer(bk.c, H1, src, dst, s["sid"], T)
        for i, (s, src, dst, T, p) in enumerate(plan):
            held = sum((l.qty for l in H.hh[H1].open_lots(H.rates, aid=dst, sids={s["sid"]})), F(0))
            q = min(held, F(rnd.choice([2, 3, 5])))
            bk.sell(dst, s, T if i % 5 == 0 else T + timedelta(days=rnd.randrange(1, 300)), q, p * F(rnd.randrange(110, 170), 100))
            if i % 6 == 1:
                bk.buy(src, s, T + timedelta(days=60), 4, p)  # the emptied source buys again
        fixture_check(bk)
        bk.flush()
    with ctx.step("a transfer of a security the account does not hold, or to the same account, is refused"):
        bk, plan = ctx.shared["transfers"]
        s, src, dst, T, p = plan[0]
        lots0 = H.lots(bk.c, dst)
        tr0 = {a: sum(1 for x in H.app_txn_ids(bk.c, a) if x["kind"] == "transfer") for a in (src, dst)}
        r1 = H.transfer(bk.c, H1, src, dst, s["sid"], T - timedelta(days=500), expect_ok=False)
        r2 = H.transfer(bk.c, H1, dst, dst, s["sid"], T + timedelta(days=1), expect_ok=False)
        ctx.eq(r1.status, 422, "a transfer that would be short (R3.5)")
        ctx.check(400 <= r2.status < 500 or r2.status in REDIRECTS, f"a transfer to the same account answered {r2.status}")
        ctx.eq(H.lots(bk.c, dst), lots0, "lots after refused transfers")
        tr1 = {a: sum(1 for x in H.app_txn_ids(bk.c, a) if x["kind"] == "transfer") for a in (src, dst)}
        ctx.eq(tr1, tr0, "transfers listed on the accounts after refused transfers")
    compare_step(ctx, ctx.shared["transfers"][0])


# ======================================================================================== W18
@workflow("W18", "Dividends and reinvestments: income by year, weekend pay dates, reinvested lots", 3, timeout=2400, critical=True)
def w18(ctx):
    rnd = random.Random(1313)
    with ctx.step("30 securities pay cash dividends (many on days without a rate) and reinvest some of them"):
        bk = Book("W18", H1, "priya")
        nw, hd = bk.account("northwind"), bk.account("harbor")
        for i in range(30):
            cur = ["USD", "CAD", "EUR"][i % 3]
            aid = hd if cur == "EUR" else nw
            s = bk.security(cur)
            p = price(rnd, 1500, 12000)
            bk.buy(aid, s, rday(rnd, date(2025, 1, 6), date(2025, 2, 28)), rnd.choice([40, 100, 250]), p)
            pays = [date(2025, 12, 31), date(2026, 1, 1)] if i % 5 == 0 else []
            pays += missing_rate_days(cur, date(2025, 3, 1), date(2026, 8, 31), 4, rnd) if cur != "USD" else \
                [rday(rnd, date(2025, 3, 1), date(2026, 8, 31)) for _ in range(4)]
            for pd in sorted(pays):
                amt = tie_amount(rnd.randrange(501, 40001), cur, pd)
                bk.div(aid, s, pd, amt)
                if i % 2 == 0:
                    bk.row(aid, s, "reinvest", pd, F(rnd.randrange(100, 99999), 10000), amt)
            if i % 3 == 0:
                bk.sell(aid, s, date(2026, 9, 21) + timedelta(days=i % 7), 30, p * F(3, 2))
        pend = plant_ties(bk, "dividend", 0, 17)
        pend += plant_ties(bk, "fx", 30, 40)
        fixture_check(bk)
        bk.flush()
        finish_ties(bk, pend)
        ctx.shared["divs"] = bk
    with ctx.step("a dividend entered by hand on a Sunday converts at the Friday rate"):
        bk = ctx.shared["divs"]
        for s in bk.secs[:6]:
            aid = list(bk.accts)[1] if s["currency"] == "EUR" else list(bk.accts)[0]
            H.manual(bk.c, aid, {"kind": "dividend", "trade_date": "2026-03-08", "security_id": s["sid"], "amount": "123.45"})
    compare_step(ctx, ctx.shared["divs"])


# ======================================================================================== W19
def wash_after(bk: Book, rnd: random.Random, accts: list[int], i: int) -> None:
    """A loss sale with replacement purchases after it: 30/31-day edges, partial and oversized replacements, splits and
    later sales of the replacement shares (tacked holding periods)."""
    cur = ["USD", "EUR", "CAD", "USD"][i % 4]
    s = bk.security(cur)
    a0 = accts[i % len(accts)]
    other = accts[(i + 1) % len(accts)]
    p = price(rnd, 3000, 20000)
    d0 = rday(rnd, date(2024, 9, 2), date(2025, 3, 31))
    q = rnd.choice([10, 12, 15, 20, 9])
    bk.buy(a0, s, d0, q, p, tie=True)
    D = d0 + timedelta(days=rnd.choice([40, 120, 300, 330, 360]))
    sold = rnd.choice([q, q - 1, F(q, 2)])
    bk.sell(a0, s, D, sold, p * F(rnd.randrange(55, 90), 100), tie=True)
    kind = i % 8
    gap = [30, 31, 29, 1, 15, 30, 22, 8][kind]
    where = other if kind in (2, 5, 6) else a0
    rq = {0: sold, 1: sold, 2: F(sold) / 3, 3: F(sold) + 7, 4: F(sold) / 2, 5: F(sold) * 2, 6: 3, 7: F(sold) - 1}[kind]
    rq = max(F(1), F(int(rq)) if F(rq).denominator != 1 and kind != 4 else F(rq))
    bk.buy(where, s, D + timedelta(days=gap), rq, p * F(rnd.randrange(60, 95), 100), tie=True)
    if kind in (4, 7):  # a second replacement purchase further out
        bk.row(where, s, "buy", D + timedelta(days=gap + 5), F(rnd.choice([2, 5])), E.rhu(p * 300))
    if kind == 6:  # a split on the replacement lot, more than 30 days after the loss sale
        bk.split(where, s, D + timedelta(days=45), (2, 1))
        bk.sell(where, s, D + timedelta(days=120), 2, p * F(rnd.randrange(30, 70), 100) / 2)
    elif kind in (0, 3, 5):  # the replacement shares are sold later: the tacked holding period may make them long-term
        bk.sell(where, s, D + timedelta(days=gap + rnd.choice([40, 120, 200])), min(rq, F(3)), p * F(rnd.randrange(70, 140), 100))


@workflow("W19", "Wash sales I: replacements bought after the loss, edges at 30/31 days, partial matches, divided lots", 5, timeout=2400, critical=True)
def w19(ctx):
    rnd = random.Random(1414)
    with ctx.step("44 securities across three accounts and two brokers, one loss scenario each"):
        bk = Book("W19", H1, "priya")
        accts = [bk.account("northwind", "FIFO"), bk.account("northwind", "HIFO"), bk.account("harbor", "FIFO")]
        for i in range(44):
            wash_after(bk, rnd, accts, i)
        pend = plant_ties(bk, "wash", 0, 20)
        fixture_check(bk)
        bk.flush()
        finish_ties(bk, pend)
        ctx.shared["wash1"] = bk
        washed = [p for p in H.hh[H1].result(H.rates, bk.sids).portions if p.disallowed]
        ctx.observe("wash_portions", len(washed))
        ctx.check(len(washed) >= 25, f"verifier fixture: only {len(washed)} wash-sale portions")
    compare_step(ctx, ctx.shared["wash1"])


# ======================================================================================== W20
def wash_before(bk: Book, rnd: random.Random, accts: list[int], i: int) -> None:
    """Replacements bought before the loss (D-30 vs D-31), same-acquisition exclusion, reinvestments, shares used
    once, chains, mixed gain/loss sales and same-day buys."""
    cur = ["USD", "CAD", "EUR"][i % 3]
    s = bk.security(cur)
    a0, a1 = accts[i % len(accts)], accts[(i + 1) % len(accts)]
    p = price(rnd, 4000, 20000)
    D = rday(rnd, date(2025, 3, 3), date(2026, 1, 30))
    kind = i % 8
    if kind in (0, 1):  # an earlier lot bought 30 (or 31) days before a loss sale of a later, higher lot
        bk.buy(a0, s, D - timedelta(days=30 if kind == 0 else 31), 10, p)
        bk.buy(a0, s, D - timedelta(days=6), 10, p * F(13, 10))
        bk.sell(a0, s, D, 6, p * F(9, 10), tie=True)          # HIFO accounts sell the later lot; FIFO the earlier
    elif kind == 2:  # the rest of the same purchase never replaces it
        bk.buy(a0, s, D - timedelta(days=10), 20, p)
        bk.sell(a0, s, D, 10, p * F(7, 10))
    elif kind == 3:  # a reinvestment in another account replaces part of the loss
        bk.buy(a0, s, D - timedelta(days=200), 12, p)
        bk.sell(a0, s, D, 12, p * F(6, 10), tie=True)
        amt = rnd.randrange(2001, 9001)
        bk.div(a1, s, D + timedelta(days=12), amt)
        bk.buy(a1, s, D - timedelta(days=90), 4, p)
        bk.row(a1, s, "reinvest", D + timedelta(days=12), F(rnd.randrange(1000, 30000), 10000), amt)
    elif kind == 4:  # one replacement purchase, two loss sales: its shares serve once
        bk.buy(a0, s, D - timedelta(days=100), 20, p)
        bk.sell(a0, s, D, 8, p * F(7, 10))
        bk.sell(a0, s, D + timedelta(days=5), 8, p * F(6, 10))
        bk.buy(a1, s, D + timedelta(days=10), 10, p * F(65, 100))
        bk.buy(a1, s, D + timedelta(days=33), 4, p * F(65, 100))
    elif kind == 5:  # a chain: the replacement is itself sold at a loss and replaced again
        bk.buy(a0, s, D - timedelta(days=150), 10, p)
        bk.sell(a0, s, D, 10, p * F(8, 10))
        bk.buy(a0, s, D + timedelta(days=3), 10, p * F(8, 10))
        bk.sell(a0, s, D + timedelta(days=20), 10, p * F(7, 10), tie=True)
        bk.buy(a1, s, D + timedelta(days=45), 6, p * F(7, 10))
        bk.sell(a1, s, D + timedelta(days=250), 3, p)
    elif kind == 6:  # one sale with a gain lot and a loss lot; only the loss is matched
        bk.buy(a0, s, D - timedelta(days=300), 5, p * F(5, 10))
        bk.buy(a0, s, D - timedelta(days=60), 5, p * F(14, 10))
        bk.sell(a0, s, D, 10, p)
        bk.buy(a1, s, D + timedelta(days=25), 3, p)
    else:  # a buy on the sale day comes before the sale
        bk.buy(a0, s, D - timedelta(days=80), 10, p)
        bk.buy(a0, s, D, 4, p * F(6, 10))
        bk.sell(a0, s, D, 10, p * F(6, 10))


@workflow("W20", "Wash sales II: earlier purchases, same-purchase shares, reinvestments, single use, chains", 5, timeout=2400, critical=True)
def w20(ctx):
    rnd = random.Random(1515)
    with ctx.step("48 securities across three accounts, one scenario each"):
        bk = Book("W20", H1, "daniel")
        accts = [bk.account("northwind", "HIFO"), bk.account("harbor", "FIFO"), bk.account("northwind", "FIFO")]
        for i in range(48):
            wash_before(bk, rnd, accts, i)
        pend = plant_ties(bk, "wash", 20, 40)
        fixture_check(bk)
        bk.flush()
        finish_ties(bk, pend)
        ctx.shared["wash2"] = bk
        washed = [p for p in H.hh[H1].result(H.rates, bk.sids).portions if p.disallowed]
        ctx.observe("wash_portions", len(washed))
        ctx.check(len(washed) >= 20, f"verifier fixture: only {len(washed)} wash-sale portions")
    compare_step(ctx, ctx.shared["wash2"])


# ======================================================================================== W21
def email_totals(text: str) -> dict:
    out = {}
    for key, label in (("short", "Short-term gains"), ("long", "Long-term gains"), ("disallowed", "Wash-sale loss disallowed"),
                       ("dividends", "Dividends"), ("adjustments", "Adjustments for earlier years")):
        m = re.search(r"^\s*" + re.escape(label) + r":\s*(-?\$[\d,]+\.\d\d)\s*$", text or "", re.M)
        out[key] = m.group(1) if m else None
    return out


def want_email(t: dict) -> dict:
    return {k: money_of(v) for k, v in t.items()}


def lock_emails(year: int, hid_people: list[str], before: dict, timeout: float = 35) -> dict:
    got = {}
    for who in hid_people:
        addr = PEOPLE[who]
        msgs = H.mail.wait_for(addr, lambda m: m["id"] not in before[who] and f"year-end summary {year}" in m["subject"].lower(), 1, timeout)
        got[who] = msgs
    return got


@workflow("W21", "Locked years and retroactive corrections: frozen reports, adjustments landing in the next year, emails", 5, timeout=1500, critical=True)
def w21(ctx):
    rnd = random.Random(1818)
    br = None
    try:
        with ctx.step("the Castellano household records 2023-2025 activity across two accounts and 18 securities"):
            bk = Book("W21", H3, "elena")
            nw, hd = bk.account("northwind", "FIFO"), bk.account("harbor", "FIFO")
            for i in range(18):
                cur = ["USD", "EUR", "CAD"][i % 3]
                aid = hd if cur == "EUR" else nw
                s = bk.security(cur)
                p = price(rnd, 2000, 15000)
                if i % 6 == 0:
                    bk.buy(aid, s, date(2023, 3, 6) + timedelta(days=i), 6, p)
                    bk.sell(aid, s, date(2023, 9, 4) + timedelta(days=i), 3, p * F(12, 10))
                bk.buy(aid, s, date(2024, 2, 5) + timedelta(days=i), 10, p, tie=True)
                bk.buy(aid, s, date(2024, 3, 11) + timedelta(days=i), 10, p * F(rnd.randrange(90, 130), 100))
                bk.sell(aid, s, date(2024, 8, 5) + timedelta(days=i), 8, p * F(rnd.randrange(80, 150), 100), tie=True)
                bk.div(aid, s, date(2024, 11, 16) + timedelta(days=i % 3), tie_amount(rnd.randrange(1001, 20001), cur, date(2024, 11, 16)))
                if i % 4 == 1:  # a December loss whose replacement comes in January
                    bk.sell(aid, s, date(2024, 12, 16), 4, p * F(6, 10))
                    bk.buy(aid, s, date(2025, 1, 6) + timedelta(days=i % 5), 3, p * F(6, 10))
                bk.sell(aid, s, date(2025, 6, 2) + timedelta(days=i), 5, p * F(rnd.randrange(80, 160), 100))
                bk.div(aid, s, date(2025, 11, 15), tie_amount(rnd.randrange(1001, 20001), cur, date(2025, 11, 15)))
            fixture_check(bk)
            bk.flush()
            ctx.shared["locks"] = bk
        with ctx.step("before locking, the 2024 page explains locking; the current year cannot be locked"):
            bk = ctx.shared["locks"]
            br = B.Browser()
            cx = br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in bk.c.cookies.items()])
            page = bpage(br, cx)
            ctx.shared["locks_page"] = page
            page.goto(H.base + "/households/3/reports/2024")
            page.wait_for_load_state()
            B.capture(page, "lock_before", {"year": 2024, "locked": False}, focus=["[data-action=lock-year]", "[data-year]"])
            ctx.check(page.locator("[data-action=lock-year]").count() >= 1, "no lock-year button on the 2024 page")
            # a year that has not ended (R8.3) when the request lands: from 23:00 UTC on 31 December the next year,
            # so the probe never races the UTC new year
            this_year = (datetime.now(timezone.utc) + timedelta(hours=1)).year
            tok = tokens(bk.c, "/households/3/reports/2024")
            r = bk.c.post_form(f"/households/3/reports/{this_year}/lock", tok)
            ctx.eq(r.status, 409, f"locking {this_year} before it has ended")
        with ctx.step("Elena locks 2024: its reports freeze, and each member gets one year-end summary"):
            bk = ctx.shared["locks"]
            before = {w: H.mail_ids(PEOPLE[w]) for w in ("elena", "marco", "lucia", "hannah")}
            r = H.lock(bk.c, H3, 2024)
            try_capture(ctx.shared["locks_page"], "/households/3/reports/2024", "report_locked", {"year": 2024, "locked": True},
                        ["[data-year]"])
            ctx.check(r.status in (200,) + REDIRECTS, f"locking 2024 answered {r.status}")
            H.hh[H3].lock(2024, H.rates)
            mails = lock_emails(2024, ["elena", "marco", "lucia"], before)
            if mails.get("elena"):
                m0 = mails["elena"][0]
                B.capture_text("summary_email", f"Subject: {m0['subject']}\n\n{m0['text']}", {"year": 2024})
            yp = H.year_page(bk.c, H3, 2024)
            ctx.eq(yp and yp["locked"], "true", "data-locked on the 2024 page")
            ctx.eq(len(H.mail_ids(PEOPLE["hannah"]) - before["hannah"]), 0, "emails to the household's preparer")
            want = want_email(H.hh[H3].totals(H.hh[H3].summary_rows(2024, H.rates)))
            for who, msgs in mails.items():
                ctx.eq(len(msgs), 1, f"2024 summaries for {who}")
                ctx.eq(email_totals(msgs[0]["text"]), want, f"2024 email totals for {who}")
                ctx.check(msgs[0]["subject"].rstrip().endswith("locked by Elena Castellano"), f"subject {msgs[0]['subject']!r}")
            # 2022 had no data: the first lock freezes it too (R8.3 "every earlier year"), empty
            ctx.shared["locks_frozen"] = {y: (H.year_csv(bk.c, H3, y, "summary")[0].body, H.year_csv(bk.c, H3, y, "realized")[0].body)
                                        for y in (2022, 2023, 2024)}
            report(ctx, H.compare_summary(bk.c, H3, 2024, bk.sids) + H.compare_realized(bk.c, H3, 2024, bk.sids, True), "2024 at lock")
        with ctx.step("late imports (a missed 2024 buy, a January replacement, a 2023 dividend) and a 2024 method change"):
            bk = ctx.shared["locks"]
            nw, hd = list(bk.accts)
            for i, s in enumerate(bk.secs[:12]):
                aid = hd if s["currency"] == "EUR" else nw
                if i % 3 == 0:
                    bk.buy(aid, s, date(2024, 1, 15) + timedelta(days=i), 4, price(rnd, 1000, 3000))
                elif i % 3 == 1:
                    bk.buy(aid, s, date(2025, 1, 2), 2, price(rnd, 1000, 3000))
                else:
                    bk.div(aid, s, date(2023, 12, 30), tie_amount(rnd.randrange(501, 5001), s["currency"], date(2023, 12, 30)))
            fixture_check(bk)
            bk.flush()
            r = H.change_method(bk.c, hd, "HIFO", date(2024, 6, 1))
            ctx.check(r.status < 400, f"method change answered {r.status}")
        with ctx.step("2023 and 2024 reports are unchanged byte for byte; 2025 carries the adjustments"):
            bk = ctx.shared["locks"]
            for y, (sb, rb) in ctx.shared["locks_frozen"].items():
                ctx.check(H.year_csv(bk.c, H3, y, "summary")[0].body == sb, f"{y} summary.csv changed after it was locked")
                ctx.check(H.year_csv(bk.c, H3, y, "realized")[0].body == rb, f"{y} realized.csv changed after it was locked")
            adj = H.hh[H3].adjustments(H.rates)
            ctx.check(len(adj) >= 6, f"verifier fixture: only {len(adj)} adjustments")
            report(ctx, H.compare_summary(bk.c, H3, 2025, bk.sids) + H.compare_realized(bk.c, H3, 2025, bk.sids), "2025 after late changes")
            t = H.hh[H3].totals(H.hh[H3].summary_rows(2025, H.rates))
            yp = H.year_page(bk.c, H3, 2025)
            ctx.eq(yp and yp["adjustment"], t["adjustments"], "2025 page adjustment total")
        with ctx.step("Marco, whose display name carries line breaks and a Bcc header, locks 2025; nothing leaks into headers"):
            bk = ctx.shared["locks"]
            mc = H.who("marco")
            evil = f"lb-{H.nonce.lower()}@{EVIL}"
            r = submit_form(mc, "/profile", "save-profile", {"full_name": f"Marco Castellano\r\nBcc: {evil}\r\nX-Injected: yes"}, follow=True)
            ctx.check(r.status < 400, f"saving a display name answered {r.status}")
            before = {w: H.mail_ids(PEOPLE[w]) for w in ("elena", "marco", "lucia")}
            r = H.lock(mc, H3, 2025)
            ctx.check(r.status in (200,) + REDIRECTS, f"locking 2025 answered {r.status}")
            H.hh[H3].lock(2025, H.rates)
            mails = lock_emails(2025, ["elena", "marco", "lucia"], before)
            time.sleep(3)
            # who actually received mail: the SMTP envelope recipients (the To header may legitimately repeat the name)
            leaked = [m for m in H.mail._get("/api/v1/messages").get("messages", [])
                      if evil in [str(x).strip().lower() for x in ((m.get("Envelope") or {}).get("To") or [])]]
            if leaked:
                ctx.breach("X13", f"a display name added a recipient to an email: {str(leaked[0].get('Subject'))[:80]!r}")
            ctx.check(not leaked, "an injected Bcc received the year-end summary")
            want = want_email(H.hh[H3].totals(H.hh[H3].summary_rows(2025, H.rates)))
            for who, msgs in mails.items():
                ctx.eq(len(msgs), 1, f"2025 summaries for {who}")
                ctx.eq(email_totals(msgs[0]["text"]), want, f"2025 email totals for {who}")
                ctx.check("\n" not in msgs[0]["subject"] and "x-injected" not in msgs[0]["subject"].lower().split("locked by")[0],
                          f"subject {msgs[0]['subject']!r}")
            submit_form(mc, "/profile", "save-profile", {"full_name": "Marco Castellano"}, follow=True)
        with ctx.step("later corrections to 2024 and 2025 land in 2026 net of what 2025 already carried"):
            bk = ctx.shared["locks"]
            nw, hd = list(bk.accts)
            for i, s in enumerate(bk.secs[12:18]):
                aid = hd if s["currency"] == "EUR" else nw
                bk.div(aid, s, date(2024, 12, 2), tie_amount(rnd.randrange(501, 5001), s["currency"], date(2024, 12, 2)))
                bk.buy(aid, s, date(2024, 1, 10), 3, price(rnd, 500, 2000))
            for s in bk.secs[:3]:
                aid = hd if s["currency"] == "EUR" else nw
                bk.div(aid, s, date(2025, 7, 1), tie_amount(rnd.randrange(501, 5001), s["currency"], date(2025, 7, 1)))
            for s in (bk.secs[3], bk.secs[9]):  # USD: a round trip dated 2022, a year frozen empty by the first lock
                bk.buy(nw, s, date(2022, 3, 7), 5, "40.00")
                bk.sell(nw, s, date(2022, 9, 12), 5, "47.31")
            fixture_check(bk)
            bk.flush()
            for y, (sb, rb) in ctx.shared["locks_frozen"].items():
                ctx.check(H.year_csv(bk.c, H3, y, "summary")[0].body == sb, f"{y} summary.csv changed after it was locked")
                ctx.check(H.year_csv(bk.c, H3, y, "realized")[0].body == rb, f"{y} realized.csv changed after it was locked")
            try_capture(ctx.shared["locks_page"], "/households/3/reports/2026", "report_year",
                        {"year": 2026, "locked": False, "has_adjustments": True}, ["[data-year]"])
            report(ctx, H.compare_summary(bk.c, H3, 2026, bk.sids) + H.compare_summary(bk.c, H3, 2025, bk.sids), "2026 adjustments")
    finally:
        # judge evidence never depends on the strict checks above (best effort, whatever state was reached)
        try:
            page = ctx.shared.get("locks_page")
            if page is None:  # the import step failed before the browser opened: nothing is locked yet
                el = H.who("elena")
                br = br or B.Browser()
                page = bpage(br, br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in el.cookies.items()]))
                try_capture(page, "/households/3/reports/2024", "lock_before", {"year": 2024, "locked": False},
                            ["[data-action=lock-year]", "[data-year]"])
            try_capture(page, "/households/3/reports/2026", "report_year", {"year": 2026, "locked": False}, ["[data-year]"])
        except Exception:  # noqa: BLE001 - evidence is best effort
            pass
        B.mark_unreached(["lock_before", "report_locked", "report_year", "summary_email"])
        if br:
            br.close()


# ======================================================================================== W22
@workflow("W22", "Locking under concurrency by an owner and a preparer: one lock, one summary per active household user, refusals", 3)
def w22(ctx):
    with ctx.step("the owner and the preparer lock 2024 at the same moment: exactly one succeeds"):
        a, j = H.who("astrid"), H.who("hannah")
        before = {w: H.mail_ids(PEOPLE[w]) for w in ("astrid", "johan", "ebba", "morgan", "priya", "daniel", "hannah")}
        ctx.shared["lockrace_before"] = before
        pages = [c.get("/households/2/reports/2024", follow=True) for c in (a, j)]
        outs = simultaneously([lambda c=c, p=p: submit_form(c, "", "lock-year", {}, page=p, follow=False) for c, p in zip((a, j), pages)])
        codes = [v.status if k == "ok" else None for k, v in outs]
        ok = [x for x in codes if x is not None and x < 400]
        if len(ok) > 1:
            ctx.breach("X6", "two lock requests for one year both succeeded")
        ctx.check(len(ok) == 1 and 409 in codes, f"lock statuses (owner, preparer) {codes}")
        ctx.shared["lockrace_winner"] = "Astrid Lindqvist" if codes[0] is not None and codes[0] < 400 else "Hannah Brooks"
        H.hh[H2].lock(2024, H.rates)
        ctx.eq((H.year_page(a, H2, 2024) or {}).get("locked"), "true", "data-locked")
    with ctx.step("each active member gets exactly one summary whose totals are the frozen summary's; nobody else gets one"):
        before = ctx.shared["lockrace_before"]
        mails = lock_emails(2024, ["astrid", "johan"], before)
        r, header, rows = H.year_csv(H.who("astrid"), H2, 2024, "summary")
        t = {"short": 0, "long": 0, "disallowed": 0, "dividends": 0, "adjustments": 0}
        for row in rows:
            t["short"] += int(row[2]); t["long"] += int(row[3]); t["disallowed"] += int(row[4]); t["dividends"] += int(row[5])  # noqa: E702
            t["adjustments"] += int(row[6]) + int(row[7]) + int(row[8])
        for who, msgs in mails.items():
            ctx.eq(len(msgs), 1, f"summaries for {who}")
            ctx.eq(email_totals(msgs[0]["text"]), want_email(t), f"email totals for {who}")
            ctx.check("Lindqvist" in msgs[0]["text"], "the email does not name the household")
            winner = ctx.shared.get("lockrace_winner", "")
            ctx.check(msgs[0]["subject"].rstrip().endswith(f"locked by {winner}"), f"subject {msgs[0]['subject']!r} (locked by {winner})")
        time.sleep(6)
        for who in ("astrid", "johan", "ebba", "morgan", "priya", "daniel", "hannah"):
            n = [m for m in H.mail.to(PEOPLE[who]) if m["id"] not in before[who] and "year-end summary" in m["subject"].lower()]
            want = 1 if who in ("astrid", "johan") else 0
            if who in ("astrid", "johan") and len(n) > 1:
                ctx.breach("X6", f"{who} received {len(n)} summaries for one lock")
            ctx.eq(len(n), want, f"year-end summaries to {who}")
    with ctx.step("locking out of order or twice is refused with 409 and sends nothing"):
        pr = H.who("priya")
        tok = tokens(pr, "/households/1/reports/2024")
        before = {w: H.mail_ids(PEOPLE[w]) for w in ("priya", "daniel")}
        r = pr.post_form("/households/1/reports/2025/lock", tok)
        ctx.eq(r.status, 409, "locking 2025 while 2024 is open")
        a = H.who("astrid")
        r = a.post_form("/households/2/reports/2024/lock", tokens(a, "/households/2/reports/2025"))
        ctx.eq(r.status, 409, "locking 2024 again")
        time.sleep(4)
        for w in ("priya", "daniel"):
            ctx.eq(len(H.mail_ids(PEOPLE[w]) - before[w]), 0, f"emails to {w} after refused locks")
        ctx.eq((H.year_page(pr, H1, 2025) or {}).get("locked"), "false", "household 1's 2025")


# ======================================================================================== W23
@workflow("W23", "CSV exports: exact headers, CRLF, quoting, order, spreadsheet-safe text columns", 2)
def w23(ctx):
    names = ['=HYPERLINK("http://x.invalid","y")', "+1+1 Fund", "-Minus Holdings", "@SUM(A1)", "\tTabbed Co", 'Comma, Quote "Inc"',
             "Line one\nLine two", "Plain Name Co"]
    accts = ["=cmd|' /C calc'!A0", "+Plus Account", '-2+3 "joint", main', "@Advisor view"]
    with ctx.step("accounts and securities whose names need quoting or neutralizing trade in 2026"):
        bk = Book("W23", H1, "priya")
        aids = [bk.account("northwind" if k % 2 else "harbor", "FIFO", name=nm) for k, nm in enumerate(accts)]
        for i, nm in enumerate(names):
            s = bk.security("USD", name=nm)
            aid = aids[i % len(aids)]
            bk.buy(aid, s, date(2026, 2, 2) + timedelta(days=i), 10, "20.00")["description"] = f"Statement line {i}"
            bk.sell(aid, s, date(2026, 6, 1) + timedelta(days=i % 3), 4, "26.13")["description"] = "Sale, partial"
            bk.sell(aid, s, date(2026, 6, 1) + timedelta(days=i % 3), 2, "19.07")["description"] = 'Sale "two"'
        fixture_check(bk)
        bk.flush()
        ctx.shared["csv"] = bk
    with ctx.step("realized.csv: exact header, CRLF everywhere, rows in order, text columns neutralized"):
        bk = ctx.shared["csv"]
        r, header, rows = H.year_csv(bk.c, H1, 2026, "realized")
        raw = r.body.decode("utf-8-sig")
        ctx.check(raw.startswith(",".join(M.REALIZED_HEADER) + "\r\n"), "the header row differs or does not end in CRLF")
        ctx.check(raw.endswith("\r\n") and "\n" not in raw.replace("\r\n", ""), "a line does not end in CRLF")
        syms = {s["symbol"] for s in bk.secs}
        got = [x for x in rows if len(x) > 2 and x[2].lstrip("'") in syms]
        for x in got:
            if x[1] and x[1][0] in "=+-@\t\r":
                ctx.breach("X4", f"realized.csv wrote the account name {x[1]!r} as a live formula")
        report(ctx, H.compare_realized(bk.c, H1, 2026, bk.sids, check_order=True), "realized order and content")
        ctx.check(all(re.fullmatch(r"-?\d+", x[i]) for x in got for i in (6, 7, 8, 9)), "amount columns are not plain integers")
    with ctx.step("summary.csv: rows by symbol, quoted names (commas, quotes, line breaks), neutralized formulas"):
        bk = ctx.shared["csv"]
        r, header, rows = H.year_csv(bk.c, H1, 2026, "summary")
        raw = r.body.decode("utf-8-sig")
        ctx.check(raw.startswith(",".join(M.SUMMARY_HEADER) + "\r\n"), "summary header differs")
        ctx.check('"Comma, Quote ""Inc"""' in raw and '"Line one\nLine two"' in raw, "names with commas, quotes or line breaks are not quoted")
        for x in rows:
            if x and x[0].lstrip("'") in {s["symbol"] for s in bk.secs} and x[1] and x[1][0] in "=+-@\t\r":
                ctx.breach("X4", f"summary.csv wrote the name {x[1]!r} as a live formula")
        report(ctx, H.compare_summary(bk.c, H1, 2026, bk.sids), "summary content")
        allsyms = [x[0].lstrip("'") for x in rows if x]
        ctx.eq(allsyms, sorted(allsyms), "summary rows ordered by symbol")


# ======================================================================================== W24
@workflow("W24", "In a real browser: stored and reflected markup shown as text, layout at 1280 and 390 px, uploads by click", 3, timeout=1500)
def w24(ctx):
    br = None
    try:
        with ctx.step("names, descriptions, memos, file names and a display name carrying script payloads exist"):
            bk = Book("W24", H1, "priya")
            aid = bk.account("northwind", "FIFO", name=B.payload("acct", 0))
            s1 = bk.security("USD", name=B.payload("secname", 1))
            s2 = bk.security("CAD", name="Plain Maple Co")
            bk.row(aid, s1, "buy", date(2026, 3, 2), 10, 100000, desc=B.payload("desc", 2))
            bk.row(aid, s2, "buy", date(2026, 3, 3), 5, 50000, desc=B.payload("desc2", 3))
            bk.flush()
            H.manual(bk.c, aid, {"kind": "buy", "trade_date": "2026-03-04", "security_id": s1["sid"], "quantity": "2", "price": "99.5",
                                 "fee": "0", "memo": B.payload("memo", 0)})
            row = bk.row(aid, s2, "sell", date(2026, 4, 1), 1, 12000)
            bk.rows[aid] = []
            data, _ = ST.northwind(H.hh[H1].accounts[aid].number, [row])
            want_new, _ = H.model_rows(aid, [row])
            r = H.upload(bk.c, aid, B.payload("file", 1).replace("/", "") + ".csv", data)
            if H.import_status(r)[0] == "imported":
                H.commit_rows(aid, want_new)
            submit_form(H.who("daniel"), "/profile", "save-profile", {"full_name": "Daniel " + B.payload("dname", 3)}, follow=True)
            ctx.shared["browser"] = (bk, aid, s1, s2)
        bk, aid, s1, s2 = ctx.shared["browser"]
        br = B.Browser()
        with ctx.step("uploading a statement by clicking records it and says how many rows were recorded and skipped"):
            # a browser holding the owner's session; judge evidence is captured before any assertion (shared rule 12)
            ucx = br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in bk.c.cookies.items()])
            up = bpage(br, ucx)
            ctx.shared["browser_upload"] = up
            row = bk.row(aid, s2, "buy", date(2026, 5, 4), 3, 30000)
            row2 = bk.row(aid, s2, "dividend", date(2026, 5, 9), None, 777)
            bk.rows[aid] = []
            data, _ = ST.northwind(H.hh[H1].accounts[aid].number, [row, row2])
            path = Path(os.environ.get("FK_UX_DIR", "/logs/verifier/ux")) / "upload-good.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            want_new, _ = H.model_rows(aid, [row, row2])
            up.goto(H.base + f"/accounts/{aid}")
            up.wait_for_load_state()
            upload = form_of(up, "import-statement")
            upload.locator("input[type=file]").first.set_input_files(str(path))
            upload.locator("[data-action=import-statement]").first.click()
            up.wait_for_load_state()
            B.capture(up, "import_done", {"recorded": 2, "duplicates": 0}, focus=["[data-import-status]"])
            st = up.locator("[data-import-status]").first
            imported = st.count() and st.get_attribute("data-import-status") == "imported"
            if imported:
                H.commit_rows(aid, want_new)
            ctx.check(imported, "the result page has no data-import-status=imported")
            ctx.eq(st.get_attribute("data-imported-count"), "2", "rows recorded")
            up.goto(H.base + f"/accounts/{aid}")
            upload = form_of(up, "import-statement")
            upload.locator("input[type=file]").first.set_input_files(str(path))
            upload.locator("[data-action=import-statement]").first.click()
            up.wait_for_load_state()
            ctx.eq(up.locator("[data-import-status]").first.get_attribute("data-duplicate-count"), "2", "duplicates on a second upload")
            bad = data.decode().replace("05/09/2026", "05/39/2026").encode()
            path.write_bytes(bad)
            up.goto(H.base + f"/accounts/{aid}")
            upload = form_of(up, "import-statement")
            upload.locator("input[type=file]").first.set_input_files(str(path))
            upload.locator("[data-action=import-statement]").first.click()
            up.wait_for_load_state()
            B.capture(up, "import_rejected", {"rejected": True, "reason": "line with the impossible date 05/39/2026"},
                      focus=["[data-import-status]"])
            ctx.eq(up.locator("[data-import-status]").first.get_attribute("data-import-status"), "rejected", "a bad file's status")
        try:
            w24_evidence(br, ctx, aid)
        except Exception:  # noqa: BLE001 - evidence is best effort and never fails the workflow
            pass
        with ctx.step("signing in through the browser form reaches a signed-in page"):
            cx = br.context()
            page = bpage(br, cx)
            page.goto(H.base + "/signin")
            form = form_of(page, "signin")
            form.locator("input[name=email]").first.fill(PEOPLE["priya"])
            form.locator("input[name=password]").first.fill(PASSWORD)
            form.locator("[data-action=signin]").first.click()
            page.wait_for_load_state()
            ctx.check(page.locator("[data-action=signout]").count() >= 1, "no sign-out button after browser sign-in")
            ctx.shared["browser_page"] = page
        page = ctx.shared["browser_page"]
        urls = ["/", "/households/1", f"/accounts/{aid}", f"/accounts/{aid}/lots", f"/accounts/{aid}/transactions/new?kind=sell&security_id={s1['sid']}",
                "/households/1/transfers", "/profile", "/households/3/reports/2026"]
        with ctx.step("pages stay within 1280 and 390 CSS pixels without horizontal scrolling"):
            ec = H.who("elena")
            ecx = br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in ec.cookies.items()])
            ep = bpage(br, ecx)
            for u in urls:
                p = ep if u.startswith("/households/3") else page
                for w in (1280, 390):
                    p.set_viewport_size({"width": w, "height": 900})
                    p.goto(H.base + u)
                    p.wait_for_load_state()
                    ov = B.horizontal_overflow(p)
                    ctx.check(ov <= 2, f"{u} overflows by {ov}px at {w}px")
            page.set_viewport_size({"width": 1280, "height": 900})
        with ctx.step("typed and uploaded text, and request parts echoed on error pages, never run as script"):
            evil = urllib.parse.quote(B.payload("reflect", 1), safe="")
            more = ["/households/1/securities", "/households/1/reports/2026", f"/households/1/nope{evil}", f"/nope?q={evil}",
                    f"/households/1/reports/2026?view={evil}", f"/accounts/{aid}?msg={evil}", f"/signin?next={evil}",
                    f"/households/1/reports/{evil}"]
            for u in urls + more:
                page.goto(H.base + u)
                page.wait_for_load_state()
                page.wait_for_timeout(200)
            anon = bpage(br, br.context())
            anon.goto(H.base + f"/signin?next={evil}")
            anon.goto(H.base + f"/password-reset/{evil}")
            dc = H.who("daniel")
            dcx = br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in dc.cookies.items()])
            dp = bpage(br, dcx)
            for u in ("/", f"/accounts/{aid}", "/households/1/reports/2026"):
                dp.goto(H.base + u)
                dp.wait_for_load_state()
            adv = H.who("morgan")
            acx = br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in adv.cookies.items()])
            ap = bpage(br, acx)
            for u in ("/households/1", f"/accounts/{aid}", f"/accounts/{aid}/lots", "/households/1/reports/2026"):
                ap.goto(H.base + u)
                ap.wait_for_load_state()
                ap.wait_for_timeout(200)
            if br.xss_hits:
                ctx.breach("X3", f"typed text ran as script: {sorted(set(br.xss_hits))[:5]}")
            ctx.check(not br.xss_hits, f"script ran from typed text: {sorted(set(br.xss_hits))[:5]}")
            ctx.shared["browser_adv"] = ap
            submit_form(H.who("daniel"), "/profile", "save-profile", {"full_name": "Daniel Okafor"}, follow=True)
    finally:
        try:  # judge evidence even when the first step failed: the advisor's view of account 1 then
            br = br or B.Browser()
            w24_evidence(br, ctx, ctx.shared.get("browser", (None, None))[1] or 1)
        except Exception:  # noqa: BLE001 - evidence is best effort
            pass
        B.mark_unreached(["import_done", "import_rejected", "lots_wash", "advisor_view"])
        if br:
            br.close()


def w24_evidence(br, ctx, aid) -> None:
    """Judge evidence for the wash-adjusted lots page and the advisor's view (never fails the workflow)."""
    # W19's first account; without it (W19's import failed), the legacy HIFO account 2, which holds wash-adjusted lots
    wash1 = ctx.shared.get("wash1")
    c, acct = (wash1.c, list(wash1.accts)[0]) if wash1 else (H.who("priya"), 2)
    pr = bpage(br, br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in c.cookies.items()]))
    try:
        marked = sum(1 for x in H.lots(c, acct) if x[5] == "true")
    except Exception:  # noqa: BLE001
        marked = None
    try_capture(pr, f"/accounts/{acct}/lots", "lots_wash", {"wash_adjusted_lots": marked}, ["[data-wash-adjusted=true]"])
    if aid:
        adv = H.who("morgan")
        ap = bpage(br, br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in adv.cookies.items()]))
        try_capture(ap, f"/accounts/{aid}", "advisor_view", {"role": "advisor", "read_only": True}, ["[data-account-id]"])


# ======================================================================================== W25
@workflow("W25", "Deployment: README, restart persistence, nothing re-sent, no secrets at rest, legacy records kept", 2)
def w25(ctx):
    with ctx.step("README.md and an executable start.sh are in the workspace"):
        ws = Path(H.workspace)
        ctx.check((ws / "README.md").is_file() and (ws / "README.md").stat().st_size > 40, "README.md missing")
        ctx.check(os.access(ws / "start.sh", os.X_OK), "start.sh is not executable")
    with ctx.step("after a restart: passwords, locked reports, imports and method changes are kept and no email is sent again"):
        el = H.who("elena")
        frozen = {y: (H.year_csv(el, H3, y, "summary")[0].body, H.year_csv(el, H3, y, "realized")[0].body) for y in (2024, 2025)}
        w06 = ctx.shared.get("nw")
        lots_before = H.lots(w06[0].c, w06[1]) if w06 else None
        n_before = len(H.mail.summaries())
        ctx.check(H.restart_app(), "the application did not answer /healthz after a restart")
        time.sleep(10)
        H.sessions.clear()
        c, _ = H.signin(PEOPLE["elena"])
        ctx.check(H.signed_in(c), "a password did not survive the restart")
        H.sessions["elena"] = c
        for y, (sb, rb) in frozen.items():
            sb2, rb2 = H.year_csv(c, H3, y, "summary")[0].body, H.year_csv(c, H3, y, "realized")[0].body
            if (sb2, rb2) != (sb, rb):
                ctx.breach("X6", f"locked {y} reports changed across a restart")
            ctx.check((sb2, rb2) == (sb, rb), f"locked {y} reports changed across the restart")
        if lots_before is not None:
            pc, _ = H.signin(PEOPLE["priya"])
            ctx.check(H.signed_in(pc), "Priya's password did not survive the restart")
            H.sessions["priya"] = pc
            ctx.eq(H.lots(pc, w06[1]), lots_before, "imported lots after the restart")
        report(ctx, H.compare_summary(c, H3, 2026, set(H.hh[H3].securities)), "2026 after the restart")
        ctx.eq(len(H.mail.summaries()), n_before, "emails sent because of the restart")
        seen: dict = {}
        for m in H.mail.summaries():
            if "year-end summary" in m["subject"].lower():
                for t in m["to"]:
                    key = (t, re.sub(r".*(year-end summary \d{4}).*", r"\1", m["subject"].lower()))
                    seen[key] = seen.get(key, 0) + 1
        dups = {k: v for k, v in seen.items() if v > 1}
        if dups:
            ctx.breach("X6", f"{len(dups)} year-end summaries were sent more than once")
        ctx.eq(dups, {}, "year-end summaries sent more than once")
    with ctx.step("no password is stored in plain text"):
        hits = []
        for t in H.q("SELECT table_schema, table_name, column_name FROM information_schema.columns WHERE table_schema NOT IN "
                     "('pg_catalog','information_schema') AND data_type IN ('text','character varying','jsonb','json','bytea','character')"):
            col = f'"{t["table_schema"]}"."{t["table_name"]}"'
            rows = H.q(f'SELECT 1 FROM {col} WHERE position(%s in "{t["column_name"]}"::text) > 0 LIMIT 1', (PASSWORD,))
            if rows:
                hits.append(f"{t['table_name']}.{t['column_name']}")
        if hits:
            ctx.breach("X8", f"plain-text password in {sorted(set(hits))}")
        ctx.check(not hits, f"plain-text password stored in {sorted(set(hits))}")
    with ctx.step("the pre-existing records are all still there, unchanged"):
        import snapshot as SNAP
        b = H.baseline
        changed = []
        for name, sql in SNAP.TABLES.items():
            if name in ("legacy_transactions", "fx_rates", "security_prices", "legacy_locked_figures"):
                continue
            cur = H.q(sql)
            keyf = (lambda r: r["id"]) if "id" in (b[name][0] if b[name] else {}) else (lambda r: tuple(sorted(r.items())))
            now = {keyf(r): r for r in cur}
            for r in b[name]:
                n = now.get(keyf(r))
                skip = {"full_name"} if name == "users" else set()
                if n is None or any(str(n[k]) != str(v) for k, v in r.items() if k not in skip):
                    changed.append(f"{name} {keyf(r)}")
        for name, sql in SNAP.HASHES.items():
            got = {k: int(v) for k, v in H.q(sql)[0].items()}
            if got != b["hashes"][name]:
                changed.append(name)
        if changed:
            ctx.breach("X10", f"pre-existing records changed: {changed[:8]}")
        ctx.check(not changed, f"pre-existing records changed: {changed[:8]}")


# ======================================================================================== W26
@workflow("W26", "Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start", 2, timeout=400)
def w26(ctx):
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
        restarted = subprocess.run([start], check=False, capture_output=True).returncode == 0 if stop and start else False
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


# ======================================================================================== W27
def db_secret_hits(secrets_to_find: list[str]) -> list[str]:
    """Text-like columns of every table (the application's and the pre-existing ones) that contain a secret."""
    pats = [f"%{x}%" for x in dict.fromkeys(secrets_to_find) if x and len(x) >= 8]
    hits = []
    if not pats:
        return hits
    for t in H.q("SELECT table_schema, table_name, column_name FROM information_schema.columns WHERE table_schema NOT IN "
                 "('pg_catalog','information_schema') AND data_type IN ('text','character varying','jsonb','json','bytea','character')"):
        col = f'"{t["table_schema"]}"."{t["table_name"]}"'
        rows = H.q(f'SELECT 1 FROM {col} WHERE "{t["column_name"]}"::text LIKE ANY(%s) LIMIT 1', (pats,))
        if rows:
            hits.append(f"{t['table_name']}.{t['column_name']}")
    return sorted(set(hits))


@workflow("W27", "Cross-cutting safety: secrets nowhere at rest or in logs, reset ends sessions, Host-proof links, headers, no stack traces", 3)
def w27(ctx):
    from kitlib import xcut
    from kitlib.mail import links
    addr = PEOPLE["lucia"]
    with ctx.step("setting a new password through a reset link signs out every existing session of that user"):
        c_old = H.account(addr)
        c_other, _ = H.signin(addr)
        ctx.check(H.signed_in(c_old) and H.signed_in(c_other), "could not sign in twice")
        ctx.check(H.set_password(addr, "Rotated-Pass-77"), "the reset flow failed")
        H.passwords.append("Rotated-Pass-77")
        survivors = [n for n, c in (("first", c_old), ("second", c_other)) if H.signed_in(c)]
        if survivors:
            ctx.breach("X13", f"{len(survivors)} session(s) still signed in after the password was reset")
        ctx.check(not survivors, f"sessions survived the password reset: {survivors}")
        c_new, _ = H.signin(addr, "Rotated-Pass-77")
        ctx.check(H.signed_in(c_new), "the new password does not sign in")
    with ctx.step("a reset link always starts with PUBLIC_BASE_URL, whatever Host header the request carried"):
        before = H.mail_ids(addr)
        c = H.client()
        page = c.get("/password-reset", headers={"Host": EVIL})
        try:
            fields = form_fields(find_form(page.dom, "request-reset")) + [("email", addr)]
        except Exception:
            fields = [("email", addr)]
        c.post_form("/password-reset", fields, headers={"Host": EVIL, "X-Forwarded-Host": EVIL})
        msgs = H.mail.wait_for(addr, lambda m: m["id"] not in before, 1, 35)
        ctx.check(msgs, "no reset email arrived")
        found = [l for m in msgs for l in links(m["text"] or m["html"]) if "/password-reset/" in l]
        H.reset_tokens += [l.rstrip("/").rsplit("/", 1)[-1] for l in found]
        bad = [l for l in found if not l.startswith(H.base + "/")]
        if bad:
            ctx.breach("X14", f"reset link built from the request's Host header: {bad[0][:80]}")
        ctx.check(found and not bad, f"reset links {found[:2]}")
    with ctx.step("signed-in pages and exports carry Cache-Control: no-store and an anti-framing header"):
        pr = H.who("priya")
        problems = []
        for path in ("/", "/households/1", "/households/1/securities", "/accounts/1", "/accounts/1/lots", "/accounts/1/transactions/new",
                     "/households/1/reports/2025", "/households/1/reports/2025/summary.csv", "/households/1/unrealized.csv", "/profile"):
            r = pr.get(path)
            ctx.check(r.status == 200, f"{path} answered {r.status}")
            problems += xcut.header_problems(r)
        problems += xcut.header_problems(H.client().get("/signin"), need_no_store=False)
        ctx.check(not problems, "; ".join(problems[:4]))
    with ctx.step("malformed requests get a plain error page without stack traces, SQL, or file paths"):
        pr = H.who("priya")
        tok = tokens(pr, "/households/1")
        sec = legacy_pick(H1)[0]
        probes = [
            pr.get("/households/99999999999999999999999"),
            pr.get("/accounts/99999999999999999999/lots"),
            pr.get("/households/1/reports/99999"),
            pr.get("/households/1/reports/2025?view=%27%20OR%201%3D1--"),
            pr.get(f"/accounts/1/transactions/new?kind=sell&security_id=%27%3B--"),
            pr.post_form("/accounts/1/transactions", tok + [("kind", "sell"), ("trade_date", "2026-02-30"), ("security_id", "1;DROP TABLE x"),
                                                          ("quantity", "1e99"), ("price", "NaN"), ("fee", "-0"), ("lot_99999999999999999999", "1")]),
            pr.post_form("/accounts/1/transactions", tok + [("kind", "buy"), ("trade_date", "never"), ("security_id", str(sec.sid)),
                                                          ("quantity", "' OR 1=1--"), ("price", "1"), ("fee", "")]),
            pr.post_form("/households/1/transfers", tok + [("from_account_id", "x"), ("to_account_id", "99999999999999999999"),
                                                         ("security_id", "1"), ("trade_date", "2026-01-01")]),
            pr.post_form("/accounts/1/method", tok + [("lot_method", "LIFO"), ("effective_from", "9999-99-99")]),
            pr.request("POST", "/accounts/1/imports", body=b"--x\r\nnot multipart", headers={"Content-Type": "multipart/form-data; boundary=x"}),
            H.upload(pr, 1, "garbage.csv", b"\xff\xfe\x00\x01" * 200),
            H.client().get("/password-reset/" + "%00" * 3),
        ]
        problems = [p for r in probes for p in xcut.error_leaks(r)]
        ctx.check(not problems, "; ".join(problems[:4]))
    with ctx.step("no password, reset token or session identifier appears in the database, the app's files or its output"):
        H.find_session_names(H.who("priya"))  # the session cookie(s): the needles never depend on W02 having run
        secrets_to_find = list(dict.fromkeys(H.passwords + [t for t in H.reset_tokens if len(t) >= 16][-12:]
                                             + [v for v in H.session_secret_values() if len(v) >= 16][-12:]))
        ctx.observe("secrets_checked", len(secrets_to_find))
        hits = xcut.scan_files_for_secrets([H.workspace, os.environ.get("FK_APP_LOG", "")], secrets_to_find)
        db_hits = db_secret_hits(secrets_to_find)
        if hits or db_hits:
            ctx.breach("X8", f"secrets written by the application: {(hits + db_hits)[:3]}")
        ctx.check(not hits and not db_hits, f"secrets in files/output {hits[:3]} or database columns {db_hits[:3]}")

# ======================================================================================== corporate actions (R13)
CA_PATH = Path(__file__).resolve().parent / "reference" / "ca_ties.json"
CA_TIES: dict = {}
LABEL_TERMS = {"new_security": "new_security_id", "acquirer": "acquirer_security_id"}


def ca_ties(cat: str) -> list[dict]:
    if not CA_TIES:
        import json
        CA_TIES.update(json.loads(CA_PATH.read_text()))
    return CA_TIES.get(cat, [])


def ca_accounts(bk: Book) -> list[int]:
    if not getattr(bk, "ca_accts", None):
        bk.ca_accts = [bk.account("northwind", "FIFO", name=f"{bk.wid} CA FIFO"), bk.account("harbor", "FIFO", name=f"{bk.wid} CA FIFO 2"),
                       bk.account("northwind", "HIFO", name=f"{bk.wid} CA HIFO"),
                       bk.account("northwind", "FIFO", name=f"{bk.wid} IRA", kind="ira")]
    return bk.ca_accts


def form_terms(kind: str, terms: dict, sec_of) -> dict:
    """Engine terms (labels, decimal strings) as form fields (R13.11)."""
    out = {}
    for k, v in terms.items():
        if k in LABEL_TERMS:
            out[LABEL_TERMS[k]] = sec_of(v)["sid"] if v else None
        else:
            out[k] = F(v) if v is not None else None
    if kind == "merger" and not out.get("acquirer_security_id"):
        out["acquirer_security_id"] = None
        out["acquirer_fmv"] = None
    return out


def plant_ca(ctx, bk: Book, cat: str, lo: int = 0, hi: int | None = None) -> int:
    """Replay ca_ties.json scenarios: fresh securities per scenario, rows imported up to each action's date, then the
    action recorded through the form, then the rest. Returns the number of actions recorded."""
    accts = ca_accounts(bk)
    pending_rows, actions = [], []
    for sc in ca_ties(cat)[lo:hi]:
        labels = {r["s"] for r in sc["rows"]} | {a["s"] for a in sc["actions"]} | {
            v for a in sc["actions"] for k, v in a["terms"].items() if k in LABEL_TERMS and v}
        secs = {lab: bk.security(sc["currency"]) for lab in sorted(labels)}
        for r in sc["rows"]:
            pending_rows.append((d(r["day"]), E.KIND_RANK[r["kind"]], accts[r["a"]], secs[r["s"]], r))
        for a in sc["actions"]:
            actions.append((d(a["day"]), a, secs))
    actions.sort(key=lambda x: x[0])
    pending_rows.sort(key=lambda x: (x[0], x[1]))
    n = 0

    def flush_until(day: date | None):
        nonlocal pending_rows
        take = [x for x in pending_rows if day is None or x[0] <= day]
        pending_rows = [x for x in pending_rows if not (day is None or x[0] <= day)]
        for dd, _rk, aid, sec, r in take:
            bk.row(aid, sec, r["kind"], dd, F(r["qty"]) if r.get("qty") else None, r.get("amount"),
                   tuple(r["ratio"]) if r.get("ratio") else None)
        if take:
            fixture_check(bk)
            bk.flush()
    for day, a, secs in actions:
        flush_until(day)
        H.record_action(bk.c, bk.hid, a["kind"], secs[a["s"]]["sid"], day, form_terms(a["kind"], a["terms"], lambda lab: secs[lab]))
        n += 1
    flush_until(None)
    return n


def actions_listed_step(ctx, bk: Book) -> None:
    with ctx.step("the corporate-actions page lists every recorded action with its markers"):
        h = H.hh[bk.hid]
        page = bk.c.get(f"/households/{bk.hid}/corporate-actions", follow=True)
        ctx.check(page.status == 200, f"the corporate-actions page answered {page.status}")
        got = Counter((n.attrs.get("data-kind"), n.attrs.get("data-action-date"), n.attrs.get("data-symbol"))
                      for n in page.dom.attr_all("data-action-id"))
        want = Counter((a.kind, a.day.isoformat(), h.securities[a.security].symbol) for a in h.actions if a.security in bk.sids)
        ctx.check(not (want - got), f"actions missing from the page: {list((want - got).elements())[:4]}")


def refused_action(ctx, c: Client, hid: int, kind: str, sid: int, day: date, terms: dict, label: str,
                   ok: tuple = (422,)) -> None:
    before = H.q("SELECT count(*) AS n FROM corporate_actions")[0]["n"]
    page = c.get(f"/households/{hid}/corporate-actions", follow=True)
    n0 = len(page.dom.attr_all("data-action-id"))
    r = H.record_action(c, hid, kind, sid, day, terms, expect_ok=False)
    n1 = len(c.get(f"/households/{hid}/corporate-actions", follow=True).dom.attr_all("data-action-id"))
    ctx.check(r.status in ok, f"{label} (R13.11): expected {' or '.join(map(str, ok))}, got {r.status}")
    ctx.eq(n1, n0, f"actions listed after a refused action ({label})")
    ctx.eq(H.q("SELECT count(*) AS n FROM corporate_actions")[0]["n"], before, "legacy corporate_actions rows")


@workflow("W28", "Spin-offs: basis allocation, cash in lieu, carried holding periods, same-day splits, refusals", 5, timeout=3600,
          critical=True)
def w28(ctx):
    with ctx.step("34 spin-off scenarios: lots in FIFO and HIFO accounts, same-day splits, later sales of the new shares"):
        bk = Book("W28", H1, "priya")
        n = plant_ca(ctx, bk, "spinoff")
        ctx.shared["spinoffs"] = bk
        ctx.observe("actions", n)
    actions_listed_step(ctx, ctx.shared["spinoffs"])
    with ctx.step("spin-offs naming another household's security, the same security twice, another currency or a 9-decimal ratio are refused"):
        bk = ctx.shared["spinoffs"]
        usd = [x for x in bk.secs if x["currency"] == "USD"]
        cad = [x for x in bk.secs if x["currency"] != "USD"]
        # a USD security of the Lindqvist household, so only its household (not its currency) makes the action invalid
        other = min(s for s, x in H.hh[H2].securities.items() if x.currency == "USD")
        base = {"ratio": F("0.5"), "parent_fmv": F(40), "new_fmv": F(10), "cash_in_lieu_price": F(10)}
        day = date(2026, 6, 1)
        refused_action(ctx, bk.c, H1, "spinoff", usd[0]["sid"], day, {**base, "new_security_id": other}, "a new security of another household",
                       ok=(403, 422))  # R1.3 (403) or R13.11 (422)
        refused_action(ctx, bk.c, H1, "spinoff", usd[0]["sid"], day, {**base, "new_security_id": usd[0]["sid"]}, "the same security twice")
        if cad:
            refused_action(ctx, bk.c, H1, "spinoff", usd[0]["sid"], day, {**base, "new_security_id": cad[0]["sid"]}, "another currency")
        refused_action(ctx, bk.c, H1, "spinoff", usd[0]["sid"], day, {**base, "new_security_id": usd[1]["sid"], "ratio": "0.123456789"},
                       "a ratio with 9 decimals")
    with ctx.step("a viewer's or advisor's spin-off is refused with 403 and records nothing"):
        bk = ctx.shared["spinoffs"]
        usd = [x for x in bk.secs if x["currency"] == "USD"]
        n0 = len(bk.c.get("/households/1/corporate-actions", follow=True).dom.attr_all("data-action-id"))
        for who in ("asha", "morgan"):
            c = H.who(who)
            tok = tokens(c, "/households/1/corporate-actions") or tokens(c, "/")
            r = c.post_form("/households/1/corporate-actions", tok + [("kind", "spinoff"), ("security_id", str(usd[0]["sid"])),
                            ("action_date", "2026-06-01"), ("new_security_id", str(usd[1]["sid"])), ("ratio", "0.5"),
                            ("parent_fmv", "40"), ("new_fmv", "10"), ("cash_in_lieu_price", "10")])
            ctx.eq(r.status, 403, f"{who} POST of a corporate action")
        n1 = len(bk.c.get("/households/1/corporate-actions", follow=True).dom.attr_all("data-action-id"))
        if n1 != n0:
            ctx.breach("X1", "a viewer or advisor recorded a corporate action")
        ctx.eq(n1, n0, "actions after refused posts")
    compare_step(ctx, ctx.shared["spinoffs"])


@workflow("W29", "Cash-and-stock mergers: recognized gain capped at the cash, unrecognized losses, carried lots, cash-only sales", 5,
          timeout=3600, critical=True)
def w29(ctx):
    with ctx.step("30 merger scenarios: cash and stock, stock only, cash only, same-day buys of the target, later sales of the acquirer"):
        bk = Book("W29", H1, "daniel")
        n = plant_ca(ctx, bk, "merger")
        ctx.shared["mergers"] = bk
        ctx.observe("actions", n)
    actions_listed_step(ctx, ctx.shared["mergers"])
    with ctx.step("a merger that would leave a later sale of the target short, or a cash-only merger naming an acquirer, is refused"):
        bk = ctx.shared["mergers"]
        acct = ca_accounts(bk)[0]
        t, a = bk.security("USD"), bk.security("USD")
        bk.buy(acct, t, date(2025, 2, 3), 10, "30.00")
        bk.sell(acct, t, date(2025, 9, 1), 4, "35.00")
        fixture_check(bk)
        bk.flush()
        refused_action(ctx, bk.c, H1, "merger", t["sid"], date(2025, 6, 2),
                       {"acquirer_security_id": a["sid"], "shares_per_share": F("0.5"), "cash_per_share": F(2), "acquirer_fmv": F(60)},
                       "a merger before a sale of the target")
        refused_action(ctx, bk.c, H1, "merger", t["sid"], date(2025, 10, 6),
                       {"acquirer_security_id": a["sid"], "shares_per_share": F(0), "cash_per_share": F(31), "acquirer_fmv": None},
                       "a cash-only merger naming an acquirer")
    compare_step(ctx, ctx.shared["mergers"])


@workflow("W31", "Retirement accounts: an IRA account by form, no reporting, losses disallowed for good, transfers refused", 4,
          timeout=3600, critical=True)
def w31(ctx):
    with ctx.step("an IRA is created through the account form and shown with data-account-type=ira"):
        bk = Book("W31", H1, "priya")
        accts = ca_accounts(bk)
        page = bk.c.get("/households/1", follow=True)
        types: dict = {}
        for n in page.dom.attr_all("data-account-id"):  # the first element of each account that carries a type
            if n.attrs.get("data-account-type") and n.attrs.get("data-account-id") not in types:
                types[n.attrs.get("data-account-id")] = n.attrs.get("data-account-type")
        ctx.eq(types.get(str(accts[3])), "ira", "data-account-type of the new IRA")
        ctx.eq(types.get(str(accts[0])), "taxable", "data-account-type of a new taxable account")
        ctx.eq(H.q("SELECT account_type FROM accounts WHERE id = %s", (accts[3],))[0]["account_type"], "ira", "accounts.account_type")
        ctx.shared["ira"] = bk
    with ctx.step("26 scenarios: taxable loss sales replaced in the IRA (and sometimes a taxable account), IRA loss sales bought back, IRA dividends"):
        bk = ctx.shared["ira"]
        plant_ca(ctx, bk, "ira")
        h = H.hh[H1]
        hits = [p for p in h.result(H.rates, bk.sids).portions if p.disallowed and not p.ira]
        ctx.observe("disallowed_portions", len(hits))
        ctx.check(len(hits) >= 10, f"verifier fixture: only {len(hits)} disallowed portions")
    with ctx.step("a transfer between the IRA and a taxable account is refused with 422 and moves nothing"):
        bk = ctx.shared["ira"]
        accts = ca_accounts(bk)
        held = [l for l in H.hh[H1].open_lots(H.rates, aid=accts[3], sids=bk.sids)]
        ctx.check(held, "verifier fixture: the IRA holds nothing")
        sec = held[0].security
        lots0 = (H.lots(bk.c, accts[3]), H.lots(bk.c, accts[0]))
        r1 = H.transfer(bk.c, H1, accts[3], accts[0], sec, date(2026, 9, 1), expect_ok=False)
        r2 = H.transfer(bk.c, H1, accts[0], accts[3], sec, date(2026, 9, 1), expect_ok=False)
        ctx.eq((r1.status, r2.status), (422, 422), "transfers IRA -> taxable and taxable -> IRA")
        ctx.eq((H.lots(bk.c, accts[3]), H.lots(bk.c, accts[0])), lots0, "lots after refused transfers")
    compare_step(ctx, ctx.shared["ira"])


@workflow("W30", "Returns of capital: basis reduced per lot, excess as a gain, never below zero, same-day buys untouched", 4,
          timeout=3600)
def w30(ctx):
    with ctx.step("26 return-of-capital scenarios: one to three distributions, lots driven to zero, buys on the distribution date"):
        bk = Book("W30", H1, "priya")
        n = plant_ca(ctx, bk, "roc")
        ctx.shared["roc"] = bk
        ctx.observe("actions", n)
    actions_listed_step(ctx, ctx.shared["roc"])
    with ctx.step("a return of capital of 0 or with 7 decimals is refused"):
        bk = ctx.shared["roc"]
        x = bk.secs[0]
        refused_action(ctx, bk.c, H1, "return_of_capital", x["sid"], date(2026, 6, 1), {"amount_per_share": "0"}, "an amount of 0")
        refused_action(ctx, bk.c, H1, "return_of_capital", x["sid"], date(2026, 6, 1), {"amount_per_share": "0.1234567"},
                       "an amount with 7 decimals")
    compare_step(ctx, ctx.shared["roc"])


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
