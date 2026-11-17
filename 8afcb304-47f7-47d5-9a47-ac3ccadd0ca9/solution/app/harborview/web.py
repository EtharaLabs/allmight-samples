"""Harborview HTTP layer: stdlib ThreadingHTTPServer, server-side sessions, CSRF, HTML pages, JSON API."""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import traceback
import urllib.parse
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

from . import avail, db, provider, service as S
from .avail import UTC, tzinfo
from .service import Refused, dollars, norm_email, now_utc, sha

COOKIE = "hv_session"
DESK = ("front_desk", "manager", "admin")
E = lambda s: html.escape("" if s is None else str(s), quote=True)  # noqa: E731
DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
WIN_RE = re.compile(r"^(MON|TUE|WED|THU|FRI|SAT|SUN)\s+(\d{2}):(\d{2})-(\d{2}):(\d{2})$")

CSS = """
*{box-sizing:border-box}body{margin:0;font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#17212b;background:#f3f6f8}
header{background:#0f4c5c;color:#fff;padding:10px 16px;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between}
header a{color:#fff;margin-right:12px;text-decoration:none}header nav{display:flex;flex-wrap:wrap;gap:4px 0}
main{max-width:1040px;margin:0 auto;padding:16px}h1{font-size:1.5rem;margin:.2em 0 .6em}h2{font-size:1.15rem}
.card{background:#fff;border:1px solid #d8e0e6;border-radius:10px;padding:16px;margin:0 0 16px;overflow-wrap:anywhere}
.flash{padding:12px 16px;border-radius:8px;margin:0 0 16px;background:#e6f3ff;border:1px solid #9cc7f0}.flash.err{background:#fdecec;border-color:#f0a8a8}
button{font:inherit;padding:8px 14px;border-radius:6px;border:1px solid #0f4c5c;background:#0f4c5c;color:#fff;cursor:pointer}
button.secondary{background:#fff;color:#0f4c5c}button.danger{background:#a83232;border-color:#a83232}
input,select,textarea{font:inherit;padding:8px;border:1px solid #b5c1ca;border-radius:6px;width:100%;max-width:440px}
textarea{min-height:120px}label{display:block;margin:8px 0}.muted{color:#56626c}
.slots{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;padding:0;list-style:none}
.slots form{margin:0}.slots button{width:100%}
.status{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;background:#e8eef2}
.status.confirmed,.status.booked{background:#dff5e3}.status.held,.status.offered,.status.pending{background:#fff4d6}
.status.cancelled,.status.expired,.status.released{background:#fde2e2}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.list{list-style:none;padding:0;margin:0}
.list>li{border-bottom:1px solid #e4e9ed;padding:10px 0}dl{display:grid;grid-template-columns:minmax(90px,max-content) 1fr;gap:4px 12px;margin:0}
dt{color:#56626c}dd{margin:0}
"""


def csrf_input(token: str | None) -> str:
    return f"<input type=hidden name=csrf_token data-csrf value='{E(token)}'>" if token else ""


class Resp(Exception):
    def __init__(self, status: int, body: str | bytes = "", headers: dict | None = None, ctype: str = "text/html; charset=utf-8"):
        self.status, self.body, self.headers, self.ctype = status, body, headers or {}, ctype


def redirect(to: str, extra: dict | None = None) -> Resp:
    return Resp(303, "", {"Location": to, **(extra or {})})


def parse_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat((s or "").strip())
    except ValueError:
        return None


def to_int(s) -> int | None:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


