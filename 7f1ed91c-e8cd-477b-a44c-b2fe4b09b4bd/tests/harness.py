"""Lotbook verifier harness: drives the submission through its interface contract and keeps the model."""

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
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # Reward Kit imports every file standalone

import psycopg  # noqa: E402

from kitlib.mail import Mail, links  # noqa: E402
from kitlib.web import Client, FormNotFound, Response, csrf_from, find_form, form_fields, submit_form  # noqa: E402
from kitlib.wf import InfraError, StepFailed  # noqa: E402
from reference import engine as E  # noqa: E402
from reference import model as M  # noqa: E402
from reference import statements as ST  # noqa: E402
from reference.engine import Txn  # noqa: E402

PASSWORD = "Correct-Horse-9"
PEOPLE = {
    "priya": "priya.raman@lotbook.example", "daniel": "daniel.okafor@lotbook.example",
    "astrid": "astrid.lindqvist@lotbook.example", "johan": "johan.lindqvist@lotbook.example",
    "ebba": "ebba.lindqvist@lotbook.example", "elena": "elena.castellano@lotbook.example",
    "marco": "marco.castellano@lotbook.example", "morgan": "morgan.ellis@ellis-advisory.example",
    "asha": "asha.okafor@lotbook.example", "hannah": "hannah@brooks-tax.example", "lucia": "lucia.castellano@lotbook.example",
    "lee": "old.preparer@brooks-tax.example",
}
H1, H2, H3 = 1, 2, 3
REDIRECTS = (301, 302, 303, 307, 308)
HTTP_TIMEOUT_S = 150.0  # well above the 60 s every page, export and import may take (R9.6)


def d(s: str) -> date:
    return date.fromisoformat(s)


