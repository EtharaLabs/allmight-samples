"""Every page and form handler (mixed into web.Handler). Typed and URL-derived text is always escaped."""

from __future__ import annotations

import secrets
import urllib.parse
from datetime import timedelta

from . import domain, mail, money, payments
from .domain import Refused
from .util import (CODE_RE, SECTION_RE, E, base_url, check_secret, dollars, dummy_check, has_control, hash_secret,
                   hold_ttl, instant, known_timezone, label_key, link_ttl, local_input, local_text, norm_email,
                   parse_int, parse_offer_token, parse_wallclock, safe_next, seat_label, sha, valid_email, valid_name)

ID = r"(\d{1,9})"
TOK = r"([A-Za-z0-9_\-]{1,200})"
ROUTES = [
    (r"/", "home"),
    (r"/signup", "signup"),
    (r"/verify/" + TOK, "verify"),
    (r"/password-reset", "reset"),
    (r"/password-reset/" + TOK, "resetlink"),
    (r"/signin", "signin"),
    (r"/signout", "signout"),
    (r"/events/" + ID, "event"),
    (r"/events/" + ID + "/hold", "hold"),
    (r"/events/" + ID + r"/sections/([^/]{1,40})/waitlist", "waitlist"),
    (r"/orders/" + ID, "order"),
    (r"/orders/" + ID + "/codes", "codes"),
    (r"/orders/" + ID + r"/codes/([^/]{1,40})/remove", "coderemove"),
    (r"/orders/" + ID + "/pay", "pay"),
    (r"/orders/" + ID + "/release", "release"),
    (r"/tickets", "tickets"),
    (r"/tickets/" + ID, "ticket"),
    (r"/tickets/" + ID + "/refund", "ticketrefund"),
    (r"/tickets/" + ID + "/transfer", "transfer"),
    (r"/waitlist/offers/" + TOK, "offer"),
    (r"/waitlist/offers/" + TOK + "/accept", "offeraccept"),
    (r"/waitlist/offers/" + TOK + "/decline", "offerdecline"),
    (r"/orgs", "orgs"),
    (r"/orgs/new", "orgnew"),
    (r"/orgs/" + ID, "orghome"),
    (r"/orgs/" + ID + "/members", "members"),
    (r"/invites/" + TOK, "invite"),
    (r"/orgs/" + ID + "/members/" + ID + "/role", "memberrole"),
    (r"/orgs/" + ID + "/members/" + ID + "/remove", "memberremove"),
    (r"/orgs/" + ID + "/venues/new", "venuenew"),
    (r"/orgs/" + ID + "/venues/" + ID, "venue"),
    (r"/orgs/" + ID + "/venues/" + ID + "/sections", "sections"),
    (r"/orgs/" + ID + "/events/new", "eventnew"),
    (r"/orgs/" + ID + "/events/" + ID, "manage"),
    (r"/orgs/" + ID + "/events/" + ID + "/publish", "publish"),
    (r"/orgs/" + ID + "/events/" + ID + "/reschedule", "reschedule"),
    (r"/orgs/" + ID + "/events/" + ID + "/cancel", "cancel"),
    (r"/orgs/" + ID + "/events/" + ID + "/codes", "promo"),
    (r"/orgs/" + ID + "/events/" + ID + "/orders", "orgorders"),
    (r"/orgs/" + ID + "/tickets/" + ID + "/refund", "orgrefund"),
    (r"/orgs/" + ID + "/events/" + ID + "/dashboard", "dashboard"),
    (r"/orgs/" + ID + "/events/" + ID + r"/attendees\.csv", "export"),
    (r"/orgs/" + ID + "/events/" + ID + "/door", "door"),
    (r"/orgs/" + ID + "/tickets/" + ID + "/checkin", "checkin"),
    (r"/orgs/" + ID + "/events/" + ID + "/devices", "devices"),
    (r"/orgs/" + ID + "/events/" + ID + "/devices/" + ID + "/revoke", "revoke"),
    (r"/orgs/" + ID + "/events/" + ID + "/reminders", "reminders"),
]

ROLE_NAMES = {"owner": "Owner", "manager": "Manager", "box_office": "Box office", "scanner": "Scanner"}
MANAGE = ("owner", "manager")
BOX = ("owner", "manager", "box_office")
STATUS_BADGE = {"valid": "ok", "paid": "ok", "published": "ok", "on_sale": "ok", "open": "ok", "admitted": "ok",
                "refund_pending": "warn", "pending": "warn", "presale": "warn", "draft": "warn", "held": "warn",
                "refunded": "bad", "expired": "bad", "released": "bad", "unavailable": "bad", "cancelled": "bad",
                "ended": "bad", "declined": "bad", "not_on_sale": ""}
STATE_TEXT = {"not_on_sale": "Not on sale yet", "presale": "Presale", "on_sale": "On sale", "ended": "Sales ended",
              "cancelled": "Cancelled"}


def badge(value: str, text: str | None = None) -> str:
    return f'<span class="badge {STATUS_BADGE.get(value, "")}">{E(text or value.replace("_", " "))}</span>'


def when(dt, tz: str) -> str:
    """Local time at the venue with the zone, e.g. 'Sat 12 Jun 2027, 20:00 CDT (America/Chicago)'."""
    if dt is None:
        return ""
    return f"{E(local_text(dt, tz))} ({E(tz)})"


def duration(seconds: int) -> str:
    if seconds % 3600 == 0 and seconds >= 3600:
        h = seconds // 3600
        return f"{h} hour{'s' if h != 1 else ''}"
    if seconds % 60 == 0 and seconds >= 60:
        m = seconds // 60
        return f"{m} minute{'s' if m != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def table(head: list[str], rows: list[str], num_cols: tuple[int, ...] = ()) -> str:
    """A table that stacks into labelled cards on narrow screens (no horizontal scrolling at 390 px)."""
    import re
    th = "".join(f'<th{" class=num" if i in num_cols else ""}>{E(h)}</th>' for i, h in enumerate(head))
    out = []
    for row in rows:
        idx = iter(range(10 ** 6))

        def lab(_m):
            i = next(idx)
            return f'<td data-label="{E(head[i]) if i < len(head) else ""}"'
        out.append(re.sub(r"<td\b", lab, row))
    return (f'<div class="tablewrap"><table class="resp"><thead><tr>{th}</tr></thead><tbody>{"".join(out)}</tbody>'
            "</table></div>")


