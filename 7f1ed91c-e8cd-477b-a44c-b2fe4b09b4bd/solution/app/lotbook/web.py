"""Lotbook HTTP layer: stdlib ThreadingHTTPServer, server-side sessions, CSRF tokens, HTML pages, CSV exports."""

from __future__ import annotations

import email.parser
import email.policy
import html
import os
import re
import secrets
import traceback
import urllib.parse
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

from . import core
from . import engine as E
from .core import connect, dollars, norm_email, now_utc, sha

ENV = os.environ
COOKIE = "lb_session"
E_ = html.escape

CSS = """
*{box-sizing:border-box}body{margin:0;font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1b2430;background:#f4f6f8}
header{background:#183a37;color:#fff;padding:10px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between}
header a{color:#fff;margin-right:12px}header nav{display:flex;flex-wrap:wrap;gap:4px}main{max-width:1120px;margin:0 auto;padding:16px}
h1{font-size:1.5rem;margin:.2em 0 .6em}h2{font-size:1.15rem}h1,h2,p,li,label,header span{overflow-wrap:anywhere}
.card{background:#fff;border:1px solid #dde2e7;border-radius:8px;padding:16px;margin:0 0 16px;overflow-wrap:anywhere}
.flash{padding:12px 16px;border-radius:8px;margin:0 0 16px;background:#e6f4ef;border:1px solid #8fcfb6}
.flash.err{background:#fdecec;border-color:#f0a5a5}.note{background:#fff8e1;border:1px solid #f0d78c;padding:10px 12px;border-radius:6px}
button{font:inherit;padding:8px 14px;border-radius:6px;border:1px solid #183a37;background:#183a37;color:#fff;cursor:pointer}
button.secondary{background:#fff;color:#183a37}input,select,textarea{font:inherit;padding:7px;border:1px solid #b7c0c9;border-radius:6px;width:100%;max-width:420px}
label{display:block;margin:8px 0}.muted{color:#5a6672}.badge{display:inline-block;padding:1px 9px;border-radius:999px;font-size:.85em;font-weight:600;background:#e8edf2}
.badge.locked{background:#fde2c8}.badge.wash{background:#ffe0e0}.badge.long{background:#dff3e4}.badge.short{background:#e7ecff}
.tablewrap{overflow-x:auto;max-width:100%}table{border-collapse:collapse;width:100%}td,th{padding:6px 8px;border-bottom:1px solid #e4e8ec;text-align:left;vertical-align:top}
td.num,th.num{text-align:right;white-space:nowrap}.totals{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px}
.totals div{background:#f7f9fa;border:1px solid #e1e6ea;border-radius:6px;padding:8px}.totals b{display:block;font-size:1.1em}
.row{display:flex;flex-wrap:wrap;gap:12px}.row>*{flex:1 1 260px}
@media (max-width:600px){
table.cards,table.cards tbody,table.cards tr,table.cards td{display:block;width:100%}
table.cards tr.hd{display:none}
table.cards tr{border:1px solid #dde2e7;border-radius:6px;margin:0 0 10px;padding:6px 10px;background:#fbfcfd}
table.cards td{border:0;padding:3px 0;text-align:left;white-space:normal;overflow-wrap:break-word;word-break:normal}
table.cards td::before{content:attr(data-label) ": ";font-weight:600;color:#4a5560}
table.cards td input{max-width:160px;display:inline-block;width:auto}
}
"""


TABLE_RE = re.compile(r"<table>(.*?)</table>", re.S)
TH_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.S)


def card_tables(body: str) -> str:
    """Give every data table class=cards, mark its header row, and label each cell with its column's heading, so the
    narrow layout (below 600 px) shows each row as a card with one labelled line per cell."""
    def one(m):
        inner = m.group(1)
        rows = inner.split("<tr")
        if len(rows) < 2 or "<th" not in rows[1]:
            return m.group(0)
        labels = [re.sub(r"<[^>]+>", "", h).strip() for h in TH_RE.findall(rows[1])]
        out = [rows[0], " class=hd" + rows[1]]
        for r in rows[2:]:
            parts = r.split("<td")
            r2 = parts[0] + "".join(f"<td data-label='{labels[i] if i < len(labels) else ''}'" + part
                                    for i, part in enumerate(parts[1:]))
            out.append(r2)
        return "<table class=cards>" + "<tr".join(out) + "</table>"
    return TABLE_RE.sub(one, body)


def page(title: str, body: str, user: dict | None = None, csrf: str | None = None, flash: str = "", err: bool = False) -> str:
    body = card_tables(body)
    nav = ""
    if user:
        links = ['<a href="/">Home</a>']
        if user["role"] in core.HOUSEHOLD_ROLES:
            hid = user["household_id"]
            links += [f'<a href="/households/{hid}">Accounts</a>', f'<a href="/households/{hid}/securities">Securities</a>',
                      f'<a href="/households/{hid}/reports/{now_utc().year - 1}">Tax years</a>']
        if core.can_rename(user):
            links.append('<a href="/profile">Profile</a>')
        role = ROLE_LABEL.get(user["role"], user["role"])
        nav = (f"<nav>{''.join(links)}</nav><form method=post action=/signout style='margin:0'>{csrf_input(csrf)}"
               f"<span style='color:#cfe6e1;margin-right:8px'>{E_(user['full_name'])} · {role}</span>"
               "<button class=secondary data-action=signout>Sign out</button></form>")
    fl = f"<div class='flash{' err' if err else ''}' role=status>{flash}</div>" if flash else ""
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{E_(title)} · Lotbook</title><style>{CSS}</style></head><body><header><strong>Lotbook</strong>{nav}</header>"
            f"<main><h1>{E_(title)}</h1>{fl}{body}</main></body></html>")


ROLE_LABEL = {"owner": "Owner", "member": "Member", "viewer": "Viewer (read-only)", "advisor": "Advisor (read-only)",
              "preparer": "Tax preparer (read, lock years)"}
ROLE_NOTE = {
    "viewer": "You have read-only access to your household: you can view accounts, lots and reports, but not change them.",
    "advisor": "You have read-only access as an advisor: you can view these households' accounts, lots and reports, but you "
               "cannot change anything.",
    "preparer": "As the tax preparer you can view these households' accounts, lots and reports and lock their finished tax "
                "years. You cannot change accounts or transactions.",
}


def role_note(user: dict | None) -> str:
    note = ROLE_NOTE.get((user or {}).get("role", ""))
    return f"<p class=note data-role-note>{note}</p>" if note else ""


def csrf_input(token: str | None) -> str:
    return f"<input type=hidden name=csrf_token data-csrf value='{E_(token or '')}'>" if token else ""


class Resp(Exception):
    def __init__(self, status: int, body: str | bytes = "", headers: dict | None = None, ctype: str = "text/html; charset=utf-8"):
        self.status, self.body, self.headers, self.ctype = status, body, headers or {}, ctype


def redirect(to: str, extra: dict | None = None) -> Resp:
    return Resp(303, "", {"Location": to, **(extra or {})})


def parse_decimal(s: str, places: int, positive: bool = False, allow_zero: bool = True) -> Fraction | None:
    s = (s or "").strip()
    if not re.fullmatch(r"\d+(\.\d+)?", s):
        return None
    if "." in s and len(s.split(".")[1]) > places:
        return None
    try:
        v = Fraction(Decimal(s))
    except InvalidOperation:
        return None
    if (positive and v <= 0) or (not allow_zero and v == 0):
        return None
    return v


def parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat((s or "").strip()) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", (s or "").strip()) else None
    except ValueError:
        return None


