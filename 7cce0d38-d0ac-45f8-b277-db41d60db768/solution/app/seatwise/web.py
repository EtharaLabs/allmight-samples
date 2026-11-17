"""Seatwise HTTP layer: stdlib ThreadingHTTPServer (HTTP/1.1), server-side sessions, CSRF tokens,
server-rendered HTML, the scan API and the payments webhook."""

from __future__ import annotations

import json
import re
import secrets
import sys
import threading
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

from . import db, domain, mail, payments, worker
from .domain import Refused
from .util import E, csrf_for, env_int, has_control, log_exc, now, sha

COOKIE = "sw_sid"
MAX_BODY = 8 * 1024 * 1024

CSS = """
:root{--ink:#1d2330;--muted:#5d6675;--line:#dfe3ea;--bg:#f4f5f8;--card:#fff;--brand:#3b2f8f;--brand2:#5b4fcf;
--ok:#1f7a44;--okbg:#e3f5ea;--warn:#8a5a00;--warnbg:#fff3d6;--bad:#a12626;--badbg:#fde7e7;--info:#e8ecff}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--ink);background:var(--bg);overflow-wrap:anywhere}
header.top{background:var(--brand);color:#fff;padding:10px 16px;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between}
header.top a{color:#fff;text-decoration:none}header.top .brand{font-weight:700;font-size:1.15rem;letter-spacing:.02em}
header.top nav{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center}header.top nav a:hover{text-decoration:underline}
header.top form{margin:0;display:inline}header.top .who{opacity:.85;font-size:.9rem}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:1.6rem;margin:.2rem 0 1rem}h2{font-size:1.2rem;margin:1.2rem 0 .6rem}h3{font-size:1.05rem;margin:.6rem 0 .4rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 16px;min-width:0}
.flash{padding:12px 16px;border-radius:8px;margin:0 0 16px;background:var(--info);border:1px solid #b9c3ff}
.flash.err{background:var(--badbg);border-color:#f0b3b3}.flash.ok{background:var(--okbg);border-color:#a8dcb9}
.muted{color:var(--muted)}.small{font-size:.9rem}
a{color:var(--brand2)}
button,.btn{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--brand);background:var(--brand);color:#fff;cursor:pointer;text-decoration:none;display:inline-block}
button.secondary,.btn.secondary{background:#fff;color:var(--brand)}button.danger{background:var(--bad);border-color:var(--bad)}
header.top button{background:transparent;border-color:#fff;padding:4px 10px}
input,select,textarea{font:inherit;padding:8px;border:1px solid #b8c0cc;border-radius:8px;width:100%;max-width:460px;background:#fff}
input[type=checkbox]{width:auto}textarea{min-height:7rem;max-width:640px}
label{display:block;margin:10px 0}label.inline{display:inline-flex;gap:6px;align-items:center;margin:6px 0}
form.inline{display:inline;margin:0}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tablewrap{overflow-x:auto;max-width:100%}table{border-collapse:collapse;width:100%}
td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:.85rem;color:var(--muted);font-weight:600}
td.num,th.num{text-align:right;white-space:nowrap}
.badge{display:inline-block;padding:1px 10px;border-radius:999px;font-size:.85rem;font-weight:600;background:#eceff4}
.badge.ok{background:var(--okbg);color:var(--ok)}.badge.warn{background:var(--warnbg);color:var(--warn)}.badge.bad{background:var(--badbg);color:var(--bad)}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:.9rem;margin:.4rem 0 1rem}.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{width:18px;height:18px;border-radius:4px;display:inline-block;border:1px solid #9aa3b2}
.seatmap .srow{display:flex;flex-wrap:wrap;gap:4px;align-items:center;margin:3px 0}
.seatmap .rl{width:1.6rem;font-weight:600;color:var(--muted);text-align:center}
.seat{position:relative;display:inline-flex;align-items:center;justify-content:center;min-width:34px;height:30px;padding:0 3px;border-radius:6px;font-size:.8rem;border:1px solid #9aa3b2;user-select:none}
.seat input{margin:0 3px 0 0}
.st-available,.sw.st-available{background:#e8f6ee;border-color:#58a578;cursor:pointer}
.st-held,.sw.st-held{background:#fff0c9;border-color:#d5a326;color:#6b4d00}
.st-sold,.sw.st-sold{background:#d9dce2;border-color:#a3a9b5;color:#59606c;text-decoration:line-through}
.st-waitlist,.sw.st-waitlist{background:#e9e4ff;border-color:#8a7be0;color:#3d2f99}
.acc{box-shadow:inset 0 0 0 2px #2266cc}.sw.acc{background:#fff}
.section-block{border-top:1px solid var(--line);padding-top:10px;margin-top:10px}
.figs{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
.fig{background:#f7f8fb;border:1px solid var(--line);border-radius:8px;padding:10px}.fig b{display:block;font-size:1.25rem}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92rem;overflow-wrap:anywhere;word-break:break-all}
.token{display:block;background:#f7f8fb;border:1px dashed #9aa3b2;border-radius:8px;padding:12px;font-size:1rem}
dl.kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:0}dl.kv dt{color:var(--muted)}dl.kv dd{margin:0}
ul.plain{list-style:none;padding:0;margin:0}ul.plain li{padding:6px 0;border-bottom:1px solid var(--line)}
@media (max-width:600px){main{padding:10px}.card{padding:12px}dl.kv{grid-template-columns:1fr}dl.kv dd{margin-bottom:6px}
 td,th{padding:5px 4px;font-size:.9rem}h1{font-size:1.35rem}
 table.resp thead{display:none}table.resp,table.resp tbody{display:block}
 table.resp tr{display:block;border:1px solid var(--line);border-radius:8px;margin:0 0 8px;padding:4px 10px}
 table.resp td{display:flex;justify-content:space-between;gap:12px;border:0;padding:3px 0;text-align:right;white-space:normal}
 table.resp td::before{content:attr(data-label);color:var(--muted);font-size:.85rem;text-align:left;flex:0 0 auto}
 table.resp td:empty{display:none}}
"""


