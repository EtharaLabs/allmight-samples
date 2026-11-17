"""Pickpath HTTP layer: stdlib ThreadingHTTPServer, server-side sessions, CSRF, HTML pages, order API, webhook."""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import threading
import traceback
import urllib.parse
from datetime import date, datetime, timedelta
from decimal import Decimal
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

from . import core, db, payments
from .core import num
from .db import connect, dollars, norm_email, sha

ENV = os.environ
COOKIE = "pp_session"
E = html.escape
CUSTOMER_ROLES = ("buyer", "approver")
INTERNAL = ("supervisor", "finance")

CSS = """
*{box-sizing:border-box}body{margin:0;font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1d2125;background:#f4f5f2}
header{background:#2f4a3a;color:#fff;padding:12px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between}
header a{color:#fff;margin-right:12px}header nav{display:flex;flex-wrap:wrap;gap:4px}main{max-width:1100px;margin:0 auto;padding:16px;overflow-wrap:anywhere}
header strong,header span{overflow-wrap:anywhere}code{overflow-wrap:anywhere;word-break:break-all}
.card{background:#fff;border:1px solid #d9ddd5;border-radius:8px;padding:16px;margin:0 0 16px;overflow-wrap:anywhere}
.flash{padding:12px 16px;border-radius:8px;margin:0 0 16px;background:#e8f3ec;border:1px solid #9fcbad}
.flash.err{background:#fdecec;border-color:#f3a9a9}button{font:inherit;padding:8px 14px;border-radius:6px;border:1px solid #2f4a3a;background:#2f4a3a;color:#fff;cursor:pointer}
button.secondary{background:#fff;color:#2f4a3a}input,select,textarea{font:inherit;padding:8px;border:1px solid #b9c0b6;border-radius:6px;width:100%;max-width:420px}
label{display:block;margin:8px 0}.muted{color:#5b6570}.pill{display:inline-block;padding:2px 10px;border-radius:999px;background:#eef1ea;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}.kv{border:1px solid #e1e4de;border-radius:6px;padding:8px;background:#fafbf9}
.line{border:1px solid #e1e4de;border-radius:8px;padding:10px;margin:8px 0;background:#fcfdfb}.stop{border-left:4px solid #2f4a3a;padding:8px 12px;margin:10px 0;background:#fff}
.task{border:1px dashed #c7ccc3;border-radius:6px;padding:8px;margin:6px 0}.row{display:flex;flex-wrap:wrap;gap:8px;align-items:end}
.row label{flex:1 1 140px}table{border-collapse:collapse;width:100%}td,th{padding:6px 8px;border-bottom:1px solid #e5e8e2;text-align:left}
.tablewrap{overflow-x:auto}
"""


def page(title: str, body: str, user=None, csrf: str | None = None, flash: str = "", err: bool = False) -> str:
    nav = ""
    if user:
        links = ['<a href="/">Home</a>', '<a href="/profile">Profile</a>']
        r = user["role"]
        if r in CUSTOMER_ROLES:
            links += ['<a href="/orders">Orders</a>', '<a href="/orders/new">New order</a>', '<a href="/account/api-keys">API keys</a>']
        if r == "supervisor":
            links += ['<a href="/orders">Orders</a>', '<a href="/waves">Waves</a>', '<a href="/skus/new">New SKU</a>',
                      '<a href="/receipts/new">Receive</a>']
        if r == "picker":
            links += ['<a href="/waves">My waves</a>']
        if r == "finance":
            links += ['<a href="/orders">Orders</a>', '<a href="/customers/new">New customer</a>', '<a href="/users/new">New user</a>',
                      '<a href="/prices">Prices</a>']
        nav = (f"<nav>{''.join(links)}</nav><form method=post action=/signout style='margin:0'>{csrf_input(csrf)}"
               f"<span style='color:#d7e6dc;margin-right:8px'>{E(user['full_name'])}</span>"
               "<button class=secondary data-action=signout>Sign out</button></form>")
    fl = f"<div class='flash{' err' if err else ''}' role=status>{E(flash)}</div>" if flash else ""
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{E(title)} · Pickpath</title><style>{CSS}</style></head><body><header><strong>Pickpath</strong>{nav}</header>"
            f"<main><h1>{E(title)}</h1>{fl}{body}</main></body></html>")


def csrf_input(token: str | None) -> str:
    return f"<input type=hidden name=csrf_token data-csrf value='{E(token or '')}'>" if token else ""


class Resp(Exception):
    def __init__(self, status: int, body: str | bytes = "", headers: dict | None = None, ctype: str = "text/html; charset=utf-8"):
        self.status, self.body, self.headers, self.ctype = status, body, headers or {}, ctype


def redirect(to: str, extra: dict | None = None) -> Resp:
    return Resp(303, "", {"Location": to, **(extra or {})})


def safe_next(nxt: str) -> str:
    if (not nxt or not nxt.startswith("/") or nxt[1:2] in ("/", "\\") or "\\" in nxt
            or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in nxt)):
        return "/"
    return nxt


def valid_name(name: str, mx: int = 100) -> bool:
    return 1 <= len(name) <= mx and not db.has_ctrl(name)


def jresp(status: int, obj) -> Resp:
    return Resp(status, json.dumps(obj), ctype="application/json")