class Pages:
    # =============================================================================================
    # Accounts
    # =============================================================================================
    def signup_form(self, email: str = "", name: str = "") -> str:
        return ('<div class=card>' + self.form_html(
            "/signup",
            f'<label>Email address <input type="email" name="email" value="{E(email)}" required autocomplete="email"></label>'
            f'<label>Full name <input type="text" name="full_name" value="{E(name)}" required maxlength="100" '
            'autocomplete="name"></label>'
            '<label>Password (at least 10 characters) <input type="password" name="password" required minlength="10" '
            'autocomplete="new-password"></label>'
            '<button type="submit" data-action="signup">Create account</button>')
            + '<p class="muted small">We email you a link to verify your address before you can sign in. Already have an '
              'account? <a href="/signin">Sign in</a>. Member of an organization already? '
              '<a href="/password-reset">Set your password</a>.</p></div>')

    def signup_get(self):
        return self.render("Create an account", self.signup_form())

    def signup_post(self):
        email, name, pw = norm_email(self.f("email")), self.f("full_name"), self.f("password")

        def again():
            return self.signup_form(email, name)
        if not valid_email(email):
            return self.render("Create an account", again(), 400, "Enter a valid email address.", "err")
        if not valid_name(name):
            return self.render("Create an account", again(), 400,
                               "Enter your full name: 1 to 100 characters, with no control characters.", "err")
        if len(pw) < 10:
            return self.render("Create an account", again(), 400, "Choose a password of at least 10 characters.", "err")
        pw_hash = hash_secret(pw)
        conn = self.conn
        conn.execute("SELECT pg_advisory_xact_lock(3, hashtext(%s))", (email,))
        acct = conn.execute("SELECT * FROM sw_accounts WHERE email_norm = %s", (email,)).fetchone()
        if acct and acct["verified"]:
            conn.rollback()
            return self.render("Create an account", again(), 409, "An account with this email address already exists. "
                               "Sign in, or use “Forgot your password?” to set a new password.", "err")
        if acct and conn.execute("SELECT 1 FROM memberships WHERE user_id = %s LIMIT 1", (acct["id"],)).fetchone():
            conn.rollback()
            return self.render("Create an account", again(), 409, "This address belongs to a member of an organization on "
                               "Seatwise. Set your password with the password reset link instead.", "err")
        t = self.now
        if acct:
            conn.execute("UPDATE sw_accounts SET full_name = %s, password_hash = %s WHERE id = %s", (name, pw_hash, acct["id"]))
            conn.execute("UPDATE sw_account_tokens SET state = 'replaced' WHERE account_id = %s AND kind = 'verify' "
                         "AND state = 'open'", (acct["id"],))
            aid = acct["id"]
        else:
            aid = conn.execute("INSERT INTO users (email, full_name, created_at) VALUES (%s, %s, %s) RETURNING id",
                               (email, name, t)).fetchone()["id"]
            conn.execute("INSERT INTO sw_accounts (id, email_norm, full_name, password_hash, verified, created_at) "
                         "VALUES (%s,%s,%s,%s,false,%s)", (aid, email, name, pw_hash, t))
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO sw_account_tokens (token_hash, account_id, kind, created_at) VALUES (%s,%s,'verify',%s)",
                     (sha(token), aid, t))
        conn.commit()
        link = f"{base_url()}/verify/{token}"
        mail.send_direct(email, name, "Verify your email address for Seatwise",
                         f"Hello {name},\n\nWelcome to Seatwise. Follow this link to verify your email address:\n{link}\n\n"
                         f"The link works once and expires in {duration(link_ttl())}. If you did not sign up, ignore this "
                         "email.\n")
        return self.render("Check your email", f"<div class=card><p>We sent a verification link to <b>{E(email)}</b>. "
                           "Follow it to verify your address, then sign in.</p><p class=muted>Didn't get it? Sign up again "
                           "with the same address to get a new link (older links stop working).</p></div>")

    def _account_token(self, token: str, kind: str):
        row = self.conn.execute("SELECT * FROM sw_account_tokens WHERE token_hash = %s AND kind = %s FOR UPDATE",
                                (sha(token), kind)).fetchone()
        if not row:
            raise self.not_found()
        if row["state"] != "open" or self.now >= row["created_at"] + timedelta(seconds=link_ttl()):
            self.conn.rollback()
            again = ('<a href="/signup">Sign up again</a> to get a new verification link.' if kind == "verify"
                     else '<a href="/password-reset">Request a new link</a>.')
            raise self.render("Link no longer valid", f"<div class=card><p>This link is no longer valid: it was already "
                              f"used, it expired, or a newer link was sent. Nothing was changed.</p><p>{again}</p></div>",
                              410, "This link is no longer valid.", "err")
        return row

    def verify_get(self, token):
        row = self._account_token(token, "verify")
        self.conn.execute("UPDATE sw_account_tokens SET state = 'used' WHERE token_hash = %s", (row["token_hash"],))
        self.conn.execute("UPDATE sw_accounts SET verified = true WHERE id = %s", (row["account_id"],))
        self.conn.commit()
        return self.render("Email address verified", "<div class=card><p>Your email address is verified. You can now "
                           '<a href="/signin">sign in</a>.</p></div>', 200, "Your email address is verified.", "ok")

    def reset_form(self) -> str:
        return ('<div class=card>' + self.form_html(
            "/password-reset", '<label>Email address <input type="email" name="email" required autocomplete="email"></label>'
            '<button type="submit" data-action="request-reset">Email me a link</button>')
            + '<p class="muted small">We email a single-use link that sets a new password. Members of an organization '
              'that moved to Seatwise use this to set their first password.</p></div>')

    def reset_get(self):
        return self.render("Reset your password", self.reset_form())

    def reset_post(self):
        email = norm_email(self.f("email"))
        conn = self.conn
        acct = conn.execute("SELECT * FROM sw_accounts WHERE email_norm = %s", (email,)).fetchone() if email else None
        eligible = bool(acct) and (acct["verified"] or conn.execute(
            "SELECT 1 FROM memberships WHERE user_id = %s LIMIT 1", (acct["id"],)).fetchone() is not None)
        if eligible:
            conn.execute("SELECT pg_advisory_xact_lock(3, hashtext(%s))", (email,))
            conn.execute("UPDATE sw_account_tokens SET state = 'replaced' WHERE account_id = %s AND kind = 'reset' "
                         "AND state = 'open'", (acct["id"],))
            token = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sw_account_tokens (token_hash, account_id, kind, created_at) VALUES (%s,%s,'reset',%s)",
                         (sha(token), acct["id"], self.now))
            conn.commit()
            link = f"{base_url()}/password-reset/{token}"
            mail.send_direct(email, acct["full_name"], "Reset your Seatwise password",
                             f"Hello {acct['full_name']},\n\nUse this single-use link to set a new Seatwise password:\n"
                             f"{link}\n\nIt expires in {duration(link_ttl())}. If you did not ask for this, ignore this "
                             "email.\n")
        else:
            conn.rollback()
        return self.render("Check your email", "<div class=card><p>If that address belongs to a Seatwise account, we have "
                           "emailed it a link to set a new password. Check your inbox.</p>"
                           '<p><a href="/signin">Back to sign in</a></p></div>')

    def _set_password_form(self, token: str) -> str:
        return ('<div class=card>' + self.form_html(
            f"/password-reset/{token}",
            '<label>New password (at least 10 characters) <input type="password" name="password" required minlength="10" '
            'autocomplete="new-password"></label><button type="submit" data-action="set-password">Set password</button>')
            + "</div>")

    def resetlink_get(self, token):
        self._account_token(token, "reset")
        self.conn.rollback()
        return self.render("Choose a new password", self._set_password_form(token))

    def resetlink_post(self, token):
        row = self._account_token(token, "reset")
        pw = self.f("password")
        if len(pw) < 10:
            self.conn.rollback()
            return self.render("Choose a new password", self._set_password_form(token), 400,
                               "Choose a password of at least 10 characters.", "err")
        self.conn.execute("UPDATE sw_accounts SET password_hash = %s, verified = true WHERE id = %s",
                          (hash_secret(pw), row["account_id"]))
        self.conn.execute("UPDATE sw_account_tokens SET state = 'used' WHERE token_hash = %s", (row["token_hash"],))
        self.conn.execute("DELETE FROM sw_sessions WHERE account_id = %s", (row["account_id"],))
        self.conn.commit()
        if self.user and self.user["id"] == row["account_id"]:
            self.user, self.csrf, self.sid_hash = None, None, None
        return self.render("Password set", '<div class=card><p>Your password is set. <a href="/signin">Sign in</a> with '
                           "it now.</p></div>", 200, "Your password is set.", "ok")

    def signin_form(self, email: str = "") -> str:
        nxt = self.f("next") or self.q("next")
        signed = (f'<p class="muted small">You are signed in as {E(self.user["email_norm"])}. Signing in again replaces '
                  "that session.</p>" if self.user else "")
        return ('<div class=card>' + signed + self.form_html(
            "/signin", f'<input type="hidden" name="next" value="{E(nxt)}">'
            f'<label>Email address <input type="email" name="email" value="{E(email)}" required autocomplete="username"></label>'
            '<label>Password <input type="password" name="password" required autocomplete="current-password"></label>'
            '<button type="submit" data-action="signin">Sign in</button>')
            + '<p class="muted small"><a href="/password-reset">Forgot your password?</a> · New here? '
              '<a href="/signup">Create an account</a>.</p></div>')

    def signin_get(self):
        return self.render("Sign in", self.signin_form())

    def signin_post(self):
        email, pw = norm_email(self.f("email")), self.f("password")
        conn = self.conn
        acct = conn.execute("SELECT * FROM sw_accounts WHERE email_norm = %s", (email,)).fetchone() if email else None
        bad = "That email address and password do not match an account. Check them and try again."
        if not acct or not acct["password_hash"]:
            dummy_check(pw)
            conn.rollback()
            return self.render("Sign in", self.signin_form(email), 400, bad, "err")
        if not check_secret(pw, acct["password_hash"]):
            conn.rollback()
            return self.render("Sign in", self.signin_form(email), 400, bad, "err")
        if not acct["verified"]:
            conn.rollback()
            return self.render("Sign in", self.signin_form(email), 403, "Verify your email address first: follow the link "
                               "we emailed you. To get a new link, sign up again with the same address.", "err")
        old = self._cookie_sid()
        if old:
            conn.execute("DELETE FROM sw_sessions WHERE sid_hash = %s", (sha(old),))
        sid = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO sw_sessions (sid_hash, account_id) VALUES (%s, %s)", (sha(sid), acct["id"]))
        conn.commit()
        return self._redirect_with_cookie(safe_next(self.f("next") or self.q("next")),
                                          f"{self.COOKIE_NAME}={sid}; Path=/; HttpOnly; SameSite=Lax")

    COOKIE_NAME = "sw_sid"

    def _redirect_with_cookie(self, to: str, cookie: str):
        from .web import redirect
        return redirect(to, {"Set-Cookie": cookie})

    def signout_post(self):
        if self.user:
            self.check_csrf()
            self.conn.execute("DELETE FROM sw_sessions WHERE sid_hash = %s", (self.sid_hash,))
            self.conn.commit()
        return self._redirect_with_cookie("/", f"{self.COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")

    # =============================================================================================
    # Public events
    # =============================================================================================
    def home_get(self):
        rows = self.conn.execute(domain.EVENT_SQL + " WHERE se.status IN ('published','cancelled') "
                                 "ORDER BY se.starts_at").fetchall()
        up = [e for e in rows if e["starts_at"] > self.now]
        past = [e for e in rows if e["starts_at"] <= self.now]

        def item(e):
            st = domain.sale_state(e, self.now)
            return (f'<li data-event-id="{e["event_id"]}"><a href="/events/{e["event_id"]}"><b>{E(e["name"])}</b></a> '
                    f'{badge(st, STATE_TEXT[st])}<br><span class="muted small">{E(e["venue_name"])} · '
                    f'{when(e["starts_at"], e["timezone"])}</span></li>')
        body = ('<div class=card><h2>Upcoming events</h2>'
                + (f'<ul class=plain>{"".join(item(e) for e in up)}</ul>' if up else "<p>No upcoming events yet.</p>")
                + "</div>")
        if past:
            body += (f'<div class=card><details><summary>Past events ({len(past)})</summary><ul class=plain>'
                     f'{"".join(item(e) for e in reversed(past))}</ul></details></div>')
        return self.render("Events", body)

    def _visible_event(self, event_id):
        ev = domain.load_event(self.conn, int(event_id))
        if not ev:
            raise self.not_found()
        if ev["status"] == "draft":
            if not self.user or not self.conn.execute(
                    "SELECT 1 FROM memberships WHERE organization_id = %s AND user_id = %s",
                    (ev["org_id"], self.user["id"])).fetchone():
                raise self.not_found()
        return ev

    def event_body(self, ev) -> str:
        conn, t = self.conn, self.now
        tz = ev["timezone"]
        seats = domain.venue_seats(conn, ev["venue_id"])
        states = domain.seat_states(conn, ev, t, seats)
        prices = domain.event_prices(conn, ev["event_id"])
        sections = domain.venue_sections(conn, ev["venue_id"])
        state = domain.sale_state(ev, t)
        if state == "not_on_sale":
            sale = f"On sale from {when(ev['on_sale_at'], tz)}."
            if ev["presale_at"] is not None:
                sale += f" Presale (with a presale code) from {when(ev['presale_at'], tz)}."
        elif state == "presale":
            sale = (f"Presale now: enter the presale code below to hold seats. General sale from "
                    f"{when(ev['on_sale_at'], tz)}.")
        elif state == "on_sale":
            sale = f"On sale now, until {when(domain.sales_end(ev), tz)}."
        elif state == "ended":
            sale = "Sales for this event have ended."
        else:
            sale = "This event has been cancelled. Nothing more can be sold; every ticket is refunded."
        draft = ('<p class="flash">Draft: only members of the organization can see this page until it is published.</p>'
                 if ev["status"] == "draft" else "")
        head = (f'<div class="card" data-event-id="{ev["event_id"]}" data-starts-at="{instant(ev["starts_at"])}" '
                f'data-sale-state="{state}">{draft}<p>{badge(state, STATE_TEXT[state])}</p>'
                f'<dl class="kv"><dt>Starts</dt><dd><b>{when(ev["starts_at"], tz)}</b></dd>'
                f'<dt>Venue</dt><dd>{E(ev["venue_name"])}, {E(ev["venue_address"])}</dd>'
                f'<dt>Sales</dt><dd>{sale}</dd>'
                f'<dt>Fees</dt><dd>Service fee 7.5 % of each face price, facility fee {dollars(ev["facility_fee_cents"])} '
                f'per ticket, tax 8.875 %.</dd></dl>'
                + (f'<p>{E(ev["description"])}</p>' if ev["description"] else "") + "</div>")
        legend = ('<div class="legend"><span><i class="sw st-available"></i>Available</span><span><i class="sw st-held">'
                  '</i>Held</span><span><i class="sw st-sold"></i>Sold</span><span><i class="sw st-waitlist"></i>Reserved '
                  'for the waiting list</span><span><i class="sw acc"></i>Accessible seat (outlined)</span></div>')
        by_sec: dict[int, list[dict]] = {}
        for s in seats:
            by_sec.setdefault(s["section_id"], []).append(s)
        can_join = ev["status"] == "published" and state == "on_sale"

        mine_wl: dict = {}
        if self.user:
            for r in self.conn.execute(
                    "SELECT w.section_id, w.quantity FROM sw_waitlist w WHERE w.event_id = %s AND w.account_id = %s AND "
                    "(w.status = 'waiting' OR (w.status = 'offered' AND EXISTS (SELECT 1 FROM sw_offers f WHERE f.entry_id = w.id "
                    "AND f.status = 'open' AND f.expires_at > %s)))", (ev["event_id"], self.user["id"], self.now)).fetchall():
                mine_wl[r["section_id"]] = r["quantity"]

        def section_html(sec, in_form: bool) -> str:
            lst = by_sec.get(sec["id"], [])
            avail = sum(1 for s in lst if states[s["id"]] == "available")
            rows, cur = [], None
            for s in lst:
                if s["row_label"] != cur:
                    if cur is not None:
                        rows.append("</div>")
                    cur = s["row_label"]
                    rows.append(f'<div class="srow"><span class="rl">{E(cur)}</span>')
                st = states[s["id"]]
                acc = "true" if s["accessible"] else "false"
                cls = f'seat st-{st}{" acc" if s["accessible"] else ""}'
                title = f'{s["label"]}: {st}{", accessible" if s["accessible"] else ""}'
                if st == "available":
                    rows.append(f'<label class="{cls}" data-seat="{E(s["label"])}" data-seat-status="available" '
                                f'data-accessible="{acc}" title="{E(title)}"><input type="checkbox" name="seats" '
                                f'value="{E(s["label"])}">{s["seat_number"]}</label>')
                else:
                    rows.append(f'<span class="{cls}" data-seat="{E(s["label"])}" data-seat-status="{st}" '
                                f'data-accessible="{acc}" title="{E(title)}">{s["seat_number"]}</span>')
            if cur is not None:
                rows.append("</div>")
            price = prices.get(sec["id"], 0)
            wl = ""
            if avail == 0 and can_join and not in_form:
                wl = ('<div class="card" style="margin-top:10px"><p>No seats are available in this section. Join its waiting '
                      "list: when seats come free you get an email offer to hold them.</p>" + self.form_html(
                          f"/events/{ev['event_id']}/sections/{sec['code']}/waitlist",
                          '<label>Seats wanted (1 to 4) <input type="number" name="quantity" min="1" max="4" value="1" '
                          'required></label><button type="submit" data-action="join-waitlist">Join the waiting list</button>')
                      + "</div>")
            my_wl = f' data-my-waitlist="{mine_wl[sec["id"]]}"' if sec["id"] in mine_wl else ""
            return (f'<div class="section-block" data-section="{E(sec["code"])}" data-price-cents="{price}" '
                    f'data-available="{avail}"{my_wl}><h3>{E(sec["name"])} ({E(sec["code"])}) · {dollars(price)} per seat · '
                    f'{avail} available</h3><div class="seatmap">{"".join(rows)}</div>{wl}</div>')

        open_secs = [s for s in sections if any(states[x["id"]] == "available" for x in by_sec.get(s["id"], []))]
        full_secs = [s for s in sections if s not in open_secs]
        hold_controls = (
            '<div class="card" style="margin-top:12px"><label class="inline"><input type="checkbox" name="accessible" '
            'value="1"> I need accessible seating (accessible seats are outlined in blue)</label>'
            '<label>Presale code (only during the presale) <input type="text" name="presale_code" autocomplete="off" '
            'maxlength="40"></label>'
            f'<p class="muted small">Tick 1 to 6 available seats. They are held for you for {duration(hold_ttl())} while '
            f'you pay; each buyer may have at most {money.MAX_TICKETS_PER_BUYER} tickets for this event.</p>'
            '<button type="submit" data-action="hold-seats">Hold selected seats</button></div>')
        inner = "".join(section_html(s, True) for s in open_secs)
        if not open_secs:
            inner = "<p>No seats are available right now.</p>"
        hold = (f'<div class="card"><h2>Choose seats</h2>{legend}'
                + self.form_html(f"/events/{ev['event_id']}/hold", inner + hold_controls) + "</div>")
        full = ""
        if full_secs:
            full = ('<div class="card"><h2>Sections with no available seats</h2>'
                    + "".join(section_html(s, False) for s in full_secs) + "</div>")
        return head + hold + full

    def event_get(self, event_id):
        ev = self._visible_event(event_id)
        return self.render(ev["name"], self.event_body(ev))

    def hold_post(self, event_id):
        user = self.need_user()
        self.check_csrf()
        ev = self._visible_event(event_id)
        conn = self.conn
        domain.lock_event(conn, ev["event_id"])
        ev = domain.load_event(conn, ev["event_id"])
        accessible = self.f("accessible").strip().lower() in ("1", "on", "true", "yes")
        try:
            oid = domain.create_hold(conn, user, ev, self.fl("seats"), self.f("presale_code"), accessible, self.now)
        except Refused as r:
            conn.rollback()
            return self.render(ev["name"], self.event_body(ev), r.status, "Seats not held: " + r.message, "err")
        conn.commit()
        domain.WORK_KICK.set()
        from .web import redirect
        return redirect(f"/orders/{oid}")

    def waitlist_post(self, event_id, code):
        user = self.need_user()
        self.check_csrf()
        ev = self._visible_event(event_id)
        sec = self.conn.execute("SELECT * FROM sections WHERE venue_id = %s AND code = %s", (ev["venue_id"], code)).fetchone()
        if not sec:
            raise self.not_found()
        domain.lock_event(self.conn, ev["event_id"])
        ev = domain.load_event(self.conn, ev["event_id"])
        try:
            domain.join_waitlist(self.conn, user, ev, sec, self.f("quantity"), self.now)
        except Refused as r:
            self.conn.rollback()
            return self.render(ev["name"], self.event_body(ev), r.status, "Not added to the waiting list: " + r.message, "err")
        self.conn.commit()
        from .web import redirect
        return redirect(f"/events/{ev['event_id']}?msg=joined")

    # =============================================================================================
    # Orders
    # =============================================================================================
    def _my_order(self, order_id, lock: bool = False):
        user = self.need_user()
        order = domain.load_order(self.conn, int(order_id), lock=lock)
        if not order:
            raise self.not_found()
        if order["buyer_email_norm"] != user["email_norm"]:
            raise self.forbidden("Only the buyer can see or change this order.")
        return order

    def order_body(self, order) -> str:
        conn, t = self.conn, self.now
        ev = domain.load_event(conn, order["event_id"])
        tz = ev["timezone"]
        status = domain.order_status(order, t)
        if order["legacy"]:
            lines = conn.execute(
                "SELECT t.*, sec.code, s.row_label, s.seat_number FROM sw_tickets t JOIN seats s ON s.id = t.seat_id "
                "JOIN sections sec ON sec.id = s.section_id WHERE t.order_id = %s", (order["id"],)).fetchall()
            ql = sorted(({"label": seat_label(r["code"], r["row_label"], r["seat_number"]), "face": r["face"],
                          "discount": r["discount"], "service": r["service"], "facility": r["facility"], "tax": r["tax"],
                          "total": r["total"], "tid": r["id"]} for r in lines), key=lambda x: label_key(x["label"]))
            tot = {k: sum(int(x[k]) for x in ql) for k in ("face", "discount", "service", "facility", "tax", "total")}
            codes = []
        else:
            q = domain.order_quote(conn, order, ev)
            tickets = {r["seat_id"]: r["id"] for r in conn.execute("SELECT id, seat_id FROM sw_tickets WHERE order_id = %s",
                                                                    (order["id"],))}
            olines = domain.order_lines(conn, order["id"])
            ql = [{"label": l.key, "face": l.face, "discount": l.discount, "service": l.service, "facility": l.facility,
                   "tax": l.tax, "total": l.total, "tid": tickets.get(ol["seat_id"])} for l, ol in zip(q.lines, olines)]
            tot = {"face": q.subtotal, "discount": q.discount, "service": q.service, "facility": q.facility, "tax": q.tax,
                   "total": q.total}
            codes = domain.order_codes(conn, order["id"])
        rows = []
        for x in ql:
            tid = f' data-ticket-id="{x["tid"]}"' if x["tid"] and status == "paid" else ""
            link = f' · <a href="/tickets/{x["tid"]}">ticket {x["tid"]}</a>' if x["tid"] and status == "paid" else ""
            rows.append(f'<tr data-seat="{E(x["label"])}" data-face-cents="{x["face"]}" data-discount-cents="{x["discount"]}" '
                        f'data-service-fee-cents="{x["service"]}" data-facility-fee-cents="{x["facility"]}" '
                        f'data-tax-cents="{x["tax"]}" data-total-cents="{x["total"]}"{tid}><td>{E(x["label"])}{link}</td>'
                        f'<td class=num>{dollars(x["face"])}</td><td class=num>−{dollars(x["discount"])}</td>'
                        f'<td class=num>{dollars(x["service"])}</td><td class=num>{dollars(x["facility"])}</td>'
                        f'<td class=num>{dollars(x["tax"])}</td><td class=num><b>{dollars(x["total"])}</b></td></tr>')
        rows.append(f'<tr><td><b>Order</b></td><td class=num>{dollars(tot["face"])}</td><td class=num>−{dollars(tot["discount"])}'
                    f'</td><td class=num>{dollars(tot["service"])}</td><td class=num>{dollars(tot["facility"])}</td>'
                    f'<td class=num>{dollars(tot["tax"])}</td><td class=num><b>{dollars(tot["total"])}</b></td></tr>')
        tbl = table(["Seat", "Face price", "Discount", "Service fee", "Facility fee", "Tax", "Total"], rows, (1, 2, 3, 4, 5, 6))
        expires = order["hold_expires_at"]
        if status == "pending":
            info = (f'<p>Your seats are held until <b>{when(expires, tz)}</b> (<span class=mono>{instant(expires)}</span>), '
                    f'{duration(hold_ttl())} from when you chose them. Pay before then; if you don\'t, the hold ends and '
                    "the seats go back on sale.</p>")
            if order["pay_pressed"]:
                info += ('<p class="muted">You started paying. Once the payment provider confirms the payment this page '
                         "shows your tickets (reload it in a few seconds). A declined card changes nothing: press Pay to "
                         "try another card while the hold lasts.</p>")
        elif status == "paid":
            info = (f"<p>Paid {when(order['paid_at'], tz)}. Your tickets are listed below and on "
                    '<a href="/tickets">My tickets</a>.</p>')
        elif status == "expired":
            info = (f"<p>The hold ended at {when(expires, tz)} without payment, so the seats were released. "
                    f'<a href="/events/{ev["event_id"]}">Choose seats again</a>.</p>')
        elif status == "released":
            info = f'<p>You released this hold. <a href="/events/{ev["event_id"]}">Choose seats again</a>.</p>'
        else:
            info = ("<p>Your payment arrived after the hold ended and the seats were no longer available, so the whole "
                    "payment was refunded to your card.</p>")
        explain = ('<p class="muted small">How the price works: each seat has its section\'s face price. Promo codes '
                   "discount the face subtotal (a percentage code first, then a fixed amount) and the discount is shared "
                   "across tickets in proportion to face price. Each ticket adds a service fee of 7.5 % of its face price "
                   "and the event's facility fee. Tax is 8.875 % of the order's face prices after discount plus service "
                   "fees, shared across tickets the same way.</p>")
        code_html = ""
        if not order["legacy"]:
            can = domain.codes_can_change(order, t)
            items = []
            for c in codes:
                desc = f"{c['value']} % off" if c["kind"] == "percent" else f"{dollars(c['value'])} off"
                rm = (self.form_html(f"/orders/{order['id']}/codes/{c['code']}/remove",
                                     '<button type="submit" class="secondary" data-action="remove-code">Remove</button>',
                                     "inline") if can else "")
                items.append(f'<li data-applied-code="{E(c["code"])}"><b>{E(c["code"])}</b> · {desc} {rm}</li>')
            code_html = ("<h2>Promo codes</h2>" + (f'<ul class=plain>{"".join(items)}</ul>' if items
                                                   else "<p class=muted>No codes applied.</p>"))
            if can:
                code_html += self.form_html(
                    f"/orders/{order['id']}/codes",
                    '<label>Promo code <input type="text" name="code" required maxlength="40" autocomplete="off"></label>'
                    '<button type="submit" data-action="apply-code">Apply code</button>')
                code_html += ('<p class="muted small">One percentage code and one fixed-amount code can be combined. Codes '
                              "can no longer change once you press Pay.</p>")
        actions = ""
        if status == "pending":
            actions = ('<div class="row" style="margin-top:12px">'
                       + self.form_html(f"/orders/{order['id']}/pay",
                                        f'<button type="submit" data-action="pay">Pay {dollars(tot["total"])}</button>', "inline")
                       + self.form_html(f"/orders/{order['id']}/release",
                                        '<button type="submit" class="secondary" data-action="release-hold">Release these '
                                        "seats</button>", "inline") + "</div>")
        return (f'<div class="card" data-order-id="{order["id"]}" data-order-status="{status}" '
                f'data-hold-expires-at="{instant(expires) if expires else ""}" data-subtotal-cents="{tot["face"]}" '
                f'data-discount-cents="{tot["discount"]}" data-service-fee-cents="{tot["service"]}" '
                f'data-facility-fee-cents="{tot["facility"]}" data-tax-cents="{tot["tax"]}" data-total-cents="{tot["total"]}">'
                f'<p><a href="/events/{ev["event_id"]}"><b>{E(ev["name"])}</b></a> · {E(ev["venue_name"])} · '
                f'{when(ev["starts_at"], tz)}</p><p>Order {order["id"]} {badge(status)}</p>{info}{tbl}{explain}'
                f"{code_html}{actions}</div>")

    def order_get(self, order_id):
        order = self._my_order(order_id)
        return self.render(f"Order {order['id']}", self.order_body(order))

    def _order_refused(self, order_id, r: Refused):
        self.conn.rollback()
        order = domain.load_order(self.conn, int(order_id))
        return self.render(f"Order {order['id']}", self.order_body(order), r.status, r.message, "err")

    def codes_post(self, order_id):
        self.need_user()
        self.check_csrf()
        order = self._my_order(order_id, lock=True)
        try:
            domain.apply_code(self.conn, order, self.f("code"), self.now)
        except Refused as r:
            return self._order_refused(order_id, r)
        self.conn.commit()
        from .web import redirect
        return redirect(f"/orders/{order['id']}?msg=codeadded")

    def coderemove_post(self, order_id, code):
        self.need_user()
        self.check_csrf()
        order = self._my_order(order_id, lock=True)
        try:
            domain.remove_code(self.conn, order, urllib.parse.unquote(code), self.now)
        except Refused as r:
            return self._order_refused(order_id, r)
        self.conn.commit()
        from .web import redirect
        return redirect(f"/orders/{order['id']}?msg=coderemoved")

    def pay_post(self, order_id):
        from .web import redirect
        self.need_user()
        self.check_csrf()
        conn = self.conn
        order = self._my_order(order_id, lock=True)
        if not domain.hold_active(order, self.now):
            return self._order_refused(order_id, Refused("This order can no longer be paid: its hold has ended. Choose "
                                                         "seats again from the event page."))
        if order["session_url"]:
            conn.commit()
            return redirect(order["session_url"])
        ev = domain.load_event(conn, order["event_id"])
        conn.execute("UPDATE sw_orders SET pay_pressed = true WHERE id = %s", (order["id"],))
        q = domain.order_quote(conn, order, ev)
        params = domain.checkout_params(conn, order, ev, q.total)
        key = domain.checkout_key(conn, order["id"])
        conn.commit()
        status, body = payments.call("POST", "/v1/checkout/sessions", params, idem_key=key, timeout=20)
        if status is not None and 200 <= status < 300 and body and isinstance(body.get("id"), str) \
                and isinstance(body.get("url"), str) and body["url"].startswith(("http://", "https://")) \
                and not has_control(body["url"]):
            conn.execute("UPDATE sw_orders SET session_id = COALESCE(session_id, %s), session_url = COALESCE(session_url, %s), "
                         "session_amount = COALESCE(session_amount, %s) WHERE id = %s",
                         (body["id"], body["url"], q.total, order["id"]))
            conn.commit()
            order = domain.load_order(conn, order["id"])
            return redirect(order["session_url"])
        return self._order_refused(order_id, Refused("The payment provider did not answer, so no payment was started. "
                                                     "Press Pay again.", 502))

    def release_post(self, order_id):
        self.need_user()
        self.check_csrf()
        order = self._my_order(order_id, lock=True)
        if not domain.hold_active(order, self.now):
            return self._order_refused(order_id, Refused("This order is not on hold any more."))
        self.conn.execute("UPDATE sw_orders SET status = 'released', ended_at = %s WHERE id = %s", (self.now, order["id"]))
        self.conn.commit()
        domain.WORK_KICK.set()
        from .web import redirect
        return redirect(f"/orders/{order['id']}?msg=released")

    # =============================================================================================
    # Tickets
    # =============================================================================================
    def tickets_get(self):
        user = self.need_user()
        conn = self.conn
        rows = conn.execute(
            "SELECT t.id, t.event_id, t.status, t.seat_id, sec.code, s.row_label, s.seat_number, e.name, se.starts_at, "
            "v.timezone FROM sw_tickets t JOIN seats s ON s.id = t.seat_id JOIN sections sec ON sec.id = s.section_id "
            "JOIN sw_events se ON se.event_id = t.event_id JOIN events e ON e.id = t.event_id JOIN venues v ON v.id = se.venue_id "
            "WHERE t.holder_email_norm = %s", (user["email_norm"],)).fetchall()
        rows.sort(key=lambda r: (r["starts_at"], r["event_id"], r["code"], r["row_label"], r["seat_number"], r["id"]))
        items = []
        for r in rows:
            lab = seat_label(r["code"], r["row_label"], r["seat_number"])
            items.append(f'<li data-ticket-id="{r["id"]}" data-event-id="{r["event_id"]}" data-seat="{E(lab)}" '
                         f'data-ticket-status="{r["status"]}"><a href="/tickets/{r["id"]}"><b>{E(lab)}</b></a> · '
                         f'{E(r["name"])} · {when(r["starts_at"], r["timezone"])} {badge(r["status"])}</li>')
        pend = conn.execute("SELECT o.id, o.hold_expires_at, e.name FROM sw_orders o JOIN events e ON e.id = o.event_id "
                            "WHERE o.buyer_email_norm = %s AND o.status = 'pending' AND o.hold_expires_at > %s ORDER BY o.id",
                            (user["email_norm"], self.now)).fetchall()
        body = ""
        if pend:
            body += ('<div class=card><h2>Seats on hold</h2><ul class=plain>' + "".join(
                f'<li><a href="/orders/{o["id"]}">Order {o["id"]}</a> · {E(o["name"])} · held until '
                f'{instant(o["hold_expires_at"])}</li>' for o in pend) + "</ul></div>")
        body += ('<div class=card>' + (f'<ul class=plain>{"".join(items)}</ul>' if items else
                                      '<p>You have no tickets yet. <a href="/">Browse events</a>.</p>') + "</div>")
        return self.render("My tickets", body)

    def _ticket(self, ticket_id):
        user = self.need_user()
        t = self.conn.execute("SELECT * FROM sw_tickets WHERE id = %s", (int(ticket_id),)).fetchone()
        if not t:
            raise self.not_found()
        order = domain.load_order(self.conn, t["order_id"])
        if user["email_norm"] not in (t["holder_email_norm"], order["buyer_email_norm"]):
            raise self.forbidden("This ticket belongs to someone else.")
        return t, order

    def ticket_body(self, t, order) -> str:
        conn, user, now_ = self.conn, self.user, self.now
        ev = domain.load_event(conn, t["event_id"])
        tz = ev["timezone"]
        label = domain.ticket_label(conn, t)
        holder = t["holder_email_norm"] == user["email_norm"]
        attrs = (f'data-ticket-id="{t["id"]}" data-seat="{E(label)}" data-ticket-status="{t["status"]}" '
                 f'data-holder-email="{E(t["holder_email_norm"])}" data-admitted-at="{instant(t["admitted_at"])}"')
        refund_html = ""
        if not t["legacy"]:
            terms = domain.refund_terms(ev, order, now_)
            pct, why = domain.buyer_refund_check(user, t, order, ev, now_)
            attrs += (f' data-full-refund-until="{instant(terms["full_until"])}" '
                      f'data-half-refund-until="{instant(terms["half_until"])}" data-refund-percent="{pct}"')
            amount = money.refund_amount(int(t["total"]), pct)
            if terms["rescheduled"]:
                dl = (f"<p>The event was rescheduled after this order was paid, so a refund returns 100 % of the ticket "
                      f"total at any time before the new start, {when(ev['starts_at'], tz)}.</p>")
            else:
                dl = (f"<ul><li>Full refund (100 %) if requested by {when(terms['full_until'], tz)}</li>"
                      f"<li>Half refund (50 %) if requested by {when(terms['half_until'], tz)}</li>"
                      "<li>No refund after that (48 hours before the start).</li></ul>")
            now_text = (f"<p><b>A refund requested now would return {pct} % of the ticket total: "
                        f"{dollars(amount)}</b> (ticket total {dollars(t['total'])}).</p>" if pct else
                        f"<p><b>A refund requested now would return nothing.</b> {E(why)}</p>")
            btn = ""
            if pct:
                btn = self.form_html(f"/tickets/{t['id']}/refund",
                                     f'<button type="submit" class="danger" data-action="request-refund">Request a refund of '
                                     f"{dollars(amount)}</button>")
            refund_html = f"<div class=card><h2>Refunds</h2>{now_text}{dl}{btn}</div>"
        else:
            refund_html = ("<div class=card><h2>Refunds</h2><p>This ticket was sold by our old platform, so it cannot be "
                           "refunded here.</p></div>")
        qr = ""
        if holder:
            qr = ("<div class=card><h2>Your QR code</h2><p>Show this token at the door. It is checked against the ticket "
                  f'when scanned.</p><code class="token" data-qr-token="{E(domain.current_token(t))}">'
                  f"{E(domain.current_token(t))}</code></div>")
        elif t["transferred"]:
            qr = ("<div class=card><p>You transferred this ticket to someone else, so its QR code is no longer shown here."
                  "</p></div>")
        xfer = ""
        if holder:
            why = domain.transfer_check(user, t, ev, now_)
            if not why:
                xfer = ("<div class=card><h2>Transfer this ticket</h2><p>Transferring gives the ticket a <b>new QR code</b>"
                        " and sends it to the recipient by email. The QR code shown above stops working at the door "
                        "immediately, so only the new holder can get in. A transferred ticket can no longer be refunded to "
                        "you.</p>" + self.form_html(
                            f"/tickets/{t['id']}/transfer",
                            '<label>Recipient email <input type="email" name="to_email" required></label>'
                            '<label>Recipient name <input type="text" name="to_name" required maxlength="100"></label>'
                            '<button type="submit" data-action="transfer-ticket">Transfer ticket</button>') + "</div>")
            else:
                xfer = f"<div class=card><h2>Transfer</h2><p class=muted>This ticket cannot be transferred now: {E(why)}</p></div>"
        adm = (f"Admitted {when(t['admitted_at'], tz)} at gate {E(t['admitted_gate'])}" if t["admitted_at"] else "Not yet admitted")
        info = (f'<div class="card" {attrs}><p><a href="/events/{ev["event_id"]}"><b>{E(ev["name"])}</b></a></p>'
                f'<dl class="kv"><dt>Seat</dt><dd><b>{E(label)}</b></dd><dt>Ticket</dt><dd>{t["id"]}</dd>'
                f'<dt>Status</dt><dd>{badge(t["status"])}</dd><dt>Starts</dt><dd>{when(ev["starts_at"], tz)}</dd>'
                f'<dt>Venue</dt><dd>{E(ev["venue_name"])}, {E(ev["venue_address"])}</dd>'
                f'<dt>Holder</dt><dd>{E(t["holder_name"])} ({E(t["holder_email_norm"])})</dd>'
                f'<dt>Admission</dt><dd>{adm}</dd><dt>Ticket total</dt><dd>{dollars(t["total"])}</dd></dl></div>')
        return info + qr + refund_html + xfer

    def ticket_get(self, ticket_id):
        t, order = self._ticket(ticket_id)
        return self.render(f"Ticket {t['id']}", self.ticket_body(t, order))

    def _ticket_refused(self, ticket_id, r: Refused):
        self.conn.rollback()
        t, order = self._ticket(ticket_id)
        return self.render(f"Ticket {t['id']}", self.ticket_body(t, order), r.status, r.message, "err")

    def ticketrefund_post(self, ticket_id):
        user = self.need_user()
        self.check_csrf()
        t, order = self._ticket(ticket_id)
        try:
            domain.request_buyer_refund(self.conn, user, t["id"], self.now)
        except Refused as r:
            return self._ticket_refused(ticket_id, r)
        self.conn.commit()
        from .web import redirect
        return redirect(f"/tickets/{t['id']}?msg=refund")

    def transfer_post(self, ticket_id):
        user = self.need_user()
        self.check_csrf()
        t, order = self._ticket(ticket_id)
        to_email, to_name = norm_email(self.f("to_email")), self.f("to_name")
        if not valid_email(to_email):
            return self._ticket_refused(ticket_id, Refused("Enter the recipient's email address.", 400))
        if not valid_name(to_name):
            return self._ticket_refused(ticket_id, Refused("Enter the recipient's name: 1 to 100 characters, no control "
                                                           "characters.", 400))
        try:
            domain.transfer(self.conn, user, t["id"], to_email, to_name, self.now)
        except Refused as r:
            return self._ticket_refused(ticket_id, r)
        self.conn.commit()
        mail.KICK.set()
        from .web import redirect
        return redirect(f"/tickets/{t['id']}?msg=transferred")

    # =============================================================================================
    # Waiting-list offers
    # =============================================================================================
    def _offer(self, token):
        oid = parse_offer_token(token)
        offer = self.conn.execute("SELECT * FROM sw_offers WHERE id = %s", (oid,)).fetchone() if oid else None
        if not offer:
            raise self.not_found()
        user = self.need_user()
        if offer["account_id"] != user["id"]:
            raise self.forbidden("This offer was made to someone else.")
        return offer

    def offer_body(self, offer, token) -> str:
        ev = domain.load_event(self.conn, offer["event_id"])
        st = domain.offer_status(offer, self.now)
        seats = [s for s in domain.venue_seats(self.conn, ev["venue_id"]) if s["id"] in set(offer["seat_ids"])]
        labels = " ".join(s["label"] for s in seats)
        btns = ""
        if st == "open":
            btns = ('<div class="row">' + self.form_html(f"/waitlist/offers/{token}/accept",
                                                         '<button type="submit" data-action="accept-offer">Accept and hold '
                                                         "these seats</button>", "inline")
                    + self.form_html(f"/waitlist/offers/{token}/decline",
                                     '<button type="submit" class="secondary" data-action="decline-offer">Decline</button>',
                                     "inline") + "</div>")
        extra = f'<p><a href="/orders/{offer["order_id"]}">Go to your order</a></p>' if offer["order_id"] else ""
        return (f'<div class="card" data-offer-status="{st}" data-offer-seats="{E(labels)}"><p><b>{E(ev["name"])}</b> · '
                f'{when(ev["starts_at"], ev["timezone"])}</p><p>Seats offered to you: <b>{E(labels)}</b> {badge(st)}</p>'
                f'<p>The offer is open until {when(offer["expires_at"], ev["timezone"])}. Accepting holds these seats for '
                f"{duration(hold_ttl())} while you pay; declining passes them to the next person on the list.</p>"
                f"{btns}{extra}</div>")

    def offer_get(self, token):
        offer = self._offer(token)
        return self.render("Waiting-list offer", self.offer_body(offer, token))

    def offeraccept_post(self, token):
        user = self.need_user()
        self.check_csrf()
        offer = self._offer(token)
        try:
            oid = domain.accept_offer(self.conn, user, offer, self.now)
        except Refused as r:
            self.conn.rollback()
            offer = self._offer(token)
            return self.render("Waiting-list offer", self.offer_body(offer, token), r.status, r.message, "err")
        self.conn.commit()
        from .web import redirect
        return redirect(f"/orders/{oid}")

    def offerdecline_post(self, token):
        self.need_user()
        self.check_csrf()
        offer = self._offer(token)
        try:
            domain.decline_offer(self.conn, offer, self.now)
        except Refused as r:
            self.conn.rollback()
            offer = self._offer(token)
            return self.render("Waiting-list offer", self.offer_body(offer, token), r.status, r.message, "err")
        self.conn.commit()
        from .web import redirect
        return redirect(f"/waitlist/offers/{token}?msg=declined")

    # =============================================================================================
    # Organizations and members
    # =============================================================================================
    def orgs_get(self):
        user = self.need_user()
        rows = self.conn.execute("SELECT o.id, o.name, m.role FROM memberships m JOIN organizations o ON o.id = m.organization_id "
                                 "WHERE m.user_id = %s ORDER BY o.name, o.id", (user["id"],)).fetchall()
        items = "".join(f'<li><a href="/orgs/{r["id"]}"><b>{E(r["name"])}</b></a> · {E(ROLE_NAMES[r["role"]])}</li>'
                        for r in rows)
        body = ('<div class=card>' + (f"<ul class=plain>{items}</ul>" if items else
                                     "<p>You are not a member of any organization.</p>")
                + '<p><a class="btn" href="/orgs/new">Create an organization</a></p></div>')
        return self.render("Your organizations", body)

    def _orgnew_form(self, name: str = "") -> str:
        return ("<div class=card>" + self.form_html(
            "/orgs/new", f'<label>Organization name <input type="text" name="name" value="{E(name)}" required maxlength="100">'
            '</label><button type="submit" data-action="create-org">Create organization</button>')
            + '<p class="muted small">You become its owner: you can invite members, set up venues and sell events.</p></div>')

    def orgnew_get(self):
        self.need_user()
        return self.render("Create an organization", self._orgnew_form())

    def orgnew_post(self):
        user = self.need_user()
        self.check_csrf()
        name = self.f("name")
        if not valid_name(name):
            return self.render("Create an organization", self._orgnew_form(name), 400,
                               "Enter a name of 1 to 100 characters, with no control characters.", "err")
        oid = self.conn.execute("INSERT INTO organizations (name, created_at) VALUES (%s, %s) RETURNING id",
                                (name, self.now)).fetchone()["id"]
        self.conn.execute("INSERT INTO memberships (organization_id, user_id, role) VALUES (%s, %s, 'owner')",
                          (oid, user["id"]))
        self.conn.commit()
        from .web import redirect
        return redirect(f"/orgs/{oid}")

    def orghome_get(self, org_id):
        org, role = self.member(org_id)
        conn = self.conn
        venues = conn.execute("SELECT * FROM venues WHERE organization_id = %s ORDER BY id", (org["id"],)).fetchall()
        events = conn.execute(domain.EVENT_SQL + " WHERE se.org_id = %s ORDER BY se.starts_at DESC", (org["id"],)).fetchall()
        links = [f'<a href="/orgs/{org["id"]}/members">Members</a>']
        if role in MANAGE:
            links += [f'<a href="/orgs/{org["id"]}/venues/new">New venue</a>', f'<a href="/orgs/{org["id"]}/events/new">New event</a>']
        vhtml = ""
        if role in MANAGE:
            vhtml = ("<div class=card><h2>Venues</h2><ul class=plain>" + "".join(
                f'<li><a href="/orgs/{org["id"]}/venues/{v["id"]}">{E(v["name"])}</a> · {E(v["timezone"])}</li>'
                for v in venues) + "</ul></div>") if venues else "<div class=card><p>No venues yet.</p></div>"
        ev_items = []
        for e in events:
            eid = e["event_id"]
            acts = []
            if role in MANAGE:
                acts.append(f'<a href="/orgs/{org["id"]}/events/{eid}">Manage</a>')
            if role in BOX:
                acts += [f'<a href="/orgs/{org["id"]}/events/{eid}/dashboard">Dashboard</a>',
                         f'<a href="/orgs/{org["id"]}/events/{eid}/orders">Orders</a>']
            acts.append(f'<a href="/orgs/{org["id"]}/events/{eid}/door">Door list</a>')
            ev_items.append(f'<li><b>{E(e["name"])}</b> {badge(e["status"])}<br><span class="muted small">'
                            f'{when(e["starts_at"], e["timezone"])} · {E(e["venue_name"])}</span><br>'
                            f'<span class=small>{" · ".join(acts)}</span></li>')
        body = (f'<div class="card" data-org-id="{org["id"]}" data-my-role="{role}"><p>Your role: '
                f'<b>{E(ROLE_NAMES[role])}</b></p><p class=row>{" · ".join(links)}</p></div>{vhtml}'
                "<div class=card><h2>Events</h2>" + (f'<ul class=plain>{"".join(ev_items)}</ul>' if ev_items else
                                                    "<p>No events yet.</p>") + "</div>")
        return self.render(org["name"], body)

    def members_body(self, org, role) -> str:
        rows = self.conn.execute(
            "SELECT m.user_id, m.role, a.full_name, a.email_norm FROM memberships m JOIN sw_accounts a ON a.id = m.user_id "
            "WHERE m.organization_id = %s ORDER BY (m.role = 'owner') DESC, a.full_name, m.user_id", (org["id"],)).fetchall()
        items = []
        for r in rows:
            forms = ""
            if role == "owner" and r["role"] != "owner":
                opts = "".join(f'<option value="{x}"{" selected" if x == r["role"] else ""}>{ROLE_NAMES[x]}</option>'
                               for x in domain.INVITE_ROLES)
                forms = ('<div class="row">' + self.form_html(
                    f"/orgs/{org['id']}/members/{r['user_id']}/role",
                    f'<select name="role" style="width:auto">{opts}</select> '
                    '<button type="submit" class="secondary" data-action="change-role">Change role</button>', "inline")
                    + self.form_html(f"/orgs/{org['id']}/members/{r['user_id']}/remove",
                                     '<button type="submit" class="danger" data-action="remove-member">Remove</button>',
                                     "inline") + "</div>")
            items.append(f'<li data-member-user-id="{r["user_id"]}" data-member-email="{E(r["email_norm"])}" data-role="{r["role"]}"><b>{E(r["full_name"])}</b> · '
                         f'{E(r["email_norm"])} · {E(ROLE_NAMES[r["role"]])}{forms}</li>')
        invite = ""
        if role == "owner":
            opts = "".join(f'<option value="{x}">{ROLE_NAMES[x]}</option>' for x in domain.INVITE_ROLES)
            invite = ("<div class=card><h2>Invite a member</h2>" + self.form_html(
                f"/orgs/{org['id']}/members",
                '<label>Email address <input type="email" name="email" required></label>'
                f'<label>Role <select name="role">{opts}</select></label>'
                '<button type="submit" data-action="invite-member">Send invitation</button>')
                + '<p class="muted small">Managers run venues, events and promo codes; box office sees dashboards and '
                  "orders and gives refunds; scanners run the door.</p></div>")
        return f'<div class=card><ul class=plain>{"".join(items)}</ul></div>{invite}'

    def members_get(self, org_id):
        org, role = self.member(org_id)
        return self.render(f"Members of {org['name']}", self.members_body(org, role))

    def members_post(self, org_id):
        org, role = self.member(org_id, ("owner",), post=True)
        conn = self.conn
        email, r = norm_email(self.f("email")), self.f("role")

        def fail(msg, status=400):
            conn.rollback()
            return self.render(f"Members of {org['name']}", self.members_body(org, role), status, msg, "err")
        if r not in domain.INVITE_ROLES:
            return fail("Choose a role of manager, box office or scanner.")
        if not valid_email(email):
            return fail("Enter a valid email address.")
        if conn.execute("SELECT 1 FROM memberships m JOIN sw_accounts a ON a.id = m.user_id WHERE m.organization_id = %s "
                        "AND a.email_norm = %s", (org["id"], email)).fetchone():
            return fail("That address already belongs to a member of this organization.", 409)
        conn.execute("UPDATE sw_invites SET state = 'replaced' WHERE org_id = %s AND email_norm = %s AND state = 'open'",
                     (org["id"], email))
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO sw_invites (org_id, email_norm, role, token_hash, invited_by, created_at) "
                     "VALUES (%s,%s,%s,%s,%s,%s)", (org["id"], email, r, sha(token), self.user["id"], self.now))
        acct = conn.execute("SELECT full_name FROM sw_accounts WHERE email_norm = %s", (email,)).fetchone()
        conn.commit()
        to_name = acct["full_name"] if acct else email.split("@")[0]
        link = f"{base_url()}/invites/{token}"
        mail.send_direct(email, to_name, f"Invitation to join {org['name']} on Seatwise",
                         f"Hello,\n\n{self.user['full_name']} invited you to join {org['name']} on Seatwise as "
                         f"{ROLE_NAMES[r].lower()}.\n\nAccept the invitation here (sign in, or create an account with "
                         f"this email address first):\n{link}\n\nThe link works once.\n")
        return self.render(f"Members of {org['name']}", self.members_body(org, role), 200,
                           f"Invitation sent to {email}.", "ok")

    def _invite(self, token):
        row = self.conn.execute("SELECT * FROM sw_invites WHERE token_hash = %s", (sha(token),)).fetchone()
        if not row:
            raise self.not_found()
        user = self.need_user()
        if user["email_norm"] != row["email_norm"]:
            raise self.forbidden("This invitation was sent to a different email address. Sign in with that address to "
                                 "accept it.")
        if row["state"] != "open":
            raise self.render("Invitation no longer valid", "<div class=card><p>This invitation link is no longer valid: "
                              "it was already used or a newer invitation was sent. Ask the organization's owner for a new "
                              "one.</p></div>", 410, "This link is no longer valid.", "err")
        return row

    def invite_get(self, token):
        row = self._invite(token)
        org = self.conn.execute("SELECT * FROM organizations WHERE id = %s", (row["org_id"],)).fetchone()
        return self.render("Invitation", f"<div class=card><p>You are invited to join <b>{E(org['name'])}</b> as "
                           f"<b>{E(ROLE_NAMES[row['role']])}</b>.</p>" + self.form_html(
                               f"/invites/{token}", '<button type="submit" data-action="accept-invite">Accept invitation'
                               "</button>") + "</div>")

    def invite_post(self, token):
        self.need_user()
        self.check_csrf()
        row = self._invite(token)
        conn = self.conn
        row = conn.execute("SELECT * FROM sw_invites WHERE id = %s FOR UPDATE", (row["id"],)).fetchone()
        if row["state"] != "open":
            conn.rollback()
            return self.render("Invitation no longer valid", "<div class=card><p>This invitation link is no longer valid."
                               "</p></div>", 410, "This link is no longer valid.", "err")
        if conn.execute("SELECT 1 FROM memberships WHERE organization_id = %s AND user_id = %s",
                        (row["org_id"], self.user["id"])).fetchone():
            conn.rollback()
            return self.render("Invitation", "<div class=card><p>You are already a member of this organization.</p></div>",
                               409, "You are already a member of this organization.", "err")
        conn.execute("INSERT INTO memberships (organization_id, user_id, role) VALUES (%s, %s, %s)",
                     (row["org_id"], self.user["id"], row["role"]))
        conn.execute("UPDATE sw_invites SET state = 'accepted', accepted_by = %s, accepted_at = %s WHERE id = %s",
                     (self.user["id"], self.now, row["id"]))
        conn.commit()
        from .web import redirect
        return redirect(f"/orgs/{row['org_id']}")

    def _target_member(self, org, user_id):
        m = self.conn.execute("SELECT * FROM memberships WHERE organization_id = %s AND user_id = %s FOR UPDATE",
                              (org["id"], int(user_id))).fetchone()
        if not m:
            raise self.not_found()
        return m

    def memberrole_post(self, org_id, user_id):
        org, role = self.member(org_id, ("owner",), post=True)
        m = self._target_member(org, user_id)
        new = self.f("role")
        if m["role"] == "owner":
            self.conn.rollback()
            return self.render(f"Members of {org['name']}", self.members_body(org, role), 400,
                               "The owner's role cannot be changed.", "err")
        if new not in domain.INVITE_ROLES:
            self.conn.rollback()
            return self.render(f"Members of {org['name']}", self.members_body(org, role), 400,
                               "Choose a role of manager, box office or scanner.", "err")
        self.conn.execute("UPDATE memberships SET role = %s WHERE organization_id = %s AND user_id = %s",
                          (new, org["id"], m["user_id"]))
        self.conn.commit()
        from .web import redirect
        return redirect(f"/orgs/{org['id']}/members?msg=rolechanged")

    def memberremove_post(self, org_id, user_id):
        org, role = self.member(org_id, ("owner",), post=True)
        m = self._target_member(org, user_id)
        if m["role"] == "owner":
            self.conn.rollback()
            return self.render(f"Members of {org['name']}", self.members_body(org, role), 400,
                               "The owner cannot be removed.", "err")
        self.conn.execute("DELETE FROM memberships WHERE organization_id = %s AND user_id = %s", (org["id"], m["user_id"]))
        self.conn.commit()
        from .web import redirect
        return redirect(f"/orgs/{org['id']}/members?msg=removed")

    # =============================================================================================
    # Venues
    # =============================================================================================
    def _venue_form(self, org, v: dict | None = None) -> str:
        v = v or {}
        return ("<div class=card>" + self.form_html(
            f"/orgs/{org['id']}/venues/new",
            f'<label>Name <input type="text" name="name" value="{E(v.get("name", ""))}" required maxlength="100"></label>'
            f'<label>Address <input type="text" name="address" value="{E(v.get("address", ""))}" required maxlength="200"></label>'
            f'<label>Time zone (IANA name, like America/Chicago) <input type="text" name="timezone" '
            f'value="{E(v.get("timezone", ""))}" required list="tzs"></label>'
            '<datalist id="tzs"><option value="America/New_York"><option value="America/Chicago"><option value="America/Denver">'
            '<option value="America/Phoenix"><option value="America/Los_Angeles"><option value="Europe/London"></datalist>'
            '<button type="submit" data-action="create-venue">Create venue</button>') + "</div>")

    def venuenew_get(self, org_id):
        org, _ = self.member(org_id, MANAGE)
        return self.render("New venue", self._venue_form(org))

    def venuenew_post(self, org_id):
        org, _ = self.member(org_id, MANAGE, post=True)
        v = {k: self.f(k) for k in ("name", "address", "timezone")}
        tz = v["timezone"].strip()
        msg = ""
        if not valid_name(v["name"]):
            msg = "Enter a venue name of 1 to 100 characters, with no control characters."
        elif not valid_name(v["address"], 200):
            msg = "Enter the venue's address (1 to 200 characters, no control characters)."
        elif not known_timezone(tz):
            msg = "That time zone is not known. Use an IANA time zone name such as America/Chicago."
        if msg:
            return self.render("New venue", self._venue_form(org, v), 400, msg, "err")
        vid = self.conn.execute("INSERT INTO venues (organization_id, name, timezone, address) VALUES (%s,%s,%s,%s) "
                                "RETURNING id", (org["id"], v["name"], tz, v["address"])).fetchone()["id"]
        self.conn.commit()
        from .web import redirect
        return redirect(f"/orgs/{org['id']}/venues/{vid}")

    def _venue(self, org, venue_id):
        v = self.conn.execute("SELECT * FROM venues WHERE id = %s", (int(venue_id),)).fetchone()
        if not v or v["organization_id"] != org["id"]:
            raise self.not_found()
        return v

    def venue_body(self, org, v, f: dict | None = None) -> str:
        conn = self.conn
        fixed = conn.execute("SELECT 1 FROM events WHERE venue_id = %s LIMIT 1", (v["id"],)).fetchone() is not None
        secs = domain.venue_sections(conn, v["id"])
        items = []
        for s in secs:
            rows = f"{s['first_row']}-{s['last_row']}" if s["first_row"] else ""
            items.append(f'<li data-section="{E(s["code"])}" data-seat-count="{s["seat_count"]}"><b>{E(s["code"])}</b> · '
                         f'{E(s["name"])} · rows {E(rows)} · {s["per_row"] or 0} seats per row · {s["seat_count"]} seats'
                         + (f' · accessible: {E(s["acc"])}' if s["acc"] else "") + "</li>")
        body = (f'<div class="card" data-venue-id="{v["id"]}"><dl class=kv><dt>Address</dt><dd>{E(v["address"])}</dd>'
                f'<dt>Time zone</dt><dd>{E(v["timezone"])}</dd></dl><h2>Seat map</h2>'
                + (f'<ul class=plain>{"".join(items)}</ul>' if items else "<p>No sections yet.</p>") + "</div>")
        if not fixed:
            f = f or {}
            body += ("<div class=card><h2>Add a section</h2>" + self.form_html(
                f"/orgs/{org['id']}/venues/{v['id']}/sections",
                f'<label>Code (1 to 8 of A-Z, 0-9) <input type="text" name="code" value="{E(f.get("code", ""))}" required '
                'maxlength="8"></label>'
                f'<label>Name <input type="text" name="name" value="{E(f.get("name", ""))}" required maxlength="100"></label>'
                f'<label>Rows (like A-F) <input type="text" name="rows" value="{E(f.get("rows", ""))}" required></label>'
                f'<label>Seats per row (1 to 60) <input type="number" name="seats_per_row" min="1" max="60" '
                f'value="{E(f.get("seats_per_row", ""))}" required></label>'
                f'<label>Accessible seats (like A1 A2 F14) <input type="text" name="accessible" '
                f'value="{E(f.get("accessible", ""))}"></label>'
                '<button type="submit" data-action="add-section">Add section</button>')
                + '<p class="muted small">The seat map is fixed once any event is created at this venue.</p></div>')
        else:
            body += ("<div class=card><p class=muted>This venue's seat map is fixed because events have been created at it."
                     "</p></div>")
        return body

    def venue_get(self, org_id, venue_id):
        org, _ = self.member(org_id, MANAGE)
        v = self._venue(org, venue_id)
        return self.render(v["name"], self.venue_body(org, v))

    def sections_post(self, org_id, venue_id):
        import re
        org, _ = self.member(org_id, MANAGE, post=True)
        v = self._venue(org, venue_id)
        conn = self.conn
        conn.execute("SELECT pg_advisory_xact_lock(4, %s)", (v["id"],))
        f = {k: self.f(k) for k in ("code", "name", "rows", "seats_per_row", "accessible")}

        def fail(msg, status=400):
            conn.rollback()
            return self.render(v["name"], self.venue_body(org, v, f), status, msg, "err")
        if conn.execute("SELECT 1 FROM events WHERE venue_id = %s LIMIT 1", (v["id"],)).fetchone():
            return fail("This venue's seat map is fixed because an event has been created at it.", 409)
        code = f["code"].strip()
        if not SECTION_RE.fullmatch(code):
            return fail("The section code must be 1 to 8 characters of A-Z and 0-9.")
        if conn.execute("SELECT 1 FROM sections WHERE venue_id = %s AND code = %s", (v["id"], code)).fetchone():
            return fail(f"This venue already has a section {code}.", 409)
        if not valid_name(f["name"]):
            return fail("Enter a section name of 1 to 100 characters, with no control characters.")
        m = re.fullmatch(r"\s*([A-Z])\s*(?:-\s*([A-Z])\s*)?", f["rows"])
        if not m or (m[2] and m[2] < m[1]):
            return fail("Rows must be a range of capital letters like A-F.")
        rows = [chr(c) for c in range(ord(m[1]), ord(m[2] or m[1]) + 1)]
        per = parse_int(f["seats_per_row"], 1, 60)
        if per is None:
            return fail("Seats per row must be a whole number from 1 to 60.")
        acc = set()
        for tok in re.split(r"[\s,]+", f["accessible"].strip()):
            if not tok:
                continue
            am = re.fullmatch(r"([A-Z])(\d{1,3})", tok)
            if not am or am[1] not in rows or not 1 <= int(am[2]) <= per:
                return fail(f"{tok} is not a seat of this section; list accessible seats like A1 A2 F14.")
            acc.add((am[1], int(am[2])))
        sid = conn.execute("INSERT INTO sections (venue_id, code, name) VALUES (%s,%s,%s) RETURNING id",
                           (v["id"], code, f["name"])).fetchone()["id"]
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO seats (section_id, row_label, seat_number, accessible) VALUES (%s,%s,%s,%s)",
                            [(sid, r, n, (r, n) in acc) for r in rows for n in range(1, per + 1)])
        conn.commit()
        from .web import redirect
        return redirect(f"/orgs/{org['id']}/venues/{v['id']}?msg=created")

    # =============================================================================================
    # Events (organizer side)
    # =============================================================================================
    def _event_form(self, org, f: dict | None = None) -> str:
        conn = self.conn
        f = f or {}
        venues = conn.execute("SELECT * FROM venues WHERE organization_id = %s ORDER BY id", (org["id"],)).fetchall()
        sel = f.get("venue_id") or self.q("venue_id")
        opts = "".join(f'<option value="{v["id"]}"{" selected" if str(v["id"]) == sel else ""}>{E(v["name"])} '
                       f'({E(v["timezone"])})</option>' for v in venues)
        codes: dict[str, list[str]] = {}
        for v in venues:
            for s in domain.venue_sections(conn, v["id"]):
                codes.setdefault(s["code"], []).append(v["name"])
        prices = "".join(
            f'<label>Face price for section {E(c)} in cents (100 or more) <span class="muted small">— {E(", ".join(vs))}'
            f'</span><input type="number" name="price_{E(c)}" min="100" value="{E(f.get("price_" + c, ""))}"></label>'
            for c, vs in sorted(codes.items()))
        val = lambda k: E(f.get(k, ""))  # noqa: E731
        return ("<div class=card>" + self.form_html(
            f"/orgs/{org['id']}/events/new",
            f'<label>Venue <select name="venue_id" required>{opts}</select></label>'
            f'<label>Name <input type="text" name="name" value="{val("name")}" required maxlength="100"></label>'
            f'<label>Description <textarea name="description" maxlength="5000">{val("description")}</textarea></label>'
            '<p class="muted small">Times are wall-clock times at the venue, in its own time zone (YYYY-MM-DDTHH:MM).</p>'
            f'<label>Starts at <input type="datetime-local" name="starts_at" value="{val("starts_at")}" required></label>'
            f'<label>On sale from <input type="datetime-local" name="on_sale_at" value="{val("on_sale_at")}" required></label>'
            f'<label>Off sale at (optional; empty means sales end at the start) <input type="datetime-local" '
            f'name="off_sale_at" value="{val("off_sale_at")}"></label>'
            f'<label>Presale from (optional) <input type="datetime-local" name="presale_at" value="{val("presale_at")}"></label>'
            f'<label>Presale code (4 to 20 of A-Z, 0-9; required with a presale) <input type="text" name="presale_code" '
            f'value="{val("presale_code")}" maxlength="20"></label>'
            f'<label>Facility fee per ticket in cents <input type="number" name="facility_fee_cents" min="0" '
            f'value="{val("facility_fee_cents") or "0"}" required></label>{prices}'
            '<button type="submit" data-action="create-event">Create event (as a draft)</button>')
            + '<p class="muted small">A new event is a draft that only your organization can see until you publish it.</p>'
            "</div>")

    def eventnew_get(self, org_id):
        org, _ = self.member(org_id, MANAGE)
        return self.render("New event", self._event_form(org))

    def eventnew_post(self, org_id):
        org, _ = self.member(org_id, MANAGE, post=True)
        conn = self.conn
        f = {k: v[-1] for k, v in self.form.items()}

        def fail(msg):
            conn.rollback()
            return self.render("New event", self._event_form(org, f), 400, "Event not created: " + msg, "err")
        vid = parse_int(f.get("venue_id", ""))
        v = conn.execute("SELECT * FROM venues WHERE id = %s", (vid,)).fetchone() if vid and vid < 2 ** 31 else None
        if not v or v["organization_id"] != org["id"]:
            return fail("choose one of your organization's venues.")
        name, desc = f.get("name", ""), f.get("description", "").replace("\r\n", "\n")
        if not valid_name(name):
            return fail("enter a name of 1 to 100 characters, with no control characters.")
        if len(desc) > 5000 or any(ord(c) < 0x20 and c not in "\n\t" for c in desc):
            return fail("the description must be at most 5000 characters, with no control characters.")
        tz = v["timezone"]
        times = {}
        for key, label, required in (("starts_at", "start", True), ("on_sale_at", "on-sale time", True),
                                     ("off_sale_at", "off-sale time", False), ("presale_at", "presale time", False)):
            raw = f.get(key, "").strip()
            if not raw:
                if required:
                    return fail(f"enter the {label}.")
                times[key] = None
                continue
            inst, err = parse_wallclock(raw, tz)
            if err:
                return fail(f"the {label}: {err}")
            times[key] = inst
        start, on, off, pre = times["starts_at"], times["on_sale_at"], times["off_sale_at"], times["presale_at"]
        code = f.get("presale_code", "").strip().upper()
        if start <= self.now:
            return fail("the start must be in the future.")
        if pre is not None and not pre < on:
            return fail("the presale must start before the on-sale time.")
        if off is not None and not on < off:
            return fail("the on-sale time must be before the off-sale time.")
        if off is None and not on < start:
            return fail("the on-sale time must be before the start.")
        if off is not None and off > start:
            return fail("the off-sale time must be no later than the start.")
        if pre is not None and not code:
            return fail("a presale needs a presale code.")
        if pre is None and code:
            return fail("a presale code needs a presale time; clear the code or set the presale time.")
        if code and not CODE_RE.fullmatch(code):
            return fail("the presale code must be 4 to 20 characters of A-Z and 0-9.")
        fee_raw = f.get("facility_fee_cents", "").strip() or "0"
        fee = parse_int(fee_raw, 0, 10 ** 9)
        if fee is None:
            return fail("the facility fee must be a whole number of cents, 0 or more.")
        secs = domain.venue_sections(conn, v["id"])
        if not secs:
            return fail("this venue has no sections yet; add its seat map first.")
        prices = {}
        for s in secs:
            p = parse_int(f.get("price_" + s["code"], ""), 100, 10 ** 9)
            if p is None:
                return fail(f"enter a face price of at least 100 cents for section {s['code']}.")
            prices[s["id"]] = p
        conn.execute("SELECT pg_advisory_xact_lock(4, %s)", (v["id"],))
        from .util import zone
        loc = lambda d: d.astimezone(zone(tz)).replace(tzinfo=None) if d else None  # noqa: E731
        eid = conn.execute(
            "INSERT INTO events (organization_id, venue_id, name, description, starts_local, starts_fold, on_sale_local, "
            "on_sale_fold, status, facility_fee_cents) VALUES (%s,%s,%s,%s,%s,0,%s,0,'draft',%s) RETURNING id",
            (org["id"], v["id"], name, desc, loc(start), loc(on), fee)).fetchone()["id"]
        for sid, p in prices.items():
            conn.execute("INSERT INTO event_prices (event_id, section_id, price_cents) VALUES (%s,%s,%s)", (eid, sid, p))
        conn.execute("INSERT INTO sw_events (event_id, org_id, venue_id, legacy, status, starts_at, original_starts_at, "
                     "on_sale_at, off_sale_at, presale_at, presale_code, created_at) VALUES (%s,%s,%s,false,'draft',%s,%s,%s,"
                     "%s,%s,%s,%s)", (eid, org["id"], v["id"], start, start, on, off, pre, code or None, self.now))
        conn.commit()
        from .web import redirect
        return redirect(f"/orgs/{org['id']}/events/{eid}")

    def manage_body(self, org, role, ev) -> str:
        conn, t = self.conn, self.now
        tz = ev["timezone"]
        eid = ev["event_id"]
        base = f"/orgs/{org['id']}/events/{eid}"
        prices = domain.event_prices(conn, eid)
        secs = domain.venue_sections(conn, ev["venue_id"])
        ptxt = ", ".join(f"{E(s['code'])} {dollars(prices.get(s['id'], 0))}" for s in secs)
        info = (f'<div class="card" data-event-id="{eid}" data-event-status="{ev["status"]}" '
                f'data-starts-at="{instant(ev["starts_at"])}"><p>{badge(ev["status"])} '
                f'{badge(domain.sale_state(ev, t), STATE_TEXT[domain.sale_state(ev, t)])}</p><dl class=kv>'
                f'<dt>Venue</dt><dd>{E(ev["venue_name"])}</dd><dt>Starts</dt><dd>{when(ev["starts_at"], tz)}</dd>'
                f'<dt>On sale</dt><dd>{when(ev["on_sale_at"], tz) or "—"}</dd>'
                f'<dt>Sales end</dt><dd>{when(domain.sales_end(ev), tz)}</dd>'
                + (f'<dt>Presale</dt><dd>{when(ev["presale_at"], tz)} with code <code>{E(ev["presale_code"])}</code></dd>'
                   if ev["presale_at"] else "")
                + f'<dt>Prices</dt><dd>{ptxt}</dd><dt>Facility fee</dt><dd>{dollars(ev["facility_fee_cents"])} per ticket</dd>'
                f'</dl><p class=row><a href="/events/{eid}">Public page</a> · <a href="{base}/codes">Promo codes</a> · '
                f'<a href="{base}/orders">Orders</a> · <a href="{base}/dashboard">Dashboard</a> · '
                f'<a href="{base}/attendees.csv">Attendee export (CSV)</a> · <a href="{base}/door">Door list</a> · '
                f'<a href="{base}/devices">Scanner devices</a> · <a href="{base}/reminders">Reminders</a></p></div>')
        acts = ""
        if ev["status"] == "draft":
            acts += ("<div class=card><h2>Publish</h2><p>Publishing makes the event visible to everyone; sales follow its "
                     "sales window.</p>" + self.form_html(f"{base}/publish", '<button type="submit" '
                                                          'data-action="publish-event">Publish event</button>') + "</div>")
        if ev["status"] == "published" and t < ev["starts_at"]:
            acts += ("<div class=card><h2>Reschedule</h2><p>Enter the new start as a wall-clock time at the venue "
                     f"({E(tz)}). Orders already paid then get a full refund at any time before the new start. If sales "
                     "would end after the new start, they end at the new start.</p>" + self.form_html(
                         f"{base}/reschedule",
                         f'<label>New start <input type="datetime-local" name="starts_at" value="{local_input(ev["starts_at"], tz)}" '
                         'required></label><button type="submit" data-action="reschedule-event">Reschedule</button>')
                     + "</div>")
        if role == "owner" and ev["status"] == "published":
            ok, why = domain.can_cancel(conn, ev)
            if ok:
                acts += ('<div class=card><h2>Cancel event</h2><p><b>Cancelling cannot be undone.</b> Every ticket will be '
                         "refunded in full to the card that paid for it, every buyer and ticket holder will be emailed, "
                         "holds end at once, the waiting lists close and nothing more can be sold.</p>"
                         + self.form_html(f"{base}/cancel", '<button type="submit" class="danger" data-action="cancel-event">'
                                          "Cancel this event and refund everyone</button>") + "</div>")
            else:
                acts += f"<div class=card><h2>Cancel event</h2><p class=muted>{E(why)}</p></div>"
        return info + acts

    def manage_get(self, org_id, event_id):
        org, role = self.member(org_id, MANAGE)
        ev = self.org_event(org, event_id)
        return self.render(ev["name"], self.manage_body(org, role, ev))

    def _manage_refused(self, org, role, ev, r: Refused):
        self.conn.rollback()
        ev = domain.load_event(self.conn, ev["event_id"])
        return self.render(ev["name"], self.manage_body(org, role, ev), r.status, r.message, "err")

    def publish_post(self, org_id, event_id):
        org, role = self.member(org_id, MANAGE, post=True)
        ev = self.org_event(org, event_id)
        domain.lock_event(self.conn, ev["event_id"])
        ev = domain.load_event(self.conn, ev["event_id"])
        if ev["status"] != "draft":
            return self._manage_refused(org, role, ev, Refused("Only draft events can be published."))
        self.conn.execute("UPDATE sw_events SET status = 'published', published_at = %s WHERE event_id = %s",
                          (self.now, ev["event_id"]))
        self.conn.commit()
        from .web import redirect
        return redirect(f"/orgs/{org['id']}/events/{ev['event_id']}?msg=published")

    def reschedule_post(self, org_id, event_id):
        org, role = self.member(org_id, MANAGE, post=True)
        ev = self.org_event(org, event_id)
        domain.lock_event(self.conn, ev["event_id"])
        ev = domain.load_event(self.conn, ev["event_id"])
        if ev["status"] != "published" or self.now >= ev["starts_at"]:
            return self._manage_refused(org, role, ev, Refused("Only published events that have not started can be "
                                                               "rescheduled."))
        new, err = parse_wallclock(self.f("starts_at"), ev["timezone"])
        if err:
            return self._manage_refused(org, role, ev, Refused("Not rescheduled: " + err, 400))
        if new <= self.now:
            return self._manage_refused(org, role, ev, Refused("Not rescheduled: the new start must be in the future.", 400))
        if new != ev["starts_at"]:
            self.conn.execute("UPDATE sw_events SET starts_at = %s, rescheduled_at = %s WHERE event_id = %s",
                              (new, self.now, ev["event_id"]))
        self.conn.commit()
        from .web import redirect
        return redirect(f"/orgs/{org['id']}/events/{ev['event_id']}?msg=rescheduled")

    def cancel_post(self, org_id, event_id):
        org, role = self.member(org_id, ("owner",), post=True)
        ev = self.org_event(org, event_id)
        domain.lock_event(self.conn, ev["event_id"])
        ev = domain.load_event(self.conn, ev["event_id"])
        try:
            domain.cancel_event(self.conn, ev, self.now)
        except Refused as r:
            return self._manage_refused(org, role, ev, r)
        self.conn.commit()
        mail.KICK.set()
        from .web import redirect
        return redirect(f"/orgs/{org['id']}/events/{ev['event_id']}?msg=cancelled")

    # ---- promo codes ---------------------------------------------------------------------------------
    def promo_body(self, org, ev, f: dict | None = None) -> str:
        f = f or {}
        rows = []
        for c in self.conn.execute("SELECT * FROM sw_promo_codes WHERE event_id = %s ORDER BY code", (ev["event_id"],)):
            used = domain.code_in_use(self.conn, c["id"], self.now)
            val = f"{c['value']} %" if c["kind"] == "percent" else dollars(c["value"])
            mx = c["max_redemptions"] if c["max_redemptions"] is not None else "unlimited"
            rows.append(f'<tr data-code="{E(c["code"])}"><td><b>{E(c["code"])}</b></td><td>{E(c["kind"])}</td><td>{val}</td>'
                        f'<td>{dollars(c["min_subtotal"])}</td><td>{used} / {mx}</td></tr>')
        val = lambda k: E(f.get(k, ""))  # noqa: E731
        return ("<div class=card>" + (table(["Code", "Kind", "Value", "Minimum face subtotal", "Redemptions in use"], rows)
                                      if rows else "<p>No promo codes yet.</p>")
                + "</div><div class=card><h2>Create a code</h2>" + self.form_html(
                    f"/orgs/{org['id']}/events/{ev['event_id']}/codes",
                    f'<label>Code (4 to 20 of A-Z, 0-9) <input type="text" name="code" value="{val("code")}" required '
                    'maxlength="20"></label><label>Kind <select name="kind"><option value="percent">percent (value 1 to 100)'
                    '</option><option value="fixed">fixed (value in cents)</option></select></label>'
                    f'<label>Value <input type="number" name="value" min="1" value="{val("value")}" required></label>'
                    f'<label>Minimum face subtotal in cents <input type="number" name="min_subtotal_cents" min="0" '
                    f'value="{val("min_subtotal_cents") or "0"}"></label>'
                    f'<label>Maximum redemptions (empty for unlimited) <input type="number" name="max_redemptions" min="1" '
                    f'value="{val("max_redemptions")}"></label>'
                    '<button type="submit" data-action="create-code">Create code</button>') + "</div>")

    def promo_get(self, org_id, event_id):
        org, _ = self.member(org_id, MANAGE)
        ev = self.org_event(org, event_id)
        return self.render(f"Promo codes · {ev['name']}", self.promo_body(org, ev))

    def promo_post(self, org_id, event_id):
        org, _ = self.member(org_id, MANAGE, post=True)
        ev = self.org_event(org, event_id)
        conn = self.conn
        f = {k: self.f(k) for k in ("code", "kind", "value", "min_subtotal_cents", "max_redemptions")}

        def fail(msg, status=400):
            conn.rollback()
            return self.render(f"Promo codes · {ev['name']}", self.promo_body(org, ev, f), status, "Code not created: " + msg,
                               "err")
        code = f["code"].strip().upper()
        if not CODE_RE.fullmatch(code):
            return fail("the code must be 4 to 20 characters of A-Z and 0-9.")
        kind = f["kind"].strip()
        if kind not in ("percent", "fixed"):
            return fail("choose percent or fixed.")
        value = parse_int(f["value"], 1, 100 if kind == "percent" else 10 ** 9)
        if value is None:
            return fail("a percent code takes a value from 1 to 100; a fixed code takes 1 cent or more.")
        mins = parse_int(f["min_subtotal_cents"].strip() or "0", 0, 10 ** 12)
        if mins is None:
            return fail("the minimum face subtotal must be a whole number of cents, 0 or more.")
        mx = None
        if f["max_redemptions"].strip():
            mx = parse_int(f["max_redemptions"], 1, 10 ** 9)
            if mx is None:
                return fail("the maximum number of redemptions must be 1 or more (or empty for unlimited).")
        ins = conn.execute("INSERT INTO sw_promo_codes (event_id, code, kind, value, min_subtotal, max_redemptions) "
                           "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (event_id, code) DO NOTHING RETURNING id",
                           (ev["event_id"], code, kind, value, mins, mx)).fetchone()
        if not ins:
            return fail(f"this event already has a code {code}.", 409)
        conn.commit()
        return self.render(f"Promo codes · {ev['name']}", self.promo_body(org, ev), 200, f"Code {code} created.", "ok")

    # ---- orders list and organizer refunds -----------------------------------------------------------------
    def orgorders_body(self, org, ev) -> str:
        conn = self.conn
        seats = {s["id"]: s for s in domain.venue_seats(conn, ev["venue_id"])}
        orders = conn.execute("SELECT * FROM sw_orders WHERE event_id = %s ORDER BY id", (ev["event_id"],)).fetchall()
        tickets: dict[int, list] = {}
        for t in conn.execute("SELECT * FROM sw_tickets WHERE event_id = %s", (ev["event_id"],)):
            tickets.setdefault(t["order_id"], []).append(t)
        blocks = []
        for o in orders:
            st = domain.order_status(o, self.now)
            ts = sorted(tickets.get(o["id"], []), key=lambda t: (seats[t["seat_id"]]["key"], t["id"]))
            trs = []
            for t in ts:
                btn = ""
                if t["status"] == "valid" and not t["legacy"]:
                    btn = self.form_html(f"/orgs/{org['id']}/tickets/{t['id']}/refund",
                                         f'<button type="submit" class="danger" data-action="organizer-refund">Refund '
                                         f'{dollars(t["total"])}</button>', "inline")
                trs.append(f'<li data-ticket-id="{t["id"]}" data-ticket-status="{t["status"]}">Ticket {t["id"]} · '
                           f'<b>{E(seats[t["seat_id"]]["label"])}</b> · {E(t["holder_name"])} {badge(t["status"])} · '
                           f'{dollars(t["total"])}{" · old platform" if t["legacy"] else ""} {btn}</li>')
            amount = o["total_cents"] if o["total_cents"] is not None else ""
            blocks.append(f'<div class="section-block" data-order-id="{o["id"]}"><b>Order {o["id"]}</b> {badge(st)} · '
                          f'{E(o["buyer_name"])} ({E(o["buyer_email_norm"])})'
                          + (f" · {dollars(amount)}" if amount != "" else "")
                          + (f'<ul class=plain>{"".join(trs)}</ul>' if trs else "") + "</div>")
        return ('<div class=card><p class="muted small">Refunding a ticket returns its full total to the original payment '
                "and makes the ticket refund pending until the payment provider confirms; the buyer is emailed when it "
                "completes. Tickets sold by the old platform cannot be refunded here.</p>"
                + ("".join(blocks) if blocks else "<p>No orders yet.</p>") + "</div>")

    def orgorders_get(self, org_id, event_id):
        org, _ = self.member(org_id, BOX)
        ev = self.org_event(org, event_id)
        return self.render(f"Orders · {ev['name']}", self.orgorders_body(org, ev))

    def orgrefund_post(self, org_id, ticket_id):
        org, _ = self.member(org_id, BOX, post=True)
        t = self.conn.execute("SELECT t.*, se.org_id FROM sw_tickets t JOIN sw_events se ON se.event_id = t.event_id "
                              "WHERE t.id = %s", (int(ticket_id),)).fetchone()
        if not t or t["org_id"] != org["id"]:
            raise self.not_found()
        ev = domain.load_event(self.conn, t["event_id"])
        try:
            domain.organizer_refund(self.conn, t["id"])
        except Refused as r:
            self.conn.rollback()
            return self.render(f"Orders · {ev['name']}", self.orgorders_body(org, ev), r.status, r.message, "err")
        self.conn.commit()
        from .web import redirect
        return redirect(f"/orgs/{org['id']}/events/{ev['event_id']}/orders?msg=orgrefund")

    # ---- dashboard and export --------------------------------------------------------------------------
    def dashboard_get(self, org_id, event_id):
        org, _ = self.member(org_id, BOX)
        ev = self.org_event(org, event_id)
        total, secs = domain.dashboard(self.conn, ev, self.now)
        money_keys = ("gross", "refunded", "net")

        def attrs(f):
            return (f'data-capacity="{f["capacity"]}" data-sold="{f["sold"]}" data-held="{f["held"]}" '
                    f'data-available="{f["available"]}" data-admitted="{f["admitted"]}" data-gross-cents="{f["gross"]}" '
                    f'data-refunded-cents="{f["refunded"]}" data-net-cents="{f["net"]}"')
        labels = [("capacity", "Capacity"), ("sold", "Sold"), ("held", "Held"), ("available", "Available"),
                  ("admitted", "Admitted"), ("gross", "Gross"), ("refunded", "Refunded"), ("net", "Net")]
        figs = "".join(f'<div class="fig"><span class="muted small">{lab}</span><b>'
                       f'{dollars(total[k]) if k in money_keys else total[k]}</b></div>' for k, lab in labels)
        rows = []
        for sec, f in secs:
            cells = "".join(f'<td class=num>{dollars(f[k]) if k in money_keys else f[k]}</td>' for k, _ in labels)
            rows.append(f'<tr data-section="{E(sec["code"])}" {attrs(f)}><td><b>{E(sec["code"])}</b> {E(sec["name"])}</td>'
                        f"{cells}</tr>")
        cells = "".join(f'<td class=num><b>{dollars(total[k]) if k in money_keys else total[k]}</b></td>' for k, _ in labels)
        rows.append(f"<tr><td><b>Whole event</b></td>{cells}</tr>")
        body = (f'<div class="card" data-dashboard-event-id="{ev["event_id"]}" {attrs(total)}><p><b>{E(ev["name"])}</b> · '
                f'{when(ev["starts_at"], ev["timezone"])} {badge(ev["status"])}</p><p class="muted small">Figures as of '
                f"{instant(self.now)}. Sold = valid and refund-pending tickets; held = seats in unexpired holds and open "
                "waiting-list offers; available = capacity − sold − held; gross = every payment received; refunded = "
                f"completed refunds; net = gross − refunded.</p><div class=figs>{figs}</div></div>"
                "<div class=card><h2>By section</h2>"
                + table(["Section"] + [lab for _, lab in labels], rows, tuple(range(1, 9))) + "</div>")
        return self.render(f"Dashboard · {ev['name']}", body)

    def export_get(self, org_id, event_id):
        from .web import Resp
        org, _ = self.member(org_id, MANAGE)
        ev = self.org_event(org, event_id)
        data = domain.attendees_csv(self.conn, ev)
        return Resp(200, data, {"Content-Disposition": f'attachment; filename="attendees-event-{ev["event_id"]}.csv"'},
                    ctype="text/csv; charset=utf-8")

    # ---- door list and check-in -------------------------------------------------------------------------
    def door_body(self, org, ev) -> str:
        conn = self.conn
        seats = {s["id"]: s for s in domain.venue_seats(conn, ev["venue_id"])}
        ts = conn.execute("SELECT * FROM sw_tickets WHERE event_id = %s", (ev["event_id"],)).fetchall()
        ts.sort(key=lambda t: (seats[t["seat_id"]]["key"], t["id"]))
        tz = ev["timezone"]
        rows = []
        for t in ts:
            btn = ""
            if t["status"] == "valid" and t["admitted_at"] is None:
                btn = self.form_html(f"/orgs/{org['id']}/tickets/{t['id']}/checkin",
                                     '<button type="submit" data-action="door-checkin">Check in</button>', "inline")
            adm = (f'{E(local_text(t["admitted_at"], tz))} · {E(t["admitted_gate"])}' if t["admitted_at"] else "—")
            rows.append(f'<tr data-ticket-id="{t["id"]}" data-ticket-status="{t["status"]}" '
                        f'data-admitted-at="{instant(t["admitted_at"])}"><td><b>{E(seats[t["seat_id"]]["label"])}</b><br>'
                        f'<span class="muted small">ticket {t["id"]}</span></td><td>{E(t["holder_name"])}</td>'
                        f'<td>{badge(t["status"])}</td><td>{adm}</td><td>{btn}</td></tr>')
        return (f'<div class=card><p><b>{E(ev["name"])}</b> · {when(ev["starts_at"], tz)} · doors admit from 3 hours before '
                "the start until 6 hours after it.</p>"
                + (table(["Seat", "Holder", "Status", "Admission", ""], rows) if rows else "<p>No tickets yet.</p>")
                + "</div>")

    def door_get(self, org_id, event_id):
        org, _ = self.member(org_id)
        ev = self.org_event(org, event_id)
        return self.render(f"Door list · {ev['name']}", self.door_body(org, ev))

    def checkin_post(self, org_id, ticket_id):
        org, _ = self.member(org_id, None, post=True)
        t = self.conn.execute("SELECT t.*, se.org_id FROM sw_tickets t JOIN sw_events se ON se.event_id = t.event_id "
                              "WHERE t.id = %s", (int(ticket_id),)).fetchone()
        if not t or t["org_id"] != org["id"]:
            raise self.not_found()
        ev = domain.load_event(self.conn, t["event_id"])
        row = {"scan_id": None, "token": domain.current_token(t), "gate": "Door",
               "scanned_at": self.now.replace(microsecond=0), "raw": None}
        res = domain.run_scans(self.conn, ev["event_id"], [row], None, self.now)[0]
        self.conn.commit()
        label = domain.ticket_label(self.conn, t)
        if res["result"] == "admitted":
            msg, kind, status = f"{label} admitted at the door.", "ok", 200
        elif res["result"] == "already_admitted":
            msg, kind, status = f"{label} was already admitted ({res['admitted_at']}, gate {res['gate']}).", "", 200
        else:
            why = {"cancelled": "the event is cancelled", "refunded": "the ticket is refunded or has a refund pending",
                   "outside_window": "doors admit only from 3 hours before the start until 6 hours after it",
                   "transferred": "the ticket was transferred", "invalid": "the ticket is not valid",
                   "wrong_event": "the ticket is for another event"}.get(res["reason"], res["reason"])
            msg, kind, status = f"{label} not admitted: {why}.", "err", 409
        return self.render(f"Door list · {ev['name']}", self.door_body(org, ev), status, msg, kind)

    # ---- scanner devices --------------------------------------------------------------------------------
    def devices_body(self, org, ev, created: str = "") -> str:
        rows = []
        for d in self.conn.execute("SELECT * FROM sw_devices WHERE event_id = %s ORDER BY id", (ev["event_id"],)):
            rv = ' data-revoked="true"' if d["revoked_at"] else ""
            btn = "" if d["revoked_at"] else self.form_html(
                f"/orgs/{org['id']}/events/{ev['event_id']}/devices/{d['id']}/revoke",
                '<button type="submit" class="danger" data-action="revoke-device">Revoke</button>', "inline")
            state = f"revoked {instant(d['revoked_at'])}" if d["revoked_at"] else "active"
            rows.append(f'<li data-device-id="{d["id"]}"{rv}><b>{E(d["label"])}</b> · device {d["id"]} · {state} {btn}</li>')
        return (created + "<div class=card><h2>Devices</h2>" + (f'<ul class=plain>{"".join(rows)}</ul>' if rows else
                                                               "<p>No devices yet.</p>")
                + "</div><div class=card><h2>Add a scanner device</h2>" + self.form_html(
                    f"/orgs/{org['id']}/events/{ev['event_id']}/devices",
                    '<label>Label <input type="text" name="label" required maxlength="100"></label>'
                    '<button type="submit" data-action="create-device">Create device</button>')
                + '<p class="muted small">The device key is shown once, right after creation. Scanners send it as '
                  "<code>Authorization: Bearer &lt;key&gt;</code> to <code>/api/scan</code> and "
                  "<code>/api/scan/batch</code>. Revoking a device stops its key at once.</p></div>")

    def devices_get(self, org_id, event_id):
        org, _ = self.member(org_id, MANAGE)
        ev = self.org_event(org, event_id)
        return self.render(f"Scanner devices · {ev['name']}", self.devices_body(org, ev))

    def devices_post(self, org_id, event_id):
        org, _ = self.member(org_id, MANAGE, post=True)
        ev = self.org_event(org, event_id)
        label = self.f("label")
        if not valid_name(label):
            return self.render(f"Scanner devices · {ev['name']}", self.devices_body(org, ev), 400,
                               "Enter a label of 1 to 100 characters, with no control characters.", "err")
        secret = secrets.token_urlsafe(32)
        did = self.conn.execute("INSERT INTO sw_devices (org_id, event_id, label, key_hash) VALUES (%s,%s,%s,%s) RETURNING id",
                                (org["id"], ev["event_id"], label, hash_secret(secret))).fetchone()["id"]
        self.conn.commit()
        key = f"swd_{did}_{secret}"
        created = (f'<div class="card" data-device-id="{did}"><p><b>Device “{E(label)}” created.</b> Copy its key now: it '
                   f'is shown only this once.</p><code class="token" data-device-key="{E(key)}">{E(key)}</code></div>')
        return self.render(f"Scanner devices · {ev['name']}", self.devices_body(org, ev, created), 200)

    def revoke_post(self, org_id, event_id, device_id):
        org, _ = self.member(org_id, MANAGE, post=True)
        ev = self.org_event(org, event_id)
        d = self.conn.execute("SELECT * FROM sw_devices WHERE id = %s", (int(device_id),)).fetchone()
        if not d or d["event_id"] != ev["event_id"] or d["org_id"] != org["id"]:
            raise self.not_found()
        self.conn.execute("UPDATE sw_devices SET revoked_at = COALESCE(revoked_at, %s) WHERE id = %s", (self.now, d["id"]))
        self.conn.commit()
        from .web import redirect
        return redirect(f"/orgs/{org['id']}/events/{ev['event_id']}/devices?msg=revoked")

    # ---- reminders -------------------------------------------------------------------------------------
    def reminders_body(self, org, ev, subject: str = "", body: str = "") -> str:
        key = secrets.token_urlsafe(24)
        n = len({r["holder_email_norm"] for r in self.conn.execute(
            "SELECT holder_email_norm FROM sw_tickets WHERE event_id = %s AND status = 'valid'", (ev["event_id"],))})
        return ("<div class=card>" + f"<p>A reminder goes by email to each of the {n} addresses that hold a valid ticket "
                "for this event, once per address.</p>" + self.form_html(
                    f"/orgs/{org['id']}/events/{ev['event_id']}/reminders",
                    f'<input type="hidden" name="reminder_key" value="{E(key)}" data-reminder-key>'
                    f'<label>Subject (1 to 150 characters) <input type="text" name="subject" value="{E(subject)}" required '
                    'maxlength="150"></label>'
                    f'<label>Message <textarea name="body">{E(body)}</textarea></label>'
                    '<button type="submit" data-action="send-reminder">Send reminder</button>') + "</div>")

    def reminders_get(self, org_id, event_id):
        org, _ = self.member(org_id, MANAGE)
        ev = self.org_event(org, event_id)
        return self.render(f"Reminders · {ev['name']}", self.reminders_body(org, ev))

    def reminders_post(self, org_id, event_id):
        org, _ = self.member(org_id, MANAGE, post=True)
        ev = self.org_event(org, event_id)
        subject, body, key = self.f("subject"), self.f("body").replace("\r\n", "\n"), self.f("reminder_key")
        if not valid_name(subject, 150):
            return self.render(f"Reminders · {ev['name']}", self.reminders_body(org, ev, subject, body), 400,
                               "Enter a subject of 1 to 150 characters, with no control characters.", "err")
        if not key or len(key) > 200:
            return self.render(f"Reminders · {ev['name']}", self.reminders_body(org, ev, subject, body), 400,
                               "This form is missing its one-time key; reload the page and try again.", "err")
        n = domain.send_reminder(self.conn, ev, key, subject, body, self.user["id"])
        self.conn.commit()
        mail.KICK.set()
        if n is None:
            return self.render(f"Reminders · {ev['name']}", self.reminders_body(org, ev), 200,
                               "This reminder was already sent; nothing more was sent.", "")
        return self.render(f"Reminders · {ev['name']}", self.reminders_body(org, ev), 200,
                           f"Reminder sent to {n} address{'es' if n != 1 else ''}.", "ok")