class Harness:
    def __init__(self):
        e = os.environ
        self.base = e["PUBLIC_BASE_URL"].rstrip("/")
        self.db_url = e["DATABASE_URL"]
        self.mail = Mail(e["MAILSINK_HTTP_URL"])
        self.reset_ttl = int(e.get("RESET_LINK_TTL_SECONDS", "120"))
        self.import_max = int(e.get("IMPORT_MAX_BYTES", "180000"))
        self.restart_hook = e.get("FK_RESTART_APP", "")
        self.workspace = e.get("FK_WORKSPACE", "/app")
        self.nonce = secrets.token_hex(2).upper()
        self.rnd = random.Random(int(e.get("FK_SEED", "20261110")))
        self.baseline = self._baseline()
        self._load()
        self.sessions: dict[str, Client] = {}
        self.reset_tokens: list[str] = []    # plain tokens seen in emails (R11.6 at-rest scan)
        self.session_values: list[str] = []  # cookie values set at sign-in (R11.6 at-rest scan)
        self.session_pairs: list[tuple[str, str]] = []  # (cookie name, value) set at sign-in
        self.session_names: set[str] | None = None  # the cookie(s) whose removal signs a client out
        self.passwords: list[str] = [PASSWORD]
        self.ref_n = 0
        self.app_ids: dict[tuple[int, str], int] = {}  # (account, ref) -> the application's transaction id

    # -- baseline and model --------------------------------------------------------------------------------
    def _baseline(self) -> dict:
        path = os.environ.get("FK_BASELINE")
        if path and os.path.exists(path):
            with open(path) as fh:
                return json.load(fh)
        raise InfraError("baseline snapshot missing")

    def _load(self) -> None:
        b = self.baseline
        self.rates = E.Rates([(r["currency"], d(r["rate_date"]), Fraction(r["rate"]), r["quote"]) for r in b["fx_rates"]])
        self.fx_days = {(r["currency"], d(r["rate_date"])) for r in b["fx_rates"]}
        self.prices: dict[int, tuple[date, Fraction]] = {}
        for r in b["security_prices"]:
            day = d(r["price_date"])
            if r["security_id"] not in self.prices or self.prices[r["security_id"]][0] < day:
                self.prices[r["security_id"]] = (day, Fraction(r["close"]))
        self.hh: dict[int, M.Household] = {r["id"]: M.Household(r["id"], r["name"]) for r in b["households"]}
        for r in b["accounts"]:
            self.hh[r["household_id"]].accounts[r["id"]] = M.Account(r["id"], r["household_id"], r["name"], r["broker"],
                                                                     r["account_number"], r["lot_method"], r.get("account_type") or "taxable")
        self.acct_hh = {r["id"]: r["household_id"] for r in b["accounts"]}
        for r in b["securities"]:
            self.hh[r["household_id"]].securities[r["id"]] = M.Sec(r["id"], r["household_id"], r["symbol"], r["isin"], r["name"],
                                                                   r["currency"])
        for r in b["lot_method_changes"]:
            self.hh[self.acct_hh[r["account_id"]]].changes.append((r["account_id"], d(r["effective_from"]), r["id"], r["lot_method"]))
        self.max_change_id = max([r["id"] for r in b["lot_method_changes"]], default=0)
        for r in b["legacy_transactions"]:
            self.hh[self.acct_hh[r["account_id"]]].txns.append(M.legacy_txn(r))
        for r in b.get("corporate_actions", []):
            self.hh[r["household_id"]].actions.append(M.action_txn(r))
        self.action_seq = max([r["id"] for r in b.get("corporate_actions", [])], default=0)
        self.seq = max([r["id"] for r in b["legacy_transactions"]], default=0) + 1
        for r in b["legacy_year_locks"]:
            h = self.hh[r["household_id"]]
            h.lock_through = max(h.lock_through or 0, r["year"])
            h.legacy_lock = h.lock_through
        for h in self.hh.values():
            if h.legacy_lock:
                figs = {}
                for r in b["legacy_locked_figures"]:
                    if r["household_id"] == h.hid and r["year"] == h.legacy_lock:
                        figs[r["security_id"]] = E.Fig(r["short_term_gain_cents"], r["long_term_gain_cents"],
                                                       r["wash_disallowed_cents"], r["dividends_cents"], True)
                rows = sorted([[h.securities[s].symbol, h.securities[s].name, str(f.short), str(f.long), str(f.disallowed),
                                str(f.dividends), "0", "0", "0"] for s, f in figs.items()], key=lambda r: r[0])
                h.frozen[h.legacy_lock] = {"figs": figs, "summary": rows, "realized": None}
            h.touch()

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def q(self, sql: str, args=()):
        with psycopg.connect(self.db_url, connect_timeout=10) as c:
            cur = c.execute(sql, args)
            cols = [x.name for x in cur.description] if cur.description else []
            return [dict(zip(cols, r)) for r in cur.fetchall()] if cols else []

    # -- accounts and sessions ------------------------------------------------------------------------------
    def client(self) -> Client:
        return Client(self.base, timeout=HTTP_TIMEOUT_S)

    def request_reset(self, email: str, client: Client | None = None) -> Response:
        return submit_form(client or self.client(), "/password-reset", "request-reset", {"email": email}, follow=True)

    def mail_ids(self, email: str) -> set:
        return {m["id"] for m in self.mail.to(email.strip().lower())}

    def reset_link(self, email: str, since: set, timeout: float = 35.0) -> str | None:
        msgs = self.mail.wait_for(email.strip().lower(), lambda m: m["id"] not in since, 1, timeout)
        for m in reversed(msgs):
            for l in links(m["text"] or m["html"]):
                if "/password-reset/" in l:
                    self.reset_tokens.append(l.rstrip("/").rsplit("/", 1)[-1])
            for l in links(m["text"] or m["html"]):
                if l.startswith(self.base) and "/password-reset/" in l:
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

    def signin(self, email: str, password: str = PASSWORD, client: Client | None = None, next_: str | None = None):
        c = client or self.client()
        path = "/signin" + ("?next=" + urllib.parse.quote(next_, safe="") if next_ is not None else "")
        r = submit_form(c, path, "signin", {"email": email, "password": password}, follow=False)
        for h in r.header_all("Set-Cookie"):
            name, _, v = h.split(";", 1)[0].partition("=")
            v = v.strip()
            if len(v) >= 16:
                self.session_values.append(v)
                self.session_pairs.append((name.strip(), v))
        return c, r

    def signed_in(self, c: Client) -> bool:
        """Signed in when / (after any redirects) is a page other than /signin that offers the sign-out button."""
        r = c.get("/", follow=True)
        path = urllib.parse.urlsplit(r.url).path.rstrip("/")
        return r.status == 200 and path != "/signin" and r.dom.attr_one("data-action", "signout") is not None

    def find_session_names(self, c: Client) -> set[str]:
        """The session cookie(s): those whose removal signs a signed-in client out (other cookies are not constrained)."""
        names = set()
        for name in list(c.cookies):
            probe = c.copy()
            probe.cookies.pop(name, None)
            if not self.signed_in(probe):
                names.add(name)
        if names:
            self.session_names = names
        return names

    def session_secret_values(self) -> list[str]:
        if not self.session_names:
            return [v for _n, v in self.session_pairs]
        return [v for n, v in self.session_pairs if n in self.session_names]

    def account(self, email: str) -> Client:
        if not self.set_password(email):
            raise StepFailed(f"could not set a password for {email} through /password-reset")
        c, r = self.signin(email)
        if not self.signed_in(c):
            raise StepFailed(f"sign-in for {email} did not produce a signed-in session (status {r.status})")
        return c

    def who(self, name: str) -> Client:
        if name not in self.sessions:
            self.sessions[name] = self.account(PEOPLE[name])
        return self.sessions[name]

    # -- setup through the interface -------------------------------------------------------------------------
    def create_account(self, c: Client, hid: int, name: str, broker: str, number: str, method: str = "FIFO",
                       kind: str = "taxable") -> int:
        # account_type is always sent; empty means taxable (R3.1), so an app that requires the field is not failed
        fields = {"name": name, "broker": broker, "account_number": number, "lot_method": method,
                  "account_type": "" if kind == "taxable" else kind}
        r = submit_form(c, f"/households/{hid}", "create-account", fields, follow=False)
        if r.status not in REDIRECTS or not re.search(r"/accounts/(\d+)", r.location or ""):
            raise StepFailed(f"creating account {number!r} answered {r.status} without redirecting to /accounts/<id>")
        aid = int(re.search(r"/accounts/(\d+)", r.location).group(1))
        page = c.get(f"/accounts/{aid}", follow=True)
        if page.dom.attr_one("data-account-id", str(aid)) is None:
            raise StepFailed(f"the new account page has no data-account-id={aid}")
        self.hh[hid].accounts[aid] = M.Account(aid, hid, name, broker, number.strip(), method, kind)
        self.acct_hh[aid] = hid
        self.hh[hid].touch()
        return aid

    def create_security(self, c: Client, hid: int, symbol: str, isin: str, name: str, currency: str) -> int:
        r = submit_form(c, f"/households/{hid}/securities", "create-security",
                        {"symbol": symbol, "isin": isin, "name": name, "currency": currency}, follow=True)
        if r.status >= 400:
            raise StepFailed(f"creating security {symbol} answered {r.status}")
        page = c.get(f"/households/{hid}/securities", follow=True)
        node = page.dom.attr_one("data-symbol", symbol.strip().upper())
        if node is None or not (node.attrs.get("data-security-id") or "").isdigit():
            raise StepFailed(f"security {symbol} is not listed with data-security-id/data-symbol")
        sid = int(node.attrs["data-security-id"])
        self.hh[hid].securities[sid] = M.Sec(sid, hid, symbol.strip().upper(), isin.strip().upper(), name, currency)
        return sid

    def record_action(self, c: Client, hid: int, kind: str, sid: int, day: date, terms: dict, expect_ok: bool = True) -> Response:
        """Record a corporate action through the form (R13.11) and, when it is accepted, in the model."""
        fields = {"kind": kind, "security_id": str(sid), "action_date": day.isoformat()}
        for k, v in terms.items():
            fields[k] = "" if v is None else (E.qty_str(v) if isinstance(v, Fraction) else str(v))
        r = submit_form(c, f"/households/{hid}/corporate-actions", "record-corporate-action", fields, follow=False)
        if r.status in REDIRECTS and expect_ok:
            self.action_seq = max(self.action_seq, 10 ** 9) + 1  # after every legacy action (ids only order actions of a date)
            row = {"id": self.action_seq, "household_id": hid, "security_id": sid, "kind": kind, "action_date": day.isoformat(),
                   **{k: (None if v is None else (E.qty_str(v) if isinstance(v, Fraction) else v)) for k, v in terms.items()}}
            self.hh[hid].actions.append(M.action_txn(row))
            self.hh[hid].touch()
        elif expect_ok:
            raise StepFailed(f"recording a {kind} answered {r.status}: {_page_text(r)[:300]}")
        return r

    def change_method(self, c: Client, aid: int, method: str, eff: date) -> Response:
        r = submit_form(c, f"/accounts/{aid}", "change-method", {"lot_method": method, "effective_from": eff.isoformat()}, follow=True)
        if r.status < 400:
            hid = self.acct_hh[aid]
            self.max_change_id += 1
            self.hh[hid].changes.append((aid, eff, 10 ** 9 + self.max_change_id, method))
            self.hh[hid].touch()
        return r

    # -- uploads ----------------------------------------------------------------------------------------------
    def upload(self, c: Client, aid: int, filename: str, data: bytes, drop_csrf: bool = False, page: Response | None = None,
               extra_fields: dict | None = None) -> Response:
        page = page or c.get(f"/accounts/{aid}", follow=True)
        form = find_form(page.dom, "import-statement")
        action = urllib.parse.urljoin(page.url, form.attrs.get("action") or page.url)
        fields = [(k, v) for k, v in form_fields(form)]
        if drop_csrf:
            names = {i.attrs.get("name") for i in form.find_all(lambda n: n.tag == "input" and "data-csrf" in n.attrs)}
            fields = [(k, v) for k, v in fields if k not in names]
        fields += list((extra_fields or {}).items())
        boundary = "----lbverify" + secrets.token_hex(12)
        out = io.BytesIO()
        for k, v in fields:
            out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        fn = filename.replace('"', "%22")
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fn}\"\r\n"
                  "Content-Type: text/csv\r\n\r\n".encode() + data + b"\r\n")
        out.write(f"--{boundary}--\r\n".encode())
        return c.request("POST", action, body=out.getvalue(), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                         follow=True)

    @staticmethod
    def import_status(r: Response) -> tuple[str | None, int | None, int | None, list[int]]:
        node = r.dom.attr_one("data-import-status")
        if node is None:
            return None, None, None, []
        st = node.attrs.get("data-import-status")
        rejected = r.dom.attr_one("data-import-status", "rejected")
        # the problems are the data-error-line elements inside the rejected element (R4.6), compared as a set
        lines = sorted({int(n.attrs["data-error-line"]) for n in (rejected.attr_all("data-error-line") if rejected else [])
                        if re.fullmatch(r"\d+", n.attrs.get("data-error-line", ""))})
        num = lambda k: int(node.attrs[k]) if re.fullmatch(r"\d+", node.attrs.get(k, "")) else None  # noqa: E731
        return st, num("data-imported-count"), num("data-duplicate-count"), lines

    def ref(self, tag: str) -> str:
        self.ref_n += 1
        return f"V{self.nonce}-{tag}-{self.ref_n:05d}"

    def model_rows(self, aid: int, rows: list[dict]) -> tuple[list[Txn], int]:
        """What recording this file should add (file order), and how many rows are duplicates."""
        hid = self.acct_hh[aid]
        h = self.hh[hid]
        known = {(t.account, (t.ref or "").strip()): t for t in h.txns if t.ref}
        new, dups = [], 0
        for r in rows:
            sid = self.sid_of(hid, r)
            t = Txn(0, 0, aid, sid, r["kind"], r["day"], Fraction(r["quantity"]) if r.get("quantity") is not None else None,
                    r.get("amount"), r.get("ratio"), ref=r["ref"])
            prev = known.get((aid, r["ref"]))
            if prev is not None:
                if M.same_content(prev, t):
                    dups += 1
                    continue
                raise StepFailed(f"verifier fixture: conflicting reference {r['ref']}")
            known[(aid, r["ref"])] = t
            new.append(t)
        return new, dups

    def sid_of(self, hid: int, r: dict) -> int:
        for s in self.hh[hid].securities.values():
            if s.symbol == r["symbol"]:
                return s.sid
        raise StepFailed(f"verifier fixture: unknown symbol {r['symbol']}")

    def commit_rows(self, aid: int, new: list[Txn]) -> None:
        h = self.hh[self.acct_hh[aid]]
        for t in new:
            t.tid = t.seq = self.next_seq()
            h.txns.append(t)
        h.touch()

    def import_rows(self, c: Client, aid: int, rows: list[dict], tag: str = "stmt", rnd: random.Random | None = None) -> tuple[int, int]:
        """Import rows (already in the wanted order) in as few files as fit IMPORT_MAX_BYTES; every file must be accepted."""
        acct = self.hh[self.acct_hh[aid]].accounts[aid]
        rnd = rnd or random.Random(aid)
        chunks, cur = [], []
        for r in rows:
            trial = cur + [r]
            data, _ = ST.build(acct.broker, acct.number, trial, rnd)
            if len(data) > self.import_max - 600 and cur:
                chunks.append(cur)
                cur = [r]
            else:
                cur = trial
        if cur:
            chunks.append(cur)
        tot_new = tot_dup = 0
        for i, ch in enumerate(chunks):
            data, _ = ST.build(acct.broker, acct.number, ch, random.Random(rnd.random()), crlf=rnd.random() < 0.6)
            want_new, want_dup = self.model_rows(aid, ch)
            r = self.upload(c, aid, f"{tag}-{i + 1}.csv", data)
            st, n_imp, n_dup, lines = self.import_status(r)
            if r.status >= 400 or st != "imported":
                raise StepFailed(f"importing {len(ch)} rows into account {aid} answered {r.status} ({st}); error lines {lines[:8]}: "
                                 f"{_errors_text(r)[:400]}")
            if (n_imp, n_dup) != (len(want_new), want_dup):
                raise StepFailed(f"import of {len(ch)} rows reported {n_imp} recorded / {n_dup} duplicates, expected "
                                 f"{len(want_new)} / {want_dup}")
            self.commit_rows(aid, want_new)
            tot_new += n_imp
            tot_dup += n_dup
        return tot_new, tot_dup

    # -- manual entries ---------------------------------------------------------------------------------------
    def manual(self, c: Client, aid: int, fields: dict, query: str = "", expect_ok: bool = True) -> Response:
        page = c.get(f"/accounts/{aid}/transactions/new{query}", follow=True)
        r = submit_form(c, "", "record-transaction", {k: str(v) for k, v in fields.items()}, page=page, follow=False)
        ok = r.status in REDIRECTS
        if ok:
            hid = self.acct_hh[aid]
            h = self.hh[hid]
            kind = fields["kind"]
            t = Txn(0, 0, aid, int(fields["security_id"]), kind, d(fields["trade_date"]))
            if kind in ("buy", "sell", "reinvest"):
                q, p = Fraction(str(fields["quantity"])), Fraction(str(fields["price"]))
                fee = Fraction(str(fields.get("fee") or "0"))
                t.quantity = q
                t.amount = E.rhu((q * p + fee if kind != "sell" else q * p - fee) * 100)
                sel = [(self.model_tid(aid, int(k[4:])), Fraction(str(v))) for k, v in fields.items()
                       if k.startswith("lot_") and str(v).strip() not in ("", "0")]
                if sel:
                    t.selection = sel
            elif kind == "dividend":
                t.amount = int(Fraction(str(fields["amount"])) * 100)
            else:
                n, o = str(fields["ratio"]).split(":")
                t.ratio = (int(n), int(o))
            t.tid = t.seq = self.next_seq()
            h.txns.append(t)
            h.touch()
            self._remember_manual(c, aid, t)
        elif expect_ok:
            raise StepFailed(f"recording {fields.get('kind')} on account {aid} answered {r.status}: {_page_text(r)[:300]}")
        return r

    def _remember_manual(self, c: Client, aid: int, t: Txn) -> None:
        self._manual = getattr(self, "_manual", [])
        self._manual.append((aid, t))

    def app_txn_ids(self, c: Client, aid: int) -> list[dict]:
        page = c.get(f"/accounts/{aid}", follow=True)
        out = []
        for n in page.dom.attr_all("data-txn-id"):
            if (n.attrs.get("data-txn-id") or "").isdigit():
                out.append({"id": int(n.attrs["data-txn-id"]), "kind": n.attrs.get("data-kind"), "day": n.attrs.get("data-trade-date"),
                            "symbol": n.attrs.get("data-symbol"), "ref": n.attrs.get("data-ref") or ""})
        return out

    def map_ids(self, c: Client, aid: int) -> dict[int, int]:
        """model tid -> application id for this account's acquisitions (by reference, else by kind/date/symbol order)."""
        h = self.hh[self.acct_hh[aid]]
        app = self.app_txn_ids(c, aid)
        by_ref = {x["ref"]: x["id"] for x in app if x["ref"]}
        out = {}
        rest = [x for x in app if not x["ref"]]
        for t in h.txns:
            if t.account != aid or t.kind not in ("buy", "reinvest"):
                continue
            if t.ref and t.ref in by_ref:
                out[t.tid] = by_ref[t.ref]
            elif not t.ref:
                sym = h.securities[t.security].symbol
                cands = sorted(x["id"] for x in rest if x["kind"] == t.kind and x["day"] == t.day.isoformat() and x["symbol"] == sym)
                used = set(out.values())
                cands = [x for x in cands if x not in used]
                if cands:
                    out[t.tid] = cands[0]
        self.id_map = getattr(self, "id_map", {})
        self.id_map.update({v: k for k, v in out.items()})
        return out

    def model_tid(self, aid: int, app_id: int) -> int:
        m = getattr(self, "id_map", {})
        return m.get(app_id, -app_id)

    def transfer(self, c: Client, hid: int, frm: int, to: int, sid: int, day: date, expect_ok: bool = True) -> Response:
        r = submit_form(c, f"/households/{hid}/transfers", "record-transfer", {"from_account_id": str(frm), "to_account_id": str(to),
                        "security_id": str(sid), "trade_date": day.isoformat()}, follow=False)
        if r.status in REDIRECTS and expect_ok:
            h = self.hh[hid]
            t = Txn(0, 0, frm, sid, "transfer", day, to_account=to)
            t.tid = t.seq = self.next_seq()
            h.txns.append(t)
            h.touch()
        elif expect_ok:
            raise StepFailed(f"recording a transfer answered {r.status}: {_page_text(r)[:300]}")
        return r

    # -- reports ------------------------------------------------------------------------------------------------
    def csv_get(self, c: Client, path: str) -> tuple[Response, list[str] | None, list[list[str]]]:
        r = c.get(path, follow=False)
        if r.status != 200:
            return r, None, []
        data = list(csv.reader(io.StringIO(r.body.decode("utf-8-sig", "replace"), newline="")))
        return r, (data[0] if data else None), data[1:]

    def year_csv(self, c: Client, hid: int, year: int, kind: str):
        return self.csv_get(c, f"/households/{hid}/reports/{year}/{kind}.csv")

    def lots(self, c: Client, aid: int) -> list[tuple]:
        page = c.get(f"/accounts/{aid}/lots", follow=True)
        if page.status != 200:
            raise StepFailed(f"/accounts/{aid}/lots answered {page.status}")
        out = []
        for n in page.dom.attr_all("data-lot-id"):
            a = n.attrs
            out.append((a.get("data-symbol"), a.get("data-quantity"), a.get("data-acquired"), a.get("data-holding-start"),
                        a.get("data-basis-cents"), a.get("data-wash-adjusted")))
        return out

    @staticmethod
    def wash_marker(l, ira=frozenset()) -> str:
        """R12.2 marks lots whose basis includes a disallowed loss. A replacement lot whose disallowed part added 0 cents
        reads either way (its holding period still moved), and so do IRA shares that served as replacement shares
        (R13.10: nothing is added to them), so the grader accepts true or false for those."""
        if l.account in ira and l.used:
            return EITHER
        if not l.wash_adjusted:
            return "false"
        return "true" if l.wash_cents else EITHER

    def want_lots(self, hid: int, aid: int, sids: set | None = None) -> list[tuple]:
        h = self.hh[hid]
        return [(h.securities[l.security].symbol, E.qty_str(l.qty), l.acquired.isoformat(), l.holding.isoformat(), str(l.basis),
                 self.wash_marker(l, h.ira)) for l in h.open_lots(self.rates, aid=aid, sids=sids)]

    def compare_realized(self, c: Client, hid: int, year: int, sids: set, check_order: bool = False) -> list[str]:
        h = self.hh[hid]
        syms = {h.securities[s].symbol for s in sids}
        want = M.render(h.realized_rows(year, self.rates, sids=sids), M.REALIZED_TEXT)
        r, header, rows = self.year_csv(c, hid, year, "realized")
        if r.status != 200:
            return [f"realized.csv for {year} answered {r.status}"]
        if header != M.REALIZED_HEADER:
            return [f"realized.csv header {header}"]
        got = [row for row in rows if len(row) > 2 and row[2].lstrip("'") in syms]
        return diff_rows(want, got, f"realized {year}", check_order)

    def compare_summary(self, c: Client, hid: int, year: int, sids: set) -> list[str]:
        h = self.hh[hid]
        syms = {h.securities[s].symbol for s in sids}
        want = M.render(h.summary_rows(year, self.rates, sids=sids), M.SUMMARY_TEXT)
        r, header, rows = self.year_csv(c, hid, year, "summary")
        if r.status != 200:
            return [f"summary.csv for {year} answered {r.status}"]
        if header != M.SUMMARY_HEADER:
            return [f"summary.csv header {header}"]
        got = [row for row in rows if row and row[0].lstrip("'") in syms]
        return diff_rows(want, got, f"summary {year}", True)

    def compare_lots(self, c: Client, hid: int, aids, sids: set | None = None) -> list[str]:
        problems = []
        h = self.hh[hid]
        syms = None if sids is None else {h.securities[s].symbol for s in sids}
        for aid in aids:
            want = self.want_lots(hid, aid, sids)
            got = tolerate_either([x for x in self.lots(c, aid) if syms is None or x[0] in syms], want)
            problems += diff_rows([list(x) for x in want], [list(x) for x in got], f"lots of account {aid}", False)
        return problems

    def lock(self, c: Client, hid: int, year: int) -> Response:
        page = c.get(f"/households/{hid}/reports/{year}", follow=True)
        try:
            return submit_form(c, "", "lock-year", {}, page=page, follow=False)
        except FormNotFound:
            raise StepFailed(f"the {year} page offers no lock-year form (status {page.status})")

    def year_page(self, c: Client, hid: int, year: int) -> dict | None:
        page = c.get(f"/households/{hid}/reports/{year}", follow=True)
        n = page.dom.attr_one("data-year", str(year))
        if n is None:
            return None
        a = n.attrs
        out = {"locked": a.get("data-locked")}
        for k in ("short", "long", "disallowed", "dividends", "adjustment"):
            v = a.get(f"data-total-{k}-cents", "")
            out[k] = int(v) if re.fullmatch(r"-?\d+", v or "") else None
        return out

    def restart_app(self) -> bool:
        if not self.restart_hook:
            return False
        # The hook is the verifier's own root-owned script (tests/test.sh writes it); no user input reaches it.
        return subprocess.run([self.restart_hook], check=False).returncode == 0