ROUTES = [
    (r"/", "home"), (r"/signin", "signin"), (r"/signout", "signout"), (r"/password-reset", "reset_request"),
    (r"/password-reset/([A-Za-z0-9_-]{10,200})", "reset_token"), (r"/profile", "profile"),
    (r"/skus/new", "sku_new"), (r"/skus/(\d+)", "sku_view"), (r"/receipts/new", "receipt_new"),
    (r"/lots/(\d+)", "lot_view"), (r"/lots/(\d+)/count", "lot_count"), (r"/prices", "prices"),
    (r"/customers/new", "customer_new"), (r"/customers/(\d+)", "customer_view"), (r"/users/new", "user_new"),
    (r"/users/(\d+)", "user_view"), (r"/users/(\d+)/deactivate", "user_deactivate"),
    (r"/orders", "orders"), (r"/orders/new", "order_new"), (r"/orders/export\.csv", "orders_export"), (r"/orders/(\d+)", "order_view"),
    (r"/orders/(\d+)/approve", "order_approve"), (r"/orders/(\d+)/cancel", "order_cancel"), (r"/orders/(\d+)/pay", "order_pay"),
    (r"/orders/(\d+)/ship", "order_ship"), (r"/orders/(\d+)/rmas", "order_rma"),
    (r"/orders/(\d+)/lines/(\d+)/cancel-backorder", "order_cancel_backorder"), (r"/shipments/(\d+)", "shipment_view"),
    (r"/rmas/(\d+)", "rma_view"), (r"/rmas/(\d+)/receive", "rma_receive"), (r"/rmas/(\d+)/inspect", "rma_inspect"),
    (r"/waves", "waves"), (r"/waves/(\d+)", "wave_view"), (r"/pick-tasks/(\d+)/confirm", "task_confirm"),
    (r"/account/api-keys", "api_keys"), (r"/account/api-keys/(\d+)/revoke", "api_key_revoke"),
    (r"/reports/on-hand\.csv", "report_on_hand"), (r"/reports/ledger\.csv", "report_ledger"),
    (r"/reports/valuation\.csv", "report_valuation"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "Pickpath"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- plumbing ------------------------------------------------------------------------------------
    def _send(self, r: Resp) -> None:
        body = r.body.encode() if isinstance(r.body, str) else r.body
        self.send_response(r.status)
        self.send_header("Content-Type", r.ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in r.headers.items():
            for item in (v if isinstance(v, list) else [v]):
                self.send_header(k, item)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _form(self, raw: bytes) -> tuple[dict, dict]:
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("application/x-www-form-urlencoded") or not ctype:
            multi = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
            return {k: v[-1] for k, v in multi.items()}, multi
        return {}, {}

    def _session(self, conn):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:
            return None, None
        if COOKIE not in c:
            return None, None
        row = conn.execute("SELECT csrf, user_id FROM pp_sessions WHERE sid_hash = %s AND NOT revoked", (sha(c[COOKIE].value),)).fetchone()
        if not row:
            return None, None
        u = core.get_user(conn, row["user_id"])
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
        path = url.path
        self.user, self.csrf = None, None
        try:
            if path == "/healthz":
                raise self.healthz()
            raw = self._read() if method == "POST" else b""
            if path == "/webhooks/payments" and method == "POST":
                raise self.webhook(raw)
            if path.startswith("/api/v1/"):
                raise self.api(method, path, raw)
            self.form, self.multi = self._form(raw) if method == "POST" else ({}, {})
            with connect() as conn:
                self.conn = conn
                self.user, self.csrf = self._session(conn)
                for pattern, handler in ROUTES:
                    m = re.fullmatch(pattern, path)
                    if m:
                        fn = getattr(self, f"{handler}_{method.lower()}", None)
                        if fn is None:
                            raise Resp(405, page("Not allowed", "<p>That method is not allowed here.</p>", self.user, self.csrf))
                        if method == "POST" and handler not in ("signin", "reset_request", "reset_token"):
                            self.need()
                            self.check_csrf()
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
            try:
                self._send(Resp(500, "Internal error", ctype="text/plain"))
            except Exception:
                pass

    # -- guards ------------------------------------------------------------------------------------------
    def need(self, roles: tuple[str, ...] | None = None):
        if not self.user:
            raise redirect("/signin?next=" + urllib.parse.quote(self.path, safe=""))
        if roles and self.user["role"] not in roles:
            raise self.forbidden()
        return self.user

    def forbidden(self, msg: str = "You are not allowed to do that.") -> Resp:
        return Resp(403, page("Not allowed", f"<p>{E(msg)}</p>", self.user, self.csrf))

    def not_found(self) -> Resp:
        return Resp(404, page("Not found", "<p>That record does not exist.</p>", self.user, self.csrf))

    def check_csrf(self) -> None:
        sent = self.form.get("csrf_token", "")
        if not self.csrf or not secrets.compare_digest(sent, self.csrf):
            raise Resp(403, page("Not allowed", "<p>This form expired or came from another site. Reload the page and try again.</p>",
                                 self.user, self.csrf))

    def render(self, title: str, body: str, status: int = 200, flash: str = "", err: bool = False) -> Resp:
        return Resp(status, page(title, body, self.user, self.csrf, flash, err))

    def commit(self):
        self.conn.commit()
        db.OUTBOX_EVENT.set()

    def order_for(self, oid: int, roles=CUSTOMER_ROLES + INTERNAL):
        u = self.need()
        o = core.get_order(self.conn, int(oid))
        if u["role"] not in roles:
            raise self.forbidden()
        if o is None or (u["role"] in CUSTOMER_ROLES and o["customer_id"] != u["customer_id"]):
            raise self.not_found()
        return o

    # -- health ------------------------------------------------------------------------------------------
    def healthz(self) -> Resp:
        try:
            with psycopg.connect(db.DATABASE_URL, connect_timeout=3) as c:
                c.execute("SET statement_timeout = 3000")
                c.execute("SELECT 1")
            return Resp(200, "ok", ctype="text/plain")
        except Exception:
            return Resp(503, "database unreachable", ctype="text/plain")

    # -- home ----------------------------------------------------------------------------------------------
    def home_get(self):
        u = self.need()
        tips = {
            "buyer": "Create orders, pay for them, follow shipments and request returns.",
            "approver": "Approve orders above your company's limit, and order like a buyer.",
            "picker": f"Pick the waves of zone {u['zone'] or ''}.",
            "supervisor": "Receive stock, count lots, create waves, ship orders and handle returns.",
            "finance": "Manage customers, users and prices; download reports.",
        }
        extra = ""
        if u["role"] in INTERNAL:
            extra = ("<p><a href='/reports/on-hand.csv'>On-hand report (CSV)</a></p>"
                     + ("<p><a href='/orders/export.csv'>Orders export (CSV)</a></p>" if u["role"] == "finance" else ""))
        return self.render(f"Welcome, {u['full_name']}", f"<div class=card><p>{E(tips[u['role']])}</p>{extra}</div>")

    # -- sign in / out --------------------------------------------------------------------------------------
    def signin_form(self, email: str = "", flash: str = "", status: int = 200) -> Resp:
        nxt = self.form.get("next") if getattr(self, "form", None) else None
        nxt = nxt if nxt is not None else self.query.get("next", "")
        body = ("<div class=card><form method=post action=/signin>"
                f"<input type=hidden name=next value='{E(nxt)}'>"
                f"<label>Email <input name=email type=email autocomplete=username value='{E(email)}' required></label>"
                "<label>Password <input name=password type=password autocomplete=current-password required></label>"
                "<button data-action=signin>Sign in</button></form>"
                "<p class=muted>First time here or forgot your password? <a href=/password-reset>Set a password</a>.</p></div>")
        return self.render("Sign in", body, status, flash, err=bool(flash))

    def signin_get(self):
        if self.user:
            return redirect("/")
        return self.signin_form()

    def signin_post(self):
        email, pw = norm_email(self.form.get("email", "")), self.form.get("password", "")
        conn = self.conn
        u = conn.execute("SELECT * FROM users WHERE lower(btrim(email)) = %s ORDER BY id LIMIT 1", (email,)).fetchone()
        bad = "That email and password don't match an active account. Check them and try again."
        cred = conn.execute("SELECT * FROM pp_credentials WHERE user_id = %s", (u["id"],)).fetchone() if u else None
        if not u or not u["active"] or not cred or not db.check_password(pw, cred["password_hash"]):
            if not cred:
                db.check_password(pw, db.hash_password("x"))
            return self.signin_form(email, bad, 401)
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        conn.execute("INSERT INTO pp_sessions (sid_hash, user_id, csrf) VALUES (%s,%s,%s)", (sha(sid), u["id"], csrf))
        conn.commit()
        return redirect(safe_next(self.form.get("next", "")),
                        {"Set-Cookie": f"{COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax"})

    def signout_post(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        if COOKIE in c:
            self.conn.execute("UPDATE pp_sessions SET revoked = true WHERE sid_hash = %s", (sha(c[COOKIE].value),))
            self.conn.commit()
        return redirect("/signin", {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})

    # -- password links --------------------------------------------------------------------------------------
    def reset_request_get(self):
        body = ("<div class=card><form method=post action=/password-reset>"
                "<label>Email <input name=email type=email required></label>"
                "<button data-action=request-reset>Email me a link</button></form></div>")
        return self.render("Set or reset your password", body)

    def reset_request_post(self):
        email = norm_email(self.form.get("email", ""))
        conn = self.conn
        u = conn.execute("SELECT * FROM users WHERE lower(btrim(email)) = %s AND active ORDER BY id LIMIT 1", (email,)).fetchone()
        if u:
            token = secrets.token_urlsafe(32)
            conn.execute("UPDATE pp_reset_tokens SET state = 'replaced' WHERE user_id = %s AND state = 'open'", (u["id"],))
            conn.execute("INSERT INTO pp_reset_tokens (token_hash, user_id, created_at) VALUES (%s,%s,now())", (sha(token), u["id"]))
            conn.commit()
            link = f"{ENV['PUBLIC_BASE_URL'].rstrip('/')}/password-reset/{token}"
            threading.Thread(target=db.send_now, args=(norm_email(u["email"]), u["full_name"], "Set your Pickpath password",
                             f"Hello {u['full_name']},\n\nUse this link to set your password. It works once and expires soon:\n\n{link}\n"),
                             daemon=True).start()
        body = "<div class=card><p>If that address belongs to an active account, we emailed it a link to set a password.</p></div>"
        return self.render("Check your email", body)

    def _token_row(self, token: str):
        return self.conn.execute("SELECT * FROM pp_reset_tokens WHERE token_hash = %s", (sha(token),)).fetchone()

    def _token_valid(self, row) -> bool:
        ttl = int(ENV.get("RESET_LINK_TTL_SECONDS", "3600"))
        return row["state"] == "open" and (db.now_utc() - row["created_at"]).total_seconds() <= ttl

    def reset_token_get(self, token):
        row = self._token_row(token)
        if not row:
            return self.not_found()
        if not self._token_valid(row):
            return self.render("Link no longer valid", "<div class=card><p>This link is no longer valid. Request a new one.</p></div>", 410)
        body = (f"<div class=card><form method=post action='/password-reset/{E(token)}'>"
                "<label>New password (at least 10 characters) <input name=password type=password minlength=10 required></label>"
                "<button data-action=set-password>Set password</button></form></div>")
        return self.render("Choose a password", body)

    def reset_token_post(self, token):
        conn = self.conn
        row = conn.execute("SELECT * FROM pp_reset_tokens WHERE token_hash = %s FOR UPDATE", (sha(token),)).fetchone()
        if not row:
            return self.not_found()
        if not self._token_valid(row):
            return self.render("Link no longer valid", "<div class=card><p>This link is no longer valid. Request a new one.</p></div>", 410)
        pw = self.form.get("password", "")
        if len(pw) < 10:
            return self.render("Choose a password", "<p>The password must be at least 10 characters.</p>", 422, "Too short.", True)
        conn.execute("UPDATE pp_reset_tokens SET state = 'used' WHERE token_hash = %s", (sha(token),))
        conn.execute("INSERT INTO pp_credentials (user_id, password_hash) VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE SET"
                     " password_hash = EXCLUDED.password_hash, updated_at = now()", (row["user_id"], db.hash_password(pw)))
        conn.commit()
        return self.render("Password set", "<div class=card><p>Your password is set. <a href=/signin>Sign in</a>.</p></div>")

    # -- profile --------------------------------------------------------------------------------------------------
    def profile_get(self, flash: str = "", status: int = 200, err: bool = False):
        u = self.need()
        body = (f"<div class=card><form method=post action=/profile>{csrf_input(self.csrf)}"
                f"<label>Display name <input name=full_name value='{E(u['full_name'])}' maxlength=100 required></label>"
                "<button data-action=save-profile>Save</button></form></div>")
        return self.render("Your profile", body, status, flash, err)

    def profile_post(self):
        name = self.form.get("full_name", "")
        if not valid_name(name):
            return self.profile_get("A display name is 1 to 100 characters with no control characters.", 422, True)
        self.conn.execute("UPDATE users SET full_name = %s WHERE id = %s", (name, self.user["id"]))
        self.commit()
        self.user = core.get_user(self.conn, self.user["id"])
        return self.profile_get("Saved.")

    # -- SKUs, receipts, lots (R3.2, R3.3, R7.7) --------------------------------------------------------------
    def sku_new_get(self, flash: str = "", status: int = 200):
        self.need(("supervisor",))
        f = lambda n, label, t="text": f"<label>{label} <input name={n} type={t} value='{E(self.form.get(n, '') if hasattr(self, 'form') else '')}'></label>"  # noqa: E731
        body = (f"<div class=card><form method=post action=/skus/new>{csrf_input(self.csrf)}"
                + f("code", "SKU code") + f("name", "Name") + f("case_pack", "Units per case") + f("unit_weight_g", "Unit weight (g)")
                + f("length_cm", "Case length (cm)") + f("width_cm", "Case width (cm)") + f("height_cm", "Case height (cm)")
                + "<label>Storage <select name=storage><option>ambient</option><option>chilled</option><option>frozen</option></select></label>"
                + "<label>Taxable <select name=taxable><option>false</option><option>true</option></select></label>"
                + f("reorder_point", "Reorder point (units)") + "<button data-action=create-sku>Create SKU</button></form></div>")
        return self.render("New SKU", body, status, flash, bool(flash))

    def sku_new_post(self):
        self.need(("supervisor",))
        f = self.form
        code = (f.get("code") or "").strip().upper()
        name = f.get("name") or ""
        ints = {k: core.parse_int(f.get(k), lo, hi) for k, lo, hi in (("case_pack", 1, 500), ("unit_weight_g", 1, 100000),
                ("length_cm", 1, 300), ("width_cm", 1, 300), ("height_cm", 1, 300), ("reorder_point", 0, 1000000))}
        bad = None
        if not core.SKU_RE.match(code):
            bad = "The SKU code must be 1 to 24 letters, digits or dashes."
        elif not valid_name(name, 120):
            bad = "The name must be 1 to 120 characters with no control characters."
        elif any(v is None for v in ints.values()):
            bad = "Check the numbers: " + ", ".join(k for k, v in ints.items() if v is None) + "."
        elif f.get("storage") not in ("ambient", "chilled", "frozen") or f.get("taxable") not in ("true", "false"):
            bad = "Choose a storage type and whether the SKU is taxable."
        elif core.sku_by_code(self.conn, code):
            bad = "That SKU code is already in use."
        if bad:
            return self.sku_new_get(bad, 422)
        sid = self.conn.execute(
            "INSERT INTO skus (code, name, case_pack, unit_weight_g, length_cm, width_cm, height_cm, storage, taxable, reorder_point)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (code, name, ints["case_pack"], ints["unit_weight_g"], ints["length_cm"], ints["width_cm"], ints["height_cm"],
             f["storage"], f["taxable"] == "true", ints["reorder_point"])).fetchone()["id"]
        self.commit()
        return redirect(f"/skus/{sid}")

    def sku_view_get(self, sid):
        self.need(INTERNAL)
        s = self.conn.execute("SELECT * FROM skus WHERE id = %s", (int(sid),)).fetchone()
        if not s:
            return self.not_found()
        lots = self.conn.execute("SELECT l.*, b.code AS bin, st.on_hand FROM lots l JOIN bins b ON b.id = l.bin_id JOIN pp_lot_state st"
                                 " ON st.lot_id = l.id WHERE l.sku_id = %s ORDER BY l.expiry_date, l.id", (s["id"],)).fetchall()
        rows = "".join(f"<tr><td><a href='/lots/{l['id']}'>{E(l['lot_code'])}</a></td><td>{l['expiry_date']}</td><td>{E(l['bin'])}</td>"
                       f"<td>{l['on_hand']}</td></tr>" for l in lots)
        body = (f"<div class=card data-sku-id='{s['id']}'><p><strong>{E(s['code'])}</strong> {E(s['name'])}</p>"
                f"<p class=muted>{s['case_pack']} per case · {s['storage']} · {'taxable' if s['taxable'] else 'not taxable'} · "
                f"reorder point {s['reorder_point']}</p></div><div class='card tablewrap'><table><tr><th>Lot</th><th>Expiry</th><th>Bin</th>"
                f"<th>On hand</th></tr>{rows}</table></div>")
        return self.render(f"SKU {s['code']}", body)

    def receipt_new_get(self, flash: str = "", status: int = 200):
        self.need(("supervisor",))
        g = lambda n: E(self.form.get(n, "") if hasattr(self, "form") else "")  # noqa: E731
        body = (f"<div class=card><form method=post action=/receipts/new>{csrf_input(self.csrf)}"
                f"<label>SKU code <input name=sku value='{g('sku')}'></label><label>Lot code <input name=lot_code value='{g('lot_code')}'></label>"
                f"<label>Expiry date <input name=expiry_date type=date value='{g('expiry_date')}'></label>"
                f"<label>Bin <input name=bin value='{g('bin')}'></label><label>Units <input name=units value='{g('units')}'></label>"
                f"<label>Unit cost (USD) <input name=unit_cost value='{g('unit_cost')}'></label>"
                "<button data-action=receive>Receive</button></form></div>")
        return self.render("Receive stock", body, status, flash, bool(flash))

    def receipt_new_post(self):
        self.need(("supervisor",))
        st, msg, lot_id = core.receive(self.conn, self.form)
        if st != 200:
            self.conn.rollback()
            return self.receipt_new_get(msg, st)
        self.commit()
        return redirect(f"/lots/{lot_id}")

    def lot_page(self, lot_id: int, flash: str = "", status: int = 200, err: bool = False):
        u = self.need(INTERNAL)
        l = self.conn.execute("SELECT l.*, s.code AS sku, s.name, b.code AS bin, st.on_hand, st.version, st.count_hold FROM lots l"
                              " JOIN skus s ON s.id = l.sku_id JOIN bins b ON b.id = l.bin_id JOIN pp_lot_state st ON st.lot_id = l.id"
                              " WHERE l.id = %s", (lot_id,)).fetchone()
        if not l:
            return self.not_found()
        reserved = core.reserved_of(self.conn, lot_id)
        status_s = ("quarantine" if l["bin"] == "QA-01" else "hold" if (l["qa_hold"] or l["count_hold"])
                    else "expired" if l["expiry_date"] < db.today(self.conn) else "available")
        form = ""
        if u["role"] == "supervisor":
            form = (f"<form method=post action='/lots/{lot_id}/count'>{csrf_input(self.csrf)}<input type=hidden name=version value='{l['version']}'>"
                    "<label>Units counted in the bin <input name=counted_units inputmode=numeric required></label>"
                    "<p class=muted>Count only what is physically in the bin; picked units waiting to ship are added for you.</p>"
                    "<button data-action=count-lot>Record count</button></form>")
        body = (f"<div class=card data-lot-id='{lot_id}' data-on-hand='{l['on_hand']}' data-reserved='{reserved}' "
                f"data-lot-version='{l['version']}' data-lot-status='{status_s}'>"
                f"<p><strong>{E(l['sku'])}</strong> {E(l['name'])}</p><div class=grid>"
                f"<div class=kv>Lot<br><strong>{E(l['lot_code'])}</strong></div><div class=kv>Bin<br><strong>{E(l['bin'])}</strong></div>"
                f"<div class=kv>Expiry<br><strong>{l['expiry_date']}</strong></div><div class=kv>On hand<br><strong>{l['on_hand']}</strong></div>"
                f"<div class=kv>Reserved<br><strong>{reserved}</strong></div><div class=kv>Status<br><strong>{status_s}</strong></div>"
                f"<div class=kv>Version<br><strong>{l['version']}</strong></div></div>{form}</div>")
        return self.render(f"Lot {l['lot_code'].strip()}", body, status, flash, err)

    def lot_view_get(self, lot_id):
        return self.lot_page(int(lot_id))

    def lot_count_post(self, lot_id):
        self.need(("supervisor",))
        st, msg = core.count_lot(self.conn, int(lot_id), self.form)
        if st != 200:
            self.conn.rollback()
            if st == 404:
                return self.not_found()
            return self.lot_page(int(lot_id), msg, st, True)
        self.commit()
        return self.lot_page(int(lot_id), msg)

    # -- prices (R3.4) ---------------------------------------------------------------------------------------
    def prices_get(self, flash: str = "", status: int = 200):
        self.need(("finance",))
        sku = (self.query.get("sku") or "").strip()
        rows = ""
        if sku:
            for p in self.conn.execute("SELECT p.* FROM price_list p JOIN skus s ON s.id = p.sku_id WHERE upper(s.code) = %s"
                                       " ORDER BY tier, effective_from", (sku.upper(),)).fetchall():
                rows += f"<tr><td>{p['tier']}</td><td>{p['effective_from']}</td><td>{p['unit_price_cents']}</td></tr>"
        body = (f"<div class=card><form method=post action=/prices>{csrf_input(self.csrf)}"
                "<label>Tier <select name=tier><option>standard</option><option>gold</option><option>platinum</option></select></label>"
                "<label>SKU code <input name=sku></label><label>Unit price (cents) <input name=unit_price_cents></label>"
                "<label>Effective from <input name=effective_from type=date></label><button data-action=create-price>Add price</button></form></div>"
                + (f"<div class='card tablewrap'><p>Prices for {E(sku)}</p><table><tr><th>Tier</th><th>From</th><th>Cents</th></tr>{rows}</table></div>" if sku else ""))
        return self.render("Prices", body, status, flash, bool(flash) and status >= 400)

    def prices_post(self):
        self.need(("finance",))
        f = self.form
        s = core.sku_by_code(self.conn, f.get("sku", ""))
        cents = core.parse_int(f.get("unit_price_cents"), 1, 10000000)
        eff = core.parse_date(f.get("effective_from"))
        if f.get("tier") not in ("standard", "gold", "platinum") or s is None or cents is None or eff is None:
            return self.prices_get("Choose a tier, an existing SKU, a price from 1 to 10000000 cents and a date.", 422)
        r = self.conn.execute("INSERT INTO price_list (tier, sku_id, unit_price_cents, effective_from) VALUES (%s,%s,%s,%s)"
                              " ON CONFLICT DO NOTHING RETURNING id", (f["tier"], s["id"], cents, eff)).fetchone()
        if r is None:
            return self.prices_get("A price for that tier, SKU and date already exists; prices are never changed.", 409)
        self.commit()
        return redirect("/prices?sku=" + urllib.parse.quote(s["code"]))

    # -- customers and users (R3.5, R3.6) ------------------------------------------------------------------------
    def _customer_fields(self, f: dict) -> tuple[dict | None, str]:
        name = f.get("name") or ""
        lim_s = (f.get("approval_limit_cents") or "").strip()
        lim = None if lim_s == "" else core.parse_int(lim_s, 0, 100000000)
        days = core.parse_int(f.get("min_shelf_life_days"), 0, 365)
        st = self.conn.execute("SELECT 1 FROM tax_rates WHERE state = %s", ((f.get("ship_to_state") or "").strip().upper(),)).fetchone()
        if not valid_name(name, 120):
            return None, "The name must be 1 to 120 characters with no control characters."
        if f.get("tier") not in ("standard", "gold", "platinum") or not st:
            return None, "Choose a tier and a ship-to state we have a tax rate for."
        if (lim_s and lim is None) or days is None:
            return None, "The approval limit is empty or 0 to 100000000 cents; shelf life is 0 to 365 days."
        if f.get("allow_split_cases") not in ("true", "false") or f.get("backorders_allowed") not in ("true", "false"):
            return None, "Answer true or false for split cases and backorders."
        return {"name": name, "tier": f["tier"], "ship_to_state": f["ship_to_state"].strip().upper(), "approval_limit_cents": lim,
                "min_shelf_life_days": days, "allow_split_cases": f["allow_split_cases"] == "true",
                "backorders_allowed": f["backorders_allowed"] == "true"}, ""

    def _customer_form(self, action: str, c: dict | None, button: str) -> str:
        v = lambda k: E("" if not c or c.get(k) is None else str(c[k]).lower() if isinstance(c.get(k), bool) else str(c[k]))  # noqa: E731
        sel = lambda n, opts: f"<select name={n}>" + "".join(f"<option{' selected' if v(n) == o else ''}>{o}</option>" for o in opts) + "</select>"  # noqa: E731
        return (f"<div class=card><form method=post action='{action}'>{csrf_input(self.csrf)}"
                f"<label>Name <input name=name value='{v('name')}'></label><label>Tier {sel('tier', ['standard', 'gold', 'platinum'])}</label>"
                f"<label>Ship-to state <input name=ship_to_state value='{v('ship_to_state')}'></label>"
                f"<label>Approval limit (cents, empty for none) <input name=approval_limit_cents value='{v('approval_limit_cents')}'></label>"
                f"<label>Minimum shelf life (days) <input name=min_shelf_life_days value='{v('min_shelf_life_days') or '0'}'></label>"
                f"<label>Split cases allowed {sel('allow_split_cases', ['false', 'true'])}</label>"
                f"<label>Backorders allowed {sel('backorders_allowed', ['true', 'false'])}</label>"
                f"<button data-action={button}>Save</button></form></div>")

    def customer_new_get(self, flash: str = "", status: int = 200):
        self.need(("finance",))
        return self.render("New customer", self._customer_form("/customers/new", None, "create-customer"), status, flash, bool(flash))

    def customer_new_post(self):
        self.need(("finance",))
        c, bad = self._customer_fields(self.form)
        if c is None:
            return self.customer_new_get(bad, 422)
        cid = self.conn.execute("INSERT INTO customers (name, tier, ship_to_state, approval_limit_cents, min_shelf_life_days,"
                                " allow_split_cases, backorders_allowed) VALUES (%(name)s,%(tier)s,%(ship_to_state)s,%(approval_limit_cents)s,"
                                "%(min_shelf_life_days)s,%(allow_split_cases)s,%(backorders_allowed)s) RETURNING id", c).fetchone()["id"]
        self.commit()
        return redirect(f"/customers/{cid}")

    def customer_view_get(self, cid, flash: str = "", status: int = 200):
        self.need(("finance",))
        c = core.get_customer(self.conn, int(cid))
        if not c:
            return self.not_found()
        users = self.conn.execute("SELECT * FROM users WHERE customer_id = %s ORDER BY id", (c["id"],)).fetchall()
        ul = "".join(f"<li><a href='/users/{u['id']}'>{E(u['full_name'])}</a> ({u['role']}{'' if u['active'] else ', inactive'})</li>" for u in users)
        body = (f"<div class=card data-customer-id='{c['id']}'><p><strong>{E(c['name'])}</strong></p><ul>{ul}</ul></div>"
                + self._customer_form(f"/customers/{c['id']}", dict(c), "save-customer"))
        return self.render(f"Customer {c['name']}", body, status, flash, status >= 400)

    def customer_view_post(self, cid):
        self.need(("finance",))
        if not core.get_customer(self.conn, int(cid)):
            return self.not_found()
        c, bad = self._customer_fields(self.form)
        if c is None:
            return self.customer_view_get(cid, bad, 422)
        self.conn.execute("UPDATE customers SET name=%(name)s, tier=%(tier)s, ship_to_state=%(ship_to_state)s, approval_limit_cents="
                          "%(approval_limit_cents)s, min_shelf_life_days=%(min_shelf_life_days)s, allow_split_cases=%(allow_split_cases)s,"
                          " backorders_allowed=%(backorders_allowed)s WHERE id=%(id)s", {**c, "id": int(cid)})
        self.commit()
        return self.customer_view_get(cid, "Saved.")

    def user_new_get(self, flash: str = "", status: int = 200):
        self.need(("finance",))
        body = (f"<div class=card><form method=post action=/users/new>{csrf_input(self.csrf)}"
                "<label>Email <input name=email type=email></label><label>Full name <input name=full_name></label>"
                "<label>Role <select name=role><option>buyer</option><option>approver</option><option>picker</option>"
                "<option>supervisor</option><option>finance</option></select></label>"
                "<label>Customer id (buyers and approvers) <input name=customer_id></label>"
                "<label>Zone (pickers) <select name=zone><option value=''></option><option>A</option><option>C</option><option>F</option></select></label>"
                "<button data-action=create-user>Create user</button></form></div>")
        return self.render("New user", body, status, flash, bool(flash))

    def user_new_post(self):
        self.need(("finance",))
        f = self.form
        email = norm_email(f.get("email", ""))
        name = f.get("full_name") or ""
        role = f.get("role")
        cust_s, zone = (f.get("customer_id") or "").strip(), (f.get("zone") or "").strip().upper()
        bad = None
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 200:
            bad = "Enter a valid email address."
        elif self.conn.execute("SELECT 1 FROM users WHERE lower(btrim(email)) = %s", (email,)).fetchone():
            bad = "That email address is already on file."
        elif not valid_name(name):
            bad = "A display name is 1 to 100 characters with no control characters."
        elif role not in ("buyer", "approver", "picker", "supervisor", "finance"):
            bad = "Choose a role."
        elif role in CUSTOMER_ROLES and not (cust_s.isdigit() and core.get_customer(self.conn, int(cust_s))):
            bad = "Buyers and approvers need an existing customer id."
        elif role not in CUSTOMER_ROLES and cust_s:
            bad = "Only buyers and approvers belong to a customer."
        elif role == "picker" and zone not in ("A", "C", "F"):
            bad = "Pickers need a zone (A, C or F)."
        elif role != "picker" and zone:
            bad = "Only pickers have a zone."
        if bad:
            return self.user_new_get(bad, 422)
        uid = self.conn.execute("INSERT INTO users (email, full_name, role, customer_id, zone) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                                (email, name, role, int(cust_s) if role in CUSTOMER_ROLES else None, zone or None)).fetchone()["id"]
        self.commit()
        return redirect(f"/users/{uid}")

    def user_view_get(self, uid, flash: str = ""):
        self.need(("finance",))
        u = core.get_user(self.conn, int(uid))
        if not u:
            return self.not_found()
        deact = (f"<form method=post action='/users/{u['id']}/deactivate'>{csrf_input(self.csrf)}"
                 "<button class=secondary data-action=deactivate-user>Deactivate</button></form>") if u["active"] else "<p>Inactive.</p>"
        body = (f"<div class=card data-user-id='{u['id']}'><p><strong>{E(u['full_name'])}</strong> &lt;{E(u['email'].strip())}&gt;</p>"
                f"<p>{u['role']}{' · zone ' + u['zone'] if u['zone'] else ''}{' · customer ' + str(u['customer_id']) if u['customer_id'] else ''}</p>{deact}</div>")
        return self.render("User", body, flash=flash)

    def user_deactivate_post(self, uid):
        self.need(("finance",))
        target = core.get_user(self.conn, int(uid))
        if not target:
            return self.not_found()
        if not target["active"]:
            return Resp(409, page("Already inactive", "<p>This user is already inactive.</p>", self.user, self.csrf))
        self.conn.execute("UPDATE users SET active = false WHERE id = %s", (int(uid),))
        self.conn.execute("UPDATE pp_sessions SET revoked = true WHERE user_id = %s", (int(uid),))
        self.commit()
        return self.user_view_get(uid, "User deactivated.")

    # -- orders ---------------------------------------------------------------------------------------------------
    def orders_get(self):
        u = self.need(CUSTOMER_ROLES + INTERNAL)
        cust = u["customer_id"] if u["role"] in CUSTOMER_ROLES else None
        where, args = ("WHERE customer_id = %s", (cust,)) if cust else ("", ())
        new = self.conn.execute(f"SELECT * FROM pp_orders {where} ORDER BY id DESC LIMIT 200", args).fetchall()
        old = self.conn.execute(f"SELECT * FROM legacy_orders {where} ORDER BY placed_local DESC LIMIT 100", args).fetchall()
        rows = "".join(f"<tr data-order-number='{num(o['id'])}'><td><a href='/orders/{o['id']}'>{num(o['id'])}</a></td><td>{o['status']}</td>"
                       f"<td>{o['ship_date']}</td><td>{E(o['po_number'])}</td><td>{dollars(o['total'])}</td></tr>" for o in new)
        rows += "".join(f"<tr data-order-number='{E(o['number'])}'><td>{E(o['number'])}</td><td>{o['status']} (old system)</td>"
                        f"<td>{o['ship_date']}</td><td>{E(o['po_number'] or '')}</td><td>{dollars(o['total_cents'])}</td></tr>" for o in old)
        exp = "<p><a href='/orders/export.csv'>Download all as CSV</a></p>" if u["role"] in CUSTOMER_ROLES + ("finance",) else ""
        body = (f"<div class='card tablewrap'>{exp}<table><tr><th>Order</th><th>Status</th><th>Ship date</th><th>PO</th><th>Total</th></tr>"
                f"{rows}</table></div>")
        return self.render("Orders", body)

    def order_new_get(self, flash: str = "", status: int = 200, errors: list | None = None):
        self.need(CUSTOMER_ROLES)
        f = self.form if hasattr(self, "form") else {}
        multi = self.multi if hasattr(self, "multi") else {}
        skus, qtys = multi.get("sku", []), multi.get("qty", [])
        rows = ""
        for i in range(12):
            rows += (f"<div class=row><label>SKU {i + 1} <input name=sku value='{E(skus[i] if i < len(skus) else '')}'></label>"
                     f"<label>Units <input name=qty inputmode=numeric value='{E(qtys[i] if i < len(qtys) else '')}'></label></div>")
        errs = "".join(f"<li>{E(e)}</li>" for e in (errors or []))
        body = ((f"<div class='flash err'><ul>{errs}</ul></div>" if errs else "")
                + f"<div class=card><form method=post action=/orders/new>{csrf_input(self.csrf)}"
                f"<label>Requested ship date <input name=ship_date type=date value='{E(f.get('ship_date', ''))}'></label>"
                f"<label>PO number <input name=po_number maxlength=40 value='{E(f.get('po_number', ''))}'></label>"
                f"<label>Notes <textarea name=notes maxlength=500>{E(f.get('notes', ''))}</textarea></label>{rows}"
                "<button data-action=create-order>Place order</button></form></div>")
        return self.render("New order", body, status, flash, bool(flash))

    def order_new_post(self):
        u = self.need(CUSTOMER_ROLES)
        skus, qtys = self.multi.get("sku", []), self.multi.get("qty", [])
        lines = [(s, qtys[i] if i < len(qtys) else "") for i, s in enumerate(skus) if s.strip()]
        oid, errors = core.create_order(self.conn, u, self.form.get("ship_date", ""), self.form.get("po_number", ""),
                                        self.form.get("notes", ""), lines)
        if oid is None:
            self.conn.rollback()
            return self.order_new_get("The order was not placed.", 422, errors)
        self.commit()
        return redirect(f"/orders/{oid}")

    def order_page(self, oid: int, flash: str = "", status: int = 200, err: bool = False):
        o = self.order_for(oid)
        u = self.user
        conn = self.conn
        j = core.order_json(conn, oid)
        cust = core.get_customer(conn, o["customer_id"])
        mine = u["role"] in CUSTOMER_ROLES
        acts = ""
        if o["status"] == "awaiting_approval" and u["role"] == "approver" and u["id"] != o["created_by"]:
            acts += (f"<form method=post action='/orders/{oid}/approve'>{csrf_input(self.csrf)}<p>This order totals "
                     f"<strong>{dollars(o['total'])}</strong>, above {E(cust['name'])}'s approval limit of "
                     f"<strong>{dollars(o['approval_limit'] or 0)}</strong>. Approving it lets a buyer pay for it.</p>"
                     "<button data-action=approve-order>Approve order</button></form>")
        elif o["status"] == "awaiting_approval":
            acts += "<p>This order waits for an approver of your company (not the person who placed it).</p>"
        if o["status"] == "awaiting_payment" and mine:
            acts += (f"<form method=post action='/orders/{oid}/pay'>{csrf_input(self.csrf)}<p>Pay {dollars(o['total'])} by card. "
                     "Your order is confirmed once the payment provider tells us the payment succeeded.</p>"
                     "<button data-action=pay-order>Pay now</button></form>")
        if o["status"] in ("awaiting_approval", "awaiting_payment") and mine:
            acts += (f"<form method=post action='/orders/{oid}/cancel'>{csrf_input(self.csrf)}"
                     "<button class=secondary data-action=cancel-order>Cancel order</button></form>")
        tasks = conn.execute("SELECT status, units FROM pp_tasks WHERE order_id = %s AND status IN ('open','picked')", (oid,)).fetchall()
        can_ship = any(t["status"] == "picked" and t["units"] > 0 for t in tasks) and not any(t["status"] == "open" for t in tasks)
        if u["role"] == "supervisor" and o["status"] == "in_fulfilment" and can_ship:
            acts += (f"<form method=post action='/orders/{oid}/ship'>{csrf_input(self.csrf)}<p>Ship every picked unit now; "
                     "the customer is emailed the tracking number and invoice.</p><button data-action=ship-order>Ship picked units</button></form>")
        if self.query.get("checkout") == "success" and o["status"] == "awaiting_payment":
            flash = flash or "Thanks! We are waiting for the payment provider to confirm your payment; this page will show it once confirmed."
        rma_used = {r["line_no"]: int(r["u"]) for r in conn.execute(
            "SELECT line_no, sum(units) AS u FROM pp_rmas WHERE order_id = %s GROUP BY line_no", (oid,)).fetchall()}
        lines = ""
        for l in j["lines"]:
            cb = ""
            if mine and l["backordered"] > 0 and o["status"] == "in_fulfilment":
                cb = (f"<form method=post action='/orders/{oid}/lines/{l['line_no']}/cancel-backorder'>{csrf_input(self.csrf)}"
                      f"<label>Cancel backordered units (1 to {l['backordered']}); cancelled units are refunded to your card"
                      f" <input name=units inputmode=numeric value='{l['backordered']}'></label>"
                      "<button class=secondary data-action=cancel-backorder>Cancel backorder</button></form>")
            rma = ""
            if mine and l["shipped"] - rma_used.get(l["line_no"], 0) > 0:
                rma = (f"<form method=post action='/orders/{oid}/rmas'>{csrf_input(self.csrf)}<input type=hidden name=line_no value='{l['line_no']}'>"
                       "<div class=row><label>Return units <input name=units inputmode=numeric></label>"
                       "<label>Reason <input name=reason maxlength=500></label></div><button class=secondary data-action=request-rma>Request return</button></form>")
            lines += (f"<div class=line data-line-no='{l['line_no']}' data-reserved='{l['reserved']}' data-shipped='{l['shipped']}' "
                      f"data-backordered='{l['backordered']}' data-cancelled='{l['cancelled']}'>"
                      f"<p><strong>{l['line_no']}. {E(l['sku'])}</strong> × {l['qty']} at {dollars(l['unit_price_cents'])} = "
                      f"{dollars(l['amount_cents'])} (tax {dollars(l['tax_cents'])})</p><div class=grid>"
                      f"<div class=kv>Reserved<br><strong>{l['reserved']}</strong></div><div class=kv>Shipped<br><strong>{l['shipped']}</strong></div>"
                      f"<div class=kv>Backordered<br><strong>{l['backordered']}</strong></div><div class=kv>Cancelled<br><strong>{l['cancelled']}</strong></div>"
                      f"<div class=kv>Returned<br><strong>{l['returned']}</strong></div></div>{cb}{rma}</div>")
        refunds = "".join(f"<li>Refund of {dollars(r['amount_cents'])}: {'refunded to the card' if r['status'] == 'refunded' else 'pending at the payment provider'}</li>"
                          for r in j["refunds"]) or "<li>No refunds.</li>"
        ships = "".join(f"<li><a href='/shipments/{s['id']}'>Shipment {s['id']}</a>: tracking {E(s['tracking_number'])}, invoice "
                        f"{E(s['invoice_number'])} for {dollars(s['invoice_total_cents'])}</li>" for s in j["shipments"]) or "<li>Nothing shipped yet.</li>"
        rmas = "".join(f"<li><a href='/rmas/{r['id']}'>Return {r['id']}</a>: {r['units']} units of line {r['line_no']} ({r['status']})</li>"
                       for r in conn.execute("SELECT * FROM pp_rmas WHERE order_id = %s ORDER BY id", (oid,)).fetchall())
        explain = {"awaiting_approval": "Waiting for approval", "awaiting_payment": "Waiting for payment", "cancelled": "Cancelled",
                   "in_fulfilment": "Paid; being picked and shipped", "complete": "Complete: every unit shipped or cancelled"}[o["status"]]
        body = (f"<div class=card data-order-id='{oid}' data-order-status='{o['status']}' data-total-cents='{o['total']}'>"
                f"<p><span class=pill>{E(explain)}</span></p><div class=grid><div class=kv>Customer<br><strong>{E(cust['name'])}</strong></div>"
                f"<div class=kv>Ship date<br><strong>{o['ship_date']}</strong></div><div class=kv>PO<br><strong>{E(o['po_number'])}</strong></div>"
                f"<div class=kv>Subtotal<br>{dollars(o['subtotal'])}</div><div class=kv>Tax<br>{dollars(o['tax'])}</div>"
                f"<div class=kv>Freight<br>{dollars(o['freight'])} + tax {dollars(o['freight_tax'])}</div>"
                f"<div class=kv>Total<br><strong>{dollars(o['total'])}</strong></div></div>"
                f"<p class=muted>{E(o['notes'])}</p>{acts}</div><div class=card><h2>Lines</h2>{lines}</div>"
                f"<div class=card><h2>Refunds</h2><ul>{refunds}</ul><h2>Shipments</h2><ul>{ships}</ul>"
                + (f"<h2>Returns</h2><ul>{rmas}</ul>" if rmas else "") + "</div>")
        return self.render(f"Order {num(oid)}", body, status, flash, err)

    def order_view_get(self, oid):
        return self.order_page(int(oid))

    def _order_action(self, oid, roles, fn):
        o = self.order_for(int(oid), roles)
        st, msg = fn(o)
        if st != 200:
            self.conn.rollback()
            if st == 403:
                return self.forbidden(msg)
            return self.order_page(int(oid), msg, st, True)
        self.commit()
        return self.order_page(int(oid), msg)

    def order_approve_post(self, oid):
        u = self.need()
        if u["role"] != "approver":
            if u["role"] == "buyer":
                self.order_for(int(oid), CUSTOMER_ROLES)
            return self.forbidden("Only an approver of the company approves orders.")
        return self._order_action(oid, ("approver",), lambda o: core.approve(self.conn, u, o["id"]))

    def order_cancel_post(self, oid):
        return self._order_action(oid, CUSTOMER_ROLES, lambda o: core.cancel_order(self.conn, self.user, o["id"]))

    def order_pay_post(self, oid):
        o = self.order_for(int(oid), CUSTOMER_ROLES)
        if o["status"] != "awaiting_payment":
            return self.order_page(int(oid), "Only an order waiting for payment can be paid.", 409, True)
        if o["session_id"] and o["session_url"]:
            return redirect(o["session_url"])
        st, s = payments.create_session(o, self.user["email"])
        if st != 200 or not s.get("url"):
            return self.order_page(int(oid), "The payment provider did not answer. Try again in a moment.", 502, True)
        self.conn.execute("UPDATE pp_orders SET session_id = %s, session_url = %s WHERE id = %s AND session_id IS NULL",
                          (s["id"], s["url"], o["id"]))
        self.commit()
        row = core.get_order(self.conn, o["id"])
        return redirect(row["session_url"] or s["url"])

    def order_ship_post(self, oid):
        self.order_for(int(oid), ("supervisor",))
        st, msg, sid = core.ship(self.conn, int(oid))
        if st != 200:
            self.conn.rollback()
            return self.order_page(int(oid), msg, st, True)
        self.commit()
        return redirect(f"/shipments/{sid}")

    def order_rma_post(self, oid):
        self.order_for(int(oid), CUSTOMER_ROLES)
        st, msg, rid = core.request_rma(self.conn, self.user, int(oid), self.form)
        if st != 200:
            self.conn.rollback()
            return self.order_page(int(oid), msg, st, True)
        self.commit()
        return redirect(f"/rmas/{rid}")

    def order_cancel_backorder_post(self, oid, line_no):
        return self._order_action(oid, CUSTOMER_ROLES, lambda o: core.cancel_backorder(self.conn, o["id"], int(line_no), self.form.get("units")))

    def orders_export_get(self):
        u = self.need(CUSTOMER_ROLES + ("finance",))
        text = core.report_orders(self.conn, u["customer_id"] if u["role"] in CUSTOMER_ROLES else None)
        return Resp(200, text, {"Content-Disposition": "attachment; filename=orders.csv"}, "text/csv; charset=utf-8")

    def shipment_view_get(self, sid):
        u = self.need(CUSTOMER_ROLES + INTERNAL)
        s = self.conn.execute("SELECT * FROM pp_shipments WHERE id = %s", (int(sid),)).fetchone()
        if s is None:
            return self.not_found()
        self.order_for(s["order_id"])
        lines = "".join(f"<tr data-invoice-line-no='{l['line_no']}' data-units='{l['units']}' data-amount-cents='{l['amount_cents']}' "
                        f"data-tax-cents='{l['tax_cents']}'><td>{l['line_no']}</td><td>{E(l['sku'])}</td><td>{l['units']}</td>"
                        f"<td>{dollars(l['unit_price_cents'])}</td><td>{dollars(l['amount_cents'])}</td><td>{dollars(l['tax_cents'])}</td></tr>"
                        for l in s["lines"])
        body = (f"<div class=card data-shipment-id='{s['id']}' data-order-id='{s['order_id']}' data-tracking-number='{E(s['tracking'])}' "
                f"data-invoice-number='{E(s['invoice_number'])}' data-invoice-total-cents='{s['total']}' data-freight-cents='{s['freight']}' "
                f"data-freight-tax-cents='{s['freight_tax']}'><p>Order <a href='/orders/{s['order_id']}'>{num(s['order_id'])}</a> · tracking "
                f"<strong>{E(s['tracking'])}</strong></p><h2>Invoice {E(s['invoice_number'])}</h2><div class=tablewrap><table>"
                f"<tr><th>Line</th><th>SKU</th><th>Units</th><th>Unit price</th><th>Amount</th><th>Tax</th></tr>{lines}</table></div>"
                f"<p>Freight {dollars(s['freight'])} · freight tax {dollars(s['freight_tax'])}</p>"
                f"<p><strong>Invoice total {dollars(s['total'])}</strong> (paid by card)</p></div>")
        return self.render(f"Shipment {s['id']}", body)

    # -- returns (R8.4-R8.7) -----------------------------------------------------------------------------------------
    def rma_page(self, rid: int, flash: str = "", status: int = 200, err: bool = False):
        u = self.need(CUSTOMER_ROLES + INTERNAL)
        r = self.conn.execute("SELECT * FROM pp_rmas WHERE id = %s", (rid,)).fetchone()
        if r is None:
            return self.not_found()
        self.order_for(r["order_id"])
        cn = self.conn.execute("SELECT * FROM pp_credit_notes WHERE rma_id = %s", (rid,)).fetchone()
        acts = ""
        if u["role"] == "supervisor" and r["status"] == "requested":
            acts = (f"<form method=post action='/rmas/{rid}/receive'>{csrf_input(self.csrf)}<p>Receiving puts the units in quarantine "
                    "(bin QA-01) and issues the credit note.</p><button data-action=receive-rma>Receive into quarantine</button></form>")
        if u["role"] == "supervisor" and r["status"] == "quarantined":
            acts = (f"<form method=post action='/rmas/{rid}/inspect'>{csrf_input(self.csrf)}<label>Outcome <select name=outcome>"
                    "<option>restock</option><option>damaged</option></select></label><p class=muted>Stock expiring within 7 days (or expired) "
                    "is written off even if you choose restock.</p><button data-action=inspect-rma>Record inspection</button></form>")
        body = (f"<div class=card data-rma-id='{rid}' data-rma-status='{r['status']}' data-disposition='{r['disposition']}' "
                f"data-credit-cents='{cn['amount'] if cn else ''}'><p>Order <a href='/orders/{r['order_id']}'>{num(r['order_id'])}</a>, "
                f"line {r['line_no']}: {r['units']} units</p><p>Reason: {E(r['reason'])}</p><p>Status: {r['status']}"
                f"{' (' + r['disposition'].replace('_', ' ') + ')' if r['disposition'] else ''}</p>"
                + (f"<p>Credit note CN-{cn['id']:06d}: {dollars(cn['amount'])}</p>" if cn else "<p>The credit note is issued when we receive the units.</p>")
                + f"{acts}</div>")
        return self.render(f"Return {rid}", body, status, flash, err)

    def rma_view_get(self, rid):
        return self.rma_page(int(rid))

    def _rma_action(self, rid, fn):
        self.need(("supervisor",))
        if self.conn.execute("SELECT 1 FROM pp_rmas WHERE id = %s", (int(rid),)).fetchone() is None:
            return self.not_found()
        st, msg = fn()
        if st != 200:
            self.conn.rollback()
            return self.rma_page(int(rid), msg, st, True)
        self.commit()
        return self.rma_page(int(rid), msg)

    def rma_receive_post(self, rid):
        return self._rma_action(rid, lambda: core.receive_rma(self.conn, int(rid)))

    def rma_inspect_post(self, rid):
        return self._rma_action(rid, lambda: core.inspect_rma(self.conn, int(rid), self.form.get("outcome", "")))

    # -- waves and picks (R7) -------------------------------------------------------------------------------------------
    def waves_get(self, flash: str = "", status: int = 200):
        u = self.need(("supervisor", "picker"))
        where, args = ("WHERE zone = %s", (u["zone"],)) if u["role"] == "picker" else ("", ())
        ws = self.conn.execute(f"SELECT * FROM pp_waves {where} ORDER BY id DESC LIMIT 100", args).fetchall()
        lst = "".join(f"<li><a href='/waves/{w['id']}'>Wave {w['id']}</a>: zone {w['zone']}, ship {w['ship_date']}, distance {w['distance']}</li>" for w in ws)
        form = ""
        if u["role"] == "supervisor":
            form = (f"<div class=card><form method=post action=/waves>{csrf_input(self.csrf)}"
                    "<label>Zone <select name=zone><option>A</option><option>C</option><option>F</option></select></label>"
                    "<label>Ship date <input name=ship_date type=date></label><label>Most tasks (1-12) <input name=max_tasks value=12></label>"
                    "<button data-action=create-wave>Create wave</button></form></div>")
        return self.render("Waves", form + f"<div class=card><ul>{lst or '<li>No waves yet.</li>'}</ul></div>", status, flash, status >= 400)

    def waves_post(self):
        self.need(("supervisor",))
        zone = self.form.get("zone")
        d = core.parse_date(self.form.get("ship_date"))
        k = core.parse_int(self.form.get("max_tasks"), 1, 12)
        if zone not in ("A", "C", "F") or d is None or k is None:
            return self.waves_get("Choose a zone, a ship date, and 1 to 12 tasks.", 422)
        st, msg, wid = core.create_wave(self.conn, zone, d, k)
        if st != 200:
            self.conn.rollback()
            return self.waves_get(msg, st)
        self.commit()
        return redirect(f"/waves/{wid}")

    def wave_page(self, wid: int, flash: str = "", status: int = 200, err: bool = False):
        u = self.need(("supervisor", "picker"))
        w = self.conn.execute("SELECT * FROM pp_waves WHERE id = %s", (wid,)).fetchone()
        if w is None:
            return self.not_found()
        if u["role"] == "picker" and u["zone"] != w["zone"]:
            raise self.forbidden("This wave is in another zone.")
        tasks = self.conn.execute("SELECT t.*, l.lot_code, b.code AS bin, s.code AS sku, s.name FROM pp_tasks t JOIN lots l ON l.id = t.lot_id"
                                  " JOIN bins b ON b.id = l.bin_id JOIN skus s ON s.id = l.sku_id WHERE t.wave_id = %s"
                                  " ORDER BY t.order_id, t.line_no, l.expiry_date, l.id", (wid,)).fetchall()
        by_bin: dict[str, list] = {}
        for t in tasks:
            by_bin.setdefault(t["bin"], []).append(t)
        stops = ""
        for i, code in enumerate(w["route"], start=1):
            ts = ""
            for t in by_bin.get(code, []):
                st = "open" if t["status"] == "open" else "picked"
                form = ""
                if st == "open":
                    form = (f"<form method=post action='/pick-tasks/{t['id']}/confirm'>{csrf_input(self.csrf)}<div class=row>"
                            "<label>Scan bin <input name=bin_code autocomplete=off></label><label>Scan lot <input name=lot_code autocomplete=off></label>"
                            f"<label>Units picked <input name=units inputmode=numeric value='{t['units']}'></label></div>"
                            "<button data-action=confirm-pick>Confirm pick</button></form>")
                ts += (f"<div class=task data-task-id='{t['id']}' data-order-id='{t['order_id']}' data-line-no='{t['line_no']}' "
                       f"data-lot-id='{t['lot_id']}' data-units='{t['units']}' data-task-status='{st}'>"
                       f"<p><strong>{E(t['sku'])}</strong> {E(t['name'])} · lot <strong>{E(t['lot_code'])}</strong> · {t['units']} units · order "
                       f"{num(t['order_id'])} line {t['line_no']} · {st}</p>{form}</div>")
            stops += f"<li class=stop data-stop-seq='{i}' data-bin='{E(code)}'><p><strong>Stop {i}: bin {E(code)}</strong></p>{ts}</li>"
        body = (f"<div class=card data-wave-id='{wid}' data-zone='{w['zone']}' data-ship-date='{w['ship_date']}' data-route-distance='{w['distance']}'>"
                f"<p>Zone {w['zone']} · ship date {w['ship_date']} · walk <strong>{w['distance']}</strong> bay lengths from the depot and back, "
                "in this order. If a bin holds fewer units than asked, confirm what you found: the lot is held for a count and the rest is "
                f"re-allocated.</p><ol style='padding-left:0;list-style:none'>{stops}</ol></div>")
        return self.render(f"Wave {wid}", body, status, flash, err)

    def wave_view_get(self, wid):
        return self.wave_page(int(wid))

    def task_confirm_post(self, tid):
        u = self.need(("supervisor", "picker"))
        st, msg = core.confirm_pick(self.conn, u, int(tid), self.form)
        if st == 404:
            return self.not_found()
        if st == 403:
            self.conn.rollback()
            return self.forbidden(msg)
        wid = self.conn.execute("SELECT wave_id FROM pp_tasks WHERE id = %s", (int(tid),)).fetchone()
        if st != 200:
            self.conn.rollback()
            return self.wave_page(wid["wave_id"], msg, st, True) if wid and wid["wave_id"] else self.render("Pick refused", f"<p>{E(msg)}</p>", st, msg, True)
        self.commit()
        return self.wave_page(wid["wave_id"], msg)

    # -- API keys (R4.6) ------------------------------------------------------------------------------------------------
    def api_keys_page(self, shown: tuple[int, str] | None = None, flash: str = "", status: int = 200):
        u = self.need(CUSTOMER_ROLES)
        keys = self.conn.execute("SELECT * FROM pp_api_keys WHERE user_id = %s ORDER BY id", (u["id"],)).fetchall()
        lst = "".join(f"<li data-api-key-id='{k['id']}'>{E(k['label'])} · created {k['created_at']:%Y-%m-%d}"
                      + (" · revoked" if k["revoked"] else f"<form method=post action='/account/api-keys/{k['id']}/revoke'>{csrf_input(self.csrf)}"
                         "<button class=secondary data-action=revoke-api-key>Revoke</button></form>") + "</li>" for k in keys)
        new = ""
        if shown:
            new = (f"<div class=card><p>Your new key (shown only this once; copy it now):</p>"
                   f"<p><code data-api-key='{E(shown[1])}' data-api-key-id='{shown[0]}'>{E(shown[1])}</code></p></div>")
        body = (new + f"<div class=card><form method=post action=/account/api-keys>{csrf_input(self.csrf)}<label>Label <input name=label maxlength=80></label>"
                f"<button data-action=create-api-key>Create key</button></form></div><div class=card><ul>{lst or '<li>No keys.</li>'}</ul></div>")
        return self.render("API keys", body, status, flash, status >= 400)

    def api_keys_get(self):
        return self.api_keys_page()

    def api_keys_post(self):
        u = self.need(CUSTOMER_ROLES)
        label = self.form.get("label") or ""
        if not valid_name(label, 80):
            return self.api_keys_page(None, "A label is 1 to 80 characters.", 422)
        key = "ppk_" + secrets.token_urlsafe(30)
        salt = secrets.token_hex(16)
        kid = self.conn.execute("INSERT INTO pp_api_keys (user_id, label, prefix, salt, key_hash) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                                (u["id"], label, key[:12], salt, sha(salt + key))).fetchone()["id"]
        self.commit()
        return self.api_keys_page((kid, key), "Key created.")

    def api_key_revoke_post(self, kid):
        u = self.need(CUSTOMER_ROLES)
        k = self.conn.execute("SELECT * FROM pp_api_keys WHERE id = %s AND user_id = %s", (int(kid), u["id"])).fetchone()
        if k is None:
            return self.not_found()
        if k["revoked"]:
            return self.api_keys_page(None, "This key is already revoked.", 409)
        self.conn.execute("UPDATE pp_api_keys SET revoked = true WHERE id = %s", (k["id"],))
        self.commit()
        return self.api_keys_page(None, "Key revoked.")

    # -- reports (R9) ---------------------------------------------------------------------------------------------------
    def report_on_hand_get(self):
        self.need(INTERNAL)
        return Resp(200, core.report_on_hand(self.conn), ctype="text/csv; charset=utf-8")

    def report_ledger_get(self):
        self.need(INTERNAL)
        text = core.report_ledger(self.conn, self.query.get("sku", ""))
        if text is None:
            return Resp(404, "unknown SKU\n", ctype="text/plain; charset=utf-8")
        return Resp(200, text, ctype="text/csv; charset=utf-8")

    def report_valuation_get(self):
        self.need(("finance",))
        s = self.query.get("as_of", "")
        m = re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})", s)
        try:
            at = datetime.fromisoformat(s.replace("z", "Z").replace("Z", "+00:00")) if m else None
        except ValueError:
            at = None
        if at is None or at.tzinfo is None:
            return Resp(400, "as_of must be an RFC 3339 instant with Z or an offset\n", ctype="text/plain; charset=utf-8")
        return Resp(200, core.report_valuation(self.conn, at), ctype="text/csv; charset=utf-8")

    # -- order API (R4.6-R4.9) ------------------------------------------------------------------------------------------
    def api(self, method: str, path: str, raw: bytes) -> Resp:
        auth = self.headers.get("Authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        with connect() as conn:
            user = None
            if key:
                for k in conn.execute("SELECT * FROM pp_api_keys WHERE prefix = %s AND NOT revoked", (key[:12],)).fetchall():
                    if secrets.compare_digest(k["key_hash"], sha(k["salt"] + key)):
                        user = core.get_user(conn, k["user_id"])
                        break
            if user is None or not user["active"] or user["role"] not in CUSTOMER_ROLES:
                return jresp(401, {"error": "invalid API key"})
            if method == "POST" and path == "/api/v1/orders":
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:
                    return jresp(400, {"error": "the body is not JSON"})
                if not isinstance(body, dict):
                    return jresp(400, {"error": "the body must be a JSON object"})
                lines = body.get("lines")
                parsed = None
                if isinstance(lines, list):
                    parsed = [((l.get("sku") if isinstance(l, dict) else None), (l.get("qty") if isinstance(l, dict) else None)) for l in lines]
                oid, errors = core.create_order(conn, user, body.get("ship_date"), body.get("po_number"), body.get("notes"), parsed)
                if oid is None:
                    conn.rollback()
                    return jresp(422, {"errors": errors})
                conn.commit()
                db.OUTBOX_EVENT.set()
                return jresp(201, core.order_json(conn, oid))
            m = re.fullmatch(r"/api/v1/orders/(\d+)", path)
            if method == "GET" and m:
                o = core.get_order(conn, int(m.group(1)))
                if o is None or o["customer_id"] != user["customer_id"]:
                    return jresp(404, {"error": "not found"})
                return jresp(200, core.order_json(conn, o["id"]))
            return jresp(404, {"error": "not found"})

    # -- webhook (R5.6) -----------------------------------------------------------------------------------------------------
    def webhook(self, raw: bytes) -> Resp:
        if not payments.verify(self.headers.get("Stripe-Signature"), raw):
            return Resp(400, "bad signature\n", ctype="text/plain")
        try:
            event = json.loads(raw.decode("utf-8"))
        except Exception:
            return Resp(400, "bad body\n", ctype="text/plain")
        if isinstance(event, dict):
            payments.handle(event)
        return Resp(200, "ok\n", ctype="text/plain")


def main() -> None:
    db.apply_schema()
    threading.Thread(target=db.outbox_loop, daemon=True).start()
    threading.Thread(target=payments.refund_loop, daemon=True).start()
    threading.Thread(target=payments.register_webhook, daemon=True).start()
    db.OUTBOX_EVENT.set()
    core.REFUND_EVENT.set()

    def warm():
        try:
            with connect() as conn:
                core.legacy_cache(conn)
        except Exception:
            pass
    threading.Thread(target=warm, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", int(ENV.get("PORT", "8080"))), Handler)
    server.daemon_threads = True
    server.serve_forever()
