"""Seatwise workflows W01-W05: accounts and sessions, organizations and roles, venues/events/DST,
sales windows, the purchase path. Each workflow is strict: it passes only when every substep passes."""

from __future__ import annotations

import re
import secrets
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import CARD_OK, H, PASSWORD, REDIRECTS, exact_wall, next_transition, now, page_attr  # noqa: E402
from kitlib.web import FormNotFound, form_fields, submit_form, visible_text  # noqa: E402
from kitlib.wf import StepFailed, workflow  # noqa: E402
from reference.model import inst, instant_to_wall, norm, parse_inst, wall_to_instant  # noqa: E402

UTC = timezone.utc


def norm_text(t: str, *remove: str) -> str:
    for r in remove:
        if r:
            t = t.replace(r, "").replace(r.lower(), "").replace(r.strip(), "")
    return re.sub(r"\s+", " ", t).strip().lower()


def page_text(r) -> str:
    return visible_text(r.text) if r is not None else ""


# ======================================================================================== W01
@workflow("W01", "Accounts and sessions: verification, reset links, refusals, session rotation, sign-out, next=", 4)
def w01(ctx):
    st = ctx.shared.setdefault("w01", {})
    with ctx.step("sign-up sends a verification link; sign-in is refused until it is followed; addresses match ignoring case and spaces"):
        addr = f"Mixed.{H.nonce}@Verify.Example"
        before = H.mail_ids(addr)
        r = H.signup("  " + addr + " ", "Marta Quinn")
        ctx.check(r.status < 400, f"sign-up answered {r.status}")
        ctx.check(H.client().get("/signup", follow=True).dom.attr_one("data-action", "signup") is not None, "no signup button")
        msgs = H.wait_mail(addr, "verify", before)
        link = H.link_in(msgs, "/verify/")
        ctx.check(link, "no verification email with a link reached the address (normalized)")
        c0, _ = H.signin(addr.lower())
        ctx.check(not H.signed_in(c0), "an unverified account signed in")
        ctx.check(H.client().get(link, follow=True).status < 400, "the verification link failed")
        c1, _ = H.signin("  " + addr.upper() + " ")
        ctx.check(H.signed_in(c1), "a verified account could not sign in with a differently cased, padded address")
        st["a"] = norm(addr)
        H.clients[norm(addr)] = c1
        H.names[norm(addr)] = "Marta Quinn"
        ctx.surface("signup")
    with ctx.step("signing up again before verifying replaces the link and password; an old or expired link verifies nothing"):
        b = f"replace.{H.nonce}@verify.example"
        before = H.mail_ids(b)
        H.signup(b, "First Name", "First-Password-1")
        l1 = H.link_in(H.wait_mail(b, "verify", before), "/verify/")
        before2 = H.mail_ids(b)
        H.signup(b, "Second Name", "Second-Password-2")
        l2 = H.link_in(H.wait_mail(b, "verify", before2), "/verify/")
        ctx.check(l1 and l2 and l1 != l2, "the second sign-up did not send a new, different link")
        H.client().get(l1, follow=True)
        cx, _ = H.signin(b, "Second-Password-2")
        ctx.check(not H.signed_in(cx), "the replaced verification link verified the account")
        H.client().get(l2, follow=True)
        cy, _ = H.signin(b, "Second-Password-2")
        ctx.check(H.signed_in(cy), "the newest verification link did not verify the account")
        cz, _ = H.signin(b, "First-Password-1")
        ctx.check(not H.signed_in(cz), "the replaced password still signs in")
        c = f"expire.{H.nonce}@verify.example"
        before3 = H.mail_ids(c)
        H.signup(c, "Late Person", "Late-Password-3")
        l3 = H.link_in(H.wait_mail(c, "verify", before3), "/verify/")
        ctx.check(l3, "no link for the expiry check")
        time.sleep(H.link_ttl + 3)
        H.client().get(l3, follow=True)
        cw, _ = H.signin(c, "Late-Password-3")
        ctx.check(not H.signed_in(cw), "an expired verification link verified the account")
    with ctx.step("sign-up is refused for a verified address, an organization member's address, and a name with a control character"):
        a = st["a"]
        before = H.mail_ids(a)
        H.signup(a.upper(), "Someone Else", "Hijack-Password-9")
        time.sleep(3)
        cv, _ = H.signin(a, "Hijack-Password-9")
        ctx.check(not H.signed_in(cv), "a second sign-up took over a verified account's password")
        ctx.check(not H.new_mail(a, "verify", before), "a verification email went to an already verified address")
        staff = "theo.marsh@lanternroom.example"
        sb = H.mail_ids(staff)
        H.signup(staff, "Not Theo", "Member-Password-7")
        time.sleep(3)
        ctx.check(not H.new_mail(staff, "verify", sb), "sign-up with an organization member's address sent a verification link")
        cm, _ = H.signin(staff, "Member-Password-7")
        ctx.check(not H.signed_in(cm), "sign-up created an account for an organization member's address")
        bad = f"bell.{H.nonce}@verify.example"
        H.signup(bad, "Bell\x07Name", "Bell-Password-1")
        time.sleep(3)
        ctx.check(not H.mail_ids(bad), "a name with a control character was accepted (a verification email was sent)")
    with ctx.step("password reset: same answer for known and unknown addresses; single use; replaced; expired; unknown link 404"):
        a = st["a"]
        before = H.mail_ids(a)
        rk = submit_form(H.client(), "/password-reset", "request-reset", {"email": a.upper()}, follow=True)
        ghost = f"nobody.{H.nonce}@verify.example"
        ru = submit_form(H.client(), "/password-reset", "request-reset", {"email": ghost}, follow=True)
        ctx.eq(ru.status, rk.status, "status for an unknown address")
        tk, tu = norm_text(page_text(rk), a.upper(), a), norm_text(page_text(ru), ghost)
        ctx.check(tk == tu, f"messages differ: known={tk[:160]!r} unknown={tu[:160]!r}")
        l1 = H.link_in(H.wait_mail(a, "reset", before), "/password-reset/")
        ctx.check(l1, "no reset email reached the known address")
        time.sleep(2)
        ctx.eq(len(H.mail.to(ghost)), 0, "emails to an unknown address")
        before = H.mail_ids(a)
        submit_form(H.client(), "/password-reset", "request-reset", {"email": a}, follow=True)
        l2 = H.link_in(H.wait_mail(a, "reset", before), "/password-reset/")
        ctx.check(l2 and l2 != l1, "no second, different reset link")
        cl = H.client()
        page = cl.get(l1, follow=True)
        if page.status == 200:
            try:
                submit_form(cl, l1, "set-password", {"password": "Old-Link-Pass-11"}, follow=True, page=page)
            except FormNotFound:
                pass
        co, _ = H.signin(a, "Old-Link-Pass-11")
        ctx.check(not H.signed_in(co), "a replaced reset link set a working password")
        cl = H.client()
        page = cl.get(l2, follow=True)
        ctx.eq(page.status, 200, "newest reset link page status")
        submit_form(cl, l2, "set-password", {"password": "Newest-Pass-22"}, follow=True, page=page)
        cn, _ = H.signin(a, "Newest-Pass-22")
        ctx.check(H.signed_in(cn), "the newest reset link did not set a working password")
        page = H.client().get(l2, follow=True)
        if page.status == 200:
            try:
                submit_form(H.client(), l2, "set-password", {"password": "Second-Use-33"}, follow=True, page=page)
            except FormNotFound:
                pass
        cs, _ = H.signin(a, "Second-Use-33")
        ctx.check(not H.signed_in(cs), "a used reset link set a password again")
        H.secrets.extend([l1.rsplit("/", 1)[-1], l2.rsplit("/", 1)[-1], "Newest-Pass-22"])
        before = H.mail_ids(a)
        submit_form(H.client(), "/password-reset", "request-reset", {"email": a}, follow=True)
        l3 = H.link_in(H.wait_mail(a, "reset", before), "/password-reset/")
        time.sleep(H.link_ttl + 3)
        page = H.client().get(l3, follow=True)
        if page.status == 200:
            try:
                submit_form(H.client(), l3, "set-password", {"password": "Expired-Link-44"}, follow=True, page=page)
            except FormNotFound:
                pass
        ce, _ = H.signin(a, "Expired-Link-44")
        ctx.check(not H.signed_in(ce), "an expired reset link set a password")
        ctx.eq(H.client().get("/password-reset/" + secrets.token_urlsafe(24)).status, 404, "unknown reset link status")
        ok = H.reset_password(a, PASSWORD)
        ctx.check(ok, "could not restore the password")
    with ctx.step("a wrong password and an unknown address are refused with the same message and no session"):
        a = st["a"]
        c1, r1 = H.signin(a, "Wrong-Password-1")
        ghost = f"ghost.{H.nonce}@verify.example"
        c2, r2 = H.signin(ghost, "Wrong-Password-1")
        ctx.check(not H.signed_in(c1) and not H.signed_in(c2), "a refused sign-in produced a session")
        p1 = r1 if r1.status not in REDIRECTS else c1.get(r1.location, follow=True)
        p2 = r2 if r2.status not in REDIRECTS else c2.get(r2.location, follow=True)
        t1, t2 = norm_text(page_text(p1), a), norm_text(page_text(p2), ghost)
        ctx.check(t1 == t2, f"refusal messages differ: {t1[:160]!r} vs {t2[:160]!r}")
    with ctx.step("signing in starts a new session identifier; an identifier held before never becomes signed in; cookie flags"):
        a = st["a"]
        c = H.client()
        c.get("/signin", follow=True)
        c.get("/signup", follow=True)
        planted = dict(c.cookies)
        _, r = H.signin(a, client=c)
        ctx.check(H.signed_in(c), "sign-in failed")
        if planted:
            pre = H.client()
            pre.cookies = dict(planted)
            if H.signed_in(pre):
                ctx.breach("X9", "a session identifier held before sign-in became signed in")
                raise StepFailed("a session identifier held before sign-in became signed in")
            ctx.check(any(planted.get(k) != v for k, v in c.cookies.items()), "no cookie changed at sign-in")
        names = H.session_cookie_names(c)      # the cookie(s) that alone keep this client signed in
        set_cookies = [h for h in r.header_all("Set-Cookie") if h.split(";")[0].split("=", 1)[0].strip() in names
                       and "=" in h.split(";")[0] and h.split(";")[0].split("=", 1)[1]]
        ctx.check(set_cookies, "sign-in set no session cookie")
        ctx.check(all("httponly" in h.lower() for h in set_cookies), "a session cookie set at sign-in is not HttpOnly")
        ctx.check(all(re.search(r"samesite=(lax|strict)", h.lower()) for h in set_cookies), "a session cookie is not SameSite=Lax/Strict")
        st["client"] = c
    with ctx.step("signing out ends the session on the server"):
        c = st["client"]
        copy = c.copy()
        submit_form(c, "/tickets", "signout", {}, follow=True)
        ctx.check(not H.signed_in(c), "still signed in after sign-out")
        if H.signed_in(copy):
            ctx.breach("X9", "a session cookie copied before sign-out still signs in")
            raise StepFailed("a cookie copied before sign-out still works")
    with ctx.step("a visitor sent to sign in returns to the page; next= never leads to another site"):
        a = st["a"]
        target = "/tickets?from=wf"
        c = H.client()
        r = c.get(target, follow=False)
        ctx.check(r.status in REDIRECTS and "/signin" in (r.location or ""), f"visitor answered {r.status}")
        page = c.get(r.location, follow=True)
        r2 = submit_form(c, "", "signin", {"email": a, "password": PASSWORD}, page=page)
        loc = urllib.parse.urlsplit(urllib.parse.urljoin(H.base + "/", r2.location or ""))
        ctx.eq(loc.path + ("?" + loc.query if loc.query else ""), target, "where sign-in returned the visitor")
        base_host = urllib.parse.urlsplit(H.base).netloc
        for evil in ("//evil.example/x", "/\\evil.example/x", "https://evil.example/x", "http:evil.example",
                     "\\\\evil.example", "///evil.example", "https:/evil.example", "/%2F%2Fevil.example"):
            c = H.client()
            page = c.get("/signin?next=" + urllib.parse.quote(evil, safe=""), follow=True)
            r = submit_form(c, "", "signin", {"email": a, "password": PASSWORD}, page=page)
            loc = (r.location or "").strip()
            nl = loc.replace("\\", "/")
            dest = urllib.parse.urlsplit(urllib.parse.urljoin(H.base + "/", nl))
            offsite = bool(loc) and (nl.startswith("//") or (re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", nl) and not nl.startswith(H.base + "/"))
                                     or (dest.netloc and dest.netloc != base_host))
            if offsite:
                ctx.breach("X11", f"sign-in redirected to {loc!r} for next={evil!r}")
                raise StepFailed(f"open redirect to {loc!r} for next={evil!r}")
            ctx.check(r.status in REDIRECTS, f"sign-in answered {r.status} for next={evil!r}")


# ======================================================================================== W02
ROLE_GETS = [  # path template, roles allowed
    ("/orgs/{o}/venues/new", {"owner", "manager"}),
    ("/orgs/{o}/events/new", {"owner", "manager"}),
    ("/orgs/{o}/events/{e}/codes", {"owner", "manager"}),
    ("/orgs/{o}/events/{e}/devices", {"owner", "manager"}),
    ("/orgs/{o}/events/{e}/dashboard", {"owner", "manager", "box_office"}),
    ("/orgs/{o}/events/{e}/orders", {"owner", "manager", "box_office"}),
    ("/orgs/{o}/events/{e}/attendees.csv", {"owner", "manager"}),
    ("/orgs/{o}/events/{e}/reminders", {"owner", "manager"}),
    ("/orgs/{o}/events/{e}/door", {"owner", "manager", "box_office", "scanner"}),
    ("/orgs/{o}", {"owner", "manager", "box_office", "scanner"}),
]


def member_rows(c, org: int) -> dict:
    """email (data-member-email) -> (user id, role) on the members page."""
    page = c.get(f"/orgs/{org}/members", follow=True)
    out = {}
    for n in page.dom.attr_all("data-member-user-id"):
        out[norm(n.attrs.get("data-member-email", ""))] = (int(n.attrs["data-member-user-id"]), n.attrs.get("data-role"))
    return out


@workflow("W02", "Organizations: invitations, the five-role permission matrix, cross-organization isolation", 4)
def w02(ctx):
    st = ctx.shared.setdefault("w02", {})
    with ctx.step("an account creates an organization and becomes its owner"):
        w = H.quick_world("roles", "America/New_York")
        page = w["owner"].get(f"/orgs/{w['org']}", follow=True)
        ctx.check(page.dom.attr_one("data-org-id", str(w["org"])) is not None and page_attr(page.dom, "data-my-role") == "owner",
                  "organization home lacks data-org-id/data-my-role=owner")
        st["w"] = w
    with ctx.step("the owner invites a manager, box office and two scanners; invitations are emailed and accepted"):
        w = st["w"]
        roles = {}
        for role in ("manager", "box_office", "scanner", "scanner"):
            email, c = H.add_member(w["owner"], w["org"], role)
            roles.setdefault(role, []).append((email, c))
        rows = member_rows(w["owner"], w["org"])
        for role, lst in roles.items():
            for email, _c in lst:
                ctx.check(email in rows and rows[email][1] == role, f"{email} is not listed as {role}: {rows.get(email)}")
        st["roles"] = roles
        st["rows"] = rows
    with ctx.step("an invitation never makes an owner; an existing member cannot be invited again; a token-less invite is refused"):
        w = st["w"]
        email, c = H.account(tag="wouldbeowner")
        before = H.mail_ids(email)
        H.invite(w["owner"], w["org"], email, "owner")
        H.invite(w["owner"], w["org"], email, "admin")
        time.sleep(3)
        link = H.link_in(H.new_mail(email, "invitation", before), "/invites/")
        if link:
            page = c.get(link, follow=True)
            try:
                submit_form(c, link, "accept-invite", {}, follow=True, page=page)
            except FormNotFound:
                pass
            if c.get(f"/orgs/{w['org']}", follow=True).dom.attr_one("data-my-role", "owner"):
                ctx.breach("X5", "an invitation made a second owner")
            raise StepFailed("an invitation with role owner/admin was sent")
        mgr_email = st["roles"]["manager"][0][0]
        b2 = H.mail_ids(mgr_email)
        H.invite(w["owner"], w["org"], mgr_email, "scanner")
        time.sleep(3)
        ctx.check(not H.new_mail(mgr_email, "invitation", b2), "an existing member was invited again")
        ctx.eq(member_rows(w["owner"], w["org"]).get(mgr_email, (0, None))[1], "manager", "the manager's role after a re-invite")
        target = f"csrf.{H.nonce}@verify.example"
        r = submit_form(w["owner"], f"/orgs/{w['org']}/members", "invite-member", {"email": target, "role": "scanner"}, drop_csrf=True)
        time.sleep(3)
        if H.mail.to(target):
            ctx.breach("X2", "an invitation was sent by a request without the session token")
        ctx.check(r.status == 403 and not H.mail.to(target), f"a token-less invitation answered {r.status}")
    with ctx.step("an invitation link works only for the invited address; a visitor is sent to sign in"):
        w = st["w"]
        invited = f"invited.{H.nonce}@verify.example"
        before = H.mail_ids(invited)
        H.invite(w["owner"], w["org"], invited, "box_office")
        link = H.link_in(H.wait_mail(invited, "invitation", before), "/invites/")
        ctx.check(link, "no invitation email")
        H.secrets.append(link.rsplit("/", 1)[-1])
        anon = H.client().get(link, follow=False)
        ctx.check(anon.status in REDIRECTS and "/signin" in (anon.location or ""), f"a visitor opening the invitation got {anon.status}")
        _e, other = H.account(tag="intruder")
        page = other.get(link, follow=True)
        r = None
        try:
            r = submit_form(other, link, "accept-invite", {}, follow=False, page=page)
        except FormNotFound:
            r = H.post_with_token(other, urllib.parse.urlsplit(link).path)
        if other.get(f"/orgs/{w['org']}", follow=True).dom.attr_one("data-org-id", str(w["org"])):
            ctx.breach("X1", "another account accepted an invitation meant for a different address")
            raise StepFailed("the wrong account became a member")
        ctx.check(r.status == 403 or page.status == 403, f"the wrong account's accept answered {r.status}")
        st["event"] = H.setup_event(w["owner"], w["org"], w["venue"], "Role Matrix Night", H.start_in(w["tz"], 60 * 24 * 12),
                                    {"BALC": 3000, "FLOOR": 4500})
        st["event2"] = H.setup_event(w["owner"], w["org"], w["venue"], "Cancel Probe Night", H.start_in(w["tz"], 60 * 24 * 13),
                                     {"BALC": 3000, "FLOOR": 4500})
        bemail, bc = H.account(tag="rolebuyer")
        oid, tickets = H.buy(bc, bemail, st["event"], ["FLOOR-C-3"])
        st["ticket"] = tickets["FLOOR-C-3"]
    with ctx.step("each role reaches exactly the pages R1.3 allows (403 otherwise); a non-member gets 403; a visitor is sent to sign in"):
        w = st["w"]
        o, e = w["org"], st["event"]
        actors = {"owner": w["owner"], "manager": st["roles"]["manager"][0][1], "box_office": st["roles"]["box_office"][0][1],
                  "scanner": st["roles"]["scanner"][0][1]}
        _x, outsider = H.account(tag="outsider")
        problems = []
        for tmpl, allowed in ROLE_GETS:
            path = tmpl.format(o=o, e=e)
            for role, c in actors.items():
                r = c.get(path, follow=False)
                want_ok = role in allowed
                if want_ok and r.status != 200:
                    problems.append(f"{role} GET {path} answered {r.status}, expected 200")
                if not want_ok:
                    if r.status == 200:
                        ctx.breach("X1", f"{role} reached {path}")
                    if r.status != 403:
                        problems.append(f"{role} GET {path} answered {r.status}, expected 403")
            r = outsider.get(path, follow=False)
            if r.status == 200:
                ctx.breach("X1", f"a non-member reached {path}")
            if r.status != 403:
                problems.append(f"non-member GET {path} answered {r.status}, expected 403")
            r = H.client().get(path, follow=False)
            if not (r.status in REDIRECTS and "/signin" in (r.location or "")):
                problems.append(f"visitor GET {path} answered {r.status}")
        ctx.check(not problems, f"{len(problems)} permission answers differ: " + " | ".join(problems[:6]))
        st["actors"] = actors
        st["outsider"] = outsider
    with ctx.step("forbidden actions answer 403 and change nothing; allowed ones work"):
        w = st["w"]
        o, e, e2, actors = w["org"], st["event"], st["event2"], st["actors"]
        # cancel: owner only
        for role in ("manager", "box_office", "scanner"):
            r = H.post_with_token(actors[role], f"/orgs/{o}/events/{e2}/cancel")
            if H.manage(w["owner"], o, e2)["status"] == "cancelled":
                ctx.breach("X1", f"{role} cancelled an event")
                raise StepFailed(f"{role} cancelled an event")
            ctx.eq(r.status, 403, f"{role} POST cancel")
        mpage = actors["manager"].get(f"/orgs/{o}/events/{e2}", follow=True)
        ctx.check(mpage.dom.attr_one("data-action", "cancel-event") is None, "the manager's manage page offers the cancel button")
        opage = w["owner"].get(f"/orgs/{o}/events/{e2}", follow=True)
        ctx.check(opage.dom.attr_one("data-action", "cancel-event") is not None, "the owner's manage page has no cancel button")
        # devices: owner and manager
        for role in ("box_office", "scanner"):
            label = f"probe-{role}-{H.nonce}"
            r = H.post_with_token(actors[role], f"/orgs/{o}/events/{e}/devices", [("label", label)])
            if label in w["owner"].get(f"/orgs/{o}/events/{e}/devices", follow=True).text:
                ctx.breach("X1", f"{role} created a scanner device")
            ctx.eq(r.status, 403, f"{role} POST create-device")
        H.create_device(actors["manager"], o, e, f"Manager gate {H.nonce}")
        # organizer refund: scanner may not
        r = H.post_with_token(actors["scanner"], f"/orgs/{o}/tickets/{st['ticket']}/refund")
        tk = H.ticket(w["owner"], st["ticket"])
        if H.refunds_for_ticket(st["ticket"]):
            ctx.breach("X1", "a scanner refunded a ticket")
        ctx.eq(r.status, 403, "scanner POST organizer-refund")
        _ = tk
        # promo codes: box office may not
        r = H.post_with_token(actors["box_office"], f"/orgs/{o}/events/{e}/codes",
                              [("code", "BOXPROBE"), ("kind", "percent"), ("value", "50"), ("min_subtotal_cents", "0"), ("max_redemptions", "")])
        if "BOXPROBE" in w["owner"].get(f"/orgs/{o}/events/{e}/codes", follow=True).text:
            ctx.breach("X1", "box office created a promo code")
        ctx.eq(r.status, 403, "box office POST create-code")
        # invitations: manager may not
        target = f"mgrinvite.{H.nonce}@verify.example"
        r = H.post_with_token(actors["manager"], f"/orgs/{o}/members", [("email", target), ("role", "scanner")])
        time.sleep(2)
        if H.mail.to(target):
            ctx.breach("X1", "a manager sent an invitation")
        ctx.eq(r.status, 403, "manager POST invite")
        # owner cancels the probe event (allowed)
        r = submit_form(w["owner"], f"/orgs/{o}/events/{e2}", "cancel-event", {}, follow=True)
        ctx.check(H.manage(w["owner"], o, e2)["status"] == "cancelled", "the owner could not cancel an event")
    with ctx.step("the owner changes roles and removes members; permissions follow at once; the owner's own role cannot change"):
        w = st["w"]
        o, e = w["org"], st["event"]
        rows = member_rows(w["owner"], o)
        bo_email, bo_c = st["roles"]["box_office"][0]
        sc_email, sc_c = st["roles"]["scanner"][1]
        uid_bo = rows[bo_email][0]
        page = w["owner"].get(f"/orgs/{o}/members", follow=True)
        form = next((f for f in page.dom.find_all(lambda n: n.tag == "form" and f"/members/{uid_bo}/role" in (n.attrs.get("action") or ""))), None)
        ctx.check(form is not None, "no change-role form for the box office member")
        w["owner"].post_form(f"/orgs/{o}/members/{uid_bo}/role", [x for x in form_fields(form) if x[0] != "role"] + [("role", "scanner")], follow=True)
        ctx.eq(member_rows(w["owner"], o).get(bo_email, (0, None))[1], "scanner", "role after change")
        ctx.eq(bo_c.get(f"/orgs/{o}/events/{e}/dashboard").status, 403, "dashboard for a member demoted to scanner")
        ctx.eq(bo_c.get(f"/orgs/{o}/events/{e}/door").status, 200, "door list for a member demoted to scanner")
        uid_sc = rows[sc_email][0]
        page = w["owner"].get(f"/orgs/{o}/members", follow=True)
        form = next((f for f in page.dom.find_all(lambda n: n.tag == "form" and f"/members/{uid_sc}/remove" in (n.attrs.get("action") or ""))), None)
        ctx.check(form is not None, "no remove form for the scanner")
        w["owner"].post_form(f"/orgs/{o}/members/{uid_sc}/remove", form_fields(form), follow=True)
        ctx.eq(sc_c.get(f"/orgs/{o}/events/{e}/door").status, 403, "door list for a removed member")
        owner_uid = rows.get(w["owner_email"], (None, None))[0]
        ctx.check(owner_uid is not None, "the owner is not listed on the members page")
        H.post_with_token(w["owner"], f"/orgs/{o}/members/{owner_uid}/role", [("role", "scanner")], token_from=f"/orgs/{o}/members")
        H.post_with_token(w["owner"], f"/orgs/{o}/members/{owner_uid}/remove", token_from=f"/orgs/{o}/members")
        ctx.eq(member_rows(w["owner"], o).get(w["owner_email"], (0, None))[1], "owner", "the owner's role after attempts to change it")
        mrow = st["roles"]["manager"][0][0]
        r = H.post_with_token(st["roles"]["manager"][0][1], f"/orgs/{o}/members/{uid_bo}/role", [("role", "manager")])
        if member_rows(w["owner"], o).get(bo_email, (0, None))[1] == "manager":
            ctx.breach("X1", "a manager changed a member's role")
        ctx.eq(r.status, 403, f"manager {mrow} POST change-role")
    with ctx.step("another organization's events, tickets and devices stay out of reach, even through this organization's URLs"):
        w = st["w"]
        o, e = w["org"], st["event"]
        p = H.quick_world("other", "America/Los_Angeles")
        pe = H.setup_event(p["owner"], p["org"], p["venue"], "Other Org Night", H.start_in(p["tz"], 60 * 24 * 12), {"BALC": 2500, "FLOOR": 3900})
        pb_email, pb = H.account(tag="otherbuyer")
        poid, pt = H.buy(pb, pb_email, pe, ["BALC-A-1"])
        ptid = pt["BALC-A-1"]
        pdid, pkey = H.create_device(p["owner"], p["org"], pe, "Other gate")
        mgr = st["roles"]["manager"][0][1]
        probs = []
        for path in (f"/orgs/{o}/events/{pe}/dashboard", f"/orgs/{o}/events/{pe}/attendees.csv", f"/orgs/{o}/events/{pe}/orders",
                     f"/orgs/{o}/events/{pe}/door", f"/orgs/{p['org']}/events/{pe}/dashboard", f"/orgs/{p['org']}/events/{pe}/attendees.csv"):
            r = mgr.get(path, follow=False)
            if r.status == 200 and (r.dom.attr_one("data-dashboard-event-id") or b"ticket_id" in r.body or r.dom.attr_one("data-ticket-id")):
                ctx.breach("X1", f"a manager of another organization read {path}")
            if r.status not in (403, 404):
                probs.append(f"{path} answered {r.status}")
        r = H.post_with_token(mgr, f"/orgs/{o}/tickets/{ptid}/refund")
        time.sleep(2)
        if H.refunds_for_ticket(ptid) or (H.ticket(pb, ptid).get("status") or "valid") != "valid":
            ctx.breach("X1", "a manager refunded another organization's ticket through their own organization's URL")
            probs.append("cross-organization refund took effect")
        H.post_with_token(mgr, f"/orgs/{o}/events/{pe}/devices/{pdid}/revoke")
        if H.scan(pkey, "not-a-token", "Gate").status == 401:
            ctx.breach("X1", "a manager revoked another organization's scanner device")
            probs.append("cross-organization device revoke took effect")
        orr = mgr.get(f"/orders/{poid}", follow=True)
        if orr.dom.attr_one("data-order-id", str(poid)):
            ctx.breach("X1", "an account read another buyer's order page")
            probs.append("another buyer's order page was shown")
        tr = mgr.get(f"/tickets/{ptid}", follow=True)
        if tr.dom.attr_one("data-qr-token"):
            ctx.breach("X1", "an account saw another holder's QR token")
            probs.append("another holder's QR token was shown")
        ctx.check(not probs, " | ".join(probs[:6]))
    with ctx.step("extra form fields never move an invitation or an event into another organization"):
        w = st["w"]
        ma_email, ma_c = H.account(tag="massassign")
        before = H.mail_ids(ma_email)
        r = submit_form(w["owner"], f"/orgs/{w['org']}/members", "invite-member", {"email": ma_email, "role": "scanner"},
                        override={"organization_id": "1", "org_id": "1", "role": "scanner"}, follow=True)
        link = H.link_in(H.wait_mail(ma_email, "invitation", before), "/invites/")
        ctx.check(link, "no invitation email for the mass-assignment probe")
        page = ma_c.get(link, follow=True)
        submit_form(ma_c, link, "accept-invite", {}, follow=True, page=page, override={"organization_id": "1", "role": "owner"})
        if ma_c.get("/orgs/1/events/11/door").status == 200 or ma_c.get(f"/orgs/{w['org']}", follow=True).dom.attr_one("data-my-role", "owner"):
            ctx.breach("X5", "extra fields moved an invitation into another organization or role")
            raise StepFailed("extra invitation fields took effect")
        ctx.check(ma_c.get(f"/orgs/{w['org']}/events/{st['event']}/door").status == 200, "the invited scanner cannot open the door list")
        r, eid = H.create_event(w["owner"], w["org"], w["venue"], "Mass Assign Night",
                                instant_to_wall(H.start_in(w["tz"], 60 * 24 * 20), w["tz"]),
                                instant_to_wall(now() - timedelta(minutes=5), w["tz"]), 100, {"BALC": 2000, "FLOOR": 3000})
        if eid:
            alt = H.staff("nadia.brooks@lanternroom.example")
            if alt.get(f"/orgs/1/events/{eid}", follow=True).dom.attr_one("data-event-id", str(eid)):
                ctx.breach("X5", "an event appeared under another organization")
    with ctx.step("a scanner of two organizations has each organization's scanner rights only"):
        jo = H.staff("jo.feld@gatecrew.example")
        ctx.eq(jo.get("/orgs/2/events/38/door").status, 200, "Jo Feld (scanner at Copperline) GET door list")
        ctx.eq(jo.get("/orgs/2/events/38/dashboard").status, 403, "Jo Feld GET Copperline dashboard")
        ctx.eq(jo.get("/orgs/1/events/11/door").status, 200, "Jo Feld (scanner at Lantern Room) GET door list")
        ctx.eq(jo.get("/orgs/3/events/55/door").status, 403, "Jo Feld GET Saguaro door list")


# ======================================================================================== W03
@workflow("W03", "Venues and seat maps; events in venue time zones (folds, gaps); drafts, publishing, rescheduling", 3)
def w03(ctx):
    st = ctx.shared.setdefault("w03", {})
    tz = "America/Denver"
    with ctx.step("a manager builds a venue with sections, row ranges and accessible seats; bad sections are refused"):
        email, owner = H.account(tag="venueowner")
        org = H.create_org(owner, f"Peak Shows {H.nonce}")
        _m, mgr = H.add_member(owner, org, "manager")
        r = submit_form(mgr, f"/orgs/{org}/venues/new", "create-venue", {"name": "Bad Zone Hall", "address": "x", "timezone": "Mars/Olympus"},
                        follow=True)
        ctx.check(r.dom.attr_one("data-venue-id") is None, "a venue with an unknown time zone was created")
        secs = [("UPPER", "Upper tier", "A-C", 12, ["A1"]), ("LOWER", "Lower tier", "A-F", 15, ["A1", "A15", "F7"])]
        vid = H.create_venue(mgr, org, "Summit Room", tz, secs)
        for bad in ({"code": "UPPER", "name": "dup", "rows": "A-B", "seats_per_row": "5", "accessible": ""},
                    {"code": "lower2", "name": "case", "rows": "A-B", "seats_per_row": "5", "accessible": ""},
                    {"code": "TOOLONGCODE", "name": "long", "rows": "A-B", "seats_per_row": "5", "accessible": ""},
                    {"code": "BOX", "name": "badacc", "rows": "A-B", "seats_per_row": "5", "accessible": "C1"},
                    {"code": "BOX2", "name": "wide", "rows": "A-B", "seats_per_row": "61", "accessible": ""}):
            submit_form(mgr, f"/orgs/{org}/venues/{vid}", "add-section", bad, follow=True)
        page = mgr.get(f"/orgs/{org}/venues/{vid}", follow=True)
        got = {n.attrs["data-section"]: n.attrs.get("data-seat-count") for n in page.dom.attr_all("data-section") if "data-seat-count" in n.attrs}
        ctx.eq(got, {"UPPER": "36", "LOWER": "90"}, "sections and seat counts on the venue page")
        ctx.check(page.dom.attr_one("data-action", "add-section") is not None, "no add-section form before any event exists")
        st.update(org=org, owner=owner, mgr=mgr, vid=vid)
    with ctx.step("event times are wall-clock times at the venue: a repeated time means its first occurrence, a missing one is refused"):
        org, mgr, vid = st["org"], st["mgr"], st["vid"]
        on_sale = instant_to_wall(now() - timedelta(minutes=5), tz)
        sp, fa = next_transition(tz, "spring"), next_transition(tz, "fall")   # the next real DST changes in Denver
        plain_wall = f"{(now() + timedelta(days=150)).astimezone(ZoneInfo(tz)).date()}T20:15"
        r, gap = H.create_event(mgr, org, vid, "Gap Night", f"{sp}T02:30", on_sale, 100, {"UPPER": 2000, "LOWER": 3000})
        ctx.check(gap is None, f"an event starting at a wall-clock time that does not exist ({sp} 02:30 Denver) was created")
        r, fold = H.create_event(mgr, org, vid, "Fold Night", f"{fa}T01:30", on_sale, 100, {"UPPER": 2000, "LOWER": 3000})
        ctx.check(fold is not None, f"an event at a repeated wall-clock time was refused ({r.status})")
        node = r.dom.attr_one("data-starts-at")
        ctx.eq(node.attrs.get("data-starts-at") if node else None, inst(wall_to_instant(f"{fa}T01:30", tz)), f"start of {fa} 01:30 in Denver")
        r, plain = H.create_event(mgr, org, vid, "Summer Night", plain_wall, on_sale, 150, {"UPPER": 2500, "LOWER": 4000})
        ctx.check(plain is not None, "an ordinary event was refused")
        node = r.dom.attr_one("data-starts-at")
        plain_start = inst(wall_to_instant(plain_wall, tz))
        ctx.eq(node.attrs.get("data-starts-at") if node else None, plain_start, f"start of {plain_wall.replace('T', ' ')} in Denver")
        r, offgap = H.create_event(mgr, org, vid, "Off Gap Night", f"{sp + timedelta(days=6)}T20:00", on_sale, 100,
                                   {"UPPER": 2000, "LOWER": 3000}, off_sale_wall=f"{sp}T02:15")
        ctx.check(offgap is None, "an off-sale time that does not exist was accepted")
        st.update(fold=fold, plain=plain, sp=sp, fa=fa, plain_start=plain_start)
    with ctx.step("invalid event settings are refused and create nothing"):
        org, mgr, vid = st["org"], st["mgr"], st["vid"]
        nowl = now()
        good_start = instant_to_wall(nowl + timedelta(days=30), tz)
        on_sale = instant_to_wall(nowl - timedelta(minutes=5), tz)
        cases = [
            ("past start", dict(start_wall=instant_to_wall(nowl - timedelta(days=1), tz), on_sale_wall=instant_to_wall(nowl - timedelta(days=2), tz))),
            ("on-sale after start", dict(start_wall=good_start, on_sale_wall=instant_to_wall(nowl + timedelta(days=31), tz))),
            ("off-sale after start", dict(start_wall=good_start, on_sale_wall=on_sale, off_sale_wall=instant_to_wall(nowl + timedelta(days=31), tz))),
            ("presale without code", dict(start_wall=good_start, on_sale_wall=on_sale, presale_wall=instant_to_wall(nowl - timedelta(days=1), tz))),
            ("presale after on-sale", dict(start_wall=good_start, on_sale_wall=on_sale, presale_wall=instant_to_wall(nowl + timedelta(days=1), tz),
                                           presale_code="EARLY1")),
            ("bad presale code", dict(start_wall=good_start, on_sale_wall=on_sale, presale_wall=instant_to_wall(nowl - timedelta(days=1), tz),
                                      presale_code="no")),
        ]
        for label, kw in cases:
            kw.setdefault("off_sale_wall", "")
            r, eid = H.create_event(mgr, org, vid, f"Invalid {label}", kw.pop("start_wall"), kw.pop("on_sale_wall"), 100,
                                    {"UPPER": 2000, "LOWER": 3000}, **kw)
            ctx.check(eid is None, f"an event with {label} was created")
        r, eid = H.create_event(mgr, org, vid, "Cheap seat", good_start, on_sale, 100, {"UPPER": 99, "LOWER": 3000})
        ctx.check(eid is None, "a face price below 100 was accepted")
        p = H.quick_world("foreignvenue", "America/Chicago")
        r, eid = H.create_event(mgr, org, p["venue"], "Foreign venue", good_start, on_sale, 100, {"BALC": 2000, "FLOOR": 3000})
        ctx.check(eid is None, "an event was created at another organization's venue")
    with ctx.step("drafts are invisible outside the organization; publishing shows them; the publish button only on drafts"):
        org, mgr, plain = st["org"], st["mgr"], st["plain"]
        ctx.eq(H.client().get(f"/events/{plain}").status, 404, "a visitor GET of a draft event page")
        _e, stranger = H.account(tag="stranger")
        ctx.eq(stranger.get(f"/events/{plain}").status, 404, "another account GET of a draft event page")
        mp = mgr.get(f"/orgs/{org}/events/{plain}", follow=True)
        ctx.check(mp.dom.attr_one("data-event-id", str(plain)) is not None and page_attr(mp.dom, "data-event-status") == "draft",
                  "manage page lacks data-event-status=draft")
        ctx.check(mp.dom.attr_one("data-action", "publish-event") is not None, "no publish button on a draft")
        ctx.check(mp.dom.attr_one("data-action", "reschedule-event") is None, "a reschedule form on a draft")
        r = H.hold(stranger, plain, ["UPPER-A-2"])[1]
        ctx.check(r is None, "seats of a draft event were held")
        H.publish(mgr, org, plain)
        mp = mgr.get(f"/orgs/{org}/events/{plain}", follow=True)
        ctx.check(mp.dom.attr_one("data-event-id", str(plain)) is not None and page_attr(mp.dom, "data-event-status") == "published",
                  "status after publishing")
        ctx.check(mp.dom.attr_one("data-action", "publish-event") is None, "the publish button remains after publishing")
        ctx.check(mp.dom.attr_one("data-action", "reschedule-event") is not None, "no reschedule form on a published event")
        ep = H.client().get(f"/events/{plain}", follow=True)
        ctx.eq(ep.status, 200, "visitor GET of the published event")
        ctx.check(ep.dom.attr_one("data-event-id", str(plain)) is not None, "event page lacks data-event-id")
        ctx.eq(page_attr(ep.dom, "data-starts-at"), st["plain_start"], "event page data-starts-at")
        seats = {n.attrs["data-seat"]: (n.attrs.get("data-seat-status"), n.attrs.get("data-accessible")) for n in ep.dom.attr_all("data-seat")
                 if "data-seat-status" in n.attrs}
        ctx.eq(len(seats), 126, "seats on the event page")
        ctx.eq(seats.get("LOWER-F-7"), ("available", "true"), "LOWER-F-7 (accessible)")
        ctx.eq(seats.get("UPPER-B-12"), ("available", "false"), "UPPER-B-12")
        secs = {n.attrs["data-section"]: (n.attrs.get("data-price-cents"), n.attrs.get("data-available")) for n in ep.dom.attr_all("data-section")
                if "data-price-cents" in n.attrs}
        ctx.eq(secs, {"UPPER": ("2500", "36"), "LOWER": ("4000", "90")}, "section prices and available counts")
    with ctx.step("the seat map is fixed once an event exists"):
        org, mgr, vid = st["org"], st["mgr"], st["vid"]
        page = mgr.get(f"/orgs/{org}/venues/{vid}", follow=True)
        ctx.check(page.dom.attr_one("data-action", "add-section") is None, "the add-section form is still offered")
        H.post_with_token(mgr, f"/orgs/{org}/venues/{vid}/sections",
                          [("code", "LATE"), ("name", "Late"), ("rows", "A-A"), ("seats_per_row", "4"), ("accessible", "")])
        page = mgr.get(f"/orgs/{org}/venues/{vid}", follow=True)
        ctx.check(page.dom.attr_one("data-section", "LATE") is None, "a section was added after an event existed")
    with ctx.step("rescheduling moves the start; a past or missing wall-clock time is refused"):
        org, mgr, plain = st["org"], st["mgr"], st["plain"]
        submit_form(mgr, f"/orgs/{org}/events/{plain}", "reschedule-event", {"starts_at": f"{st['sp']}T02:10"}, follow=True)
        submit_form(mgr, f"/orgs/{org}/events/{plain}", "reschedule-event",
                    {"starts_at": instant_to_wall(now() - timedelta(days=2), tz)}, follow=True)
        ctx.eq(H.manage(mgr, org, plain)["starts_at"], st["plain_start"], "start after refused reschedules")
        submit_form(mgr, f"/orgs/{org}/events/{plain}", "reschedule-event", {"starts_at": f"{st['fa']}T01:45"}, follow=True)
        ctx.eq(H.manage(mgr, org, plain)["starts_at"], inst(wall_to_instant(f"{st['fa']}T01:45", tz)),
               f"start after rescheduling to {st['fa']} 01:45")


# ======================================================================================== W04
def sale_state(c, event: int) -> str | None:
    r = H.event_page(c, event)
    n = r.dom.attr_one("data-sale-state")
    return n.attrs.get("data-sale-state") if n else None


def sleep_until(t: datetime) -> None:
    d = (t - now()).total_seconds()
    if d > 0:
        time.sleep(d)


@workflow("W04", "Sales windows: presale with a code, on-sale, off-sale, at the venue's local times", 3, timeout=900)
def w04(ctx):
    st = ctx.shared.setdefault("w04", {})

    def clean(tz_, at):   # the 20 minutes from `at` contain no wall-clock time a fall-back repeats
        base = at.replace(second=0, microsecond=0)
        return all(exact_wall(base + timedelta(minutes=k), tz_) for k in range(21))
    tz = "America/Los_Angeles"
    if not (clean(tz, now()) and clean(tz, now() + timedelta(days=9))):
        tz = "America/Phoenix"   # no clock changes there
    with ctx.step("an event with a presale, on-sale and off-sale a minute apart, typed in the venue's local time"):
        w = H.quick_world("windows", tz)
        t = now() + timedelta(seconds=75)
        presale = t.replace(second=0, microsecond=0) + timedelta(minutes=1)
        on_sale, off_sale = presale + timedelta(minutes=1), presale + timedelta(minutes=2)
        start = presale + timedelta(days=9)
        r, eid = H.create_event(w["owner"], w["org"], w["venue"], "Windowed Show", instant_to_wall(start, tz), instant_to_wall(on_sale, tz),
                                250, {"BALC": 3000, "FLOOR": 4200}, off_sale_wall=instant_to_wall(off_sale, tz),
                                presale_wall=instant_to_wall(presale, tz), presale_code="FANCLUB24")
        ctx.check(eid is not None, f"creating the windowed event answered {r.status}")
        H.publish(w["owner"], w["org"], eid)
        r2, eid2 = H.create_event(w["owner"], w["org"], w["venue"], "Plain On-sale", instant_to_wall(start, tz), instant_to_wall(on_sale, tz), 0,
                                  {"BALC": 2000, "FLOOR": 2500})
        H.publish(w["owner"], w["org"], eid2)
        email, c = H.account(tag="presalefan")
        st.update(w=w, eid=eid, eid2=eid2, presale=presale, on_sale=on_sale, off_sale=off_sale, c=c, email=email)
    with ctx.step("before the presale nobody may hold seats, code or not"):
        c, eid = st["c"], st["eid"]
        ctx.check(now() < st["presale"] - timedelta(seconds=3), "verifier fixture: too late for the before-presale check")
        ctx.eq(sale_state(c, eid), "not_on_sale", "data-sale-state before the presale")
        ctx.check(H.hold(c, eid, ["BALC-A-1"], presale_code="FANCLUB24")[1] is None, "a hold before the presale succeeded")
        ctx.check(H.hold(c, st["eid2"], ["BALC-A-1"])[1] is None, "a hold before on-sale succeeded")
    with ctx.step("during the presale a hold needs the code (any case, spaces ignored)"):
        c, eid = st["c"], st["eid"]
        sleep_until(st["presale"] + timedelta(seconds=2))
        ctx.eq(sale_state(c, eid), "presale", "data-sale-state during the presale")
        ctx.check(H.hold(c, eid, ["BALC-A-2"])[1] is None, "a presale hold without the code succeeded")
        ctx.check(H.hold(c, eid, ["BALC-A-2"], presale_code="FANCLUB25")[1] is None, "a presale hold with a wrong code succeeded")
        r, oid = H.hold(c, eid, ["BALC-A-2"], presale_code="  fanclub24 ")
        ctx.check(oid is not None, f"a presale hold with the code answered {r.status}: {page_text(r)[:160]}")
        H.release(c, oid)
        ctx.check(H.hold(c, st["eid2"], ["BALC-A-2"])[1] is None, "an event without presale sold before on-sale")
    with ctx.step("from on-sale anyone may hold seats without a code"):
        c, eid = st["c"], st["eid"]
        sleep_until(st["on_sale"] + timedelta(seconds=2))
        ctx.eq(sale_state(c, eid), "on_sale", "data-sale-state after on-sale")
        r, oid = H.hold(c, eid, ["BALC-A-3"])
        ctx.check(oid is not None, f"an on-sale hold answered {r.status}")
        H.release(c, oid)
        r, oid2 = H.hold(c, st["eid2"], ["BALC-A-3"])
        ctx.check(oid2 is not None, f"an on-sale hold on the plain event answered {r.status}")
        H.release(c, oid2)
    with ctx.step("from off-sale nobody may hold seats"):
        c, eid = st["c"], st["eid"]
        sleep_until(st["off_sale"] + timedelta(seconds=2))
        ctx.eq(sale_state(c, eid), "ended", "data-sale-state after off-sale")
        ctx.check(H.hold(c, eid, ["BALC-A-4"])[1] is None, "a hold after off-sale succeeded")
        ctx.check(H.hold(c, eid, ["BALC-A-4"], presale_code="FANCLUB24")[1] is None, "a presale-code hold after off-sale succeeded")


# ======================================================================================== W05
@workflow("W05", "Purchase: seat map, hold, price breakdown, Checkout Session, hosted payment, webhook, tickets, one email", 5, critical=True)
def w05(ctx):
    st = ctx.shared.setdefault("w05", {})
    with ctx.step("a published event shows its seat map with prices, availability and accessible seats"):
        w = H.quick_world("purchase", "America/Chicago", [("BALC", "Balcony", "A-B", 12, []), ("FLOOR", "Floor", "A-C", 12, ["A1", "A2"])])
        eid = H.setup_event(w["owner"], w["org"], w["venue"], "Velvet Static Live", H.start_in(w["tz"], 60 * 24 * 15),
                            {"BALC": 3150, "FLOOR": 4820}, fee=225)
        email, c = H.account(tag="happybuyer", name="Harper Lane")
        ep = H.event_page(c, eid)
        n = ep.dom.attr_one("data-sale-state")
        ctx.eq(n.attrs.get("data-sale-state") if n else None, "on_sale", "data-sale-state")
        secs = {x.attrs["data-section"]: (x.attrs.get("data-price-cents"), x.attrs.get("data-available")) for x in ep.dom.attr_all("data-section")
                if "data-price-cents" in x.attrs}
        ctx.eq(secs, {"BALC": ("3150", "24"), "FLOOR": ("4820", "36")}, "sections")
        boxes = {x.attrs.get("value") for x in ep.dom.find_all(lambda x: x.tag == "input" and x.attrs.get("name") == "seats")}
        ctx.check("FLOOR-C-12" in boxes and "BALC-A-1" in boxes, "the hold form lacks seat checkboxes named seats")
        st.update(w=w, eid=eid, email=email, c=c)
    with ctx.step("an accessible seat needs the accessible-seating box; a hold creates a pending order with an expiry"):
        c, eid, email = st["c"], st["eid"], st["email"]
        ctx.check(H.hold(c, eid, ["FLOOR-A-1"])[1] is None, "an accessible seat was held without ticking accessible seating")
        r, oid = H.hold(c, eid, ["FLOOR-A-1"], accessible=True)
        ctx.check(oid is not None, f"an accessible hold with the box ticked answered {r.status}")
        H.release(c, oid)
        t0 = now()
        seats = ["FLOOR-B-10", "FLOOR-B-9", "BALC-B-2"]
        r, oid = H.hold(c, eid, seats, buyer=email)
        t1 = now()
        ctx.check(oid is not None, f"holding three seats answered {r.status}: {page_text(r)[:200]}")
        o = H.order(c, oid)
        ctx.eq(o.get("status"), "pending", "order status after the hold")
        ex = parse_inst(o.get("hold_expires_at"))
        ctx.check(ex and t0 + timedelta(seconds=H.hold_ttl - 2) <= ex <= t1 + timedelta(seconds=H.hold_ttl + 2),
                  f"data-hold-expires-at {o.get('hold_expires_at')} is not HOLD_TTL_SECONDS after the hold")
        q = H.model.quote_for(eid, seats)
        probs = H.quote_diff(o, q)
        ctx.check(not probs, "price breakdown differs: " + " | ".join(probs[:6]))
        ctx.check(o["has_pay"] and o["has_release"] and o["has_apply"], "pending order lacks pay/release/apply-code controls")
        states = H.seat_states(H.client(), eid)
        ctx.eq([states.get(s) for s in seats], ["held"] * 3, "held seats on the event page")
        st.update(oid=oid, seats=seats, q=q)
    with ctx.step("Pay redirects to one Checkout Session for the order total; pressing it again goes to the same session"):
        c, oid, q = st["c"], st["oid"], st["q"]
        r, sid = H.pay_click(c, oid)
        ctx.check(sid, f"Pay answered {r.status} {r.location!r}, not a 303 to the provider's session url")
        ctx.eq(r.status, 303, "Pay status")
        s = H.pay.session(sid)
        ctx.eq(s.get("amount_total"), q.total, "the session's amount_total")
        ctx.eq(str((s.get("metadata") or {}).get("order_id")), str(oid), "the session's metadata[order_id]")
        ctx.check(str(s.get("success_url", "")).startswith(H.base) and str(s.get("cancel_url", "")).startswith(H.base),
                  "success_url/cancel_url do not point back to the application")
        o = H.order(c, oid)
        ctx.check(o["has_pay"] and o["has_release"], "a pending order lost its pay/release buttons after Pay was pressed")
        ctx.check(not o["has_apply"], "the apply-code form is still offered after Pay")
        r2, sid2 = H.pay_click(c, oid)
        ctx.eq(sid2, sid, "session after pressing Pay again")
        st["sid"] = sid
    with ctx.step("paying on the hosted page returns to the order page; only the webhook marks it paid and issues tickets"):
        c, oid, sid, email = st["c"], st["oid"], st["sid"], st["email"]
        before = H.mail_ids(email)
        t0 = now()
        status, loc = H.pay.pay_redirect(sid, CARD_OK)
        ctx.check(status == 303 and loc and loc.startswith(H.base), f"the hosted page answered {status} {loc!r}")
        back = c.get(loc, follow=True)
        ctx.check(back.status == 200, f"the success_url answered {back.status}")
        ids = H.settle_paid(c, oid, (t0 - timedelta(seconds=2), now() + timedelta(seconds=30)))
        o = H.order(c, oid)
        ctx.check(not o["has_pay"] and not o["has_release"], "a paid order still offers pay/release")
        ctx.check(not H.quote_diff(o, st["q"]), "the paid order's breakdown changed")
        st.update(ids=ids, before=before)
    with ctx.step("exactly one confirmation email lists every seat, ticket id and QR token, addressed with the buyer's name"):
        c, email, ids = st["c"], st["email"], st["ids"]
        msgs = H.wait_mail(email, "your tickets", st["before"])
        if msgs:   # judge evidence first, so a strict miss below costs this workflow only
            from kitlib.browser import capture_text
            capture_text("confirmation_email", f"Subject: {msgs[0]['subject']}\n\n{msgs[0]['text'] or msgs[0]['html']}",
                         {"event": "Velvet Static Live", "tickets": {s: t for s, t in ids.items()}})
        ctx.eq(len(msgs), 1, "confirmation emails")
        time.sleep(3)
        ctx.eq(len(H.new_mail(email, "your tickets", st["before"])), 1, "confirmation emails after 3 s")
        body = msgs[0]["text"] or msgs[0]["html"]
        ctx.check("Velvet Static Live" in body, "the email does not name the event")
        full = H.mail._get("/api/v1/message/" + str(msgs[0]["id"]))
        names = [str(t.get("Name", "")) for t in (full.get("To") or [])]
        ctx.check("Harper Lane" in names, f"To display names {names!r} do not include the buyer's name")
        for seat, tid in ids.items():
            tok = H.token(c, tid)
            ctx.check(seat in body and str(tid) in body and tok in body, f"the email lacks {seat}, ticket {tid} or its token")
    with ctx.step("My tickets and the ticket pages show the tickets; the event page shows the seats sold"):
        c, eid, ids = st["c"], st["eid"], st["ids"]
        page = c.get("/tickets", follow=True)
        got = {(n.attrs.get("data-ticket-id"), n.attrs.get("data-seat"), n.attrs.get("data-ticket-status"))
               for n in page.dom.attr_all("data-ticket-id") if n.attrs.get("data-event-id") == str(eid)}
        ctx.eq(got, {(str(t), s, "valid") for s, t in ids.items()}, "tickets listed for the event")
        for s, t in ids.items():
            tk = H.ticket(c, t)
            ctx.check(tk.get("seat") == s and tk.get("status") == "valid" and tk.get("holder") == st["email"],
                      f"ticket {t} page markers {tk.get('seat')}, {tk.get('status')}, {tk.get('holder')}")
        states = H.seat_states(H.client(), eid)
        ctx.eq([states.get(s) for s in st["seats"]], ["sold"] * 3, "sold seats on the event page")
        _e, other = H.account(tag="snoop")
        r = other.get(f"/orders/{st['oid']}", follow=True)
        if r.dom.attr_one("data-order-id", str(st["oid"])):
            ctx.breach("X1", "another account read a buyer's order page")
            raise StepFailed("another account read the order page")
        ctx.check(r.status in (403, 404), f"another account's GET of the order answered {r.status}")