EITHER = "true|false"


def tolerate_either(got: list[tuple], want: list[tuple]) -> list[tuple]:
    """Rewrite the marker of got lots that match an expected 0-cent replacement lot (marker EITHER) on every other field."""
    open_keys = Counter(tuple(w[:-1]) for w in want if w[-1] == EITHER)
    out = []
    for g in got:
        k = tuple(g[:-1])
        if open_keys.get(k) and g[-1] in ("true", "false"):
            open_keys[k] -= 1
            g = tuple(g[:-1]) + (EITHER,)
        out.append(tuple(g))
    return out


def diff_rows(want: list[list[str]], got: list[list[str]], what: str, check_order: bool) -> list[str]:
    cw, cg = Counter(map(tuple, want)), Counter(map(tuple, got))
    missing = list((cw - cg).elements())
    extra = list((cg - cw).elements())
    out = []
    if missing or extra:
        out.append(f"{what}: {len(missing)} expected rows missing, {len(extra)} unexpected (of {len(want)} expected)")
        for m in sorted(missing)[:4]:
            out.append("  want " + ",".join(m))
        for x in sorted(extra)[:4]:
            out.append("  got  " + ",".join(x))
    elif check_order and [tuple(r) for r in want] != [tuple(r) for r in got]:
        out.append(f"{what}: rows are right but out of order")
    return out


def _page_text(r: Response) -> str:
    try:
        from kitlib.web import visible_text
        return visible_text(r.text)
    except Exception:
        return ""


def _errors_text(r: Response) -> str:
    return " | ".join(n.text()[:120] for n in r.dom.attr_all("data-error-line"))