def hm_ok(s: str) -> int | None:
    m = re.fullmatch(r"(\d{2}):(\d{2})", (s or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if mi > 59 or h > 24 or (h == 24 and mi):
        return None
    return h * 60 + mi


def safe_next(v: str) -> bool:
    """R2.3: a path on this site: one leading slash, not followed by / or \\, no control characters."""
    return (len(v) >= 1 and v[0] == "/" and (len(v) == 1 or v[1] not in "/\\")
            and not any(ord(ch) < 32 or ord(ch) == 127 for ch in v))


def bad_name(v: str) -> bool:
    """R2.7: 1 to 200 characters, no line break."""
    return not v or len(v) > 200 or "\r" in v or "\n" in v


def local_time(a_start: datetime, tz) -> str:
    loc = a_start.astimezone(tz)
    return loc.strftime("%A %Y-%m-%d at %H:%M ") + (loc.tzname() or "")


class Handler(BaseHTTPRequestHandler):
    server_version = "Harborview"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- plumbing ---------------------------------------------------------------------------------
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

    def _form(self, raw: bytes) -> dict:
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("application/x-www-form-urlencoded") or not ctype:
            return {k: v[-1] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}
        return {}

    def _session(self, conn):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        if COOKIE not in c:
            return None, None
        row = conn.execute("SELECT s.csrf, s.user_id FROM hv_sessions s WHERE s.sid_hash = %s AND NOT s.revoked",
                           (sha(c[COOKIE].value),)).fetchone()
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
        self.query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        path = url.path
        try:
            if path == "/healthz":
                raise self.healthz()
            raw = self._read() if method == "POST" else b""
            if path == "/webhooks/payments" and method == "POST":
                raise self.webhook(raw)
            self.form = self._form(raw) if method == "POST" else {}
            with db.connect() as conn:
                self.conn = conn
                self.user, self.csrf = self._session(conn)
                self.clin = S.clinician_of_user(conn, self.user["id"]) if self.user and self.user["role"] == "clinician" else None
                for pattern, name in ROUTES:
                    m = re.fullmatch(pattern, path)
                    if m:
                        fn = getattr(self, f"{name}_{method.lower()}", None)
                        if fn is None:
                            raise Resp(405, "Method not allowed", ctype="text/plain")
                        if method == "POST" and name not in ("signin", "register", "reset", "reset_token"):
                            self.need_user()
                            self.check_csrf()
                        raise fn(*m.groups())
                raise self.render("Not found", "<div class=card><p>That page does not exist.</p></div>", 404)
        except Resp as r:
            try:
                self._send(r)
            except Exception:
                pass
        except Refused as r:
            self._send(self.render("Not possible", f"<div class=card><p>{E(r.msg)}</p><p><a href='javascript:history.back()'>Go back</a>"
                                   " or <a href=/appointments>see your appointments</a>.</p></div>", r.status, err=True))
        except psycopg.OperationalError:
            self._send(Resp(503, "Database unavailable", ctype="text/plain"))
        except Exception:
            traceback.print_exc()
            self._send(Resp(500, "Internal error", ctype="text/plain"))

    # -- guards & rendering -------------------------------------------------------------------------
    def need_user(self, roles: tuple[str, ...] | None = None) -> dict:
        if not self.user:
            target = self.path if self.command == "GET" else "/"
            raise redirect("/signin?next=" + urllib.parse.quote(target, safe=""))
        if roles and self.user["role"] not in roles:
            raise self.forbidden()
        return self.user

    def staff_clinics(self) -> set:
        """Clinics whose desk work the signed-in user may do (R1.3)."""
        if not self.user:
            return set()
        if self.user["role"] == "admin":
            return {r["id"] for r in self.conn.execute("SELECT id FROM clinics")}
        if self.user["role"] in ("front_desk", "manager") and self.user.get("clinic_id"):
            return {self.user["clinic_id"]}
        return set()

    def need_clinic(self, clinic_id) -> None:
        if clinic_id not in self.staff_clinics():
            raise self.forbidden()

    def forbidden(self) -> Resp:
        return self.render("Not allowed", "<div class=card><p>You are not allowed to see or do that.</p></div>", 403, err=True)

    def check_csrf(self) -> None:
        sent = self.form.get("csrf_token", "")
        if not self.csrf or not secrets.compare_digest(sent, self.csrf):
            raise self.render("Not allowed", "<div class=card><p>This form expired or came from another site. Reload the page and"
                              " try again.</p></div>", 403, err=True)

    def page(self, title: str, body: str, flash: str = "", err: bool = False) -> str:
        nav = ""
        u = self.user
        if u:
            links = []
            if u["role"] == "patient":
                links += ['<a href="/book">Book</a>', '<a href="/appointments">My appointments</a>', '<a href="/waitlist">Waitlist</a>']
            if u["role"] == "clinician" and self.clin:
                links += [f'<a href="/clinician/day">My day</a>', f'<a href="/clinicians/{self.clin["id"]}/schedule">My schedule</a>']
            if u["role"] in ("front_desk", "manager", "admin"):
                links += ['<a href="/desk/day">Day sheet</a>', '<a href="/desk/book">Book for a patient</a>', '<a href="/desk/reminders">Reminders</a>']
            if u["role"] == "manager" and u.get("clinic_id"):
                links.append(f'<a href="/admin/clinics/{u["clinic_id"]}">Clinic</a>')
            if u["role"] == "admin":
                links += ['<a href="/admin/clinicians/new">Clinicians</a>', '<a href="/admin/types/new">Types</a>',
                          '<a href="/admin/rooms/new">Rooms</a>']
            links.append('<a href="/profile">Profile</a>')
            nav = (f"<nav>{''.join(links)}</nav><form method=post action=/signout style='margin:0' class=row>{csrf_input(self.csrf)}"
                   f"<span style='color:#cfe6ec'>{E(u['full_name'])}</span>"
                   "<button class=secondary data-action=signout>Sign out</button></form>")
        else:
            nav = "<nav><a href=/book>Book</a><a href=/signin>Sign in</a><a href=/register>Register</a></nav>"
        fl = f"<div class='flash{' err' if err else ''}' role=status>{flash}</div>" if flash else ""
        return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                f"<title>{E(title)} · Harborview Physio</title><style>{CSS}</style></head><body><header><strong>Harborview Physio</strong>{nav}"
                f"</header><main><h1>{E(title)}</h1>{fl}{body}</main></body></html>")

    def render(self, title: str, body: str, status: int = 200, flash: str = "", err: bool = False) -> Resp:
        return Resp(status, self.page(title, body, flash, err))

    def form_html(self, action: str, inner: str, button: str, marker: str, cls: str = "") -> str:
        return (f"<form method=post action='{E(action)}'>{csrf_input(self.csrf)}{inner}"
                f"<button{(' class=' + cls) if cls else ''} data-action='{marker}'>{E(button)}</button></form>")

    # -- health & webhook ----------------------------------------------------------------------------
    def healthz(self) -> Resp:
        try:
            with psycopg.connect(db.DATABASE_URL, connect_timeout=3) as c:
                c.execute("SET statement_timeout = 3000")
                c.execute("SELECT 1")
            return Resp(200, "ok", ctype="text/plain")
        except Exception:
            return Resp(503, "database unreachable", ctype="text/plain")

    def webhook(self, raw: bytes) -> Resp:
        if not provider.verify(self.headers.get("Stripe-Signature"), raw):
            return Resp(400, "bad signature", ctype="text/plain")
        try:
            event = json.loads(raw.decode())
        except ValueError:
            return Resp(400, "bad body", ctype="text/plain")
        if isinstance(event, dict):
            S.handle_event(event)
        return Resp(200, "ok", ctype="text/plain")

    # -- public API ------------------------------------------------------------------------------------
    def availability_get(self) -> Resp:
        cid, code, d = to_int(self.query.get("clinician_id")), self.query.get("type", ""), parse_date(self.query.get("date"))
        if cid is None or d is None:
            return Resp(400, json.dumps({"error": "clinician_id and date (YYYY-MM-DD) are required"}), ctype="application/json")
        c, t = avail.context(self.conn, cid, code)
        if c is None or t is None:
            return Resp(404, json.dumps({"error": "unknown clinician or type"}), ctype="application/json")
        tz = tzinfo(c["timezone"])
        dur = timedelta(minutes=t["duration_minutes"])
        slots = [{"start": avail.iso_local(s, tz), "end": avail.iso_local(s + dur, tz)}
                 for s, _ in avail.compute_slots(self.conn, cid, code, d, now_utc())]
        return Resp(200, json.dumps({"clinician_id": cid, "type": code, "date": d.isoformat(), "slots": slots}),
                    ctype="application/json")

    # -- home ------------------------------------------------------------------------------------------
    def home_get(self) -> Resp:
        if not self.user:
            return self.render("Welcome", "<div class=card><p>Book physiotherapy at our Seattle and Honolulu clinics.</p>"
                               "<p><a href=/book>Find a time</a> · <a href=/signin>Sign in</a> · <a href=/register>Register</a></p></div>")
        role = self.user["role"]
        target = {"patient": "/appointments", "clinician": "/clinician/day", "front_desk": "/desk/day", "manager": "/desk/day", "admin": "/desk/day"}[role]
        return redirect(target)

    # -- accounts ---------------------------------------------------------------------------------------
    def _start_session(self, user_id: int) -> dict:
        sid = secrets.token_urlsafe(32)
        self.conn.execute("INSERT INTO hv_sessions (sid_hash, user_id, csrf) VALUES (%s, %s, %s)",
                          (sha(sid), user_id, secrets.token_urlsafe(24)))
        self.conn.commit()
        return {"Set-Cookie": f"{COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax"}

    def register_get(self, msg: str = "", status: int = 200, email: str = "", name: str = "") -> Resp:
        body = ("<div class=card><form method=post action=/register>"
                f"<label>Email <input name=email type=email value='{E(email)}' required></label>"
                f"<label>Full name <input name=full_name value='{E(name)}' required></label>"
                "<label>Password (at least 10 characters) <input name=password type=password minlength=10 required></label>"
                "<button data-action=register>Create account</button></form>"
                "<p class=muted>Already a patient of ours? <a href=/password-reset>Set a password</a> for your existing record.</p></div>")
        return self.render("Create a patient account", body, status, msg, err=bool(msg))

    def register_post(self) -> Resp:
        email, name, pw = self.form.get("email", ""), self.form.get("full_name", "").strip(" "), self.form.get("password", "")
        if "@" not in email or bad_name(name) or len(pw) < 10:
            return self.register_get("Enter an email address, your name, and a password of at least 10 characters.", 400, email, name)
        if S.any_user_with(self.conn, email):
            return self.register_get("That address is already on file. Use “Set a password” to sign in with it.", 409, email, name)
        uid = self.conn.execute("INSERT INTO users (email, full_name, role, active) VALUES (%s, %s, 'patient', true) RETURNING id",
                                (norm_email(email), name)).fetchone()["id"]
        self.conn.execute("INSERT INTO hv_credentials (user_id, password_hash) VALUES (%s, %s)", (uid, S.hash_password(pw)))
        self.conn.commit()
        return redirect("/appointments", self._start_session(uid))

    def signin_get(self, msg: str = "", status: int = 200) -> Resp:
        nxt = self.form.get("next", "") if self.command == "POST" else self.query.get("next", "")
        body = ("<div class=card><form method=post action=/signin>"
                f"<input type=hidden name=next value='{E(nxt)}'>"
                "<label>Email <input name=email type=email autocomplete=username required></label>"
                "<label>Password <input name=password type=password autocomplete=current-password required></label>"
                "<button data-action=signin>Sign in</button></form>"
                "<p class=muted>First time here or forgot your password? <a href=/password-reset>Set a password</a>. "
                "New patient? <a href=/register>Register</a>.</p></div>")
        return self.render("Sign in", body, status, msg, err=bool(msg))

    def signin_post(self) -> Resp:
        email, pw = self.form.get("email", ""), self.form.get("password", "")
        u = S.account_for(self.conn, email)
        cred = self.conn.execute("SELECT password_hash FROM hv_credentials WHERE user_id = %s", (u["id"],)).fetchone() if u else None
        if not cred or not S.check_password(pw, cred["password_hash"]):
            return self.signin_get("That email and password do not match an active account.", 401)
        nxt = self.form.get("next", "")
        return redirect(nxt if safe_next(nxt) else "/", self._start_session(u["id"]))

    def signout_post(self) -> Resp:
        c = SimpleCookie(self.headers.get("Cookie", ""))
        if COOKIE in c:
            self.conn.execute("UPDATE hv_sessions SET revoked = true WHERE sid_hash = %s", (sha(c[COOKIE].value),))
            self.conn.commit()
        return redirect("/signin", {"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})

    RESET_MSG = "If that address belongs to an account, we have emailed it a link to set a password."

    def reset_get(self, msg: str = "") -> Resp:
        body = ("<div class=card><form method=post action=/password-reset>"
                "<label>Email <input name=email type=email required></label>"
                "<button data-action=request-reset>Email me a link</button></form></div>")
        return self.render("Set or reset your password", body, 200, msg)

    def reset_post(self) -> Resp:
        u = S.account_for(self.conn, self.form.get("email", ""))
        if u:
            token = secrets.token_urlsafe(32)
            self.conn.execute("UPDATE hv_reset_tokens SET state = 'replaced' WHERE user_id = %s AND state = 'open'", (u["id"],))
            self.conn.execute("INSERT INTO hv_reset_tokens (token_hash, user_id, created_at) VALUES (%s, %s, now())", (sha(token), u["id"]))
            self.conn.commit()
            S.send_now(u["email"], u["full_name"], "Set your Harborview Physio password",
                       f"Hello {u['full_name']},\n\nUse this link to set your password (it works once and expires in "
                       f"{max(1, db.RESET_TTL // 60)} minute(s)):\n{db.PUBLIC_BASE_URL}/password-reset/{token}\n\nHarborview Physio\n")
        return self.reset_get(self.RESET_MSG)

    def _token_row(self, token: str):
        return self.conn.execute("SELECT * FROM hv_reset_tokens WHERE token_hash = %s", (sha(token),)).fetchone()

    def _token_valid(self, row) -> bool:
        return row["state"] == "open" and (now_utc() - row["created_at"]).total_seconds() <= db.RESET_TTL

    def reset_token_get(self, token: str) -> Resp:
        row = self._token_row(token)
        if row is None:
            return self.render("Link not found", "<div class=card><p>That link does not exist.</p></div>", 404)
        if not self._token_valid(row):
            return self.render("Link no longer valid", "<div class=card><p>This link is no longer valid. "
                               "<a href=/password-reset>Request a new one</a>.</p></div>", 410)
        body = (f"<div class=card><form method=post action='/password-reset/{E(token)}'>"
                "<label>New password (at least 10 characters) <input name=password type=password minlength=10 required></label>"
                "<button data-action=set-password>Set password</button></form></div>")
        return self.render("Choose a password", body)

    def reset_token_post(self, token: str) -> Resp:
        self.conn.execute("SELECT pg_advisory_xact_lock(73300, hashtext(%s))", (token,))
        row = self._token_row(token)
        if row is None:
            return self.render("Link not found", "<div class=card><p>That link does not exist.</p></div>", 404)
        if not self._token_valid(row):
            return self.render("Link no longer valid", "<div class=card><p>This link is no longer valid. "
                               "<a href=/password-reset>Request a new one</a>.</p></div>", 410)
        pw = self.form.get("password", "")
        if len(pw) < 10:
            return self.render("Choose a password", "<div class=card><p>Use at least 10 characters.</p></div>", 400)
        self.conn.execute("UPDATE hv_reset_tokens SET state = 'used' WHERE token_hash = %s", (sha(token),))
        self.conn.execute("INSERT INTO hv_credentials (user_id, password_hash) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE"
                          " SET password_hash = EXCLUDED.password_hash, updated_at = now()", (row["user_id"], S.hash_password(pw)))
        self.conn.execute("UPDATE hv_sessions SET revoked = true WHERE user_id = %s", (row["user_id"],))
        self.conn.commit()
        return self.render("Password set", "<div class=card><p>Your password is set. <a href=/signin>Sign in</a>.</p></div>")

    def profile_get(self, msg: str = "") -> Resp:
        u = self.need_user()
        body = "<div class=card>" + self.form_html("/profile", f"<label>Display name <input name=full_name value='{E(u['full_name'])}' required></label>",
                                                   "Save", "save-profile") + "</div>"
        return self.render("Your profile", body, flash=msg)

    def profile_post(self) -> Resp:
        name = self.form.get("full_name", "").strip(" ")
        if bad_name(name):
            return self.render("Your profile", "<div class=card><p>Enter a name of 1 to 200 characters.</p></div>", 400, err=True)
        self.conn.execute("UPDATE users SET full_name = %s WHERE id = %s", (name, self.user["id"]))
        self.conn.commit()
        self.user["full_name"] = name
        return self.profile_get("Saved.")

    # -- booking ------------------------------------------------------------------------------------------
    def _clinician_options(self, selected: int | None, clinic_id: int | None = None, among: set | None = None) -> str:
        rows = self.conn.execute("SELECT c.id, c.clinic_id, u.full_name, cl.name AS clinic FROM clinicians c JOIN users u ON u.id = c.user_id"
                                 " JOIN clinics cl ON cl.id = c.clinic_id WHERE c.active" + (" AND c.clinic_id = %s" if clinic_id else "")
                                 + " ORDER BY cl.id, u.full_name", (clinic_id,) if clinic_id else ()).fetchall()
        if among is not None:
            rows = [r for r in rows if r["clinic_id"] in among]
        return "".join(f"<option value={r['id']}{' selected' if r['id'] == selected else ''}>{E(r['full_name'])} ({E(r['clinic'])})</option>"
                       for r in rows)

    def book_get(self) -> Resp:
        cid, code, d = to_int(self.query.get("clinician_id")), self.query.get("type", ""), parse_date(self.query.get("date"))
        types = self.conn.execute("SELECT code, name, duration_minutes FROM appointment_types WHERE active ORDER BY name").fetchall()
        topts = "".join(f"<option value='{E(t['code'])}'{' selected' if t['code'] == code else ''}>{E(t['name'])} ({t['duration_minutes']} min)</option>"
                        for t in types)
        chooser = ("<div class=card><form method=get action=/book class=row>"
                   f"<label>Clinician <select name=clinician_id>{self._clinician_options(cid)}</select></label>"
                   f"<label>Type <select name=type>{topts}</select></label>"
                   f"<label>Date <input type=date name=date value='{E(d.isoformat() if d else '')}' required></label>"
                   "<button class=secondary>Show times</button></form></div>")
        if cid is None or d is None or not code:
            return self.render("Book an appointment", chooser)
        c, t = avail.context(self.conn, cid, code)
        if c is None or t is None:
            return self.render("Book an appointment", chooser + "<div class=card><p>Unknown clinician or type.</p></div>", 404)
        tz = tzinfo(c["timezone"])
        name = self.conn.execute("SELECT full_name FROM users WHERE id = %s", (c["user_id"],)).fetchone()["full_name"]
        slots = avail.compute_slots(self.conn, cid, code, d, now_utc())
        fee = avail.fee_on(self.conn, t["id"], d)
        dep = S.half_up(fee["fee_cents"] * fee["deposit_bp"], 10000) if fee else None
        items = []
        for s, _ in slots:
            loc = s.astimezone(tz)
            inner = (f"<input type=hidden name=clinician_id value={cid}><input type=hidden name=type value='{E(code)}'>"
                     f"<input type=hidden name=start value='{E(loc.isoformat())}'>")
            if self.user and self.user["role"] == "patient":
                items.append("<li>" + self.form_html("/book", inner, f"{loc.strftime('%H:%M')} {loc.tzname()}", "book-slot") + "</li>")
            else:
                items.append(f"<li><span class=status>{loc.strftime('%H:%M')} {loc.tzname()}</span></li>")
        head = (f"<div class=card><h2>{E(t['name'])} with {E(name)}</h2><p>{E(c['clinic_name'])} · "
                f"{E(d.strftime('%A %Y-%m-%d'))} · times are local clinic time ({E(c['timezone'])})</p>"
                + (f"<p>Deposit due at booking: <strong>{dollars(dep)}</strong>. Your slot is held for "
                   f"{max(1, db.HOLD_TTL // 60)} minute(s) while you pay.</p>" if dep is not None else "")
                + (f"<ul class=slots>{''.join(items)}</ul>" if items else "<p>No times are free on this day.</p>"))
        if self.user and self.user["role"] == "patient":
            inner = (f"<input type=hidden name=clinician_id value={cid}><input type=hidden name=type value='{E(code)}'>"
                     f"<input type=hidden name=date value='{d.isoformat()}'>")
            head += ("<p class=muted>Nothing suitable? Join the waitlist and we will email you if a time opens up that day.</p>"
                     + self.form_html("/waitlist", inner, "Join the waitlist for this day", "join-waitlist", "secondary"))
        elif not self.user:
            head += "<p class=muted><a href=/signin>Sign in</a> to book.</p>"
        return self.render("Book an appointment", chooser + head + "</div>")

    def book_post(self) -> Resp:
        u = self.need_user()
        if u["role"] != "patient":
            return self.forbidden()
        cid = to_int(self.form.get("clinician_id"))
        if cid is None:
            raise Refused(400, "Choose a clinician.")
        _, url = S.book(u, cid, self.form.get("type", ""), self.form.get("start", ""))
        return redirect(url)

    # -- appointments -------------------------------------------------------------------------------------
    def appointments_get(self) -> Resp:
        u = self.need_user()
        rows = [S.enrich(r) for r in self.conn.execute(S.APPT_SQL + " WHERE a.patient_id = %s ORDER BY a.local_start DESC LIMIT 200",
                                                        (u["id"],)).fetchall()]
        items = []
        for a in rows:
            if a["status"] == "offered":
                continue
            items.append(f"<li data-appointment-id={a['id']} data-start='{E(a['start'].isoformat())}' data-status='{E(a['status'])}'>"
                         f"<a href=/appointments/{a['id']}>{E(a['type_name'])} with {E(a['clinician_name'])}</a> — "
                         f"{E(local_time(a['start'], a['tz']))} · <span class='status {E(a['status'])}'>{E(a['status'])}</span></li>")
        body = "<div class=card>" + (f"<ul class=list>{''.join(items)}</ul>" if items else "<p>No appointments yet. <a href=/book>Book one</a>.</p>") + "</div>"
        return self.render("My appointments", body)

    def _can_view(self, a: dict) -> bool:
        u = self.user
        if u["role"] in ("front_desk", "manager", "admin"):
            return a["clinic_id"] in self.staff_clinics()
        if u["role"] == "patient":
            return a["patient_id"] == u["id"]
        return bool(self.clin) and a["clinician_id"] == self.clin["id"]

    def appointment_get(self, aid: str) -> Resp:
        self.need_user()
        a = S.load_appt(self.conn, int(aid))
        if a is None:
            return self.render("Not found", "<div class=card><p>No such appointment.</p></div>", 404)
        if not self._can_view(a):
            return self.forbidden()
        tz, st = a["tz"], a["status"]
        marker = (f"<div class=card data-appointment-id={a['id']} data-status='{E(st)}' data-start='{E(a['start'].astimezone(tz).isoformat())}'"
                  f" data-clinician-id={a['clinician_id']} data-type='{E(a['code'])}' data-room-id='{a['room_id'] or ''}'"
                  f" data-deposit-cents={a['deposit_cents']} data-refund-cents={a['refund_cents'] or 0}"
                  f" data-refund-status='{E(a['refund_status'] or 'none')}'"
                  + (f" data-rescheduled-to={a['rescheduled_to']}" if a.get("rescheduled_to") else "") + ">")
        info = (f"<p><span class='status {E(st)}'>{E(st.replace('_', ' '))}</span></p><dl>"
                f"<dt>When</dt><dd>{E(local_time(a['start'], tz))} ({a['duration_minutes']} minutes)</dd>"
                f"<dt>Clinic</dt><dd>{E(a['clinic_name'])}</dd><dt>Clinician</dt><dd>{E(a['clinician_name'])}</dd>"
                f"<dt>Type</dt><dd>{E(a['type_name'])}</dd><dt>Room</dt><dd>{E(a['room_name'] or 'not assigned')}</dd>"
                f"<dt>Patient</dt><dd>{E(a['patient_name'])}</dd><dt>Deposit</dt><dd>{dollars(a['deposit_cents'])}</dd></dl>")
        parts = [marker + info]
        ck = self.query.get("checkout", "")
        if st == "held":
            exp = a["expires_at"].astimezone(tz)
            left = max(0, int((a["expires_at"] - now_utc()).total_seconds()))
            msg = ("Thank you. We are waiting for the payment provider to confirm your payment; this page shows "
                   "“confirmed” as soon as it does (reload to check)." if ck == "success" else
                   "You left the payment page. The slot is still held for you; pay from the link below or release it."
                   if ck == "cancelled" else "This slot is held for you while you pay the deposit.")
            parts.append(f"<p>{msg}</p><p>The hold lasts until <strong>{exp.strftime('%H:%M:%S')} {exp.tzname()}</strong> "
                         f"(about {left // 60} min {left % 60} s from now). If the payment is not confirmed by then, "
                         "the slot is given up and any late payment is refunded in full.</p>")
            if a["patient_id"] == self.user["id"]:
                parts.append(self.form_html(f"/appointments/{a['id']}/release", "", "Release this hold", "release-hold", "secondary"))
        elif st == "confirmed":
            before = (a["start"] - now_utc()).total_seconds()
            parts.append(f"<p>Confirmed. Deposit paid: <strong>{dollars(a['paid_cents'])}</strong>.</p>")
            staff = self.user["role"] in ("front_desk", "manager", "admin") and a["clinic_id"] in self.staff_clinics()
            if a["app"] and before > 0 and (staff or a["patient_id"] == self.user["id"]):
                refund = S.refund_for(a["paid_cents"], before, staff)
                parts.append(f"<h2>Cancel</h2><p>Cancelling now gives a refund of <strong>{dollars(refund)}</strong> — "
                             f"{E(S.band_text(before, staff))}. (Refunds: 24 h or more before the start, the whole deposit; "
                             "2 to 24 h, half; under 2 h, nothing.)</p>"
                             + self.form_html(f"/appointments/{a['id']}/cancel", "", "Cancel appointment", "cancel-appointment", "danger"))
            if a["app"] and a["patient_id"] == self.user["id"]:
                if before >= 86400:
                    parts.append("<h2>Reschedule</h2><p>Pick a new time with any clinician at this clinic (same appointment type). "
                                 "Your deposit carries over; there is nothing more to pay.</p>"
                                 + self.form_html(f"/appointments/{a['id']}/reschedule",
                                                  f"<label>Clinician <select name=clinician_id>{self._clinician_options(a['clinician_id'], a['clinic_id'])}</select></label>"
                                                  f"<label>New start (as shown on the booking page, e.g. {E(a['start'].astimezone(tz).isoformat())}) "
                                                  "<input name=start required></label>", "Reschedule", "reschedule"))
                else:
                    parts.append("<p class=muted>Rescheduling closes 24 hours before the start; you can still cancel.</p>")
        elif st == "cancelled":
            rs = a["refund_status"]
            parts.append(f"<p>This appointment was cancelled{' by the clinic' if a['cancelled_by_staff'] else ''}. Refund: "
                         f"<strong>{dollars(a['refund_cents'])}</strong> — "
                         + {"none": "no refund is due.", "pending": "the refund is on its way to your card (pending).",
                            "refunded": "refunded to your card."}.get(rs, rs) + "</p>")
        elif st in ("expired", "released"):
            why = "The hold expired before the payment was confirmed" if st == "expired" else "You released this hold"
            parts.append(f"<p>{why}, so the slot was given up and is open to others again.</p>"
                         f"<p><a href='/book?clinician_id={a['clinician_id']}&amp;type={E(a['code'])}&amp;date={a['local_date']}'>"
                         "Book again</a> to choose a new time.</p>")
            if a["refund_status"] != "none":
                parts.append(f"<p>Your late payment of {dollars(a['refund_cents'])} is being refunded in full "
                             f"({'refunded' if a['refund_status'] == 'refunded' else 'pending'}).</p>")
        elif st == "rescheduled":
            parts.append(f"<p>This appointment was moved. <a href=/appointments/{a['rescheduled_to']}>See the new appointment</a>.</p>")
        if self.user["role"] == "admin" or (self.clin and a["clinician_id"] == self.clin["id"]):
            parts.append(f"<p><a href=/appointments/{a['id']}/notes>Clinical notes</a></p>")
        return self.render(f"{a['type_name']} with {a['clinician_name']}", "".join(parts) + "</div>")

    def release_post(self, aid: str) -> Resp:
        S.release(self.user, int(aid))
        return redirect(f"/appointments/{aid}")

    def cancel_post(self, aid: str) -> Resp:
        S.cancel(self.user, int(aid))
        return redirect(f"/appointments/{aid}")

    def reschedule_post(self, aid: str) -> Resp:
        cid = to_int(self.form.get("clinician_id"))
        if cid is None:
            raise Refused(400, "Choose a clinician.")
        new = S.reschedule(self.user, int(aid), cid, self.form.get("start", ""))
        return redirect(f"/appointments/{new}")

    # -- notes ----------------------------------------------------------------------------------------------
    def _notes_access(self, aid: int):
        self.need_user()
        a = S.load_appt(self.conn, aid)
        if a is None:
            raise self.render("Not found", "<div class=card><p>No such appointment.</p></div>", 404)
        treating = bool(self.clin) and a["clinician_id"] == self.clin["id"]
        if not (treating or self.user["role"] == "admin"):
            raise self.forbidden()
        return a, treating

    def notes_get(self, aid: str, msg: str = "", status: int = 200) -> Resp:
        a, treating = self._notes_access(int(aid))
        notes = self.conn.execute("SELECT n.*, u.full_name FROM clinical_notes n JOIN users u ON u.id = n.author_id"
                                  " WHERE n.appointment_id = %s ORDER BY n.created_at, n.id", (a["id"],)).fetchall()
        items = "".join(f"<li data-note-id={n['id']}><p style='white-space:pre-wrap'>{E(n['body'])}</p>"
                        f"<p class=muted>{E(n['full_name'])} · {E(n['created_at'].astimezone(a['tz']).strftime('%Y-%m-%d %H:%M %Z'))}</p></li>"
                        for n in notes)
        body = (f"<div class=card><p>{E(a['patient_name'])} · {E(a['type_name'])} · {E(local_time(a['start'], a['tz']))}</p>"
                + (f"<ul class=list>{items}</ul>" if items else "<p>No notes yet.</p>") + "</div>")
        if treating:
            body += "<div class=card>" + self.form_html(f"/appointments/{a['id']}/notes", "<label>New note <textarea name=body maxlength=5000 required></textarea></label>",
                                                        "Add note", "add-note") + "</div>"
        return self.render("Clinical notes", body, status, msg, err=status >= 400)

    def notes_post(self, aid: str) -> Resp:
        a, treating = self._notes_access(int(aid))
        if not treating:
            return self.forbidden()
        text = self.form.get("body", "")
        if not (1 <= len(text) <= 5000) or not text.strip():
            return self.notes_get(aid, "A note needs 1 to 5000 characters.", 400)
        self.conn.execute("INSERT INTO clinical_notes (appointment_id, author_id, body) VALUES (%s, %s, %s)",
                          (a["id"], self.user["id"], text))
        self.conn.commit()
        return redirect(f"/appointments/{a['id']}/notes")

    # -- waitlist and offers -----------------------------------------------------------------------------------
    def waitlist_get(self) -> Resp:
        u = self.need_user(("patient",))
        rows = self.conn.execute("SELECT w.*, cu.full_name AS clinician_name, t.name AS type_name FROM hv_waitlist w"
                                 " JOIN clinicians c ON c.id = w.clinician_id JOIN users cu ON cu.id = c.user_id"
                                 " JOIN appointment_types t ON t.code = w.type_code WHERE w.patient_id = %s ORDER BY w.joined_at DESC",
                                 (u["id"],)).fetchall()
        items = "".join(f"<li data-clinician-id={r['clinician_id']} data-type='{E(r['type_code'])}' data-date='{r['on_date']}'"
                        f" data-entry-status='{E(r['status'])}'>{E(r['type_name'])} with {E(r['clinician_name'])} on {r['on_date']} · "
                        f"<span class='status {E(r['status'])}'>{E(r['status'])}</span></li>" for r in rows)
        types = self.conn.execute("SELECT code, name FROM appointment_types WHERE active ORDER BY name").fetchall()
        topts = "".join(f"<option value='{E(t['code'])}'>{E(t['name'])}</option>" for t in types)
        join = ("<div class=card><h2>Join a waitlist</h2><p class=muted>If a time opens up that day, we email you an offer that "
                "holds the slot for you for a while.</p>" + self.form_html("/waitlist",
                    f"<label>Clinician <select name=clinician_id>{self._clinician_options(None)}</select></label>"
                    f"<label>Type <select name=type>{topts}</select></label><label>Date <input type=date name=date required></label>",
                    "Join the waitlist", "join-waitlist") + "</div>")
        return self.render("My waitlist", "<div class=card>" + (f"<ul class=list>{items}</ul>" if items else
                           "<p>You are not on any waitlist yet.</p>") + "</div>" + join)

    def waitlist_post(self) -> Resp:
        u = self.need_user(("patient",))
        cid, d = to_int(self.form.get("clinician_id")), parse_date(self.form.get("date"))
        if cid is None or d is None:
            raise Refused(400, "Choose a clinician and a date.")
        S.join_waitlist(u, cid, self.form.get("type", ""), d)
        return redirect("/waitlist")

    def offer_get(self, token: str) -> Resp:
        u = self.need_user()
        e = S.offer_by_token(self.conn, token)
        if e is None:
            return self.render("Not found", "<div class=card><p>No such offer.</p></div>", 404)
        if e["patient_id"] != u["id"]:
            return self.forbidden()
        a = S.load_appt(self.conn, e["offer_appointment_id"])
        if a["status"] == "offered":  # accepting creates the hold, which fixes the deposit at today's fees (R3.4)
            fee = avail.fee_on(self.conn, a["type_id"], a["local_date"])
            if fee:
                a["deposit_cents"] = S.half_up(fee["fee_cents"] * fee["deposit_bp"], 10000)
        state = {"offered": "offered", "booked": "accepted", "declined": "declined", "expired": "expired"}.get(e["status"], e["status"])
        if state == "offered" and a["expires_at"] <= now_utc():
            state = "expired"
        tz = a["tz"]
        body = (f"<div class=card data-offer-status='{state}' data-start='{E(a['start'].astimezone(tz).isoformat())}'"
                f" data-clinician-id={a['clinician_id']} data-type='{E(a['code'])}'><dl>"
                f"<dt>When</dt><dd>{E(local_time(a['start'], tz))}</dd><dt>Clinic</dt><dd>{E(a['clinic_name'])}</dd>"
                f"<dt>Clinician</dt><dd>{E(a['clinician_name'])}</dd><dt>Type</dt><dd>{E(a['type_name'])}</dd>"
                f"<dt>Deposit</dt><dd>{dollars(a['deposit_cents'])}</dd></dl>")
        if state == "offered":
            exp = a["expires_at"].astimezone(tz)
            body += (f"<p>This slot is reserved for you until <strong>{exp.strftime('%Y-%m-%d %H:%M:%S')} {exp.tzname()}</strong>. "
                     f"Accepting holds it for {max(1, db.HOLD_TTL // 60)} more minute(s) and takes you to pay the "
                     f"{dollars(a['deposit_cents'])} deposit; the booking is confirmed once the payment is.</p><div class=row>"
                     + self.form_html(f"/offers/{token}/accept", "", "Accept and pay deposit", "accept-offer")
                     + self.form_html(f"/offers/{token}/decline", "", "Decline", "decline-offer", "secondary") + "</div>")
        else:
            body += f"<p>This offer is {state}.</p>" + (f"<p><a href=/appointments/{a['id']}>See the appointment</a>.</p>" if state == "accepted" else "")
        return self.render("A slot opened up", body + "</div>")

    def offer_accept_post(self, token: str) -> Resp:
        return redirect(S.accept_offer(self.user, token))

    def offer_decline_post(self, token: str) -> Resp:
        S.decline_offer(self.user, token)
        return redirect(f"/offers/{token}")

    # -- day sheets -------------------------------------------------------------------------------------------
    def _sheet(self, rows: list[dict], show_clinician: bool) -> str:
        items = []
        for a in rows:
            items.append(f"<li data-appointment-id={a['id']} data-start='{E(a['start'].astimezone(a['tz']).isoformat())}'"
                         f" data-status='{E(a['status'])}' data-clinician-id={a['clinician_id']}>"
                         f"<strong>{E(a['start'].astimezone(a['tz']).strftime('%H:%M %Z'))}</strong> · {E(a['patient_name'])} · "
                         f"{E(a['type_name'])}" + (f" · {E(a['clinician_name'])}" if show_clinician else "")
                         + f" · {E(a['room_name'] or 'no room')} · <a href=/appointments/{a['id']}>open</a></li>")
        return f"<ul class=list>{''.join(items)}</ul>" if items else "<p>No appointments.</p>"

    def clinician_day_get(self) -> Resp:
        self.need_user(("clinician",))
        if not self.clin:
            return self.forbidden()
        tz = tzinfo(self.conn.execute("SELECT timezone FROM clinics WHERE id = %s", (self.clin["clinic_id"],)).fetchone()["timezone"])
        d = parse_date(self.query.get("date")) or avail.local_today(tz, now_utc())
        rows = S.day_sheet(self.conn, self.clin["clinic_id"], d, self.clin["id"])
        body = ("<div class=card><form method=get action=/clinician/day class=row>"
                f"<label>Date <input type=date name=date value='{d.isoformat()}'></label><button class=secondary>Show</button></form>"
                f"<p>{E(d.strftime('%A %Y-%m-%d'))} · local clinic time</p>{self._sheet(rows, False)}</div>")
        return self.render("My day", body)

    def desk_day_get(self) -> Resp:
        self.need_user(DESK)
        allowed = self.staff_clinics()
        clinics = [c for c in self.conn.execute("SELECT * FROM clinics ORDER BY id").fetchall() if c["id"] in allowed]
        cid = to_int(self.query.get("clinic_id")) or clinics[0]["id"]
        self.need_clinic(cid)
        clinic = next(c for c in clinics if c["id"] == cid)
        tz = tzinfo(clinic["timezone"])
        d = parse_date(self.query.get("date")) or avail.local_today(tz, now_utc())
        opts = "".join(f"<option value={c['id']}{' selected' if c['id'] == clinic['id'] else ''}>{E(c['name'])}</option>" for c in clinics)
        rows = S.day_sheet(self.conn, clinic["id"], d)
        body = ("<div class=card><form method=get action=/desk/day class=row>"
                f"<label>Clinic <select name=clinic_id>{opts}</select></label>"
                f"<label>Date <input type=date name=date value='{d.isoformat()}'></label><button class=secondary>Show</button></form>"
                f"<p>{E(clinic['name'])} · {E(d.strftime('%A %Y-%m-%d'))}</p>{self._sheet(rows, True)}</div>")
        return self.render("Day sheet", body)

    def reminders_get(self, msg: str = "", sent: int | None = None) -> Resp:
        self.need_user(DESK)
        allowed = self.staff_clinics()
        clinics = [c for c in self.conn.execute("SELECT * FROM clinics ORDER BY id").fetchall() if c["id"] in allowed]
        opts = "".join(f"<option value={c['id']}>{E(c['name'])}</option>" for c in clinics)
        res = f"<div class=card data-reminders-sent={sent}><p>{msg}</p></div>" if sent is not None else ""
        body = res + "<div class=card><p>Sends one reminder email per booked appointment that has not had one.</p>" + self.form_html(
            "/desk/reminders", f"<label>Clinic <select name=clinic_id>{opts}</select></label><label>Date <input type=date name=date required></label>",
            "Send reminders", "send-reminders") + "</div>"
        return self.render("Reminders", body)

    def reminders_post(self) -> Resp:
        self.need_user(DESK)
        cid, d = to_int(self.form.get("clinic_id")), parse_date(self.form.get("date"))
        if cid is None or d is None or not self.conn.execute("SELECT 1 FROM clinics WHERE id = %s", (cid,)).fetchone():
            raise Refused(400, "Choose a clinic and a date.")
        self.need_clinic(cid)
        n = S.send_reminders(cid, d)
        return self.reminders_get(f"Sent {n} reminder(s) for {d.isoformat()}.", n)

    # -- front desk booking, export -----------------------------------------------------------------------------
    def desk_book_get(self, msg: str = "", status: int = 200) -> Resp:
        self.need_user(DESK)
        types = self.conn.execute("SELECT code, name FROM appointment_types WHERE active ORDER BY name").fetchall()
        topts = "".join(f"<option value='{E(t['code'])}'>{E(t['name'])}</option>" for t in types)
        body = ("<div class=card><p>Hold a slot for a patient. They get an email with a link to pay the deposit; the booking is "
                "confirmed once they pay.</p>" + self.form_html("/desk/book",
                    "<label>Patient email <input name=patient_email type=email required></label>"
                    f"<label>Clinician <select name=clinician_id>{self._clinician_options(None, None, self.staff_clinics())}</select></label>"
                    f"<label>Type <select name=type>{topts}</select></label>"
                    "<label>Start (as shown on the booking page, e.g. 2026-12-08T09:15:00-08:00) <input name=start required></label>",
                    "Hold and email payment link", "desk-book") + "</div>")
        return self.render("Book for a patient", body, status, msg, err=status >= 400)

    def desk_book_post(self) -> Resp:
        self.need_user(DESK)
        cid = to_int(self.form.get("clinician_id"))
        if cid is None:
            raise Refused(400, "Choose a clinician.")
        c = self.conn.execute("SELECT clinic_id FROM clinicians WHERE id = %s", (cid,)).fetchone()
        if c is None:
            raise Refused(400, "Unknown clinician.")
        self.need_clinic(c["clinic_id"])
        aid, url = S.book_for_patient(self.form.get("patient_email", ""), cid, self.form.get("type", ""), self.form.get("start", ""))
        a = S.load_appt(self.conn, aid)
        exp = a["expires_at"].astimezone(a["tz"])
        return self.render("Slot held for the patient", f"<div class=card data-appointment-id={aid}><p>{E(a['type_name'])} with "
                           f"{E(a['clinician_name'])} on {E(local_time(a['start'], a['tz']))} is held for {E(a['patient_name'])} until "
                           f"{exp.strftime('%H:%M:%S')} {exp.tzname()}. We emailed them this payment link:</p>"
                           f"<p data-checkout-url='{E(url)}'><a href='{E(url)}'>{E(url)}</a></p></div>")

    def desk_export_get(self) -> Resp:
        self.need_user(DESK)
        cid, d0, d1 = to_int(self.query.get("clinic_id")), parse_date(self.query.get("from")), parse_date(self.query.get("to"))
        if cid is not None and self.conn.execute("SELECT 1 FROM clinics WHERE id = %s", (cid,)).fetchone():
            self.need_clinic(cid)
        if cid is None or d0 is None or d1 is None or d1 < d0 or (d1 - d0).days > 30:
            return Resp(400, "clinic_id, from and to (at most 31 days) are required", ctype="text/plain; charset=utf-8")
        if not self.conn.execute("SELECT 1 FROM clinics WHERE id = %s", (cid,)).fetchone():
            return Resp(404, "no such clinic", ctype="text/plain; charset=utf-8")
        import csv
        import io
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(S.EXPORT_HEADER)
        for row in S.export_rows(self.conn, cid, d0, d1):
            w.writerow(row)
        return Resp(200, buf.getvalue(), {"Content-Disposition": f"attachment; filename=appointments-{cid}-{d0}-{d1}.csv"},
                    ctype="text/csv; charset=utf-8")

    def _closure_access(self, cid: str):
        self.need_user(("manager", "admin"))
        c = self.conn.execute("SELECT * FROM clinics WHERE id = %s", (int(cid),)).fetchone()
        if c is None:
            raise self.render("Not found", "<div class=card><p>No such clinic.</p></div>", 404)
        self.need_clinic(c["id"])
        return c

    def admin_clinic_get(self, cid: str, msg: str = "", status: int = 200) -> Resp:
        c = self._closure_access(cid)
        rows = self.conn.execute("SELECT * FROM clinic_closures WHERE clinic_id = %s ORDER BY on_date", (c["id"],)).fetchall()
        items = "".join(f"<li data-closure-date='{r['on_date']}'>{r['on_date']}: {E(r['reason'])}</li>" for r in rows)
        body = (f"<div class=card><p>{E(c['name'])} · {E(c['timezone'])}</p><h2>Closed days</h2><ul>{items or '<li>none</li>'}</ul>"
                "<p class=muted>On a closed day nobody at this clinic can be booked. Existing appointments are not changed.</p></div>"
                "<div class=card>" + self.form_html(f"/admin/clinics/{c['id']}", "<label>Date <input type=date name=on_date required></label>"
                                                    "<label>Reason <input name=reason required></label>", "Close the clinic that day", "add-closure") + "</div>")
        return self.render("Clinic settings", body, status, msg, err=status >= 400)

    def admin_clinic_post(self, cid: str) -> Resp:
        c = self._closure_access(cid)
        d, why = parse_date(self.form.get("on_date")), self.form.get("reason", "").strip(" ")
        if d is None or bad_name(why):
            return self.admin_clinic_get(cid, "Give a date and a one-line reason.", 400)
        try:
            self.conn.execute("INSERT INTO clinic_closures (clinic_id, on_date, reason) VALUES (%s, %s, %s)", (c["id"], d, why))
            self.conn.commit()
        except psycopg.errors.UniqueViolation:
            self.conn.rollback()
            return self.admin_clinic_get(cid, "The clinic is already closed that day.", 409)
        return self.admin_clinic_get(cid, "Closure added.")

    # -- schedules -------------------------------------------------------------------------------------------------
    def _schedule_access(self, cid: int):
        self.need_user()
        c = self.conn.execute("SELECT c.*, u.full_name, cl.name AS clinic_name FROM clinicians c JOIN users u ON u.id = c.user_id"
                              " JOIN clinics cl ON cl.id = c.clinic_id WHERE c.id = %s", (cid,)).fetchone()
        if c is None:
            raise self.render("Not found", "<div class=card><p>No such clinician.</p></div>", 404)
        mgr = self.user["role"] == "manager" and self.user.get("clinic_id") == c["clinic_id"]
        if not (self.user["role"] == "admin" or mgr or (self.clin and self.clin["id"] == cid)):
            raise self.forbidden()
        return c

    def schedule_get(self, cid: str, msg: str = "", status: int = 200) -> Resp:
        c = self._schedule_access(int(cid))
        vers = self.conn.execute("SELECT * FROM schedule_versions WHERE clinician_id = %s ORDER BY effective_from DESC, id DESC LIMIT 30",
                                 (c["id"],)).fetchall()
        vitems = []
        for v in vers:
            ws = self.conn.execute("SELECT weekday, start_time::text AS s, end_time::text AS e FROM schedule_windows WHERE version_id = %s"
                                   " ORDER BY weekday, start_time", (v["id"],)).fetchall()
            txt = ", ".join(f"{DAYS[w['weekday'] - 1]} {w['s'][:5]}-{w['e'][:5]}" for w in ws) or "no working hours"
            vitems.append(f"<li>From {v['effective_from']}: {E(txt)}</li>")
        exc = self.conn.execute("SELECT on_date, kind, start_time::text AS s, end_time::text AS e FROM schedule_exceptions"
                                " WHERE clinician_id = %s ORDER BY on_date DESC LIMIT 40", (c["id"],)).fetchall()
        eitems = "".join(f"<li>{x['on_date']}: {E(x['kind'])} {E((x['s'] or '')[:5])}{'-' if x['s'] else 'all day'}{E((x['e'] or '')[:5])}</li>"
                         for x in exc)
        body = (f"<div class=card><p>{E(c['full_name'])} · {E(c['clinic_name'])} · up to {c['max_per_day']} appointments a day</p>"
                f"<h2>Weekly hours</h2><ul>{''.join(vitems) or '<li>none</li>'}</ul><h2>Exceptions</h2><ul>{eitems or '<li>none</li>'}</ul></div>"
                "<div class=card><h2>New weekly hours</h2>" + self.form_html(
                    f"/clinicians/{c['id']}/schedule/versions",
                    "<label>Effective from <input type=date name=effective_from required></label>"
                    "<label>Windows, one per line, like <code>TUE 09:10-12:00</code> <textarea name=windows></textarea></label>",
                    "Add weekly hours", "add-version") + "</div>"
                "<div class=card><h2>One-day change</h2>" + self.form_html(
                    f"/clinicians/{c['id']}/schedule/exceptions",
                    "<label>Date <input type=date name=on_date required></label>"
                    "<label>Kind <select name=kind><option value=off>off</option><option value=extra>extra hours</option></select></label>"
                    "<label>From (optional for a whole day off) <input name=start_time placeholder=HH:MM></label>"
                    "<label>To <input name=end_time placeholder=HH:MM></label>", "Add exception", "add-exception") + "</div>")
        return self.render("Working schedule", body, status, msg, err=status >= 400)

    def versions_post(self, cid: str) -> Resp:
        c = self._schedule_access(int(cid))
        eff = parse_date(self.form.get("effective_from"))
        if eff is None:
            return self.schedule_get(cid, "Enter an effective date.", 400)
        wins = []
        for line in self.form.get("windows", "").splitlines():
            line = line.strip()
            if not line:
                continue
            m = WIN_RE.match(line)
            s = hm_ok(f"{m.group(2)}:{m.group(3)}") if m else None
            e = hm_ok(f"{m.group(4)}:{m.group(5)}") if m else None
            if not m or s is None or e is None or e <= s or s >= 1440:
                return self.schedule_get(cid, f"Could not use the line “{E(line)}”: write DAY HH:MM-HH:MM with the end after the start.", 400)
            wins.append((DAYS.index(m.group(1)) + 1, s, e))
        vid = self.conn.execute("INSERT INTO schedule_versions (clinician_id, effective_from) VALUES (%s, %s) RETURNING id",
                                (c["id"], eff)).fetchone()["id"]
        for wd, s, e in wins:
            self.conn.execute("INSERT INTO schedule_windows (version_id, weekday, start_time, end_time) VALUES (%s, %s, %s, %s)",
                              (vid, wd, f"{s // 60:02d}:{s % 60:02d}", "24:00" if e == 1440 else f"{e // 60:02d}:{e % 60:02d}"))
        self.conn.commit()
        return self.schedule_get(cid, "Weekly hours added.")

    def exceptions_post(self, cid: str) -> Resp:
        c = self._schedule_access(int(cid))
        d, kind = parse_date(self.form.get("on_date")), self.form.get("kind", "")
        st, en = self.form.get("start_time", "").strip(), self.form.get("end_time", "").strip()
        s, e = (hm_ok(st) if st else None), (hm_ok(en) if en else None)
        bad = (d is None or kind not in ("off", "extra") or (st and s is None) or (en and e is None)
               or (kind == "extra" and (s is None or e is None)) or ((s is None) != (e is None)) or (s is not None and e <= s))
        if bad:
            return self.schedule_get(cid, "Give a date, a kind, and for extra hours (or a partial day off) a start and an end after it.", 400)
        self.conn.execute("INSERT INTO schedule_exceptions (clinician_id, on_date, kind, start_time, end_time) VALUES (%s,%s,%s,%s,%s)",
                          (c["id"], d, kind, None if s is None else f"{s // 60:02d}:{s % 60:02d}",
                           None if e is None else ("24:00" if e == 1440 else f"{e // 60:02d}:{e % 60:02d}")))
        self.conn.commit()
        return self.schedule_get(cid, "Exception added.")

    # -- admin ---------------------------------------------------------------------------------------------------------
    def _clinic_options(self) -> str:
        return "".join(f"<option value={c['id']}>{E(c['name'])}</option>" for c in self.conn.execute("SELECT * FROM clinics ORDER BY id"))

    def admin_clinician_get(self, msg: str = "", status: int = 200) -> Resp:
        self.need_user(("admin",))
        body = "<div class=card>" + self.form_html("/admin/clinicians/new",
            "<label>Email <input name=email type=email required></label><label>Full name <input name=full_name required></label>"
            f"<label>Clinic <select name=clinic_id>{self._clinic_options()}</select></label>"
            "<label>Appointments per day at most <input name=max_per_day type=number min=1 value=8 required></label>",
            "Create clinician", "create-clinician") + "</div>"
        return self.render("New clinician", body, status, msg, err=status >= 400)

    def admin_clinician_post(self) -> Resp:
        self.need_user(("admin",))
        email, name = self.form.get("email", ""), self.form.get("full_name", "").strip(" ")
        clinic, mx = to_int(self.form.get("clinic_id")), to_int(self.form.get("max_per_day"))
        if "@" not in email or bad_name(name) or mx is None or mx < 1 or not self.conn.execute("SELECT 1 FROM clinics WHERE id = %s", (clinic,)).fetchone():
            return self.admin_clinician_get("Fill in every field.", 400)
        if S.any_user_with(self.conn, email):
            return self.admin_clinician_get("That address is already on file.", 409)
        uid = self.conn.execute("INSERT INTO users (email, full_name, role, active) VALUES (%s, %s, 'clinician', true) RETURNING id",
                                (norm_email(email), name)).fetchone()["id"]
        cid = self.conn.execute("INSERT INTO clinicians (user_id, clinic_id, max_per_day, active) VALUES (%s, %s, %s, true) RETURNING id",
                                (uid, clinic, mx)).fetchone()["id"]
        self.conn.commit()
        return self.render("Clinician created", f"<div class=card data-clinician-id={cid} data-user-id={uid}><p>{E(name)} was added. "
                           f"They set a password from the sign-in page. <a href=/clinicians/{cid}/schedule>Set their working hours</a>.</p></div>")

    def admin_room_get(self, msg: str = "", status: int = 200) -> Resp:
        self.need_user(("admin",))
        body = "<div class=card>" + self.form_html("/admin/rooms/new",
            f"<label>Clinic <select name=clinic_id>{self._clinic_options()}</select></label><label>Name <input name=name required></label>"
            "<label>Room type <input name=room_type required placeholder='treatment, gym, hydro, …'></label>", "Create room", "create-room") + "</div>"
        return self.render("New room", body, status, msg, err=status >= 400)

    def admin_room_post(self) -> Resp:
        self.need_user(("admin",))
        clinic, name, rt = to_int(self.form.get("clinic_id")), self.form.get("name", "").strip(" "), self.form.get("room_type", "").strip()
        if bad_name(name) or not rt or not self.conn.execute("SELECT 1 FROM clinics WHERE id = %s", (clinic,)).fetchone():
            return self.admin_room_get("Fill in every field.", 400)
        rid = self.conn.execute("INSERT INTO rooms (clinic_id, name, room_type, active) VALUES (%s, %s, %s, true) RETURNING id",
                                (clinic, name, rt)).fetchone()["id"]
        self.conn.commit()
        return self.render("Room created", f"<div class=card data-room-id={rid}><p>{E(name)} ({E(rt)}) was added.</p></div>")

    def admin_type_get(self, msg: str = "", status: int = 200) -> Resp:
        self.need_user(("admin",))
        body = "<div class=card>" + self.form_html("/admin/types/new",
            "<label>Code <input name=code required></label><label>Name <input name=name required></label>"
            "<label>Duration (minutes) <input name=duration_minutes type=number min=5 required></label>"
            "<label>Buffer after (minutes) <input name=buffer_minutes type=number min=0 value=0 required></label>"
            "<label>Room type <input name=room_type required></label><label>Fee (cents) <input name=fee_cents type=number min=1 required></label>"
            "<label>Deposit rate (basis points, 2000 = 20%) <input name=deposit_bp type=number min=1 max=10000 required></label>"
            "<label>Fee effective from <input type=date name=effective_from required></label>", "Create type", "create-type") + "</div>"
        return self.render("New appointment type", body, status, msg, err=status >= 400)

    def admin_type_post(self) -> Resp:
        self.need_user(("admin",))
        f = self.form
        code, name, rt = f.get("code", "").strip(), f.get("name", "").strip(" "), f.get("room_type", "").strip()
        dur, buf, fee, bp, eff = (to_int(f.get("duration_minutes")), to_int(f.get("buffer_minutes")), to_int(f.get("fee_cents")),
                                  to_int(f.get("deposit_bp")), parse_date(f.get("effective_from")))
        if not code or bad_name(name) or not rt or not dur or dur < 1 or buf is None or buf < 0 or not fee or fee < 1 or not bp or not (1 <= bp <= 10000) or not eff:
            return self.admin_type_get("Fill in every field with valid values.", 400)
        if self.conn.execute("SELECT 1 FROM appointment_types WHERE code = %s", (code,)).fetchone():
            return self.admin_type_get("That code already exists.", 409)
        tid = self.conn.execute("INSERT INTO appointment_types (code, name, duration_minutes, buffer_minutes, room_type, active)"
                                " VALUES (%s,%s,%s,%s,%s,true) RETURNING id", (code, name, dur, buf, rt)).fetchone()["id"]
        self.conn.execute("INSERT INTO type_fees (type_id, effective_from, fee_cents, deposit_bp) VALUES (%s,%s,%s,%s)", (tid, eff, fee, bp))
        self.conn.commit()
        return self.render("Type created", f"<div class=card data-type-code='{E(code)}' data-type-id={tid}><p>{E(name)} was added. "
                           f"<a href='/admin/types/{urllib.parse.quote(code)}'>See its fees</a>.</p></div>")

    def admin_type_page_get(self, code: str, msg: str = "", status: int = 200) -> Resp:
        self.need_user(("admin",))
        code = urllib.parse.unquote(code)
        t = self.conn.execute("SELECT * FROM appointment_types WHERE code = %s", (code,)).fetchone()
        if t is None:
            return self.render("Not found", "<div class=card><p>No such type.</p></div>", 404)
        fees = self.conn.execute("SELECT * FROM type_fees WHERE type_id = %s ORDER BY effective_from", (t["id"],)).fetchall()
        items = "".join(f"<li>From {r['effective_from']}: fee {dollars(r['fee_cents'])}, deposit rate {r['deposit_bp'] / 100:.2f}%</li>" for r in fees)
        body = (f"<div class=card data-type-code='{E(code)}'><p>{E(t['name'])} · {t['duration_minutes']} min + {t['buffer_minutes']} min buffer · "
                f"room type {E(t['room_type'])}</p><ul>{items}</ul></div><div class=card>" + self.form_html(
                    f"/admin/types/{urllib.parse.quote(code)}",
                    "<label>Effective from <input type=date name=effective_from required></label>"
                    "<label>Fee (cents) <input name=fee_cents type=number min=1 required></label>"
                    "<label>Deposit rate (basis points) <input name=deposit_bp type=number min=1 max=10000 required></label>",
                    "Add fee", "add-fee") + "</div>")
        return self.render(f"Type {code}", body, status, msg, err=status >= 400)

    def admin_type_page_post(self, code: str) -> Resp:
        self.need_user(("admin",))
        code = urllib.parse.unquote(code)
        t = self.conn.execute("SELECT * FROM appointment_types WHERE code = %s", (code,)).fetchone()
        if t is None:
            return self.render("Not found", "<div class=card><p>No such type.</p></div>", 404)
        fee, bp, eff = to_int(self.form.get("fee_cents")), to_int(self.form.get("deposit_bp")), parse_date(self.form.get("effective_from"))
        if not fee or fee < 1 or not bp or not (1 <= bp <= 10000) or eff is None:
            return self.admin_type_page_get(code, "Fill in every field with valid values.", 400)
        if self.conn.execute("SELECT 1 FROM type_fees WHERE type_id = %s AND effective_from = %s", (t["id"], eff)).fetchone():
            return self.admin_type_page_get(code, "A fee for that date already exists.", 409)
        self.conn.execute("INSERT INTO type_fees (type_id, effective_from, fee_cents, deposit_bp) VALUES (%s,%s,%s,%s)", (t["id"], eff, fee, bp))
        self.conn.commit()
        return self.admin_type_page_get(code, "Fee added.")


ROUTES = [
    (r"/", "home"),
    (r"/api/availability", "availability"),
    (r"/register", "register"),
    (r"/signin", "signin"),
    (r"/signout", "signout"),
    (r"/password-reset", "reset"),
    (r"/password-reset/([A-Za-z0-9_-]+)", "reset_token"),
    (r"/profile", "profile"),
    (r"/book", "book"),
    (r"/appointments", "appointments"),
    (r"/appointments/(\d+)", "appointment"),
    (r"/appointments/(\d+)/release", "release"),
    (r"/appointments/(\d+)/cancel", "cancel"),
    (r"/appointments/(\d+)/reschedule", "reschedule"),
    (r"/appointments/(\d+)/notes", "notes"),
    (r"/waitlist", "waitlist"),
    (r"/offers/([A-Za-z0-9_-]+)", "offer"),
    (r"/offers/([A-Za-z0-9_-]+)/accept", "offer_accept"),
    (r"/offers/([A-Za-z0-9_-]+)/decline", "offer_decline"),
    (r"/clinician/day", "clinician_day"),
    (r"/clinicians/(\d+)/schedule", "schedule"),
    (r"/clinicians/(\d+)/schedule/versions", "versions"),
    (r"/clinicians/(\d+)/schedule/exceptions", "exceptions"),
    (r"/desk/day", "desk_day"),
    (r"/desk/reminders", "reminders"),
    (r"/admin/clinicians/new", "admin_clinician"),
    (r"/admin/clinics/(\d+)", "admin_clinic"),
    (r"/desk/book", "desk_book"),
    (r"/desk/export\.csv", "desk_export"),
    (r"/admin/rooms/new", "admin_room"),
    (r"/admin/types/new", "admin_type"),
    (r"/admin/types/([^/]+)", "admin_type_page"),
]


def main() -> None:
    db.apply_schema()
    S.start_workers()
    srv = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler)
    srv.daemon_threads = True
    srv.serve_forever()