ROUTES = [
    (r"/", "home"), (r"/signin", "signin"), (r"/signout", "signout"), (r"/password-reset", "reset"),
    (r"/password-reset/([A-Za-z0-9_\-]+)", "reset_token"), (r"/profile", "profile"),
    (r"/households/(\d+)", "household"), (r"/households/(\d+)/accounts", "accounts"),
    (r"/households/(\d+)/securities", "securities"), (r"/households/(\d+)/transfers", "transfers"),
    (r"/households/(\d+)/corporate-actions", "actions"),
    (r"/households/(\d+)/reports/(\d{4})", "year"), (r"/households/(\d+)/reports/(\d{4})/lock", "lock"),
    (r"/households/(\d+)/reports/(\d{4})/summary\.csv", "summary_csv"),
    (r"/households/(\d+)/reports/(\d{4})/realized\.csv", "realized_csv"), (r"/households/(\d+)/unrealized\.csv", "unrealized_csv"),
    (r"/accounts/(\d+)", "account"), (r"/accounts/(\d+)/imports", "imports"), (r"/accounts/(\d+)/transactions/new", "txn_new"),
    (r"/accounts/(\d+)/transactions", "txns"), (r"/accounts/(\d+)/lots", "lots"), (r"/accounts/(\d+)/method", "method"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "Lotbook"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, r: Resp) -> None:
        body = r.body.encode() if isinstance(r.body, str) else r.body
        self.send_response(r.status)
        self.send_header("Content-Type", r.ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'none'")
        for k, v in r.headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_body(self, limit: int) -> bytes | None:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            self.close_connection = True
            return None
        return self.rfile.read(n) if n else b""

    def _form(self) -> dict:
        raw = self._read_body(2_000_000)
        if raw is None:
            raise Resp(413, "Request too large", ctype="text/plain")
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("application/x-www-form-urlencoded") or not ctype:
            return {k: v[-1] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}
        return {}

    def _session(self, conn):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        if COOKIE not in c:
            return None, None
        row = conn.execute("SELECT csrf, user_id FROM lb_sessions WHERE sid_hash = %s AND NOT revoked", (sha(c[COOKIE].value),)).fetchone()
        if not row:
            return None, None
        u = conn.execute("SELECT * FROM users WHERE id = %s", (row["user_id"],)).fetchone()
        if not u or not u["active"]:
            return None, None
        return u, row["csrf"]

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        url = urllib.parse.urlsplit(self.path)
        self.query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        path = url.path.rstrip("/") or "/"
        try:
            if path == "/healthz":
                raise self.healthz()
            multipart = self.headers.get("Content-Type", "").startswith("multipart/form-data")
            self.form = self._form() if method == "POST" and not multipart else {}
            with connect(autocommit=True) as conn:  # every write below runs in an explicit conn.transaction()
                self.conn = conn
                self.user, self.csrf = self._session(conn)
                for pattern, name in ROUTES:
                    m = re.fullmatch(pattern, path)
                    if m:
                        fn = getattr(self, f"{name}_{method.lower()}", None)
                        if fn is None:
                            raise Resp(405, "Method not allowed", ctype="text/plain")
                        raise fn(*m.groups())
                raise Resp(404, page("Not found", "<p>That page does not exist.</p>", self.user, self.csrf))
        except Resp as r:
            try:
                self._send(r)
            except Exception:
                pass
        except psycopg.OperationalError:
            self._send(Resp(503, "Database unavailable", ctype="text/plain"))
        except Exception:
            traceback.print_exc()
            self._send(Resp(500, "Internal error", ctype="text/plain"))

    # -- guards ------------------------------------------------------------------------------------------
    def need_user(self) -> dict:
        if not self.user:
            target = self.path if self.command == "GET" else "/"
            raise redirect("/signin?next=" + urllib.parse.quote(target, safe=""))
        return self.user

    def forbid(self, msg: str = "You are not allowed to do that.") -> Resp:
        return Resp(403, page("Not allowed", f"<p>{E_(msg)}</p>", self.user, self.csrf))

    def check_csrf(self, sent: str | None = None) -> None:
        sent = self.form.get("csrf_token", "") if sent is None else sent
        if not self.csrf or not secrets.compare_digest(sent or "", self.csrf):
            raise Resp(403, page("Not allowed", "<p>This form expired or came from another site. Reload the page and try again.</p>",
                                 self.user, self.csrf))

    def see(self, hid: int) -> dict:
        u = self.need_user()
        if not core.can_see(self.conn, u, hid):
            raise self.forbid()
        return u

    def change(self, hid: int, sent: str | None = None) -> dict:
        u = self.need_user()
        if not core.can_change(u, hid):
            raise self.forbid("Your access does not allow this change." if core.can_see(self.conn, u, hid) else
                              "You are not allowed to do that.")
        self.check_csrf(sent)
        return u

    def account_row(self, aid: int) -> dict:
        a = self.conn.execute("SELECT * FROM accounts WHERE id = %s", (aid,)).fetchone()
        if not a:
            raise Resp(404, page("Not found", "<p>No such account.</p>", self.user, self.csrf))
        return a

    def render(self, title: str, body: str, status: int = 200, flash: str = "", err: bool = False) -> Resp:
        return Resp(status, page(title, body, self.user, self.csrf, flash, err))

    def is_advisor(self) -> bool:
        return bool(self.user and self.user["role"] == "advisor")

    # -- health ------------------------------------------------------------------------------------------
    def healthz(self) -> Resp:
        try:
            with psycopg.connect(core.DATABASE_URL, connect_timeout=3) as c:
                c.execute("SET statement_timeout = 3000")
                c.execute("SELECT 1")
            return Resp(200, "ok", ctype="text/plain")
        except Exception:
            return Resp(503, "database unreachable", ctype="text/plain")

    # -- sign in -----------------------------------------------------------------------------------------
    def signin_form(self, email: str = "", flash: str = "", status: int = 200, nxt: str = "") -> Resp:
        body = ("<div class=card><form method=post action=/signin>"
                f"<input type=hidden name=next value='{E_(nxt)}'>"
                f"<label>Email <input name=email type=email autocomplete=username value='{E_(email)}' required></label>"
                "<label>Password <input name=password type=password autocomplete=current-password required></label>"
                "<button data-action=signin>Sign in</button></form>"
                "<p class=muted>First time here or forgot your password? <a href=/password-reset>Set a password</a>.</p></div>")
        return self.render("Sign in", body, status, flash, err=bool(flash))

    def signin_get(self):
        if self.user:
            return redirect(safe_next(self.query.get("next", "")))
        return self.signin_form(nxt=self.query.get("next", ""))

    def signin_post(self):
        email, pw = norm_email(self.form.get("email", "")), self.form.get("password", "")
        conn = self.conn
        u = conn.execute("SELECT * FROM users WHERE lower(btrim(email)) = %s ORDER BY id LIMIT 1", (email,)).fetchone()
        cred = conn.execute("SELECT password_hash FROM lb_credentials WHERE user_id = %s", (u["id"],)).fetchone() if u else None
        ok = bool(u and u["active"] and cred and core.check_password(pw, cred["password_hash"]))
        if not ok:
            if not cred:
                core.check_password(pw, core.hash_password("timing-equalizer"))
            return self.signin_form(email, "That email and password don't match an active account. Check them and try again.", 401,
                                    self.form.get("next", ""))
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with conn.transaction():
            conn.execute("INSERT INTO lb_sessions (sid_hash, user_id, csrf) VALUES (%s,%s,%s)", (sha(sid), u["id"], csrf))
            old = SimpleCookie(self.headers.get("Cookie", ""))
            if COOKIE in old:
                conn.execute("UPDATE lb_sessions SET revoked = true WHERE sid_hash = %s", (sha(old[COOKIE].value),))
        return redirect(safe_next(self.form.get("next", "")), {"Set-Cookie": f"{COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax"})

    def signout_post(self):
        if self.user:
            self.check_csrf()
            c = SimpleCookie(self.headers.get("Cookie", ""))
            self.conn.execute("UPDATE lb_sessions SET revoked = true WHERE sid_hash = %s", (sha(c[COOKIE].value),))
            self.conn.commit()
        return redirect("/signin", {"Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})

    # -- password links ----------------------------------------------------------------------------------
    def reset_get(self):
        body = ("<div class=card><p>Enter your email address. If it belongs to an active Lotbook user, we email a link to set a "
                "new password.</p><form method=post action=/password-reset><label>Email <input name=email type=email required></label>"
                "<button data-action=request-reset>Email me a link</button></form></div>")
        return self.render("Set or reset your password", body)

    def reset_post(self):
        email = norm_email(self.form.get("email", ""))
        conn = self.conn
        u = conn.execute("SELECT * FROM users WHERE lower(btrim(email)) = %s AND active ORDER BY id LIMIT 1", (email,)).fetchone()
        if u:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(7, %s)", (u["id"],))
                conn.execute("UPDATE lb_reset_tokens SET state = 'replaced' WHERE user_id = %s AND state = 'open'", (u["id"],))
                tok = secrets.token_urlsafe(32)
                conn.execute("INSERT INTO lb_reset_tokens (token_hash, user_id, created_at) VALUES (%s,%s,now())", (sha(tok), u["id"]))
            link = ENV.get("PUBLIC_BASE_URL", "").rstrip("/") + "/password-reset/" + tok
            ttl = int(ENV.get("RESET_LINK_TTL_SECONDS", "3600"))
            core.send_now(norm_email(u["email"]), "Set your Lotbook password",
                          f"Hello {u['full_name']},\n\nUse this link to set your Lotbook password. It works once and expires in "
                          f"{ttl} seconds:\n\n{link}\n\nIf you did not ask for it, ignore this email.\n")
        body = ("<div class=card><p>If that address belongs to an active Lotbook user, a link to set a password is on its way. "
                "The link works once.</p><p><a href=/signin>Back to sign in</a></p></div>")
        return self.render("Check your email", body)

    def _token(self, tok: str):
        row = self.conn.execute("SELECT * FROM lb_reset_tokens WHERE token_hash = %s", (sha(tok),)).fetchone()
        if not row:
            raise Resp(404, page("Link not found", "<p>This link does not exist.</p>", self.user, self.csrf))
        ttl = int(ENV.get("RESET_LINK_TTL_SECONDS", "3600"))
        valid = row["state"] == "open" and (now_utc() - row["created_at"]).total_seconds() < ttl
        return row, valid

    def reset_token_get(self, tok):
        row, valid = self._token(tok)
        if not valid:
            return self.render("Link no longer valid", "<p>This link is no longer valid. <a href=/password-reset>Request a new one</a>.</p>", 410)
        body = (f"<div class=card><form method=post action='/password-reset/{E_(tok)}'><label>New password (at least 10 characters) "
                "<input name=password type=password minlength=10 autocomplete=new-password required></label>"
                "<button data-action=set-password>Set password</button></form></div>")
        return self.render("Choose a password", body)

    def reset_token_post(self, tok):
        with self.conn.transaction():
            return self._reset_token_post(tok)

    def _reset_token_post(self, tok):
        conn = self.conn
        row = conn.execute("SELECT * FROM lb_reset_tokens WHERE token_hash = %s FOR UPDATE", (sha(tok),)).fetchone()
        if not row:
            raise Resp(404, page("Link not found", "<p>This link does not exist.</p>", self.user, self.csrf))
        ttl = int(ENV.get("RESET_LINK_TTL_SECONDS", "3600"))
        if row["state"] != "open" or (now_utc() - row["created_at"]).total_seconds() >= ttl:
            return self.render("Link no longer valid", "<p>This link is no longer valid. <a href=/password-reset>Request a new one</a>.</p>", 410)
        pw = self.form.get("password", "")
        if len(pw) < 10:
            return self.render("Choose a password", "<p>The password must be at least 10 characters. Use the link again.</p>", 422,
                               "Too short.", True)
        conn.execute("UPDATE lb_reset_tokens SET state = 'used' WHERE token_hash = %s", (sha(tok),))
        conn.execute("UPDATE lb_sessions SET revoked = true WHERE user_id = %s", (row["user_id"],))
        conn.execute("INSERT INTO lb_credentials (user_id, password_hash) VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE "
                     "SET password_hash = EXCLUDED.password_hash, updated_at = now()", (row["user_id"], core.hash_password(pw)))
        return self.render("Password set", "<p>Your password is set. <a href=/signin>Sign in</a>.</p>")

    # -- home and profile --------------------------------------------------------------------------------
    def home_get(self):
        u = self.need_user()
        items = []
        for hid in core.visible_households(self.conn, u):
            h = self.conn.execute("SELECT name FROM households WHERE id = %s", (hid,)).fetchone()
            items.append(f"<li data-household-id='{hid}'><a href='/households/{hid}'>{E_(h['name'])}</a></li>")
        return self.render("Households", f"<div class=card>{role_note(u)}<ul>{''.join(items) or '<li>None</li>'}</ul></div>")

    def profile_get(self):
        u = self.need_user()
        if not core.can_rename(u):
            return self.render("Profile", f"<div class=card><p>{E_(u['full_name'])}</p><p class=note>Your login cannot change "
                               "its display name; ask the household to update your details.</p></div>")
        body = (f"<div class=card><form method=post action=/profile>{csrf_input(self.csrf)}<label>Display name "
                f"<input name=full_name value='{E_(u['full_name'])}' maxlength=120 required></label>"
                "<button data-action=save-profile>Save</button></form></div>")
        return self.render("Profile", body)

    def profile_post(self):
        u = self.need_user()
        if not core.can_rename(u):
            raise self.forbid("Your login cannot change its display name.")
        self.check_csrf()
        name = self.form.get("full_name", "")
        if not 1 <= len(name) <= 120 or not name.strip():
            return self.render("Profile", "<p>Enter a display name of 1 to 120 characters.</p>", 422)
        self.conn.execute("UPDATE users SET full_name = %s WHERE id = %s", (name, u["id"]))
        self.conn.commit()
        return redirect("/profile")

    # -- household, accounts, securities -----------------------------------------------------------------
    def household_get(self, hid):
        hid = int(hid)
        u = self.see(hid)
        h = self.conn.execute("SELECT * FROM households WHERE id = %s", (hid,)).fetchone()
        accts = self.conn.execute("SELECT * FROM accounts WHERE household_id = %s ORDER BY id", (hid,)).fetchall()
        rows = "".join(f"<tr data-account-id='{a['id']}' data-account-type='{a['account_type']}'><td><a href='/accounts/{a['id']}'>{E_(a['name'])}</a></td>"
                       f"<td>{'Northwind Securities' if a['broker'] == 'northwind' else 'Harbor Direct'}</td><td>{E_(a['account_number'])}</td>"
                       f"<td>{E_(a['lot_method'])}</td><td>{'IRA (not reported)' if a['account_type'] == 'ira' else 'Taxable'}</td></tr>" for a in accts)
        this_year = now_utc().year
        years = " ".join(f"<a href='/households/{hid}/reports/{y}'>{y}</a>" for y in range(2023, this_year + 1))
        body = (f"<div class=card><h2>Accounts</h2><div class=tablewrap><table><tr><th>Account</th><th>Broker</th><th>Number</th>"
                f"<th>Initial lot method</th><th>Type</th></tr>{rows}</table></div></div>"
                f"<div class=card><h2>Tax years</h2><p>{years}</p><p><a href='/households/{hid}/securities'>Securities</a> · "
                f"<a href='/households/{hid}/transfers'>Transfers</a> · <a href='/households/{hid}/corporate-actions'>Corporate actions</a> · "
                f"<a href='/households/{hid}/unrealized.csv'>Unrealized gains (CSV)</a></p></div>")
        if core.can_change(u, hid):
            body += (f"<div class=card><h2>Add an account</h2><form method=post action='/households/{hid}/accounts'>{csrf_input(self.csrf)}"
                     "<label>Name <input name=name maxlength=100 required></label><label>Broker <select name=broker>"
                     "<option value=northwind>Northwind Securities</option><option value=harbor>Harbor Direct</option></select></label>"
                     "<label>Account number <input name=account_number maxlength=40 required></label><label>Lot method <select name=lot_method>"
                     "<option>FIFO</option><option>HIFO</option></select></label><label>Account type <select name=account_type>"
                     "<option value=taxable>Taxable</option><option value=ira>IRA (retirement, not reported)</option></select></label>"
                     "<button data-action=create-account>Add account</button></form></div>")
        return self.render(h["name"], role_note(u) + body)

    def accounts_post(self, hid):
        hid = int(hid)
        u = self.change(hid)
        f = self.form
        name, broker = f.get("name", ""), f.get("broker", "")
        number, method = f.get("account_number", "").strip(), f.get("lot_method", "")
        atype = f.get("account_type", "").strip() or "taxable"
        if not (1 <= len(name) <= 100 and broker in ("northwind", "harbor") and 1 <= len(number) <= 40 and method in ("FIFO", "HIFO")
                and atype in ("taxable", "ira")):
            return self.render("Add an account", "<p>Check the fields: name 1-100 characters, a broker, an account number "
                               "and a lot method.</p>", 422, "The account was not added.", True)
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(9, 0)")
            if self.conn.execute("SELECT 1 FROM accounts WHERE broker = %s AND btrim(account_number) = %s", (broker, number)).fetchone():
                return self.render("Add an account", "<p>That broker account is already on file.</p>", 409, "Already on file.", True)
            aid = self.conn.execute("INSERT INTO accounts (household_id, name, broker, account_number, lot_method, opened_on, account_type) "
                                    "VALUES (%s,%s,%s,%s,%s,current_date,%s) RETURNING id",
                                    (hid, name, broker, number, method, atype)).fetchone()["id"]
            core.bump(self.conn, hid)
        return redirect(f"/accounts/{aid}")

    def securities_get(self, hid, flash="", status=200, err=False):
        hid = int(hid)
        u = self.see(hid)
        secs = self.conn.execute("SELECT * FROM securities WHERE household_id = %s ORDER BY symbol", (hid,)).fetchall()
        rows = "".join(f"<tr data-security-id='{s['id']}' data-symbol='{E_(s['symbol'])}'><td>{E_(s['symbol'])}</td><td>{E_(s['isin'])}</td>"
                       f"<td>{E_(s['name'])}</td><td>{E_(s['currency'])}</td></tr>" for s in secs)
        body = (f"<div class=card><div class=tablewrap><table><tr><th>Symbol</th><th>ISIN</th><th>Name</th><th>Currency</th></tr>{rows}"
                "</table></div></div>")
        if core.can_change(u, hid):
            body += (f"<div class=card><h2>Add a security</h2><form method=post action='/households/{hid}/securities'>{csrf_input(self.csrf)}"
                     "<label>Symbol <input name=symbol maxlength=12 required></label><label>ISIN <input name=isin maxlength=12 required></label>"
                     "<label>Name <input name=name maxlength=200 required></label><label>Currency <select name=currency><option>USD</option>"
                     "<option>CAD</option><option>EUR</option></select></label><button data-action=create-security>Add security</button></form></div>")
        return self.render("Securities", body, status, flash, err)

    def securities_post(self, hid):
        hid = int(hid)
        self.change(hid)
        f = self.form
        sym, isin = f.get("symbol", "").strip().upper(), f.get("isin", "").strip().upper()
        name, cur = f.get("name", ""), f.get("currency", "")
        if not (re.fullmatch(r"[A-Z0-9.]{1,12}", sym) and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin) and 1 <= len(name) <= 200
                and cur in ("USD", "CAD", "EUR")):
            return self.securities_get(hid, "Check the symbol, ISIN, name and currency.", 422, True)
        with self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(10, %s)", (hid,))
            if self.conn.execute("SELECT 1 FROM securities WHERE household_id = %s AND (upper(btrim(symbol)) = %s OR upper(btrim(isin)) = %s)",
                                 (hid, sym, isin)).fetchone():
                return self.securities_get(hid, "That symbol or ISIN is already used by a security of this household.", 409, True)
            self.conn.execute("INSERT INTO securities (household_id, symbol, isin, name, currency) VALUES (%s,%s,%s,%s,%s)",
                              (hid, sym, isin, name, cur))
            core.bump(self.conn, hid)
        return redirect(f"/households/{hid}/securities")

    # -- transfers ------------------------------------------------------------------------------------------
    def transfers_get(self, hid, flash="", status=200, err=False):
        hid = int(hid)
        u = self.see(hid)
        accts = self.conn.execute("SELECT * FROM accounts WHERE household_id = %s ORDER BY id", (hid,)).fetchall()
        secs = self.conn.execute("SELECT * FROM securities WHERE household_id = %s ORDER BY symbol", (hid,)).fetchall()
        names = {a["id"]: a["name"] for a in accts}
        sym = {s["id"]: s["symbol"] for s in secs}
        with core.household(self.conn, hid) as st:
            ts = [t for t in st.txns() if t.kind == "transfer"]
        rows = "".join(f"<tr><td>{t.day}</td><td>{E_(sym.get(t.security, '?'))}</td><td>{E_(names.get(t.account, '?'))}</td>"
                       f"<td>{E_(names.get(t.to_account, '?'))}</td></tr>" for t in sorted(ts, key=lambda t: t.day, reverse=True))
        body = (f"<div class=card><p>A transfer moves every share of the security that the source account holds on that date, "
                f"keeping each lot's basis and dates.</p><div class=tablewrap><table><tr><th>Date</th><th>Security</th><th>From</th>"
                f"<th>To</th></tr>{rows}</table></div></div>")
        if core.can_change(u, hid):
            opts = "".join(f"<option value={a['id']}>{E_(a['name'])}</option>" for a in accts)
            sopts = "".join(f"<option value={s['id']}>{E_(s['symbol'])} - {E_(s['name'])}</option>" for s in secs)
            body += (f"<div class=card><h2>Record a transfer</h2><form method=post action='/households/{hid}/transfers'>{csrf_input(self.csrf)}"
                     f"<label>From <select name=from_account_id>{opts}</select></label><label>To <select name=to_account_id>{opts}</select></label>"
                     f"<label>Security <select name=security_id>{sopts}</select></label><label>Date <input name=trade_date type=date required></label>"
                     "<button data-action=record-transfer>Record transfer</button></form></div>")
        return self.render("Transfers", body, status, flash, err)

    def transfers_post(self, hid):
        hid = int(hid)
        u = self.change(hid)
        f = self.form
        try:
            fa, ta, sid = int(f.get("from_account_id", "")), int(f.get("to_account_id", "")), int(f.get("security_id", ""))
        except ValueError:
            return self.transfers_get(hid, "Choose both accounts and a security.", 422, True)
        day = parse_date(f.get("trade_date", ""))
        ok_ids = {r["id"] for r in self.conn.execute("SELECT id FROM accounts WHERE household_id = %s", (hid,))}
        if fa not in ok_ids or ta not in ok_ids or not self.conn.execute(
                "SELECT 1 FROM securities WHERE id = %s AND household_id = %s", (sid, hid)).fetchone():
            return self.transfers_get(hid, "Both accounts and the security must belong to this household.", 422, True)
        if fa == ta or day is None:
            return self.transfers_get(hid, "Choose two different accounts and a valid date.", 422, True)
        types = {r["id"]: r["account_type"] for r in self.conn.execute("SELECT id, account_type FROM accounts WHERE id IN (%s, %s)", (fa, ta))}
        if types.get(fa) != types.get(ta):
            return self.transfers_get(hid, "Shares cannot move between an IRA and a taxable account (R13.10).", 422, True)
        t = E.Txn(0, 0, fa, sid, "transfer", day, to_account=ta)
        ok, why = core.record(self.conn, hid, u["id"], [(t, None, None)])
        if not ok:
            return self.transfers_get(hid, f"The transfer was not recorded: {E_(why)}.", 422, True)
        return redirect(f"/households/{hid}/transfers")

    # -- corporate actions (R13) ---------------------------------------------------------------------------------
    def actions_get(self, hid, flash="", status=200, err=False):
        hid = int(hid)
        u = self.see(hid)
        secs = {r["id"]: r for r in self.conn.execute("SELECT * FROM securities WHERE household_id = %s ORDER BY symbol", (hid,))}
        sym = lambda i: E_(secs[i]["symbol"]) if i in secs else ""  # noqa: E731
        rows = []
        for t in ("corporate_actions", "lb_corporate_actions"):
            for r in self.conn.execute(core.ACTION_SQL.format(t=t), (hid,)):
                rows.append(r)
        rows.sort(key=lambda r: (r["action_date"], r["id"]), reverse=True)
        items = []
        for r in rows:
            if r["kind"] == "spinoff":
                what = (f"spin-off of {sym(r['new_security_id'])}: {E.qty_str(Fraction(str(r['ratio'])))} per share; values "
                        f"{r['parent_fmv']:f} and {r['new_fmv']:f}; cash in lieu at {r['cash_in_lieu_price']:f}")
            elif r["kind"] == "merger":
                what = ("cash-only merger" if not r["acquirer_security_id"] else f"merger into {sym(r['acquirer_security_id'])}: "
                        f"{E.qty_str(Fraction(str(r['shares_per_share'])))} shares (value {r['acquirer_fmv']:f}) plus") + \
                       f" {r['cash_per_share']:f} cash per share"
            else:
                what = f"return of capital of {r['amount_per_share']:f} per share"
            items.append(f"<tr data-action-id='{r['id']}' data-kind='{r['kind']}' data-action-date='{r['action_date']}' "
                         f"data-symbol='{sym(r['security_id'])}'><td>{r['action_date']}</td><td>{sym(r['security_id'])}</td>"
                         f"<td>{E_(what)}</td></tr>")
        body = ("<div class=card><p>Corporate actions apply to every account holding the security on their date, before that "
                "day's trades. Spin-offs split each lot's basis; mergers carry lots into the acquirer; returns of capital reduce "
                "basis.</p><div class=tablewrap><table><tr><th>Date</th><th>Security</th><th>Terms</th></tr>"
                f"{''.join(items)}</table></div></div>")
        if core.can_change(u, hid):
            opts = "".join(f"<option value={i}>{E_(r['symbol'])} - {E_(r['name'])} ({r['currency']})</option>" for i, r in secs.items())
            body += (f"<div class=card><h2>Record a corporate action</h2><form method=post action='/households/{hid}/corporate-actions'>"
                     f"{csrf_input(self.csrf)}<label>Kind <select name=kind><option value=spinoff>Spin-off</option><option value=merger>Merger"
                     "</option><option value=return_of_capital>Return of capital</option></select></label>"
                     f"<label>Security (parent, target or distributing) <select name=security_id>{opts}</select></label>"
                     "<label>Date <input type=date name=action_date required></label>"
                     f"<h3>Spin-off</h3><label>New security <select name=new_security_id><option value=''></option>{opts}</select></label>"
                     "<label>New shares per share <input name=ratio inputmode=decimal></label>"
                     "<label>Parent value per share <input name=parent_fmv inputmode=decimal></label>"
                     "<label>New security value per share <input name=new_fmv inputmode=decimal></label>"
                     "<label>Cash-in-lieu price <input name=cash_in_lieu_price inputmode=decimal></label>"
                     f"<h3>Merger</h3><label>Acquirer (empty for cash only) <select name=acquirer_security_id><option value=''></option>{opts}"
                     "</select></label><label>Acquirer shares per share <input name=shares_per_share inputmode=decimal></label>"
                     "<label>Cash per share <input name=cash_per_share inputmode=decimal></label>"
                     "<label>Acquirer value per share <input name=acquirer_fmv inputmode=decimal></label>"
                     "<h3>Return of capital</h3><label>Amount per share <input name=amount_per_share inputmode=decimal></label>"
                     "<p><button data-action=record-corporate-action>Record corporate action</button></p></form></div>")
        return self.render("Corporate actions", role_note(u) + body, status, flash, err)

    def actions_post(self, hid):
        hid = int(hid)
        u = self.change(hid)
        f = self.form
        bad = lambda msg: self.actions_get(hid, f"Nothing was recorded: {E_(msg)}", 422, True)  # noqa: E731
        kind = f.get("kind", "")
        day = parse_date(f.get("action_date", ""))
        try:
            sid = int(f.get("security_id", ""))
        except ValueError:
            return bad("choose a security.")
        if kind not in ("spinoff", "merger", "return_of_capital") or day is None:
            return bad("choose a kind and a valid date.")

        def opt_id(name):
            v = f.get(name, "").strip()
            if not v:
                return None
            return int(v) if v.isdigit() else -1
        terms = {}
        if kind == "spinoff":
            terms["new_security_id"] = opt_id("new_security_id")
            terms["ratio"] = parse_decimal(f.get("ratio", ""), 8, positive=True)
            for k in ("parent_fmv", "new_fmv", "cash_in_lieu_price"):
                terms[k] = parse_decimal(f.get(k, ""), 6, positive=True)
            if terms["new_security_id"] in (None, -1) or any(terms[k] is None for k in ("ratio", "parent_fmv", "new_fmv", "cash_in_lieu_price")):
                return bad("a spin-off needs the new security, a positive ratio (up to 8 decimals) and positive values (up to 6).")
        elif kind == "merger":
            terms["shares_per_share"] = parse_decimal(f.get("shares_per_share", "") or "0", 8)
            terms["cash_per_share"] = parse_decimal(f.get("cash_per_share", "") or "0", 6)
            acq, fmv = opt_id("acquirer_security_id"), f.get("acquirer_fmv", "").strip()
            if terms["shares_per_share"] is None or terms["cash_per_share"] is None or acq == -1:
                return bad("check the merger terms (shares up to 8 decimals, amounts up to 6).")
            if terms["shares_per_share"] == 0 and terms["cash_per_share"] == 0:
                return bad("a merger pays cash, shares or both.")
            if terms["shares_per_share"] > 0:
                terms["acquirer_fmv"] = parse_decimal(fmv, 6, positive=True)
                if acq is None or terms["acquirer_fmv"] is None:
                    return bad("a merger paying shares needs the acquirer and a positive acquirer value.")
                terms["acquirer_security_id"] = acq
            elif acq is not None or fmv:
                return bad("a cash-only merger names no acquirer.")
        else:
            terms["amount_per_share"] = parse_decimal(f.get("amount_per_share", ""), 6, positive=True)
            if terms["amount_per_share"] is None:
                return bad("enter a positive amount per share (up to 6 decimals).")
        ok, why = core.record_action(self.conn, hid, u["id"], kind, sid, day, terms)
        if not ok:
            return bad(why + ".")
        return redirect(f"/households/{hid}/corporate-actions")

    # -- accounts ----------------------------------------------------------------------------------------------
    def account_get(self, aid, flash="", status=200, err=False, extra=""):
        a = self.account_row(int(aid))
        u = self.see(a["household_id"])
        hid = a["household_id"]
        with core.household(self.conn, hid) as st:
            ts = sorted((t for t in st.txns() if t.account == a["id"] or t.to_account == a["id"]),
                        key=lambda t: (t.day, t.tid), reverse=True)
            secs = st.securities
            changes = [c for c in st.changes if c[0] == a["id"]]
        desc = {r["id"]: r for r in self.conn.execute("SELECT id, description, memo FROM lb_transactions WHERE account_id = %s", (a["id"],))}
        leg = {r["id"]: r for r in self.conn.execute("SELECT id, memo FROM legacy_transactions WHERE account_id = %s AND memo IS NOT NULL", (a["id"],))}
        trs = []
        for t in ts:
            s = secs.get(t.security, {})
            what = t.kind if t.kind != "transfer" else ("transfer in" if t.to_account == a["id"] else "transfer out")
            q = E.qty_str(t.quantity) if t.quantity is not None and t.kind != "transfer" else (f"{t.ratio[0]}:{t.ratio[1]}" if t.ratio else "")
            amt = f"{t.amount // 100:,}.{t.amount % 100:02d} {E_(s.get('currency', ''))}" if t.amount is not None else ""
            note = desc.get(t.tid) or {}
            text = " ".join(x for x in [note.get("description") or "", note.get("memo") or "", (leg.get(t.tid) or {}).get("memo") or ""] if x)
            trs.append(f"<tr data-txn-id='{t.tid}' data-kind='{t.kind}' data-trade-date='{t.day}' data-symbol='{E_(s.get('symbol', ''))}' "
                       f"data-ref='{E_(t.ref or '')}'><td>{t.day}</td><td>{what}</td><td>{E_(s.get('symbol', ''))}</td><td class=num>{q}</td>"
                       f"<td class=num>{amt}</td><td>{E_(t.ref or '')}</td><td>{E_(text)}</td></tr>")
        imps = self.conn.execute("SELECT * FROM lb_imports WHERE account_id = %s ORDER BY id DESC", (a["id"],)).fetchall()
        ilist = "".join(f"<li data-import-filename='{E_(i['filename'])}'>{E_(i['filename'])}: {i['imported']} recorded, "
                        f"{i['duplicates']} duplicates ({i['created_at']:%Y-%m-%d %H:%M} UTC)</li>" for i in imps)
        method_hist = "".join(f"<li>{m} from {eff}</li>" for _a, eff, _i, m in changes)
        body = (f"<div data-account-id='{a['id']}' class=card><p>{'Northwind Securities' if a['broker'] == 'northwind' else 'Harbor Direct'} "
                f"account {E_(a['account_number'])} · lot method {E_(a['lot_method'])} initially"
                f"{'; changes: <ul>' + method_hist + '</ul>' if method_hist else ''}</p>"
                f"<p><a href='/accounts/{a['id']}/lots'>Open lots</a> · <a href='/households/{hid}'>Household</a></p></div>{extra}")
        if core.can_change(u, hid):
            fmt = "Northwind CSV (comma-separated)" if a["broker"] == "northwind" else "Harbor Direct CSV (semicolon-separated)"
            body += (f"<div class=row><div class=card><h2>Import a statement</h2><p class=muted>Upload a {fmt} export for this account. "
                     f"Rows already recorded are skipped as duplicates; if any row has a problem, nothing is imported.</p>"
                     f"<form method=post action='/accounts/{a['id']}/imports' enctype=multipart/form-data>{csrf_input(self.csrf)}"
                     "<label>Statement file <input type=file name=file required></label><button data-action=import-statement>Import</button></form>"
                     f"<p><a href='/accounts/{a['id']}/transactions/new'>Enter a transaction by hand</a></p></div>"
                     f"<div class=card><h2>Lot method</h2><p class=muted>Choose how sales without chosen lots are matched from a date on.</p>"
                     f"<form method=post action='/accounts/{a['id']}/method'>{csrf_input(self.csrf)}<label>Method <select name=lot_method>"
                     "<option>FIFO</option><option>HIFO</option></select></label><label>Effective from <input type=date name=effective_from required>"
                     "</label><button data-action=change-method>Record change</button></form></div></div>")
        body += (f"<div class=card><h2>Imports</h2><ul>{ilist or '<li>No imports yet.</li>'}</ul></div>"
                 f"<div class=card><h2>Transactions</h2><div class=tablewrap><table><tr><th>Date</th><th>Kind</th><th>Symbol</th>"
                 f"<th class=num>Quantity</th><th class=num>Amount</th><th>Reference</th><th>Description</th></tr>{''.join(trs)}</table></div></div>")
        return self.render(a["name"], role_note(u) + body, status, flash, err)

    def imports_post(self, aid):
        a = self.account_row(int(aid))
        limit = int(ENV.get("IMPORT_MAX_BYTES", "1048576"))
        raw = self._read_body(limit + 262144)
        if raw is None:
            raise Resp(413, page("File too large", f"<p>Statement files may be at most {limit:,} bytes. Nothing was imported.</p>",
                                 self.user, self.csrf), {"Connection": "close"})
        fields, files = parse_multipart(self.headers.get("Content-Type", ""), raw)
        u = self.change(a["household_id"], fields.get("csrf_token", ""))
        if "file" not in files:
            return self.account_get(aid, "Choose a statement file to import.", 400, True)
        filename, data = files["file"]
        if len(data) > limit:
            return self.account_get(aid, f"That file is {len(data):,} bytes; statement files may be at most {limit:,} bytes. "
                                    "Nothing was imported.", 413, True)
        res = core.import_file(self.conn, a, u["id"], filename or "statement.csv", data)
        if res.status == "imported":
            box = (f"<div class=card data-import-status=imported data-imported-count='{res.imported}' data-duplicate-count='{res.duplicates}'>"
                   f"<h2>Import finished</h2><p><b>{res.imported}</b> new transaction{'s' if res.imported != 1 else ''} recorded from "
                   f"<i>{E_(filename)}</i>; <b>{res.duplicates}</b> row{'s were' if res.duplicates != 1 else ' was'} already recorded and "
                   "skipped as duplicates.</p></div>")
            return self.account_get(aid, extra=box)
        items = "".join(f"<li data-error-line='{line}'>{'Line ' + str(line) if line else 'The file'}: {E_(why)}</li>" for line, why in res.errors)
        box = (f"<div class=card data-import-status=rejected><h2>Nothing was imported</h2><p>The file <i>{E_(filename)}</i> has "
               f"{len(res.errors)} problem{'s' if len(res.errors) != 1 else ''}. Fix them and upload the whole file again; rows that "
               f"were fine were not recorded either.</p><ul>{items}</ul></div>")
        return self.account_get(aid, status=422, extra=box)

    def method_post(self, aid):
        a = self.account_row(int(aid))
        u = self.change(a["household_id"])
        m, eff = self.form.get("lot_method", ""), parse_date(self.form.get("effective_from", ""))
        if m not in ("FIFO", "HIFO") or eff is None:
            return self.account_get(aid, "Choose FIFO or HIFO and a valid date.", 422, True)
        with self.conn.transaction():
            core.lock_household(self.conn, a["household_id"])
            self.conn.execute("INSERT INTO lot_method_changes (account_id, effective_from, lot_method) VALUES (%s,%s,%s)", (a["id"], eff, m))
            core.bump(self.conn, a["household_id"])
        return redirect(f"/accounts/{a['id']}")

    def lots_get(self, aid):
        a = self.account_row(int(aid))
        self.see(a["household_id"])
        today = now_utc().date()
        with core.household(self.conn, a["household_id"]) as st:
            res = core.household_result(self.conn, st)
            lots = sorted((l for l in res.lots if l.account == a["id"]), key=lambda l: (st.securities[l.security]["symbol"], l.key()))
            secs = st.securities
        rows = []
        for l in lots:
            s = secs[l.security]
            term = E.term_of(l.holding, today)
            wash = ("<span class='badge wash'>wash-sale adjusted</span>" if l.wash_adjusted else "")
            rows.append(f"<tr data-lot-id='{l.acq}' data-symbol='{E_(s['symbol'])}' data-quantity='{E.qty_str(l.qty)}' data-acquired='{l.acquired}' "
                        f"data-holding-start='{l.holding}' data-basis-cents='{l.basis}' data-wash-adjusted='{'true' if l.wash_adjusted else 'false'}'>"
                        f"<td>{l.acq}</td><td>{E_(s['symbol'])}</td><td class=num>{E.qty_str(l.qty)}</td><td class=num>{dollars(l.basis)}</td>"
                        f"<td>{l.acquired}</td><td>{l.holding}</td><td><span class='badge {term}'>{term}-term</span> {wash}</td></tr>")
        body = (f"<div class=card><p>Open lots of <a href='/accounts/{a['id']}'>{E_(a['name'])}</a>. The holding period decides short- or "
                "long-term (long-term once held more than a year). A wash-sale adjusted lot's basis includes a disallowed loss and its "
                "holding period was extended by the sold shares' holding period.</p><div class=tablewrap><table><tr><th>Lot</th><th>Symbol</th>"
                "<th class=num>Quantity</th><th class=num>Basis (USD)</th><th>Acquired</th><th>Holding period from</th><th>Today</th></tr>"
                f"{''.join(rows)}</table></div></div>")
        return self.render("Open lots", body)

    # -- manual transactions ---------------------------------------------------------------------------------
    def txn_new_get(self, aid, flash="", status=200, err=False, values=None):
        a = self.account_row(int(aid))
        u = self.see(a["household_id"])
        if not core.can_change(u, a["household_id"]):
            return self.render("Enter a transaction", "<p class=note>Your login is read-only; transactions cannot be entered.</p>")
        values = values or {}
        kind = self.query.get("kind", values.get("kind", "buy"))
        sid = self.query.get("security_id", values.get("security_id", ""))
        secs = self.conn.execute("SELECT * FROM securities WHERE household_id = %s ORDER BY symbol", (a["household_id"],)).fetchall()
        sopts = "".join(f"<option value={s['id']}{' selected' if str(s['id']) == str(sid) else ''}>{E_(s['symbol'])} - {E_(s['name'])}</option>"
                        for s in secs)
        kopts = "".join(f"<option{' selected' if k == kind else ''}>{k}</option>" for k in ("buy", "sell", "reinvest", "dividend", "split"))
        lots_html = ""
        if kind == "sell" and sid:
            with core.household(self.conn, a["household_id"]) as st:
                res = core.household_result(self.conn, st)
                by: dict[int, list] = {}
                for l in res.lots:
                    if l.account == a["id"] and str(l.security) == str(sid):
                        by.setdefault(l.acq, []).append(l)
            rows = "".join(f"<tr><td>{acq}</td><td>{ls[0].acquired}</td><td class=num>{E.qty_str(sum((x.qty for x in ls), Fraction(0)))}</td>"
                           f"<td class=num>{dollars(sum(x.basis for x in ls))}</td><td><input name='lot_{acq}' inputmode=decimal size=8 "
                           f"aria-label='Shares from lot {acq}'></td></tr>" for acq, ls in sorted(by.items(), key=lambda kv: kv[1][0].key()))
            lots_html = ("<h2>Choose lots (optional)</h2><p class=muted>Enter how many shares to sell from each lot. Leave all empty and the "
                         "account's lot method on the trade date (FIFO or HIFO) chooses the lots. Chosen quantities must add up to the "
                         "quantity sold.</p><div class=tablewrap><table><tr><th>Lot</th><th>Acquired</th><th class=num>Shares open now</th>"
                         f"<th class=num>Basis</th><th>Sell</th></tr>{rows}</table></div>")
        elif kind == "sell":
            lots_html = ("<p class=muted>Without chosen lots, the account's lot method on the trade date (FIFO or HIFO) chooses the lots. "
                         f"To choose lots, pick the security first: <a href='/accounts/{a['id']}/transactions/new?kind=sell'>sell form</a> "
                         "with <code>security_id</code>.</p>")
        v = lambda k: E_(values.get(k, ""))  # noqa: E731
        body = (f"<div class=card><form method=post action='/accounts/{a['id']}/transactions'>{csrf_input(self.csrf)}"
                f"<label>Kind <select name=kind>{kopts}</select></label><label>Trade date <input type=date name=trade_date value='{v('trade_date')}' required></label>"
                f"<label>Security <select name=security_id>{sopts}</select></label>"
                f"<label>Quantity (buy, sell, reinvest) <input name=quantity inputmode=decimal value='{v('quantity')}'></label>"
                f"<label>Price per share in the security's currency <input name=price inputmode=decimal value='{v('price')}'></label>"
                f"<label>Fee or commission <input name=fee inputmode=decimal value='{v('fee')}'></label>"
                f"<label>Dividend amount <input name=amount inputmode=decimal value='{v('amount')}'></label>"
                f"<label>Split ratio N:M <input name=ratio value='{v('ratio')}' placeholder='10:1'></label>"
                f"<label>Memo <input name=memo maxlength=500 value='{v('memo')}'></label>{lots_html}"
                "<p><button data-action=record-transaction>Record transaction</button></p></form>"
                "<p class=muted>A buy's amount is quantity × price + fee and a sale's is quantity × price − fee, rounded to the cent.</p></div>")
        return self.render(f"Enter a transaction: {a['name']}", body, status, flash, err)

    def txns_post(self, aid):
        a = self.account_row(int(aid))
        u = self.change(a["household_id"])
        hid = a["household_id"]
        f = self.form
        kind = f.get("kind", "")
        day = parse_date(f.get("trade_date", ""))
        bad = lambda msg: self.txn_new_get(aid, f"Nothing was recorded: {msg}", 422, True, f)  # noqa: E731
        try:
            sid = int(f.get("security_id", ""))
        except ValueError:
            return bad("choose a security.")
        sec = self.conn.execute("SELECT * FROM securities WHERE id = %s AND household_id = %s", (sid, hid)).fetchone()
        if not sec:
            return bad("the security does not belong to this household.")
        if kind not in ("buy", "sell", "reinvest", "dividend", "split") or day is None:
            return bad("choose a kind and a valid trade date.")
        memo = f.get("memo", "")
        if len(memo) > 500:
            return bad("the memo is longer than 500 characters.")
        t = E.Txn(0, 0, a["id"], sid, kind, day)
        if kind in ("buy", "sell", "reinvest"):
            q = parse_decimal(f.get("quantity", ""), 8, positive=True)
            p = parse_decimal(f.get("price", ""), 6)
            fee = parse_decimal(f.get("fee", "") or "0", 2)
            if q is None or p is None or fee is None:
                return bad("enter a positive quantity (up to 8 decimals), a price (up to 6) and a fee (up to 2).")
            gross = q * p + fee if kind != "sell" else q * p - fee
            if gross <= 0:
                return bad("the amount must be positive.")
            t.quantity, t.amount = q, E.rhu(gross * 100)
            if t.amount <= 0:
                return bad("the amount must be positive.")
            if kind == "sell":
                sel = []
                for k, val in f.items():
                    m = re.fullmatch(r"lot_(\d+)", k)
                    if m and val.strip() not in ("", "0"):
                        qq = parse_decimal(val, 8, positive=True)
                        if qq is None:
                            return bad(f"the quantity for lot {m.group(1)} is not valid.")
                        sel.append((int(m.group(1)), qq))
                if sel:
                    t.selection = sorted(sel)
        elif kind == "dividend":
            amt = parse_decimal(f.get("amount", ""), 2, positive=True)
            if amt is None:
                return bad("enter a positive dividend amount (up to 2 decimals).")
            t.amount = int(amt * 100)
        else:
            try:
                t.ratio = core.S.ratio(f.get("ratio", ""))
            except core.S.Bad as e:
                return bad(str(e) + ".")
        ok, why = core.record(self.conn, hid, u["id"], [(t, None, memo or None)])
        if not ok:
            return bad(why + ".")
        return redirect(f"/accounts/{a['id']}")

    # -- tax years ---------------------------------------------------------------------------------------------
    def year_get(self, hid, year, flash="", status=200, err=False):
        hid, year = int(hid), int(year)
        u = self.see(hid)
        h = self.conn.execute("SELECT name FROM households WHERE id = %s", (hid,)).fetchone()
        with core.household(self.conn, hid) as st:
            rep = core.year_report(self.conn, st, year)
            L = st.lock_through
            lock_row = self.conn.execute("SELECT locked_at FROM lb_locks WHERE household_id = %s AND year = %s", (hid, year)).fetchone()
        t = rep["totals"]
        state_txt = ("<span class='badge locked'>Locked</span> " + (f"since {lock_row['locked_at']:%Y-%m-%d %H:%M} UTC. " if lock_row else
                     "by the old tool. ") + "Its reports are frozen; later changes appear as adjustments in "
                     f"{(L or year) + 1}." if rep["locked"] else "<span class=badge>Open</span> This year is not locked; its figures follow "
                     "every change.")
        totals = (f"<div class=totals data-year='{year}' data-locked='{'true' if rep['locked'] else 'false'}' "
                  f"data-total-short-cents='{t['short']}' data-total-long-cents='{t['long']}' data-total-disallowed-cents='{t['disallowed']}' "
                  f"data-total-dividends-cents='{t['dividends']}' data-total-adjustment-cents='{t['adjustments']}'>"
                  f"<div>Short-term gains<b>{dollars(t['short'])}</b></div><div>Long-term gains<b>{dollars(t['long'])}</b></div>"
                  f"<div>Wash-sale loss disallowed<b>{dollars(t['disallowed'])}</b></div><div>Dividends<b>{dollars(t['dividends'])}</b></div>"
                  f"<div>Adjustments for earlier years<b>{dollars(t['adjustments'])}</b></div></div>")
        rows = "".join("<tr>" + "".join(f"<td{' class=num' if i > 1 else ''}>{E_(c) if i < 2 else dollars(int(c))}</td>" for i, c in enumerate(r))
                       + "</tr>" for r in rep["rows"][:400])
        more = f"<p class=muted>Showing 400 of {len(rep['rows'])} securities; the CSV has all.</p>" if len(rep["rows"]) > 400 else ""
        head = (f"<div class=card><p>{state_txt}</p>{totals}<p><a href='/households/{hid}/reports/{year}/summary.csv'>summary.csv</a> · "
                f"<a href='/households/{hid}/reports/{year}/realized.csv'>realized.csv</a> (every sale, lot by lot)</p></div>")
        body = (f"<div class=card><h2>By security</h2><div class=tablewrap><table><tr><th>Symbol</th><th>Name</th><th class=num>Short-term</th>"
                f"<th class=num>Long-term</th><th class=num>Disallowed</th><th class=num>Dividends</th><th class=num>Adj. short</th>"
                f"<th class=num>Adj. long</th><th class=num>Adj. dividends</th></tr>{rows}</table></div>{more}"
                f"<p class=muted>Adjustment columns are corrections to years that were already locked, counted in this year.</p></div>")
        may_lock = core.can_lock(self.conn, u, hid)
        lockable = (may_lock and not rep["locked"] and date(year, 12, 31) < now_utc().date()
                    and (L is None or year == L + 1))
        lock_html = ""  # placed right after the totals, before the per-security table, so it stays near the top on phones
        if lockable:
            lock_html = (f"<div class=card><h2>Lock {year}</h2><p class=note>Locking freezes {year}'s reports and figures for good. Anything "
                     f"recorded later that changes {year} or an earlier year appears as an adjustment in {year + 1} instead. Each member "
                     f"of the household gets a year-end summary email. This cannot be undone.</p>"
                     f"<form method=post action='/households/{hid}/reports/{year}/lock'>{csrf_input(self.csrf)}"
                     f"<button data-action=lock-year>Lock {year}</button></form></div>")
        elif may_lock and not rep["locked"]:
            why = (f"{year} has not ended yet." if not date(year, 12, 31) < now_utc().date() else f"Lock {L + 1} first; years are locked in order.")
            lock_html = f"<div class=card><p class=muted>{year} cannot be locked yet: {why}</p></div>"
        return self.render(f"{h['name']}: tax year {year}", role_note(u) + head + lock_html + body, status, flash, err)

    def lock_post(self, hid, year):
        hid, year = int(hid), int(year)
        u = self.need_user()
        if not core.can_lock(self.conn, u, hid):
            raise self.forbid("Your access does not allow locking tax years." if core.can_see(self.conn, u, hid) else
                              "You are not allowed to do that.")
        self.check_csrf()
        code, why = core.lock_year(self.conn, hid, year, u)
        if code != 200:
            return self.year_get(hid, year, f"{year} was not locked: {E_(why)}", code, True)
        return redirect(f"/households/{hid}/reports/{year}")

    def _csv(self, text: str, name: str) -> Resp:
        return Resp(200, text.encode("utf-8"), {"Content-Disposition": f"attachment; filename={name}"}, "text/csv; charset=utf-8")

    def summary_csv_get(self, hid, year):
        hid, year = int(hid), int(year)
        self.see(hid)
        with core.household(self.conn, hid) as st:
            rep = core.year_report(self.conn, st, year)
        return self._csv(rep["summary_csv"], f"summary-{year}.csv")

    def realized_csv_get(self, hid, year):
        hid, year = int(hid), int(year)
        self.see(hid)
        with core.household(self.conn, hid) as st:
            rep = core.year_report(self.conn, st, year)
        return self._csv(rep["realized_csv"], f"realized-{year}.csv")

    def unrealized_csv_get(self, hid):
        hid = int(hid)
        self.see(hid)
        with core.household(self.conn, hid) as st:
            text = core.unrealized_csv(self.conn, st)
        return self._csv(text, "unrealized.csv")


def safe_next(nxt: str) -> str:
    """R2.6: only a path on this site; anything else (//host, /\\host, scheme:..., control characters) goes home."""
    if nxt.startswith("/") and nxt[1:2] not in ("/", "\\") and not any(ord(c) < 32 or ord(c) == 127 for c in nxt):
        return nxt
    return "/"


def parse_multipart(ctype: str, raw: bytes) -> tuple[dict, dict]:
    fields, files = {}, {}
    if not ctype.startswith("multipart/form-data"):
        return fields, files
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + ctype.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw)
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        fn = part.get_filename()
        if fn is not None:
            files[name] = (fn, payload)
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128


def main() -> None:
    core.apply_schema()
    core.start_outbox()
    srv = Server(("0.0.0.0", int(ENV.get("PORT", "8080"))), Handler)
    srv.serve_forever()
