"""Shiftbook HTTP layer: stdlib ThreadingHTTPServer, server-side sessions, CSRF, HTML pages."""

from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import secrets
import time
import traceback
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

from . import core
from .core import connect, dollars, hours, norm_email, sha

ENV = os.environ
COOKIE = "sb_session"
E = html.escape

CSS = """
*{box-sizing:border-box}body{margin:0;font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1c1f24;background:#f5f6f8}
header{background:#1f3b57;color:#fff;padding:12px 16px;display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between}
header a{color:#fff;margin-right:12px}main{max-width:1100px;margin:0 auto;padding:16px}
.card{background:#fff;border:1px solid #dde1e6;border-radius:8px;padding:16px;margin:0 0 16px;overflow-wrap:anywhere}
.flash{padding:12px 16px;border-radius:8px;margin:0 0 16px;background:#e7f1ff;border:1px solid #9cc3ff}
.flash.err{background:#fdecec;border-color:#f3a9a9}button{font:inherit;padding:8px 14px;border-radius:6px;border:1px solid #1f3b57;background:#1f3b57;color:#fff;cursor:pointer}
button.secondary{background:#fff;color:#1f3b57}input,select{font:inherit;padding:8px;border:1px solid #b9c0c8;border-radius:6px;width:100%;max-width:420px}
label{display:block;margin:8px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px}
.day{border:1px solid #dde1e6;border-radius:6px;padding:8px;background:#fafbfc}.muted{color:#5b6570}
.status{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600}.status.approved{background:#dff5e3}.status.pending{background:#fff4d6}.status.exception{background:#fde2e2}
.tablewrap{overflow-x:auto}table{border-collapse:collapse;width:100%}td,th{padding:6px 8px;border-bottom:1px solid #e5e8eb;text-align:left;white-space:nowrap}
"""


def page(title: str, body: str, user: dict | None = None, csrf: str | None = None, flash: str = "", err: bool = False) -> str:
    nav = ""
    if user:
        links = ['<a href="/timesheet">My timesheet</a>', '<a href="/profile">Profile</a>']
        if user["role"] in ("manager", "payroll_admin"):
            links.append('<a href="/approvals">Approvals</a>')
        if user["role"] == "payroll_admin":
            links += ['<a href="/admin/employees/new">Add employee</a>', '<a href="/admin/devices">Time clocks</a>',
                      '<a href="/payroll">Payroll</a>']
        nav = (f"<nav>{''.join(links)}</nav><form method=post action=/signout style='margin:0'>{csrf_input(csrf)}"
               f"<span class=muted style='color:#cfe0f0;margin-right:8px'>{E(user['full_name'])}</span>"
               "<button class=secondary data-action=signout>Sign out</button></form>")
    fl = f"<div class='flash{' err' if err else ''}' role=status>{flash}</div>" if flash else ""
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{E(title)} · Shiftbook</title><style>{CSS}</style></head><body><header><strong>Shiftbook</strong>{nav}</header>"
            f"<main><h1>{E(title)}</h1>{fl}{body}</main></body></html>")


def csrf_input(token: str | None) -> str:
    return f"<input type=hidden name=csrf_token data-csrf value='{E(token or '')}'>" if token else ""


class Resp(Exception):
    def __init__(self, status: int, body: str = "", headers: dict | None = None, ctype: str = "text/html; charset=utf-8"):
        self.status, self.body, self.headers, self.ctype = status, body, headers or {}, ctype


def redirect(to: str, extra: dict | None = None) -> Resp:
    return Resp(303, "", {"Location": to, **(extra or {})})


def safe_next(nxt: str) -> str:
    """R2.8: only same-site paths: a single leading '/', not followed by '/' or '\\', no scheme or host."""
    if (not nxt or not nxt.startswith("/") or nxt[1:2] in ("/", "\\") or "\\" in nxt
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in nxt)):
        return "/timesheet"
    return nxt


def valid_name(name: str) -> bool:
    return 1 <= len(name) <= 100 and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name)