class Resp(Exception):
    def __init__(self, status: int, body: str | bytes = "", headers: dict | None = None,
                 ctype: str = "text/html; charset=utf-8"):
        super().__init__(status)
        self.status, self.body, self.headers, self.ctype = status, body, headers or {}, ctype


def redirect(to: str, extra: dict | None = None) -> Resp:
    return Resp(303, "", {"Location": to, **(extra or {})})


def jresp(status: int, payload) -> Resp:
    return Resp(status, json.dumps(payload), ctype="application/json")


MESSAGES = {
    "joined": ("You joined the waiting list. We will email you when seats come free.", "ok"),
    "released": ("Your hold was released and the seats are available to others again.", "ok"),
    "refund": ("Refund requested. The ticket is now refund pending; you will get an email when the refund is "
               "completed.", "ok"),
    "transferred": ("Ticket transferred. The recipient has been emailed the ticket with its new QR code.", "ok"),
    "published": ("The event is published and visible to everyone.", "ok"),
    "rescheduled": ("The event was rescheduled.", "ok"),
    "cancelled": ("The event is cancelled. Every ticket is being refunded and every buyer and holder emailed.", "ok"),
    "orgrefund": ("Refund requested. The ticket is refund pending until the payment provider confirms it.", "ok"),
    "declined": ("You declined the offer; the seats go to the next person on the waiting list.", "ok"),
    "revoked": ("The device was revoked; its key no longer works.", "ok"),
    "codeadded": ("Code applied.", "ok"),
    "coderemoved": ("Code removed.", "ok"),
    "rolechanged": ("Role changed.", "ok"),
    "removed": ("Member removed.", "ok"),
    "created": ("Saved.", "ok"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "Seatwise"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    # ---- plumbing ------------------------------------------------------------------------------
    def _send(self, r: Resp) -> None:
        body = r.body.encode() if isinstance(r.body, str) else r.body
        self.send_response(r.status)
        self.send_header("Content-Type", r.ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if r.ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; script-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        for k, v in r.headers.items():
            for item in (v if isinstance(v, list) else [v]):
                self.send_header(k, item)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n < 0 or n > MAX_BODY:
            raise Resp(413, "Request too large", ctype="text/plain; charset=utf-8")
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("OTHER")

    def do_DELETE(self):
        self._dispatch("OTHER")

    def do_PATCH(self):
        self._dispatch("OTHER")

    def _dispatch(self, method: str) -> None:
        url = urllib.parse.urlsplit(self.path)
        self.url_path = url.path
        self.query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        self.form: dict[str, list[str]] = {}
        self.user = None
        self.sid_hash = None
        self.csrf = None
        self.conn = None
        self.now = now()
        path = url.path
        try:
            if path == "/healthz":
                raise self.healthz()
            if method == "OTHER":
                raise Resp(405, "Method not allowed", ctype="text/plain; charset=utf-8")
            if path == "/webhooks/payments":
                raise self.webhook(method)
            if path in ("/api/scan", "/api/scan/batch"):
                raise self.api_scan(method, batch=path.endswith("/batch"))
            if method == "POST":
                raw = self._read_body()
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype in ("", "application/x-www-form-urlencoded"):
                    self.form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
            if self._has_nul():
                raise Resp(400, self.page("Bad request", "<div class=card><p>The request contained characters that are "
                                          "not allowed.</p></div>"))
            with db.connect() as conn:
                self.conn = conn
                self._load_session()
                for pattern, name in ROUTES:
                    m = pattern.fullmatch(path)
                    if m:
                        fn = getattr(self, f"{name}_{method.lower()}", None)
                        if fn is None:
                            raise Resp(405, self.page("Not allowed", "<p>That method is not allowed here.</p>"),
                                       {"Allow": "GET, POST"})
                        raise fn(*m.groups())
                raise self.not_found()
        except Resp as r:
            try:
                self._send(r)
            except Exception:
                pass
        except psycopg.DataError:
            log_exc("web")
            self._safe_send(Resp(400, "Bad request: a value in the request is not valid.", ctype="text/plain; charset=utf-8"))
        except psycopg.OperationalError:
            log_exc("web")
            self._safe_send(Resp(503, "The database is unavailable. Try again shortly.", ctype="text/plain; charset=utf-8"))
        except Exception:
            log_exc("web")
            self._safe_send(Resp(500, self.page("Something went wrong", "<p>Something went wrong on our side. Try again.</p>")))

    def _has_nul(self) -> bool:
        if "\x00" in urllib.parse.unquote(self.url_path):
            return True
        for d in (self.form, self.query):
            for k, vs in d.items():
                if "\x00" in k or any("\x00" in v for v in vs):
                    return True
        return False

    def _safe_send(self, r: Resp) -> None:
        try:
            self._send(r)
        except Exception:
            pass

    # ---- request helpers -------------------------------------------------------------------------
    def f(self, name: str) -> str:
        v = self.form.get(name)
        return v[-1] if v else ""

    def fl(self, name: str) -> list[str]:
        return self.form.get(name, [])

    def q(self, name: str) -> str:
        v = self.query.get(name)
        return v[-1] if v else ""

    def _cookie_sid(self) -> str | None:
        try:
            c = SimpleCookie(self.headers.get("Cookie", ""))
        except Exception:
            return None
        return c[COOKIE].value if COOKIE in c else None

    def _load_session(self) -> None:
        sid = self._cookie_sid()
        if not sid:
            return
        h = sha(sid)
        row = self.conn.execute("SELECT a.* FROM sw_sessions s JOIN sw_accounts a ON a.id = s.account_id "
                                "WHERE s.sid_hash = %s", (h,)).fetchone()
        self.conn.commit()
        if row:
            self.user, self.sid_hash, self.csrf = row, h, csrf_for(h)

    def csrf_input(self) -> str:
        token = self.csrf or secrets.token_hex(20)
        return f'<input type="hidden" name="csrf_token" value="{E(token)}" data-csrf>'

    def form_html(self, action: str, inner: str, cls: str = "") -> str:
        return (f'<form method="post" action="{E(action)}"{f" class={chr(34)}{cls}{chr(34)}" if cls else ""}>'
                f'{self.csrf_input()}{inner}</form>')

    def need_user(self):
        if not self.user:
            if self.command in ("GET", "HEAD"):
                raise redirect("/signin?next=" + urllib.parse.quote(self.path, safe=""))
            raise redirect("/signin")
        return self.user

    def check_csrf(self) -> None:
        sent = self.f("csrf_token")
        if not self.csrf or not secrets.compare_digest(sent.encode(), self.csrf.encode()):
            raise Resp(403, self.page("Request refused", "<div class=card><p>This form is out of date or did not come from "
                                      "Seatwise, so nothing was changed. Go back, reload the page and try again.</p></div>"))

    def forbidden(self, msg: str = "You are not allowed to do that.") -> Resp:
        return Resp(403, self.page("Not allowed", f"<div class=card><p>{E(msg)}</p><p><a href=/>Back to events</a></p></div>"))

    def not_found(self, what: str = "") -> Resp:
        body = (f"<div class=card><p>There is no page at <code>{E(self.url_path)}</code>{E(what)}.</p>"
                "<p><a href=/>Back to events</a></p></div>")
        return Resp(404, self.page("Not found", body))

    def member(self, org_id: str, roles: tuple[str, ...] | None = None, post: bool = False):
        """The signed-in account's membership of the organization in the URL (403 otherwise)."""
        user = self.need_user()
        if post:
            self.check_csrf()
        oid = int(org_id)
        org = self.conn.execute("SELECT * FROM organizations WHERE id = %s", (oid,)).fetchone()
        if not org:
            raise self.not_found()
        m = self.conn.execute("SELECT role FROM memberships WHERE organization_id = %s AND user_id = %s",
                              (oid, user["id"])).fetchone()
        if not m:
            raise self.forbidden("You are not a member of this organization.")
        if roles and m["role"] not in roles:
            raise self.forbidden("Your role in this organization does not allow this.")
        return org, m["role"]

    def org_event(self, org, event_id: str):
        ev = domain.load_event(self.conn, int(event_id))
        if not ev or ev["org_id"] != org["id"]:
            raise self.not_found()
        return ev

    def page(self, title: str, body: str, flash: str = "", kind: str = "") -> str:
        u = self.user
        if u:
            nav = ('<a href="/">Events</a><a href="/tickets">My tickets</a><a href="/orgs">Organizations</a>'
                   f'<span class=who>{E(u["full_name"])}</span>'
                   + self.form_html("/signout", '<button type="submit" data-action="signout">Sign out</button>', "inline"))
        else:
            nav = '<a href="/">Events</a><a href="/signin">Sign in</a><a href="/signup">Create account</a>'
        if not flash:
            key = self.q("msg")
            if key in MESSAGES:
                flash, kind = MESSAGES[key]
        fl = f'<div class="flash {E(kind)}" role="status">{E(flash)}</div>' if flash else ""
        return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                f"<title>{E(title)} · Seatwise</title><style>{CSS}</style></head><body>"
                f'<header class="top"><a class="brand" href="/">Seatwise</a><nav>{nav}</nav></header>'
                f"<main><h1>{E(title)}</h1>{fl}{body}</main></body></html>")

    def render(self, title: str, body: str, status: int = 200, flash: str = "", kind: str = "") -> Resp:
        return Resp(status, self.page(title, body, flash, kind))

    def refused(self, title: str, body_fn, err: Refused) -> Resp:
        self.conn.rollback()
        return Resp(err.status, self.page(title, body_fn(), err.message, "err"))

    # ---- health, webhook, scan API -------------------------------------------------------------
    def healthz(self) -> Resp:
        """A fresh connection each time, with a hard deadline so a stalled database cannot hold the answer."""
        result = {"ok": False}

        def probe():
            try:
                with psycopg.connect(db.database_url(), connect_timeout=2, options="-c statement_timeout=2000") as c:
                    c.execute("SELECT 1")
                result["ok"] = True
            except Exception:
                result["ok"] = False
        t = threading.Thread(target=probe, daemon=True, name="healthz")
        t.start()
        t.join(3.5)
        if result["ok"]:
            return Resp(200, "ok", ctype="text/plain; charset=utf-8")
        return Resp(503, "database unreachable", ctype="text/plain; charset=utf-8")

    def webhook(self, method: str) -> Resp:
        if method != "POST":
            return jresp(400, {"error": "webhook deliveries are POST requests"})
        raw = self._read_body()
        if not payments.verify(raw, self.headers.get("Stripe-Signature")):
            return jresp(400, {"error": "invalid signature"})
        try:
            event = json.loads(raw.decode("utf-8"))
            assert isinstance(event, dict)
        except Exception:
            return jresp(400, {"error": "invalid payload"})
        with db.connect() as conn:
            domain.handle_webhook(conn, event, now())
            conn.commit()
        mail.KICK.set()
        domain.WORK_KICK.set()
        return jresp(200, {"received": True})

    _DEVICE_OK: dict[str, int] = {}

    def _device(self, conn):
        auth = (self.headers.get("Authorization") or "").strip()
        m = re.fullmatch(r"Bearer\s+(swd_([1-9]\d{0,8})_([A-Za-z0-9_\-]{16,128}))", auth)
        if not m:
            return None
        dev = conn.execute("SELECT * FROM sw_devices WHERE id = %s", (int(m[2]),)).fetchone()
        conn.commit()
        if not dev or dev["revoked_at"] is not None:
            return None
        h = sha(m[1])
        if Handler._DEVICE_OK.get(h) == dev["id"]:
            return dev
        from .util import check_secret
        if check_secret(m[3], dev["key_hash"]):
            Handler._DEVICE_OK[h] = dev["id"]
            return dev
        return None

    def api_scan(self, method: str, batch: bool) -> Resp:
        if method != "POST":
            return jresp(405, {"error": "use POST"})
        raw = self._read_body()
        arrived = now().replace(microsecond=0)
        with db.connect() as conn:
            dev = self._device(conn)
            if dev is None:
                return jresp(401, {"error": "unknown or revoked device key"})
            rows = self._scan_rows(raw, batch, arrived)
            if rows is None:
                return jresp(400, {"error": ('body must be JSON {"scans": [...]} with at most 1000 rows of scan_id, token, '
                                             'gate and scanned_at' if batch else 'body must be JSON {"token": ..., "gate": ...}')})
            results = domain.run_scans(conn, dev["event_id"], rows, dev["id"], now())
            conn.commit()
        if batch:
            return jresp(200, {"results": results})
        return jresp(200, {**results[0], "results": results})

    @staticmethod
    def _gate_ok(g) -> bool:
        return isinstance(g, str) and 1 <= len(g) <= 40 and not has_control(g)

    def _scan_rows(self, raw: bytes, batch: bool, arrived):
        from .util import parse_rfc3339
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(body, dict):
            return None
        if not batch:
            tok, gate = body.get("token"), body.get("gate")
            if not isinstance(tok, str) or "\x00" in tok or not self._gate_ok(gate):
                return None
            return [{"scan_id": None, "token": tok, "gate": gate, "scanned_at": arrived, "raw": None}]
        scans = body.get("scans")
        if not isinstance(scans, list) or len(scans) > 1000:
            return None
        out = []
        for s in scans:
            if not isinstance(s, dict):
                return None
            sid, tok, gate, at = s.get("scan_id"), s.get("token"), s.get("gate"), s.get("scanned_at")
            if not isinstance(sid, str) or not 1 <= len(sid) <= 64 or not isinstance(tok, str) or not self._gate_ok(gate) \
                    or "\x00" in sid or "\x00" in tok:
                return None
            when = parse_rfc3339(at)
            if when is None:
                return None
            out.append({"scan_id": sid, "token": tok, "gate": gate, "scanned_at": when.replace(microsecond=0), "raw": at})
        return out


from . import pages  # noqa: E402  (handlers for every page live in pages.py)

ROUTES = [(re.compile(p), n) for p, n in pages.ROUTES]
for _name in dir(pages.Pages):
    if not _name.startswith("__"):
        setattr(Handler, _name, getattr(pages.Pages, _name))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256

    def handle_error(self, request, client_address):  # no tracebacks with request data in the log
        log_exc("http")


def main() -> None:
    db.apply_schema()
    mail.start(db.connect)
    worker.start()
    port = env_int("PORT", 8080)
    srv = Server(("0.0.0.0", port), Handler)
    sys.stderr.write(f"seatwise listening on 0.0.0.0:{port}\n")
    srv.serve_forever()