class Handler(BaseHTTPRequestHandler):
    server_version = "Shiftbook"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    # -- plumbing --------------------------------------------------------------------------------
    def _send(self, r: Resp) -> None:
        body = r.body.encode() if isinstance(r.body, str) else r.body
        self.send_response(r.status)
        self.send_header("Content-Type", r.ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in r.headers.items():
            if isinstance(v, list):
                for item in v:
                    self.send_header(k, item)
            else:
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _form(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        self._raw = raw
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("application/x-www-form-urlencoded") or not ctype:
            return {k: v[-1] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}
        return {}

    def _session(self, conn):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        if COOKIE not in c:
            return None, None
        row = conn.execute(
            "SELECT s.csrf, s.employee_id FROM sb_sessions s WHERE s.sid_hash = %s AND NOT s.revoked", (sha(c[COOKIE].value),)
        ).fetchone()
        if not row:
            return None, None
        emp = core.employee(conn, row["employee_id"])
        if not emp or not emp["active"]:
            return None, None
        return emp, row["csrf"]

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
        try:
            if path == "/healthz":
                raise self.healthz()
            if path == "/api/punches" and method == "POST":
                raise self.api_punches()
            self.form = self._form() if method == "POST" else {}
            with connect() as conn:
                self.conn = conn
                self.user, self.csrf = self._session(conn)
                for pattern, handler in ROUTES:
                    m = re.fullmatch(pattern, path)
                    if m:
                        fn = getattr(self, f"{handler}_{method.lower()}", None)
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

    # -- guards ----------------------------------------------------------------------------------
    def need_user(self, roles: tuple[str, ...] | None = None) -> dict:
        if not self.user:
            raise redirect("/signin?next=" + urllib.parse.quote(self.path, safe=""))
        if roles and self.user["role"] not in roles:
            raise Resp(403, page("Not allowed", "<p>You are not allowed to do that.</p>", self.user, self.csrf))
        return self.user

    def check_csrf(self) -> None:
        sent = self.form.get("csrf_token", "")
        if not self.csrf or not secrets.compare_digest(sent, self.csrf):
            raise Resp(403, page("Not allowed", "<p>This form expired or came from another site. Reload the page and try again.</p>",
                                 self.user, self.csrf))

    def render(self, title: str, body: str, status: int = 200, flash: str = "", err: bool = False) -> Resp:
        return Resp(status, page(title, body, self.user, self.csrf, flash, err))

    # -- health ----------------------------------------------------------------------------------
    def healthz(self) -> Resp:
        try:
            with psycopg.connect(core.DATABASE_URL, connect_timeout=3) as c:
                c.execute("SET statement_timeout = 3000")
                c.execute("SELECT 1")
            return Resp(200, "ok", ctype="text/plain")
        except Exception:
            return Resp(503, "database unreachable", ctype="text/plain")

    # -- sign in ---------------------------------------------------------------------------------
    def signin_form(self, email: str = "", flash: str = "", status: int = 200) -> Resp:
        nxt = self.form.get("next") or self.query.get("next", "")
        body = ("<div class=card><form method=post action=/signin>"
                f"<input type=hidden name=next value='{E(nxt)}'>"
                f"<label>Email <input name=email type=email autocomplete=username value='{E(email)}' required></label>"
                "<label>Password <input name=password type=password autocomplete=current-password required></label>"
                "<button data-action=signin>Sign in</button></form>"
                "<p class=muted>First time here or forgot your password? <a href=/password-reset>Set a password</a>.</p></div>")
        return self.render("Sign in", body, status, flash, err=bool(flash))

    def signin_get(self):
        if self.user:
            return redirect("/timesheet")
        return self.signin_form()

    def signin_post(self):
        email, pw = norm_email(self.form.get("email", "")), self.form.get("password", "")
        conn = self.conn
        emp = conn.execute("SELECT * FROM employees WHERE lower(btrim(email)) = %s ORDER BY id LIMIT 1", (email,)).fetchone()
        bad = "That email and password don't match an active account. Check them and try again."
        if not emp:
            core.check_password(pw, core.hash_password("x"))  # similar timing
            return self.signin_form(email, bad, 401)
        conn.execute("SELECT pg_advisory_xact_lock(3, %s)", (emp["id"],))
        stt = conn.execute("SELECT * FROM sb_login_state WHERE employee_id = %s", (emp["id"],)).fetchone()
        now = core.now_utc()
        if stt and stt["locked_until"] and stt["locked_until"] > now:
            conn.commit()
            return self.signin_form(email, "This account is temporarily locked after too many failed sign-ins. Try again later.", 423)
        cred = conn.execute("SELECT password_hash FROM sb_credentials WHERE employee_id = %s", (emp["id"],)).fetchone()
        ok = bool(emp["active"] and cred and core.check_password(pw, cred["password_hash"]))
        if not ok:
            failures = (stt["failures"] if stt else 0) + 1
            locked = None
            if failures >= 5:
                locked, failures = now + timedelta(seconds=int(ENV.get("LOGIN_LOCKOUT_SECONDS", "900"))), 0
            conn.execute(
                "INSERT INTO sb_login_state (employee_id, failures, locked_until) VALUES (%s,%s,%s) ON CONFLICT (employee_id)"
                " DO UPDATE SET failures = EXCLUDED.failures, locked_until = EXCLUDED.locked_until", (emp["id"], failures, locked))
            conn.commit()
            if locked:
                return self.signin_form(email, "This account is temporarily locked after too many failed sign-ins. Try again later.", 423)
            return self.signin_form(email, bad, 401)
        conn.execute("INSERT INTO sb_login_state (employee_id, failures, locked_until) VALUES (%s,0,NULL) ON CONFLICT (employee_id)"
                     " DO UPDATE SET failures = 0, locked_until = NULL", (emp["id"],))
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        conn.execute("INSERT INTO sb_sessions (sid_hash, employee_id, csrf) VALUES (%s,%s,%s)", (sha(sid), emp["id"], csrf))
        old = SimpleCookie(self.headers.get("Cookie", ""))
        if COOKIE in old:
            conn.execute("UPDATE sb_sessions SET revoked = true WHERE sid_hash = %s", (sha(old[COOKIE].value),))
        conn.commit()
        return redirect(safe_next(self.form.get("next") or self.query.get("next", "")),
                        {"Set-Cookie": f"{COOKIE}={sid}; Path=/; HttpOnly; SameSite=Lax"})

    def signout_post(self):
        if self.user:
            self.check_csrf()
            c = SimpleCookie(self.headers.get("Cookie", ""))
            self.conn.execute("UPDATE sb_sessions SET revoked = true WHERE sid_hash = %s", (sha(c[COOKIE].value),))
            self.conn.commit()
        return redirect("/signin", {"Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})

    def reset_get(self):
        body = ("<div class=card><form method=post action=/password-reset>"
                "<label>Work email <input name=email type=email required></label>"
                "<button data-action=request-reset>Email me a link</button></form>"
                "<p class=muted>We'll email a single-use link that lets you set a new password.</p></div>")
        return self.render("Set or reset your password", body)

    def reset_post(self):
        email = norm_email(self.form.get("email", ""))
        conn = self.conn
        emp = conn.execute("SELECT * FROM employees WHERE lower(btrim(email)) = %s AND active ORDER BY id LIMIT 1", (email,)).fetchone()
        if emp:
            conn.execute("SELECT pg_advisory_xact_lock(4, %s)", (emp["id"],))
            conn.execute("UPDATE sb_reset_tokens SET state = 'replaced' WHERE employee_id = %s AND state = 'open'", (emp["id"],))
            token = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sb_reset_tokens (token_hash, employee_id, created_at) VALUES (%s,%s,now())",
                         (sha(token), emp["id"]))
            link = ENV["PUBLIC_BASE_URL"].rstrip("/") + "/password-reset/" + token
            core.send_now(norm_email(emp["email"]), "Set your Shiftbook password",
                              f"Hello {emp['full_name']},\n\nUse this single-use link to set your Shiftbook password:\n{link}\n\n"
                              f"It expires in {int(ENV.get('RESET_LINK_TTL_SECONDS', '3600')) // 60} minutes. "
                              "If you did not ask for this, ignore this email.\n", emp["full_name"])
            conn.commit()
        msg = "If that address belongs to an active employee, a link to set the password is on its way. Check your inbox."
        return self.render("Check your email", f"<div class=card><p>{msg}</p><p><a href=/signin>Back to sign in</a></p></div>")

    def _token(self, token: str):
        row = self.conn.execute("SELECT * FROM sb_reset_tokens WHERE token_hash = %s FOR UPDATE", (sha(token),)).fetchone()
        if not row:
            raise self.render("Link not found", "<p>That link does not exist.</p>", 404)
        ttl = int(ENV.get("RESET_LINK_TTL_SECONDS", "3600"))
        if row["state"] != "open" or row["created_at"] + timedelta(seconds=ttl) < core.now_utc():
            raise self.render("Link no longer valid", "<div class=card><p>This link is no longer valid: it was already used, "
                              "it expired, or a newer link was requested. <a href=/password-reset>Request a new link</a>.</p></div>", 410)
        return row

    def resetlink_get(self, token):
        self._token(token)
        body = (f"<div class=card><form method=post action='/password-reset/{E(token)}'>"
                "<label>New password (at least 10 characters) <input name=password type=password minlength=10 required autocomplete=new-password></label>"
                "<button data-action=set-password>Set password</button></form></div>")
        return self.render("Choose a password", body)

    def resetlink_post(self, token):
        row = self._token(token)
        pw = self.form.get("password", "")
        if len(pw) < 10:
            body = (f"<div class=card><form method=post action='/password-reset/{E(token)}'>"
                    "<label>New password (at least 10 characters) <input name=password type=password minlength=10 required></label>"
                    "<button data-action=set-password>Set password</button></form></div>")
            return self.render("Choose a password", body, 400, "Passwords need at least 10 characters.", True)
        self.conn.execute(
            "INSERT INTO sb_credentials (employee_id, password_hash) VALUES (%s,%s) ON CONFLICT (employee_id)"
            " DO UPDATE SET password_hash = EXCLUDED.password_hash, updated_at = now()",
            (row["employee_id"], core.hash_password(pw)))
        self.conn.execute("UPDATE sb_reset_tokens SET state = 'used' WHERE token_hash = %s", (row["token_hash"],))
        self.conn.execute("UPDATE sb_sessions SET revoked = true WHERE employee_id = %s", (row["employee_id"],))
        self.conn.commit()
        return self.render("Password set", "<div class=card><p>Your password is set. <a href=/signin>Sign in</a> with it now.</p></div>")

    # -- profile ---------------------------------------------------------------------------------
    def profile_get(self, flash: str = "", err: bool = False, status: int = 200):
        u = self.need_user()
        body = (f"<div class=card><form method=post action=/profile>{csrf_input(self.csrf)}"
                f"<label>Display name <input name=full_name value='{E(u['full_name'])}' maxlength=100 required></label>"
                "<button data-action=save-profile>Save</button></form>"
                f"<p class=muted>Email: {E(norm_email(u['email']))} · Role: {E(u['role'])}</p></div>")
        return self.render("Your profile", body, status, flash, err)

    def profile_post(self):
        self.need_user()
        self.check_csrf()
        name = self.form.get("full_name", "").strip()
        if not valid_name(name):
            return self.profile_get("Enter a display name of 1 to 100 characters without control characters.", True, 400)
        self.conn.execute("UPDATE employees SET full_name = %s WHERE id = %s", (name, self.user["id"]))
        self.conn.commit()
        self.user = core.employee(self.conn, self.user["id"])
        return self.profile_get("Your display name is saved.")

    # -- timesheets ------------------------------------------------------------------------------
    def _week_param(self, tz: str) -> date:
        w = self.query.get("week")
        try:
            d = date.fromisoformat(w) if w else core.local_today(tz)
        except ValueError:
            raise self.render("Bad week", "<p>Use week=YYYY-MM-DD.</p>", 400)
        return core.engine.week_start_of(d)

    def _timesheet(self, emp_id: int):
        u = self.need_user()
        emp = core.employee(self.conn, emp_id)
        if not emp:
            raise self.render("Not found", "<p>No such employee.</p>", 404)
        if not core.can_view(self.conn, u, emp_id):
            raise self.render("Not allowed", "<p>You can only see timesheets of people below you.</p>", 403)
        loc = core.locations(self.conn)[emp["location_id"]]
        ws = self._week_param(loc["timezone"])
        st = core.employee_states(self.conn, [emp_id])[emp_id]
        wp = st.weeks.get(ws)
        status = st.status(ws)
        days = []
        for i in range(7):
            d = ws + timedelta(days=i)
            r, o, t = (wp.per_day.get(d, [0, 0, 0]) if wp else [0, 0, 0])
            days.append(f"<div class=day data-day='{d.isoformat()}' data-reg-hours='{hours(r)}' data-ot-hours='{hours(o)}' "
                        f"data-dt-hours='{hours(t)}'><strong>{d.strftime('%a %b')} {d.day}</strong><br>Regular {hours(r)} h<br>"
                        f"Overtime {hours(o)} h<br>Double time {hours(t)} h</div>")
        exc = ""
        if status == "exception":
            exc = ("<div class='flash err'><strong>Punch exception.</strong> This week can't be approved or paid until the punches are fixed: "
                   + "; ".join(E(x) for x in st.exceptions[ws]) + ".</div>")
        approve = ""
        if status == "pending" and st.has_work(ws) and core.can_approve(self.conn, u, emp_id):
            approve = (f"<form method=post action='/employees/{emp_id}/weeks/{ws.isoformat()}/approve'>{csrf_input(self.csrf)}"
                       "<p class=muted>Approving makes this week payable in the next payroll run. Any later punch or rate change "
                       "returns it to pending.</p><button data-action=approve-week>Approve this week</button></form>")
        label = {"approved": "Approved", "pending": "Pending approval", "exception": "Has an exception"}[status]
        totals = (f"Regular {hours(wp.reg_units)} h · Overtime {hours(wp.ot_units)} h · Double time {hours(wp.dt_units)} h · "
                  f"Meal premiums {wp.meal_premiums}") if wp else "No worked time this week."
        prev, nxt = ws - timedelta(days=7), ws + timedelta(days=7)
        base = "/timesheet" if emp_id == u["id"] and self.path.startswith("/timesheet") else f"/employees/{emp_id}/timesheet"
        body = (f"<div class=card data-employee-id='{emp_id}' data-week-start='{ws.isoformat()}' data-status='{status}'>"
                f"<p><strong>{E(emp['full_name'])}</strong> · {E(loc['name'])} · week of {ws.isoformat()} "
                f"(times in {E(loc['timezone'])})</p><p><span class='status {status}'>{label}</span></p>{exc}"
                f"<div class=grid>{''.join(days)}</div><p>{totals}</p>{approve}"
                f"<p><a href='{base}?week={prev.isoformat()}'>← Previous week</a> · <a href='{base}?week={nxt.isoformat()}'>Next week →</a></p></div>")
        return self.render("Timesheet", body)

    def timesheet_get(self):
        return self._timesheet(self.need_user()["id"])

    def emptimesheet_get(self, emp_id):
        return self._timesheet(int(emp_id))

    def approve_post(self, emp_id, week):
        u = self.need_user()
        self.check_csrf()
        emp_id = int(emp_id)
        if not core.employee(self.conn, emp_id):
            return self.render("Not found", "<p>No such employee.</p>", 404)
        if not core.can_approve(self.conn, u, emp_id):
            return self.render("Not allowed", "<p>You can't approve this week.</p>", 403)
        try:
            ws = date.fromisoformat(week)
        except ValueError:
            return self.render("Bad week", "<p>Unknown week.</p>", 400)
        if core.engine.week_start_of(ws) != ws:
            return self.render("Bad week", "<p>A week starts on a Sunday.</p>", 400)
        status, msg = core.approve_week(self.conn, u, emp_id, ws)
        self.conn.commit()
        if status != 200:
            return self.render("Not approved", f"<div class=card><p>{E(msg)}</p><p><a href='/employees/{emp_id}/timesheet?week={ws}'>Back to the timesheet</a></p></div>", status, msg, True)
        return redirect(f"/employees/{emp_id}/timesheet?week={ws.isoformat()}")

    def approvals_get(self):
        u = self.need_user(("manager", "payroll_admin"))
        if u["role"] == "payroll_admin":
            ids = [r["id"] for r in self.conn.execute(
                "SELECT DISTINCT employee_id AS id FROM legacy_punches UNION SELECT DISTINCT employee_id FROM sb_punches")]
        else:
            ids = core.below_ids(self.conn, u["id"])
        ids = [i for i in ids if i != u["id"]]
        states = core.employee_states(self.conn, ids)
        rows = []
        for eid in sorted(states):
            st = states[eid]
            for ws in sorted(st.weeks):
                if st.status(ws) == "pending" and st.has_work(ws):
                    wp = st.weeks[ws]
                    rows.append(f"<tr data-employee-id='{eid}' data-week-start='{ws.isoformat()}'><td>{E(st.emp['full_name'])}</td>"
                                f"<td>{ws.isoformat()}</td><td>{hours(wp.total_units)} h</td>"
                                f"<td><a href='/employees/{eid}/timesheet?week={ws.isoformat()}'>Review</a></td></tr>")
        body = ("<div class=card><p class=muted>Weeks waiting for your approval. Approving makes a week payable in the next run.</p>"
                f"<div class=tablewrap><table><tr><th>Employee</th><th>Week</th><th>Hours</th><th></th></tr>{''.join(rows)}</table></div></div>")
        return self.render("Approvals", body)

    # -- admin -----------------------------------------------------------------------------------
    def newemp_get(self, flash: str = "", status: int = 200, f: dict | None = None):
        self.need_user(("payroll_admin",))
        f = f or {}
        locs = core.locations(self.conn)
        lopts = "".join(f"<option value={l['id']}{' selected' if str(l['id']) == f.get('location_id') else ''}>{E(l['name'])}</option>" for l in locs.values())
        ropts = "".join(f"<option{' selected' if r == f.get('role') else ''}>{r}</option>" for r in ("employee", "manager", "payroll_admin"))
        v = lambda k: E(f.get(k, ""))
        body = (f"<div class=card><form method=post action=/admin/employees/new>{csrf_input(self.csrf)}"
                f"<label>Email <input name=email value='{v('email')}' required></label><label>Full name <input name=full_name value='{v('full_name')}' required></label>"
                f"<label>Location <select name=location_id>{lopts}</select></label><label>Manager id (optional) <input name=manager_id value='{v('manager_id')}'></label>"
                f"<label>Role <select name=role>{ropts}</select></label><label>Job code <input name=job_code value='{v('job_code')}' required></label>"
                f"<label>Hourly rate (cents) <input name=rate_cents value='{v('rate_cents')}' required></label>"
                f"<label>Rate effective from <input name=effective_from type=date value='{v('effective_from')}' required></label>"
                "<button data-action=create-employee>Add employee</button></form></div>")
        return self.render("Add an employee", body, status, flash, bool(flash))

    def newemp_post(self):
        self.need_user(("payroll_admin",))
        self.check_csrf()
        f = self.form
        try:
            email = norm_email(f.get("email", ""))
            name = f.get("full_name", "").strip()
            loc = int(f.get("location_id", ""))
            mgr = int(f["manager_id"]) if f.get("manager_id", "").strip() else None
            role = f.get("role", "")
            job = f.get("job_code", "").strip()
            cents = int(f.get("rate_cents", ""))
            eff = date.fromisoformat(f.get("effective_from", ""))
            assert "@" in email and valid_name(name) and role in ("employee", "manager", "payroll_admin") and job and len(job) <= 32 and cents > 0
            assert loc in core.locations(self.conn)
            if mgr is not None:
                assert core.employee(self.conn, mgr)
        except (ValueError, AssertionError, KeyError):
            return self.newemp_get("Check the fields: every field except manager is required, the rate is a positive whole number of cents, and the manager must exist.", 400, f)
        self.conn.execute("SELECT pg_advisory_xact_lock(5, 0)")
        if self.conn.execute("SELECT 1 FROM employees WHERE lower(btrim(email)) = %s", (email,)).fetchone():
            return self.newemp_get("An employee with that email address already exists.", 409, f)
        eid = self.conn.execute(
            "INSERT INTO employees (email, full_name, location_id, manager_id, role, active, hired_on) VALUES (%s,%s,%s,%s,%s,true,%s) RETURNING id",
            (email, name, loc, mgr, role, core.local_today(core.locations(self.conn)[loc]["timezone"]))).fetchone()["id"]
        self.conn.execute("INSERT INTO job_rates (employee_id, job_code, rate_cents, effective_from) VALUES (%s,%s,%s,%s)", (eid, job, cents, eff))
        self.conn.commit()
        return redirect(f"/admin/employees/{eid}")

    def emp_get(self, emp_id, flash: str = "", err: bool = False, status: int = 200):
        self.need_user(("payroll_admin",))
        emp = core.employee(self.conn, int(emp_id))
        if not emp:
            return self.render("Not found", "<p>No such employee.</p>", 404)
        loc = core.locations(self.conn)[emp["location_id"]]
        rates = self.conn.execute("SELECT * FROM job_rates WHERE employee_id = %s ORDER BY job_code, effective_from", (emp["id"],)).fetchall()
        rrows = "".join(f"<tr><td>{E(r['job_code'])}</td><td>{dollars(r['rate_cents'])}/h</td><td>{r['effective_from']}</td></tr>" for r in rates)
        body = (f"<div class=card data-employee-id='{emp['id']}'><p><strong>{E(emp['full_name'])}</strong> · {E(norm_email(emp['email']))} · "
                f"{E(loc['name'])} · {E(emp['role'])} · manager {emp['manager_id'] or '—'}</p>"
                f"<p><a href='/employees/{emp['id']}/timesheet'>Timesheet</a></p>"
                f"<div class=tablewrap><table><tr><th>Job</th><th>Rate</th><th>Effective from</th></tr>{rrows}</table></div></div>"
                f"<div class=card><h2>Add a rate</h2><form method=post action='/admin/employees/{emp['id']}/rates'>{csrf_input(self.csrf)}"
                "<label>Job code <input name=job_code required></label><label>Hourly rate (cents) <input name=rate_cents required></label>"
                "<label>Effective from <input name=effective_from type=date required></label>"
                "<p class=muted>Work on or after this date is paid at the new rate. A change that affects an already approved week returns "
                "it to pending; already paid weeks are corrected by an adjustment in the next run.</p>"
                "<button data-action=add-rate>Add rate</button></form></div>")
        return self.render("Employee", body, status, flash, err)

    def rates_post(self, emp_id):
        self.need_user(("payroll_admin",))
        self.check_csrf()
        emp = core.employee(self.conn, int(emp_id))
        if not emp:
            return self.render("Not found", "<p>No such employee.</p>", 404)
        try:
            job = self.form.get("job_code", "").strip()
            cents = int(self.form.get("rate_cents", ""))
            eff = date.fromisoformat(self.form.get("effective_from", ""))
            assert job and len(job) <= 32 and cents > 0
        except (ValueError, AssertionError):
            return self.emp_get(emp_id, "Enter a job code, a positive whole number of cents, and a date.", True, 400)
        status, msg = core.add_rate(self.conn, emp["id"], job, cents, eff)
        self.conn.commit()
        return self.emp_get(emp_id, msg, status != 200, status)

    def devices_get(self, flash: str = "", status: int = 200, extra: str = ""):
        self.need_user(("payroll_admin",))
        locs = core.locations(self.conn)
        parts = []
        for d in self.conn.execute("SELECT * FROM sb_devices ORDER BY id"):
            revoke = ""
            if not d["revoked"]:
                revoke = (f"<form method=post action=/admin/devices/{d['id']}/revoke style=margin:0>{csrf_input(self.csrf)}"
                          "<button class=secondary data-action=revoke-device>Revoke</button></form>")
            parts.append(f"<tr><td>{d['id']}</td><td>{E(d['label'])}</td><td>{E(locs[d['location_id']]['name'])}</td>"
                         f"<td>{'revoked' if d['revoked'] else 'active'}</td><td>{revoke}</td></tr>")
        rows = "".join(parts)
        lopts = "".join(f"<option value={l['id']}>{E(l['name'])}</option>" for l in locs.values())
        body = (f"{extra}<div class=card><form method=post action=/admin/devices>{csrf_input(self.csrf)}"
                f"<label>Location <select name=location_id>{lopts}</select></label><label>Label <input name=label required maxlength=80></label>"
                "<button data-action=create-device>Create time clock</button></form></div>"
                f"<div class=card><div class=tablewrap><table><tr><th>Id</th><th>Label</th><th>Location</th><th>State</th><th></th></tr>{rows}</table></div></div>")
        return self.render("Time clocks", body, status, flash)

    def devices_post(self):
        self.need_user(("payroll_admin",))
        self.check_csrf()
        try:
            loc = int(self.form.get("location_id", ""))
            label = self.form.get("label", "").strip()
            assert loc in core.locations(self.conn) and label
        except (ValueError, AssertionError):
            return self.devices_get("Choose a location and enter a label.", 400)
        secret = secrets.token_urlsafe(32)
        salt = secrets.token_hex(16)
        did = self.conn.execute("INSERT INTO sb_devices (location_id, label, salt, key_hash) VALUES (%s,%s,%s,'') RETURNING id",
                                (loc, label, salt)).fetchone()["id"]
        key = f"sbk_{did}_{secret}"
        self.conn.execute("UPDATE sb_devices SET key_hash = %s WHERE id = %s", (sha(salt + secret), did))
        self.conn.commit()
        extra = (f"<div class=card data-device-id='{did}'><p><strong>Time clock created.</strong> Copy its key now; it will not be shown again.</p>"
                 f"<p><code data-device-key='{E(key)}'>{E(key)}</code></p></div>")
        return self.devices_get("", 200, extra)

    def revoke_post(self, did):
        self.need_user(("payroll_admin",))
        self.check_csrf()
        self.conn.execute("UPDATE sb_devices SET revoked = true WHERE id = %s", (int(did),))
        self.conn.commit()
        return self.devices_get("Time clock revoked; its key no longer works.")

    def api_punches(self) -> Resp:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        auth = self.headers.get("Authorization", "")
        m = re.fullmatch(r"Bearer sbk_(\d+)_([A-Za-z0-9_\-]+)", auth.strip())
        device = None
        if m:
            with connect() as conn:
                d = conn.execute("SELECT * FROM sb_devices WHERE id = %s", (int(m.group(1)),)).fetchone()
                if d and not d["revoked"] and secrets.compare_digest(d["key_hash"], sha(d["salt"] + m.group(2))):
                    device = d
        if device is None:
            return Resp(401, json.dumps({"error": "unknown or revoked time clock key"}), ctype="application/json")
        try:
            body = json.loads(raw.decode("utf-8"))
            rows = body["punches"]
            assert isinstance(body, dict) and isinstance(rows, list) and len(rows) <= 5000
        except Exception:
            return Resp(400, json.dumps({"error": "body must be JSON {\"punches\": [...]} with at most 5000 rows"}), ctype="application/json")
        results = core.import_punches(device, rows)
        return Resp(200, json.dumps({"results": results}), ctype="application/json")

    # -- payroll -----------------------------------------------------------------------------------
    def payroll_get(self):
        self.need_user(("payroll_admin",))
        runs = self.conn.execute("SELECT id, period_start, status, version FROM sb_runs ORDER BY period_start").fetchall()
        rows = "".join(f"<tr><td><a href=/payroll/runs/{r['id']}>{r['period_start']}</a></td><td>{r['status']}</td></tr>" for r in runs)
        body = (f"<div class=card><form method=post action=/payroll/runs>{csrf_input(self.csrf)}"
                "<label>Pay period starting (a Sunday, every 14 days from 2026-09-20) <input name=period_start type=date required></label>"
                "<button data-action=create-run>Create or recompute draft</button></form></div>"
                f"<div class=card><div class=tablewrap><table><tr><th>Period</th><th>Status</th></tr>{rows}</table></div></div>")
        return self.render("Payroll", body)

    def runs_post(self):
        self.need_user(("payroll_admin",))
        self.check_csrf()
        try:
            ps = date.fromisoformat(self.form.get("period_start", ""))
        except ValueError:
            return self.render("Not a pay period", "<p>Enter a date as YYYY-MM-DD.</p>", 400, "That is not a date.", True)
        if not core.is_period_start(ps):
            return self.render("Not a pay period", "<p>Pay periods start on 2026-09-20 and every 14 days after it.</p>", 400,
                               "That date is not the first day of a pay period.", True)
        self.conn.commit()
        status, rid, msg = core.create_or_recompute_run(ps)
        if status != 200:
            return self.render("Run already finalized", f"<div class=card><p>{E(msg)}</p><p><a href=/payroll/runs/{rid}>View the finalized run</a></p></div>", status, msg, True)
        return redirect(f"/payroll/runs/{rid}")

    def run_get(self, rid, flash: str = "", err: bool = False, status: int = 200):
        self.need_user(("payroll_admin",))
        run = self.conn.execute("SELECT * FROM sb_runs WHERE id = %s", (int(rid),)).fetchone()
        if not run:
            return self.render("Not found", "<p>No such run.</p>", 404)
        comp = run["computed"]
        names = {r["id"]: r["full_name"] for r in self.conn.execute(
            "SELECT id, full_name FROM employees WHERE id = ANY(%s)", ([int(k) for k in comp["lines"]] + [h["employee_id"] for h in comp["held"]],))}
        rows = []
        for k in sorted(comp["lines"], key=int):
            l = comp["lines"][k]
            adj = ""
            if l["adjustments"]:
                adj = "Adjustments: " + ", ".join(f"week {w} {dollars(a)}" for w, a in sorted(l["adjustments"].items()))
            rows.append(f"<tr data-line-employee-id='{k}' data-gross-cents='{l['gross']}'><td>{E(names.get(int(k), ''))}</td><td>{hours(l['reg'])}</td>"
                        f"<td>{hours(l['ot'])}</td><td>{hours(l['dt'])}</td><td>{l['meal']}</td><td>{dollars(l['gross'])}</td><td>{E(adj)}</td></tr>")
        held = "".join(f"<li data-held-employee-id='{h['employee_id']}' data-week-start='{h['week_start']}'>{E(names.get(h['employee_id'], ''))} — "
                       f"week of {h['week_start']}: held ({E(h['reason'])})</li>" for h in comp["held"])
        ps = run["period_start"]
        fin = ""
        if run["status"] == "draft":
            fin = (f"<div class=card><form method=post action=/payroll/runs/{run['id']}/finalize>{csrf_input(self.csrf)}"
                   f"<input type=hidden name=version value='{run['version']}'><p><strong>Finalizing locks this run, emails every employee "
                   "listed a pay statement, and cannot be undone.</strong> Later changes are paid as adjustments in a later run.</p>"
                   "<button data-action=finalize-run>Finalize run</button></form>"
                   f"<form method=post action=/payroll/runs style=margin-top:8px>{csrf_input(self.csrf)}<input type=hidden name=period_start value='{ps}'>"
                   "<button class=secondary data-action=create-run>Recompute draft</button></form></div>")
        body = (f"<div class=card data-run-id='{run['id']}' data-run-status='{run['status']}' data-period-start='{ps}' data-version='{run['version']}'>"
                f"<p>Pay period {ps} to {ps + timedelta(days=13)} · <strong>{run['status']}</strong> · version {run['version']}</p>"
                f"<p><a href=/payroll/runs/{run['id']}/export.csv>Download CSV</a></p>"
                "<div class=tablewrap><table><tr><th>Employee</th><th>Regular h</th><th>OT h</th><th>DT h</th><th>Meal</th><th>Gross</th><th>Adjustments</th></tr>"
                f"{''.join(rows)}</table></div></div>"
                f"<div class=card><h2>Held weeks</h2><p class=muted>These weeks have worked time but are not paid in this run until they are approved and free of exceptions.</p><ul>{held}</ul></div>{fin}")
        return self.render(f"Payroll run {ps}", body, status, flash, err)

    def finalize_post(self, rid):
        self.need_user(("payroll_admin",))
        self.check_csrf()
        self.conn.commit()
        status, msg = core.finalize_run(int(rid), self.form.get("version", ""))
        if status == 404:
            return self.render("Not found", "<p>No such run.</p>", 404)
        return self.run_get(rid, msg, status != 200, status)

    def export_get(self, rid):
        self.need_user(("payroll_admin",))
        run = self.conn.execute("SELECT * FROM sb_runs WHERE id = %s", (int(rid),)).fetchone()
        if not run:
            return self.render("Not found", "<p>No such run.</p>", 404)
        comp = run["computed"]
        ids = [int(k) for k in comp["lines"]]
        info = {r["id"]: r for r in self.conn.execute("SELECT id, full_name, location_id FROM employees WHERE id = ANY(%s)", (ids,))}
        locs = core.locations(self.conn)

        def safe(s: str) -> str:
            return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s

        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(["employee_id", "full_name", "location", "reg_hours", "ot_hours", "dt_hours", "meal_premiums", "straight_cents",
                    "ot_premium_cents", "dt_premium_cents", "meal_cents", "adjustment_cents", "gross_cents"])
        for eid in sorted(ids):
            l = comp["lines"][str(eid)]
            w.writerow([eid, safe(info[eid]["full_name"]), safe(locs[info[eid]["location_id"]]["name"]), hours(l["reg"]), hours(l["ot"]),
                        hours(l["dt"]), l["meal"], l["straight"], l["otp"], l["dtp"], l["mealc"], l["adj"], l["gross"]])
        return Resp(200, buf.getvalue(), {"Content-Disposition": f"attachment; filename=payroll-{run['period_start']}.csv"},
                    ctype="text/csv; charset=utf-8")

    def root_get(self):
        return redirect("/timesheet" if self.user else "/signin")


ROUTES = [
    (r"/", "root"),
    (r"/signin", "signin"),
    (r"/signout", "signout"),
    (r"/password-reset", "reset"),
    (r"/password-reset/([A-Za-z0-9_\-]+)", "resetlink"),
    (r"/profile", "profile"),
    (r"/timesheet", "timesheet"),
    (r"/employees/(\d+)/timesheet", "emptimesheet"),
    (r"/employees/(\d+)/weeks/(\d{4}-\d{2}-\d{2})/approve", "approve"),
    (r"/approvals", "approvals"),
    (r"/admin/employees/new", "newemp"),
    (r"/admin/employees/(\d+)", "emp"),
    (r"/admin/employees/(\d+)/rates", "rates"),
    (r"/admin/devices", "devices"),
    (r"/admin/devices/(\d+)/revoke", "revoke"),
    (r"/payroll", "payroll"),
    (r"/payroll/runs", "runs"),
    (r"/payroll/runs/(\d+)", "run"),
    (r"/payroll/runs/(\d+)/finalize", "finalize"),
    (r"/payroll/runs/(\d+)/export\.csv", "export"),
]


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def main() -> None:
    core.apply_schema()
    core.start_outbox()
    srv = Server(("0.0.0.0", int(ENV.get("PORT", "8080"))), Handler)
    srv.serve_forever()
