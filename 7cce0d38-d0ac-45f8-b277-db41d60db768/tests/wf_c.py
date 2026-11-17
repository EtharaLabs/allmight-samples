"""Seatwise workflows W11-W16: buyer refunds, refund mechanics under provider faults, organizer refunds
and cancellation, transfers and QR tokens, scanner devices and online check-in, offline sync."""

from __future__ import annotations

import json
import urllib.parse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import CARD_OK, H, REDIRECTS, batch_results, exact_wall, join, next_transition, now, offer_page, section_node  # noqa: E402
from kitlib.conc import simultaneously  # noqa: E402
from kitlib.web import FormNotFound, form_fields, submit_form, visible_text  # noqa: E402
from kitlib.wf import InfraError, StepFailed, workflow  # noqa: E402
from reference import money as M  # noqa: E402
from reference.model import inst, instant_to_wall, norm, parse_inst, wall_to_instant  # noqa: E402

UTC = timezone.utc


def sleep_until(t: datetime) -> None:
    d = (t - now()).total_seconds()
    if d > 0:
        time.sleep(d)


def is_refund_mail(m) -> bool:
    s = m["subject"].lower()
    return "refund" in s and "cancel" not in s and "no longer available" not in s


def dollars(c: int) -> str:
    return f"${c // 100:,}.{c % 100:02d}"


def expect_refund(ctx, c, tid: int, amount: int, buyer: str, before: set, seat: str, email: bool = True) -> None:
    t = H.wait_ticket(c, tid, "refunded", 90)
    ctx.eq(t.get("status"), "refunded", f"ticket {tid} status after its refund")
    got = [r["amount"] for r in H.refunds_for_ticket(tid)]
    if len(got) > 1:
        ctx.breach("X6", f"ticket {tid} was refunded {len(got)} times at the provider")
    ctx.eq(got, [amount], f"provider refunds for ticket {tid}")
    H.model.refund_completed(tid, amount) if H.model.tickets[tid].status != "refunded" else None
    if email:
        msgs = [m for m in H.wait_mail(buyer, "refund", before) if is_refund_mail(m)]
        hit = [m for m in msgs if dollars(amount) in (m["text"] or m["html"]) and seat in (m["text"] or m["html"])]
        ctx.check(hit, f"no refund email stating {dollars(amount)} and {seat} reached {buyer}")


# ======================================================================================== W11
@workflow("W11", "Buyer refunds: 168/48-hour tiers to the second, deadlines across DST, reschedules, who may ask", 5,
          timeout=1800, critical=True)
def w11(ctx):
    st = ctx.shared.setdefault("w11", {})
    with ctx.step("tickets for events 10 days, 100 hours and 20 hours away and across DST changes show their deadlines"):
        w = H.quick_world("refunds", "America/New_York")
        chi = H.create_venue(w["owner"], w["org"], "Refund Loft Chicago", "America/Chicago", [("BALC", "Balcony", "A-B", 10, []),
                                                                                                ("FLOOR", "Floor", "A-D", 12, [])])
        tz = w["tz"]
        ev = {
            "full": H.setup_event(w["owner"], w["org"], w["venue"], "Ten Days Out", H.start_in(tz, 60 * 24 * 10), {"BALC": 3000, "FLOOR": 5000}, fee=150),
            "half": H.setup_event(w["owner"], w["org"], w["venue"], "Hundred Hours", H.start_in(tz, 60 * 100), {"BALC": 2750, "FLOOR": 4020}),
            "none": H.setup_event(w["owner"], w["org"], w["venue"], "Tomorrow Night", H.start_in(tz, 60 * 20), {"BALC": 2500, "FLOOR": 3500}),
            "resched": H.setup_event(w["owner"], w["org"], w["venue"], "Moved Show", H.start_in(tz, 60 * 24 * 9), {"BALC": 2900, "FLOOR": 4100}),
        }
        # the 168-hour deadline of each of these events falls before the next real DST change and the start after it
        sp_wall = f"{next_transition('America/Chicago', 'spring', 10) + timedelta(days=6)}T19:00"
        fa_wall = f"{next_transition(tz, 'fall', 10) + timedelta(days=3)}T19:00"
        r, e1 = H.create_event(w["owner"], w["org"], chi, "Spring Forward Gala", sp_wall,
                               instant_to_wall(now() - timedelta(minutes=5), "America/Chicago"), 200, {"BALC": 3100, "FLOOR": 4700})
        r, e2 = H.create_event(w["owner"], w["org"], w["venue"], "Fall Back Ball", fa_wall,
                               instant_to_wall(now() - timedelta(minutes=5), tz), 200, {"BALC": 3100, "FLOOR": 4700})
        ctx.check(e1 and e2, "creating the DST events failed")
        H.publish(w["owner"], w["org"], e1)
        H.publish(w["owner"], w["org"], e2)
        ev["dst_spring"], ev["dst_fall"] = e1, e2
        H.create_code(w["owner"], w["org"], ev["full"], "FRIENDS15", "percent", 15)
        H.create_code(w["owner"], w["org"], ev["full"], "TAKE350", "fixed", 350)
        email, c = H.account(tag="refunder", name="Rowan Fair")
        buys = {
            "full": (["BALC-A-3", "BALC-A-4", "FLOOR-B-7"], ["FRIENDS15", "TAKE350"]),
            "half": (["FLOOR-C-9", "FLOOR-C-10"], []),
            "none": (["BALC-B-1"], []),
            "resched": (["FLOOR-A-3", "FLOOR-A-4"], []),
            "dst_spring": (["BALC-A-1"], []),
            "dst_fall": (["BALC-A-1"], []),
        }
        tix = {}
        for k, (seats, codes) in buys.items():
            oid, ids = H.buy(c, email, ev[k], seats, codes)
            tix[k] = ids
        want = {}
        for k in ("full", "half", "none", "dst_spring", "dst_fall"):
            S = H.model.events[ev[k]].start
            want[k] = (inst(S - timedelta(hours=168)), inst(S - timedelta(hours=48)))
        pct = {"full": "100", "half": "50", "none": "0", "dst_spring": "100", "dst_fall": "100"}
        probs = []
        for k, (fu, hu) in want.items():
            tid = next(iter(tix[k].values()))
            t = H.ticket(c, tid)
            got = (t.get("full-refund-until"), t.get("half-refund-until"), t.get("refund-percent"), t.get("has_refund"))
            exp = (fu, hu, pct[k], pct[k] != "0")
            if got != exp:
                probs.append(f"{k} ticket {tid}: got {got} want {exp}")
        ctx.check(not probs, "ticket refund markers differ: " + " | ".join(probs))
        st.update(w=w, ev=ev, email=email, c=c, tix=tix, chi=chi)
    with ctx.step("refunds at 100 % and 50 % return each ticket's share exactly; within 48 hours none is possible"):
        c, email, ev, tix = st["c"], st["email"], st["ev"], st["tix"]
        for k, seat, p in (("full", "FLOOR-B-7", 100), ("full", "BALC-A-4", 100), ("half", "FLOOR-C-10", 50)):
            tid = tix[k][seat]
            amount = M.refund_amount(H.model.tickets[tid].total, p)
            before = H.mail_ids(email)
            r = H.request_refund(c, tid)
            ctx.check(r.status < 400, f"refund request for {seat} answered {r.status}")
            H.model.refund_requested(tid, amount)
            expect_refund(ctx, c, tid, amount, email, before, seat)
            states = H.seat_states(H.client(), ev[k])
            ctx.eq(states.get(seat), "available", f"{seat} after its refund completed")
        tid = tix["none"]["BALC-B-1"]
        H.post_with_token(c, f"/tickets/{tid}/refund", token_from=f"/tickets/{tid}")
        time.sleep(3)
        ctx.check(not H.refunds_for_ticket(tid) and H.ticket(c, tid).get("status") == "valid", "a refund was made within 48 hours of the start")
    with ctx.step("at the 168-hour and 48-hour deadlines (to the second): D-5 s and D+0.05 s get the higher tier, D+1.05 s and D+2 s the lower"):
        w, c, email = st["w"], st["c"], st["email"]
        tz = w["tz"]
        def deadline_event(name, minutes, prices):
            """An event starting at the next whole minute >= `minutes` from now, at whichever venue (New York or
            Chicago) can name that instant exactly as a wall-clock time: a fall-back's repeated hour falls an hour
            apart in UTC in the two zones, so one of them always can, and the deadline stays where it was planned."""
            t = (now() + timedelta(minutes=minutes)).replace(second=0, microsecond=0) + timedelta(minutes=1)
            for v in (w["venue"], st["chi"]):
                if exact_wall(t, H.model.venue_tz[v]):
                    return H.setup_event(w["owner"], w["org"], v, name, t, prices)
            raise InfraError(f"verifier fixture: neither venue names {inst(t)} exactly")
        # The margin has to clear everything done before the first send - two four-seat purchases and the
        # eight ticket pages fetched below - or the deadlines pass during setup and every tier is judged one
        # step low. Worst seen: purchases under 474 s plus 302 s of page loads, so 16 minutes leaves ~3 min
        # spare. It also has to stay well inside this workflow's own timeout (see the decorator), which the
        # idle wait counts against: 16 min of waiting plus ~2 min of work sits at ~18 min against 30.
        e168 = deadline_event("Deadline 168", 168 * 60 + 16, {"BALC": 3330, "FLOOR": 4440})
        e48 = deadline_event("Deadline 48", 48 * 60 + 17, {"BALC": 2220, "FLOOR": 3570})
        _o1, t168 = H.buy(c, email, e168, ["BALC-A-1", "BALC-A-2", "BALC-A-3", "BALC-A-4"])
        _o2, t48 = H.buy(c, email, e48, ["FLOOR-A-5", "FLOOR-A-6", "FLOOR-A-7", "FLOOR-A-8"])
        D1 = H.model.events[e168].start - timedelta(hours=168)
        D2 = H.model.events[e48].start - timedelta(hours=48)
        ms = lambda x: timedelta(milliseconds=x)  # noqa: E731
        # (send at, ticket, percent, seat, guard): a request sent at D + 0.05 s is judged at second D only if the server
        # read its clock before D + 1 s; when its answer arrives after D + 0.5 s that is unknowable, so the case is skipped.
        plan = sorted([(D1 - ms(5000), t168["BALC-A-1"], 100, "BALC-A-1", False), (D1 + ms(50), t168["BALC-A-3"], 100, "BALC-A-3", True),
                       (D1 + ms(1050), t168["BALC-A-4"], 50, "BALC-A-4", False), (D1 + ms(2000), t168["BALC-A-2"], 50, "BALC-A-2", False),
                       (D2 - ms(5000), t48["FLOOR-A-5"], 50, "FLOOR-A-5", False), (D2 + ms(50), t48["FLOOR-A-7"], 50, "FLOOR-A-7", True),
                       (D2 + ms(1050), t48["FLOOR-A-8"], 0, "FLOOR-A-8", False), (D2 + ms(2000), t48["FLOOR-A-6"], 0, "FLOOR-A-6", False)])
        pages = {x[1]: c.get(f"/tickets/{x[1]}", follow=True) for x in plan}
        # The clock is checked after the page loads, never before. Loading eight ticket pages has been seen to take
        # minutes on a loaded host, so guarding first measures headroom that the fetch then spends: the whole plan fires
        # late in one burst and the application's correct lower-tier answers are read as defects. The first send, at
        # D1 - 5 s, is the binding constraint; every later send only sleeps, so this one guard covers the plan.
        # InfraError rather than ctx.check because W11 is critical: a failed check caps every workflow in the trial at
        # 0.4 and reports a plausible score for a test that never ran, where InfraError reports an invalid trial.
        if now() >= D1 - timedelta(seconds=6):
            raise InfraError(f"verifier fixture: setup and page loads ran {(now() - D1).total_seconds():+.1f} s past the "
                             f"first deadline; this host needs a wider margin than the plan allows")
        sent = []
        for at, tid, p, seat, guard in plan:
            sleep_until(at)
            t_send = now()
            try:
                submit_form(c, f"/tickets/{tid}", "request-refund", {}, follow=False, page=pages[tid])
            except FormNotFound:
                c.post_form(f"/tickets/{tid}/refund", [(i.attrs.get("name"), i.attrs.get("value", "")) for i in pages[tid].dom.find_all(
                    lambda n: n.tag == "input" and "data-csrf" in n.attrs)])
            t_recv = now()
            deadline = D1 if at < D1 + ms(10000) and at > D1 - ms(10000) else D2
            # The server read its clock somewhere in [t_send, t_recv], so a case proves something only when that whole
            # window fell on the side of the deadline the plan intended. Without this the six unguarded cases are
            # scored unconditionally, and a request that arrived minutes late is judged as though it were punctual.
            window_ok = (t_recv < deadline) if at < deadline else (t_send > deadline)
            conclusive = window_ok and (not guard or t_recv <= deadline + ms(500))
            sent.append((tid, p, seat, conclusive, (t_send - deadline).total_seconds(), (t_recv - t_send).total_seconds()))
        ctx.observe("deadline_requests", [(seat, round(off, 3), round(rtt, 3), ok) for _t, _p, seat, ok, off, rtt in sent])
        probs = []
        for tid, p, seat, conclusive, _off, _rtt in sent:
            if not conclusive:
                continue
            if p == 0:
                time.sleep(2)
                if H.refunds_for_ticket(tid) or H.ticket(c, tid).get("status") != "valid":
                    probs.append(f"{seat}: refunded after the 48-hour deadline")
                continue
            amount = M.refund_amount(H.model.tickets[tid].total, p)
            t = H.wait_ticket(c, tid, "refunded", 90)
            got = [x["amount"] for x in H.refunds_for_ticket(tid)]
            if t.get("status") != "refunded" or got != [amount]:
                probs.append(f"{seat}: status {t.get('status')}, provider refunds {got}, want [{amount}] ({p} %)")
            else:
                H.model.refund_requested(tid, amount)
                H.model.refund_completed(tid, amount)
        ctx.check(not probs, " | ".join(probs))
    with ctx.step("after a reschedule, orders paid before it get 100 % until the new start; later orders follow the tiers"):
        w, c, email, ev, tix = st["w"], st["c"], st["email"], st["ev"], st["tix"]
        eid = ev["resched"]
        new_start = H.start_in(w["tz"], 60 * 30)
        r = submit_form(w["owner"], f"/orgs/{w['org']}/events/{eid}", "reschedule-event", {"starts_at": instant_to_wall(new_start, w["tz"])},
                        follow=True)
        ctx.eq(H.manage(w["owner"], w["org"], eid)["starts_at"], inst(new_start), "start after rescheduling")
        H.model.events[eid].start = new_start
        tid = tix["resched"]["FLOOR-A-3"]
        t = H.ticket(c, tid)
        ctx.eq((t.get("full-refund-until"), t.get("half-refund-until"), t.get("refund-percent"), t.get("has_refund")),
               (inst(new_start), inst(new_start), "100", True), "markers of a ticket paid before the reschedule")
        e2, c2 = H.account(tag="lateresched")
        _o, later = H.buy(c2, e2, eid, ["BALC-B-5"])
        t2 = H.ticket(c2, later["BALC-B-5"])
        ctx.eq((t2.get("refund-percent"), t2.get("has_refund")), ("0", False), "a ticket bought after the reschedule, 30 h out")
        amount = H.model.tickets[tid].total
        before = H.mail_ids(email)
        H.request_refund(c, tid)
        H.model.refund_requested(tid, amount)
        expect_refund(ctx, c, tid, amount, email, before, "FLOOR-A-3")
    with ctx.step("a buyer who transferred a ticket away, and its new holder, cannot get a buyer refund"):
        c, email, tix = st["c"], st["email"], st["tix"]
        tid = tix["full"]["BALC-A-3"]
        r_email = f"gift.{H.nonce}@verify.example"
        H.transfer(c, tid, r_email, "Gift Taker")
        t = H.ticket(c, tid)
        ctx.check(not t.get("has_refund") and not t.get("token"), "the former holder's page still offers a refund or shows the token")
        H.post_with_token(c, f"/tickets/{tid}/refund", token_from="/tickets")
        r_email, rc = H.account(r_email, "Gift Taker")
        t = H.ticket(rc, tid)
        ctx.check(t.get("token") and not t.get("has_refund"), "the new holder sees no token, or is offered a buyer refund")
        H.post_with_token(rc, f"/tickets/{tid}/refund", token_from="/tickets")
        time.sleep(3)
        ctx.check(not H.refunds_for_ticket(tid) and H.ticket(rc, tid).get("status") == "valid", "a transferred ticket was refunded to a buyer request")


# ======================================================================================== W12
@workflow("W12", "Refund mechanics: Idempotency-Key retries through provider faults, pending until the webhook, events out of order", 4,
          timeout=900, critical=True)
def w12(ctx):
    st = ctx.shared.setdefault("w12", {})
    with ctx.step("a buyer with six tickets for an event ten days away"):
        w = H.quick_world("faults", "America/Chicago")
        eid = H.setup_event(w["owner"], w["org"], w["venue"], "Faulty Provider Night", H.start_in(w["tz"], 60 * 24 * 10),
                            {"BALC": 3170, "FLOOR": 4580}, fee=175)
        email, c = H.account(tag="faultbuyer")
        oid, ids = H.buy(c, email, eid, ["FLOOR-B-1", "FLOOR-B-2", "FLOOR-B-3", "FLOOR-B-4", "BALC-A-9", "BALC-A-10"])
        st.update(w=w, eid=eid, email=email, c=c, ids=ids, sid=H.model.orders[oid].session)
    for fault, seat in (("lost_response", "FLOOR-B-1"), ("timeout", "FLOOR-B-2"), ("error", "FLOOR-B-3")):
        with ctx.step(f"a refund whose first provider call ends in '{fault}' is retried with the same key and made exactly once"):
            c, email, ids = st["c"], st["email"], st["ids"]
            tid = ids[seat]
            amount = H.model.tickets[tid].total
            n0 = len(H.pay.refund_requests())
            H.pay.fault(refund_create=fault, refund_create_delay=3)
            before = H.mail_ids(email)
            r = H.request_refund(c, tid)
            ctx.check(r.status < 400, f"refund request answered {r.status}")
            H.model.refund_requested(tid, amount)
            expect_refund(ctx, c, tid, amount, email, before, seat)
            if fault == "lost_response":
                time.sleep(12)       # any retry after the lost answer has happened by now
                reqs = H.pay.refund_requests()[n0:]
            else:
                reqs = H.pay.wait(lambda s: (lambda r: r if len(r) >= 2 and all(x.get("status") for x in r) else None)(
                    H.pay.refund_requests(s)[n0:]), 30) or H.pay.refund_requests()[n0:]
            keys = {x.get("idempotency_key") for x in reqs}
            got = [r["amount"] for r in H.refunds_for_ticket(tid)]
            if len(got) > 1:
                ctx.breach("X6", f"ticket {tid} was refunded {len(got)} times after a '{fault}' fault")
            ctx.eq(got, [amount], f"provider refunds for {seat} after the retries")
            want_n = 1 if fault == "lost_response" else 2
            ctx.check(len(reqs) >= want_n and len(keys) == 1 and None not in keys,
                      f"provider refund calls for {seat}: {[(x.get('idempotency_key'), x.get('status')) for x in reqs]} "
                      f"(want {'≥2 calls' if want_n == 2 else 'calls'} sharing one Idempotency-Key)")
            gaps = []
            for x, nxt in zip(reqs, reqs[1:]):
                if x.get("status") is None or int(x.get("status") or 0) >= 500:
                    answered = int(x.get("at") or 0) + (3 if x.get("fault") in ("timeout", "lost_response") else 0)
                    gaps.append(int(nxt.get("at") or 0) - answered)
            ctx.observe(f"retry_gaps_{fault}", gaps)
            ctx.check(all(g <= 11 for g in gaps), f"after a failed refund call the next attempt came {gaps} s later (R10.1: within 10 s)")
    with ctx.step("a refund stays refund_pending (no email, not counted) until its refund.created event, delivered out of order"):
        w, eid, c, email, ids = st["w"], st["eid"], st["c"], st["email"], st["ids"]
        tid = ids["FLOOR-B-4"]
        amount = H.model.tickets[tid].total
        before = H.mail_ids(email)
        H.pay.hold(["refund.created", "charge.refunded"])
        try:
            r = H.request_refund(c, tid)
            ctx.check(r.status < 400, f"refund request answered {r.status}")
            H.model.refund_requested(tid, amount)
            refunded = H.pay.wait(lambda s: H.refunds_for_ticket(tid, s) or None, 90)
            ctx.check(refunded, "no provider refund was created")
            time.sleep(4)
            t = H.ticket(c, tid)
            ctx.eq(t.get("status"), "refund_pending", "ticket status while the refund webhook is held")
            ctx.check(not t.get("has_transfer"), "a refund_pending ticket offers a transfer")
            ctx.check(not H.new_mail(email, "refund", before), "a refund email was sent before the refund completed")
            _r, dash = H.dashboard(w["owner"], w["org"], eid)
            want = H.model.dashboard(eid, now())
            ctx.check(not H.dash_diff(dash, want), "dashboard while a refund is pending: " + "; ".join(H.dash_diff(dash, want)[:4]))
            ctx.eq(H.seat_states(H.client(), eid).get("FLOOR-B-4"), "sold", "the seat while its refund is pending")
            s = H.pay.state()
            mine = [e for e in s.get("held", []) if (s["events"][e]["data"]["object"].get("metadata") or {}).get("ticket_id") in (str(tid), tid)
                    or s["events"][e]["data"]["object"].get("id") == refunded[0]["charge"]]
            order = sorted(mine, key=lambda e: 0 if s["events"][e]["type"] == "charge.refunded" else 1)
            H.pay.release(order)
        finally:
            H.pay.release()
        expect_refund(ctx, c, tid, amount, email, before, "FLOOR-B-4")
        st["t4"] = tid
    with ctx.step("refund.created delivered again changes nothing and sends nothing; every refund used its own key"):
        w, eid, c, email = st["w"], st["eid"], st["c"], st["email"]
        tid = st["t4"]
        n0 = len([m for m in H.mail.to(email) if is_refund_mail(m)])
        for ev in H.pay.events(etype="refund.created"):
            if str((ev["data"]["object"].get("metadata") or {}).get("ticket_id")) == str(tid):
                H.pay.redeliver(ev["id"])
        time.sleep(5)
        n1 = len([m for m in H.mail.to(email) if is_refund_mail(m)])
        ctx.eq(n1, n0, "refund emails after a duplicate refund.created")
        _r, dash = H.dashboard(w["owner"], w["org"], eid)
        want = H.model.dashboard(eid, now())
        ctx.check(not H.dash_diff(dash, want), "dashboard after duplicates: " + "; ".join(H.dash_diff(dash, want)[:4]))
        pi = H.pay.intent_of(st["sid"])
        keys = [x.get("idempotency_key") for x in H.pay.refund_requests() if x.get("payment_intent") == pi]
        ctx.eq(len(set(keys)), 4, "distinct Idempotency-Keys over the four refunds")


# ======================================================================================== W13
@workflow("W13", "Organizer refunds and cancelling an event: top-up refunds, one email per address, late payment after cancellation", 5,
          timeout=900, critical=True)
def w13(ctx):
    st = ctx.shared.setdefault("w13", {})
    with ctx.step("an event 100 hours away with buyers, a 50 % buyer refund, a transfer, and a box office refund"):
        w = H.quick_world("cancel", "America/Denver")
        _m, mgr = H.add_member(w["owner"], w["org"], "manager")
        _b, box = H.add_member(w["owner"], w["org"], "box_office")
        eid = H.setup_event(w["owner"], w["org"], w["venue"], "Doomed Tour", H.start_in(w["tz"], 60 * 100), {"BALC": 2990, "FLOOR": 4520},
                            fee=250)
        H.create_code(w["owner"], w["org"], eid, "CREW12", "percent", 12)
        b1, c1 = H.account(tag="cancelb1")
        b2, c2 = H.account(tag="cancelb2")
        b3, c3 = H.account(tag="cancelb3")
        _o1, t1 = H.buy(c1, b1, eid, ["FLOOR-A-3", "FLOOR-A-4"], ["CREW12"])
        _o2, t2 = H.buy(c2, b2, eid, ["BALC-B-1", "BALC-B-2"])
        _o3, t3 = H.buy(c3, b3, eid, ["BALC-A-5"])
        tid = t1["FLOOR-A-3"]
        half = M.refund_amount(H.model.tickets[tid].total, 50)
        before = H.mail_ids(b1)
        H.request_refund(c1, tid)
        H.model.refund_requested(tid, half)
        expect_refund(ctx, c1, tid, half, b1, before, "FLOOR-A-3")
        rcp = f"cancelholder.{H.nonce}@verify.example"
        H.transfer(c2, t2["BALC-B-2"], rcp, "Holder Cancel")
        H.model.transfer(t2["BALC-B-2"], rcp, "Holder Cancel", None)
        page = box.get(f"/orgs/{w['org']}/events/{eid}/orders", follow=True)
        btns = {}
        for n in page.dom.attr_all("data-ticket-id"):
            btns[int(n.attrs["data-ticket-id"])] = (n.attrs.get("data-ticket-status"), any(
                x.attrs.get("data-action") == "organizer-refund" for x in n.find_all(lambda y: "data-action" in y.attrs)))
        ctx.eq(btns.get(tid), ("refunded", False), "orders list entry of the refunded ticket")
        ctx.eq(btns.get(t2["BALC-B-1"]), ("valid", True), "orders list entry of a valid ticket")
        full = H.model.tickets[t2["BALC-B-1"]].total
        before = H.mail_ids(b2)
        r = H.org_refund(box, w["org"], eid, t2["BALC-B-1"])
        ctx.check(r.status < 400, f"box office refund answered {r.status}")
        H.model.refund_requested(t2["BALC-B-1"], full)
        expect_refund(ctx, c2, t2["BALC-B-1"], full, b2, before, "BALC-B-1")
        st.update(w=w, mgr=mgr, box=box, eid=eid, b=(b1, b2, b3), c=(c1, c2, c3), t=(t1, t2, t3), rcp=rcp)
    with ctx.step("only the owner may cancel; the owner cancels while a pending order and a pending 50 % buyer refund exist"):
        w, mgr, box, eid = st["w"], st["mgr"], st["box"], st["eid"]
        for who, cl in (("manager", mgr), ("box office", box)):
            r = H.post_with_token(cl, f"/orgs/{w['org']}/events/{eid}/cancel")
            ctx.eq(r.status, 403, f"{who} POST cancel")
        c1, c3 = st["c"][0], st["c"][2]
        t1 = st["t"][0]
        r, poid = H.hold(c3, eid, ["FLOOR-D-12"], buyer=st["b"][2])
        r, psid = H.pay_click(c3, poid)
        ctx.check(psid, "no session for the pending order")
        st["pending"] = (poid, psid, int(H.order(c3, poid)["total"]))
        page = w["owner"].get(f"/orgs/{w['org']}/events/{eid}", follow=True)
        ctx.check(page.dom.attr_one("data-action", "cancel-event") is not None, "the owner has no cancel button")
        st["before"] = {a: H.mail_ids(a) for a in list(st["b"]) + [st["rcp"]]}
        st["refund_before"] = {a: len([m for m in H.mail.to(a) if is_refund_mail(m)]) for a in st["b"]}
        tid4 = t1["FLOOR-A-4"]
        half4 = M.refund_amount(H.model.tickets[tid4].total, 50)
        H.pay.hold(["refund.created", "charge.refunded"])
        try:
            r = H.request_refund(c1, tid4)
            ctx.check(r.status < 400, f"the 50 % refund request answered {r.status}")
            ok = H.pay.wait(lambda s_: H.refunds_for_ticket(tid4, s_) or None, 90)
            ctx.check(ok, "the 50 % buyer refund never reached the provider")
            ctx.eq(H.ticket(c1, tid4).get("status"), "refund_pending", "the ticket while its refund webhook is held")
            H.model.refund_requested(tid4, half4)
            st["half4"] = half4
            r = submit_form(w["owner"], f"/orgs/{w['org']}/events/{eid}", "cancel-event", {}, follow=True)
            ctx.check(r.status < 400, f"cancel answered {r.status}")
        except BaseException:
            H.pay.release()
            raise
        ctx.eq(H.manage(w["owner"], w["org"], eid)["status"], "cancelled", "event status after cancelling")
        H.model.events[eid].status = "cancelled"
    with ctx.step("every ticket is refunded exactly what it had left (counting the pending refund), once, at the provider"):
        w, eid = st["w"], st["eid"]
        t1, t2, t3 = st["t"]
        c1, c2, c3 = st["c"]
        tid3, tid4 = t1["FLOOR-A-3"], t1["FLOOR-A-4"]
        # what each ticket must receive at the provider, in total, by the end: its whole total (all were valid or partly refunded)
        want = {tid3: sorted([H.model.tickets[tid3].refunded, H.model.tickets[tid3].total - H.model.tickets[tid3].refunded]),
                tid4: sorted([st["half4"], H.model.tickets[tid4].total - st["half4"]]),
                t2["BALC-B-2"]: [H.model.tickets[t2["BALC-B-2"]].total], t3["BALC-A-5"]: [H.model.tickets[t3["BALC-A-5"]].total]}
        try:
            H.pay.wait(lambda s_: all(len(H.refunds_for_ticket(tid, s_)) >= len(v) for tid, v in want.items()) or None, 90)
            time.sleep(3)   # any extra (wrong) refund would have been sent by now
            st_ = H.pay.state()
            probs = []
            for tid, v in want.items():
                rs = sorted(r["amount"] for r in H.refunds_for_ticket(tid, st_))
                if sum(rs) > H.model.tickets[tid].total:
                    ctx.breach("X6", f"ticket {tid} was refunded {sum(rs)} at the provider, more than its total {H.model.tickets[tid].total}")
                if rs != v:
                    probs.append(f"ticket {tid}: provider refunds {rs}, want {v}")
            bx = [r["amount"] for r in H.refunds_for_ticket(t2["BALC-B-1"], st_)]
            if len(bx) != 1:
                probs.append(f"the box-office-refunded ticket was refunded again: {bx}")
                if len(bx) > 1:
                    ctx.breach("X6", "a fully refunded ticket was refunded again at cancellation")
            ctx.check(not probs, " | ".join(probs))
        finally:
            H.pay.release()
        for tid in want:            # every one of these tickets ends fully refunded
            mt = H.model.tickets[tid]
            mt.status, mt.refunded, mt.pending = "refunded", mt.total, 0
        for (cl, tid) in ((c1, tid4), (c3, t3["BALC-A-5"])):
            ctx.eq(H.wait_ticket(cl, tid, "refunded", 90).get("status"), "refunded", f"ticket {tid} after cancellation")
        ctx.eq(H.order(c3, st["pending"][0]).get("status"), "expired", "the pending order after cancellation")
        H.model.orders[st["pending"][0]].status = "expired"
        n = H.event_page(H.client(), eid).dom.attr_one("data-sale-state")
        ctx.eq(n.attrs.get("data-sale-state") if n else None, "cancelled", "data-sale-state of a cancelled event")
        e4, c4 = H.account(tag="toolate")
        ctx.check(H.hold(c4, eid, ["BALC-B-9"])[1] is None, "a cancelled event sold a seat")
    with ctx.step("each buyer and holder gets exactly one cancellation email; the only refund email is the buyer's own 50 % refund"):
        probs = []
        for a in list(st["b"]) + [st["rcp"]]:
            n = len(H.wait_mail(a, "cancelled", st["before"][a], 1, 35))
            if n != 1:
                probs.append(f"{a}: {n} cancellation emails")
        H.wait_mail(st["b"][0], "refund", st["before"][st["b"][0]], 1, 35)
        time.sleep(4)
        for a in list(st["b"]) + [st["rcp"]]:
            n = len(H.new_mail(a, "cancelled", st["before"][a]))
            if n != 1:
                probs.append(f"{a}: {n} cancellation emails after waiting")
        for i, a in enumerate(st["b"]):
            extra = len([m for m in H.mail.to(a) if is_refund_mail(m)]) - st["refund_before"][a]
            allowed = 1 if i == 0 else 0
            if extra > allowed:
                probs.append(f"{a}: {extra} refund emails after the cancellation (at most {allowed})")
        ctx.check(not probs, " | ".join(probs))
    with ctx.step("a payment completing after the cancellation is refunded in full and the buyer told"):
        poid, psid, total = st["pending"]
        b3 = st["b"][2]
        before = H.mail_ids(b3)
        res = H.pay.force_complete(psid)
        ctx.check(res.get("ok"), f"the provider did not complete the late payment: {res}")
        o = H.wait_status(st["c"][2], poid, "unavailable", 90)
        ctx.eq(o.get("status"), "unavailable", "late payment for a cancelled event")
        pi = H.pay.intent_of(psid)
        refunds = H.pay.wait(lambda s: H.pay.refunds_for_intent(pi, s) or None, 90) or []
        ctx.eq([x["amount"] for x in refunds], [total], "provider refunds of the late payment")
        ctx.eq(len(H.wait_mail(b3, "seats no longer available", before)), 1, "'Seats no longer available' emails")
        mo = H.model.orders[poid]
        mo.status, mo.late_paid, mo.late_refunded = "unavailable", 1, 1
        _r, dash = H.dashboard(st["w"]["owner"], st["w"]["org"], st["eid"])
        want = H.model.dashboard(st["eid"], now())
        ctx.check(not H.dash_diff(dash, want), "dashboard of the cancelled event: " + "; ".join(H.dash_diff(dash, want)[:5]))
    with ctx.step("an event with tickets from the old platform cannot be cancelled"):
        owner = H.staff("nadia.brooks@lanternroom.example")
        page = owner.get("/orgs/1/events/11", follow=True)
        ctx.check(page.dom.attr_one("data-action", "cancel-event") is None, "the cancel button is offered on an event with old-platform tickets")
        H.post_with_token(owner, "/orgs/1/events/11/cancel", token_from="/orgs/1/events/11")
        st11 = H.manage(owner, 1, 11)["status"]
        if st11 == "cancelled":
            ctx.breach("X10", "an event with old-platform tickets was cancelled")
        ctx.eq(st11, "published", "status of event 11 after a cancel attempt")
    with ctx.step("cancelling an event ends an open waitlist offer: shown expired or declined (R14.3's endings), no controls, no order"):
        w = st["w"]
        o = w["org"]
        vid = H.create_venue(w["owner"], o, "Cancel Annex", w["tz"], [("DUO", "Duo", "A-A", 2, [])])
        eid = H.setup_event(w["owner"], o, vid, "Duo Night", H.start_in(w["tz"], 60 * 24 * 10), {"DUO": 3900})
        x1, cx1 = H.account(tag="duo1")
        x2, cx2 = H.account(tag="duo2")
        _o, ids1 = H.buy(cx1, x1, eid, ["DUO-A-1"])
        H.buy(cx2, x2, eid, ["DUO-A-2"])
        wt, cw = H.account(tag="duowaiter")
        join(cw, eid, "DUO", 1)
        ctx.eq(section_node(H.event_page(cw, eid), "DUO").attrs.get("data-my-waitlist"), "1", "data-my-waitlist after joining")
        before = H.mail_ids(wt)
        H.org_refund(w["owner"], o, eid, ids1["DUO-A-1"])
        msgs = H.wait_mail(wt, "seats available", before, 1, 75)
        link = H.link_in(msgs, "/waitlist/offers/")
        ctx.check(link, "no offer email after a seat came free")
        op = offer_page(cw, link)
        ctx.eq((op["status"], op["accept"]), ("open", True), "the offer before the cancellation")
        r = submit_form(w["owner"], f"/orgs/{o}/events/{eid}", "cancel-event", {}, follow=True)
        ctx.check(r.status < 400, f"cancel answered {r.status}")
        op = offer_page(cw, link)
        ctx.check(op["status"] in ("open", "accepted", "expired", "declined"),
                  f"data-offer-status {op['status']!r} is not one of open|accepted|expired|declined")
        ctx.check(op["status"] in ("expired", "declined"),
                  f"data-offer-status of an offer that was open when its event was cancelled is {op['status']!r}, want expired or declined")
        ctx.check(not op["accept"] and not op["decline"], "the offer page still offers accept or decline after the cancellation")
        r = H.post_with_token(cw, urllib.parse.urlsplit(link).path + "/accept", token_from=urllib.parse.urlsplit(link).path)
        if r.status in REDIRECTS and "/orders/" in (r.location or ""):
            ctx.breach("X6", "an offer of a cancelled event was accepted into an order")
            raise StepFailed("accepting an offer of a cancelled event created an order")
        ctx.check(section_node(H.event_page(cw, eid), "DUO").attrs.get("data-my-waitlist") is None,
                  "data-my-waitlist is still shown after the event was cancelled")


# ======================================================================================== W14
def tamper(tok: str) -> list[str]:
    out = []
    last = tok[-1]
    out.append(tok[:-1] + ("0" if last != "0" else "1"))
    mid = len(tok) // 2
    out.append(tok[:mid] + ("A" if tok[mid] != "A" else "B") + tok[mid + 1:])
    out.append(tok + "0")
    out.append(tok[:-2])
    out.append(tok.upper() if tok.upper() != tok else tok.lower())
    return [x for x in out if x != tok]


@workflow("W14", "Transfers: the recipient holds a new QR token, every earlier token stops admitting, emails, who may transfer", 4)
def w14(ctx):
    st = ctx.shared.setdefault("w14", {})
    with ctx.step("a buyer transfers a ticket; the recipient and the buyer each get one email; the new token admits, the old one does not"):
        w = H.quick_world("transfer", "America/New_York")
        eid = H.setup_event(w["owner"], w["org"], w["venue"], "Pass It On", H.start_in(w["tz"], 100), {"BALC": 2400, "FLOOR": 3600})
        did, key = H.create_device(w["owner"], w["org"], eid, "Front gate")
        email, c = H.account(tag="giver", name="Gia Giver")
        _oid, ids = H.buy(c, email, eid, ["FLOOR-A-5", "FLOOR-A-6", "BALC-A-1"])
        t1 = ids["FLOOR-A-5"]
        old = H.token(c, t1)
        rcp = f"Taker.{H.nonce}@Verify.Example"
        bg, br = H.mail_ids(email), H.mail_ids(rcp)
        r = H.transfer(c, t1, "  " + rcp + " ", "Tara Taker")
        ctx.check(r.status < 400, f"transfer answered {r.status}")
        got = H.wait_mail(rcp, "you have a ticket", br)
        ctx.eq(len(got), 1, "'You have a ticket' emails to the recipient")
        body = got[0]["text"] or got[0]["html"]
        ctx.check("FLOOR-A-5" in body and str(t1) in body and f"/tickets/{t1}" in body, "the recipient's email lacks seat, id or link")
        ctx.eq(len(H.wait_mail(email, "ticket transferred", bg)), 1, "'Ticket transferred' emails to the former holder")
        t = H.ticket(c, t1)
        ctx.check(not t.get("token") and not t.get("has_transfer"), "the former holder still sees the token or a transfer form")
        H.model.transfer(t1, rcp, "Tara Taker", None)
        r_email, rc = H.account(rcp, "Tara Taker")
        new = H.token(rc, t1)
        ctx.check(new != old and new in body, "the recipient's token is not new or not the one emailed")
        res = H.scan(key, old, "North").json()
        ctx.eq((res.get("result"), res.get("reason")), ("refused", "transferred"), "scan of the pre-transfer token")
        res = H.scan(key, new, "North").json()
        ctx.eq(res.get("result"), "admitted", "scan of the recipient's token")
        H.model.tickets[t1].admitted_at, H.model.tickets[t1].gate = parse_inst(res.get("admitted_at")), "North"
        page = rc.get("/tickets", follow=True)
        ctx.check(any(n.attrs.get("data-ticket-id") == str(t1) for n in page.dom.attr_all("data-ticket-id")), "the ticket is not on the recipient's /tickets")
        st.update(w=w, eid=eid, key=key, email=email, c=c, ids=ids, rcp=r_email, rc=rc)
    with ctx.step("nobody else may transfer it, nor transfer to themselves, nor transfer an admitted ticket"):
        c, rc, ids = st["c"], st["rc"], st["ids"]
        t1 = ids["FLOOR-A-5"]
        H.post_with_token(c, f"/tickets/{t1}/transfer", [("to_email", f"thief.{H.nonce}@verify.example"), ("to_name", "Thief")])
        t = H.ticket(rc, t1)
        if t.get("holder") != st["rcp"]:
            ctx.breach("X1", "the former holder transferred a ticket they no longer hold")
            raise StepFailed("a non-holder transferred the ticket")
        ctx.check(not t.get("has_transfer"), "an admitted ticket offers a transfer form")
        H.post_with_token(rc, f"/tickets/{t1}/transfer", [("to_email", f"later.{H.nonce}@verify.example"), ("to_name", "Later")])
        ctx.eq(H.ticket(rc, t1).get("holder"), st["rcp"], "holder after attempting to transfer an admitted ticket")
        t2 = ids["FLOOR-A-6"]
        H.transfer(c, t2, st["email"].upper(), "Myself Again")
        ctx.eq(H.ticket(c, t2).get("holder"), st["email"], "holder after a transfer to oneself")
        H.transfer(c, t2, f"ctrl.{H.nonce}@verify.example", "Bad\r\nName")
        ctx.eq(H.ticket(c, t2).get("holder"), st["email"], "holder after a transfer with a control character in the name")
    with ctx.step("a chain of transfers leaves only the newest token admitting; altered tokens are invalid"):
        c, key, ids = st["c"], st["key"], st["ids"]
        t3 = ids["BALC-A-1"]
        tok0 = H.token(c, t3)
        r2 = f"second.{H.nonce}@verify.example"
        H.transfer(c, t3, r2, "Second Holder")
        r2, c2 = H.account(r2, "Second Holder")
        tok1 = H.token(c2, t3)
        r3 = f"third.{H.nonce}@verify.example"
        H.transfer(c2, t3, r3, "Third Holder")
        r3, c3 = H.account(r3, "Third Holder")
        tok2 = H.token(c3, t3)
        H.model.transfer(t3, r3, "Third Holder", None)
        for label, tok in (("first", tok0), ("second", tok1)):
            res = H.scan(key, tok, "South").json()
            ctx.eq((res.get("result"), res.get("reason")), ("refused", "transferred"), f"scan of the {label} token")
        for bad in tamper(tok2) + tamper(H.token(st["c"], ids["FLOOR-A-6"])):
            res = H.scan(key, bad, "South").json()
            if res.get("result") in ("admitted", "already_admitted"):
                ctx.breach("X7", f"an altered QR token was admitted: {bad!r}")
                raise StepFailed(f"altered token {bad!r} admitted")
            ctx.eq((res.get("result"), res.get("reason"), res.get("ticket_id")), ("refused", "invalid", None), f"scan of altered token {bad!r}")
        res = H.scan(key, tok2, "South").json()
        ctx.eq(res.get("result"), "admitted", "scan of the newest token")
        H.model.tickets[t3].admitted_at, H.model.tickets[t3].gate = parse_inst(res.get("admitted_at")), "South"
    with ctx.step("an old-platform ticket is transferred here: its barcode stops admitting, the new token admits"):
        cand = None
        raw_holder = {x["id"]: x["holder_email"] for x in H.baseline["legacy_tickets"]}
        staff_emails = {norm(u["email"]) for u in H.baseline["users"]}
        for t in sorted(H.model.tickets.values(), key=lambda t: t.id):
            ev = H.model.events[t.event]
            if t.legacy and t.status == "valid" and ev.org == 1 and ev.start > now() + timedelta(days=30) and ev.status == "published" \
                    and t.admitted_at is None and "@" in t.holder_email and t.holder_email not in H.clients \
                    and t.holder_email not in staff_emails:
                if raw_holder[t.id] != t.holder_email:
                    cand = t
                    break
        if not cand:
            raise InfraError("verifier fixture: no legacy ticket for the transfer check")
        h_email, hc = H.account(cand.holder_email, cand.holder_name)
        ctx.eq(H.token(hc, cand.id), cand.tokens[0], "a legacy ticket's token is its barcode")
        new_holder = f"legacygift.{H.nonce}@verify.example"
        r = H.transfer(hc, cand.id, new_holder, "Legacy Gift")
        ctx.check(r.status < 400, f"transferring a legacy ticket answered {r.status}")
        H.model.transfer(cand.id, new_holder, "Legacy Gift", None)
        ng, ngc = H.account(new_holder, "Legacy Gift")
        tok = H.token(ngc, cand.id)
        ctx.check(tok != cand.tokens[0], "the legacy ticket kept its barcode after a transfer")
        mgr = H.staff("theo.marsh@lanternroom.example")
        did, key = H.create_device(mgr, 1, cand.event, f"Legacy gate {H.nonce}")
        ev = H.model.events[cand.event]
        at = ev.start - timedelta(minutes=30)
        rows = [{"scan_id": f"lg-{H.nonce}-1", "token": cand.tokens[0], "gate": "Legacy", "scanned_at": inst(at)},
                {"scan_id": f"lg-{H.nonce}-2", "token": tok, "gate": "Legacy", "scanned_at": inst(at)}]
        r = H.scan_batch(key, rows)
        want = H.model.scan(cand.event, [dict(x, scanned_at=parse_inst(x["scanned_at"])) for x in rows])
        ctx.eq(r.status, 200, "batch status")
        res = batch_results(r)
        ctx.eq([(x.get("result"), x.get("reason")) for x in res], [(x.get("result"), x.get("reason")) for x in want],
               "results for the old barcode and the new token")
        st["legacy_ticket"] = cand.id


# ======================================================================================== W15
@workflow("W15", "Scanner devices and online check-in: keys shown once, scope, every refusal reason, idempotent rescans, door list", 4,
          critical=True)
def w15(ctx):
    st = ctx.shared.setdefault("w15", {})
    with ctx.step("an owner and manager create devices (key shown once); box office and scanners may not"):
        w = H.quick_world("checkin", "America/Chicago")
        _m, mgr = H.add_member(w["owner"], w["org"], "manager")
        _s, scanner = H.add_member(w["owner"], w["org"], "scanner")
        tz = w["tz"]
        e1 = H.setup_event(w["owner"], w["org"], w["venue"], "Doors Soon", H.start_in(tz, 60), {"BALC": 2500, "FLOOR": 3900})
        e2 = H.setup_event(w["owner"], w["org"], w["venue"], "Other Room", H.start_in(tz, 60), {"BALC": 2500, "FLOOR": 3900})
        e4 = H.setup_event(w["owner"], w["org"], w["venue"], "Doors Later", H.start_in(tz, 60 * 5), {"BALC": 2500, "FLOOR": 3900})
        e5 = H.setup_event(w["owner"], w["org"], w["venue"], "Called Off", H.start_in(tz, 60), {"BALC": 2500, "FLOOR": 3900})
        d1, k1 = H.create_device(mgr, w["org"], e1, "North gate")
        listing = mgr.get(f"/orgs/{w['org']}/events/{e1}/devices", follow=True).text
        ctx.check(k1 not in listing, "the device key is shown again on the devices page")
        r = H.post_with_token(scanner, f"/orgs/{w['org']}/events/{e1}/devices", [("label", "sneaky")])
        ctx.eq(r.status, 403, "scanner POST create-device")
        _d4, k4 = H.create_device(w["owner"], w["org"], e4, "Later gate")
        _d5, k5 = H.create_device(w["owner"], w["org"], e5, "Off gate")
        p = H.quick_world("checkinother", "America/Chicago")
        pe = H.setup_event(p["owner"], p["org"], p["venue"], "Elsewhere", H.start_in(tz, 60), {"BALC": 2500, "FLOOR": 3900})
        email, c = H.account(tag="scanbuyer")
        _o, t1 = H.buy(c, email, e1, ["FLOOR-B-1", "FLOOR-B-2", "FLOOR-B-3", "FLOOR-B-4", "FLOOR-B-5"])
        _o, t2 = H.buy(c, email, e2, ["FLOOR-B-1"])
        _o, t4 = H.buy(c, email, e4, ["FLOOR-B-1"])
        _o, t5 = H.buy(c, email, e5, ["FLOOR-B-1"])
        pemail, pc = H.account(tag="otherscan")
        _o, tp = H.buy(pc, pemail, pe, ["FLOOR-B-1"])
        st.update(w=w, mgr=mgr, scanner=scanner, e=(e1, e2, e4, e5), k=(k1, k4, k5), d1=d1, c=c, t=(t1, t2, t4, t5),
                  tp=tp["FLOOR-B-1"], pc=pc)
    with ctx.step("a valid token is admitted once; scanning it again reports the first admission"):
        c, (k1, _k4, _k5) = st["c"], st["k"]
        t1 = st["t"][0]
        tok = H.token(c, t1["FLOOR-B-1"])
        a0 = now().replace(microsecond=0)
        r = H.scan(k1, tok, "North")
        a1 = now()
        ctx.eq(r.status, 200, "scan status")
        res = r.json()
        at = parse_inst(res.get("admitted_at"))
        ctx.check(res.get("result") == "admitted" and res.get("ticket_id") == t1["FLOOR-B-1"] and res.get("gate") == "North"
                  and at and a0 - timedelta(seconds=1) <= at <= a1 + timedelta(seconds=1), f"first scan result {res}")
        H.model.tickets[t1["FLOOR-B-1"]].admitted_at, H.model.tickets[t1["FLOOR-B-1"]].gate = at, "North"
        time.sleep(1.2)
        res2 = H.scan(k1, tok, "South").json()
        ctx.eq((res2.get("result"), res2.get("admitted_at"), res2.get("gate")), ("already_admitted", res.get("admitted_at"), "North"),
               "second scan")
        ctx.eq(H.ticket(c, t1["FLOOR-B-1"]).get("admitted_at"), res.get("admitted_at"), "data-admitted-at on the ticket page")
    with ctx.step("refusals: invalid, wrong event (same and other organization), refunded, cancelled, outside the window"):
        w, c = st["w"], st["c"]
        (k1, k4, k5), (t1, t2, t4, t5) = st["k"], st["t"]
        probs = []

        def expect(key, tok, reason, tid):
            res = H.scan(key, tok, "North").json()
            if res.get("result") in ("admitted", "already_admitted") and reason in ("invalid", "wrong_event"):
                ctx.breach("X7", f"a {reason} token was admitted: {res}")
            got = (res.get("result"), res.get("reason"), res.get("ticket_id"))
            if got != ("refused", reason, tid):
                probs.append(f"{reason}: got {got}")
        expect(k1, "SW-" + "0" * 40, "invalid", None)
        expect(k1, H.token(c, t2["FLOOR-B-1"]), "wrong_event", t2["FLOOR-B-1"])
        expect(k1, H.token(st["pc"], st["tp"]), "wrong_event", st["tp"])
        tid = t1["FLOOR-B-3"]
        tok3 = H.token(c, tid)
        H.pay.hold(["refund.created", "charge.refunded"])
        try:
            H.org_refund(w["owner"], w["org"], st["e"][0], tid)
            H.pay.wait(lambda s: H.refunds_for_ticket(tid, s) or None, 90)
            expect(k1, tok3, "refunded", tid)
        finally:
            H.pay.release()
        H.wait_ticket(c, tid, "refunded", 90)
        amt = H.model.tickets[tid].total
        H.model.refund_requested(tid, amt)
        H.model.refund_completed(tid, amt)
        expect(k1, tok3, "refunded", tid)
        expect(k4, H.token(c, t4["FLOOR-B-1"]), "outside_window", t4["FLOOR-B-1"])
        tok5 = H.token(c, t5["FLOOR-B-1"])
        submit_form(w["owner"], f"/orgs/{w['org']}/events/{st['e'][3]}", "cancel-event", {}, follow=True)
        expect(k5, tok5, "cancelled", t5["FLOOR-B-1"])
        ctx.check(not probs, " | ".join(probs))
    with ctx.step("unknown and revoked keys get 401; malformed bodies get 400"):
        w, c = st["w"], st["c"]
        k1 = st["k"][0]
        t1 = st["t"][0]
        tok = H.token(c, t1["FLOOR-B-2"])
        ctx.eq(H.scan("sk_" + "x" * 30, tok, "North").status, 401, "unknown key")
        cl = H.client()
        ctx.eq(cl.request("POST", "/api/scan", body=b"{nope", headers={"Authorization": f"Bearer {k1}", "Content-Type": "application/json"}).status,
               400, "malformed JSON")
        ctx.eq(cl.post_json("/api/scan", {"token": tok}, headers={"Authorization": f"Bearer {k1}"}).status, 400, "missing gate")
        ctx.eq(H.scan_batch(k1, [{"scan_id": "x", "token": tok, "gate": "G", "scanned_at": "2027-01-01T10:00:00"}]).status, 400,
               "scanned_at without an offset")
        page = st["mgr"].get(f"/orgs/{w['org']}/events/{st['e'][0]}/devices", follow=True)
        form = next((f for f in page.dom.find_all(lambda n: n.tag == "form" and f"/devices/{st['d1']}/revoke" in (n.attrs.get("action") or ""))), None)
        ctx.check(form is not None, "no revoke form for the device")
        st["mgr"].post_form(f"/orgs/{w['org']}/events/{st['e'][0]}/devices/{st['d1']}/revoke", form_fields(form), follow=True)
        r = H.scan(k1, tok, "North")
        if r.status == 200 and r.json().get("result") == "admitted":
            ctx.breach("X7", "a revoked device key admitted a ticket")
        ctx.eq(r.status, 401, "revoked key")
        page = st["mgr"].get(f"/orgs/{w['org']}/events/{st['e'][0]}/devices", follow=True)
        node = page.dom.attr_one("data-device-id", str(st["d1"]))
        ctx.check(node is not None and node.attrs.get("data-revoked") == "true", "the revoked device is not marked data-revoked=true")
    with ctx.step("the door list offers check-in only for valid tickets not yet admitted; a door check-in admits with gate Door"):
        w, scanner, c = st["w"], st["scanner"], st["c"]
        t1 = st["t"][0]
        e1 = st["e"][0]
        page = scanner.get(f"/orgs/{w['org']}/events/{e1}/door", follow=True)
        ctx.eq(page.status, 200, "scanner GET door list")
        rows = {}
        for n in page.dom.attr_all("data-ticket-id"):
            btn = any(x.attrs.get("data-action") == "door-checkin" for x in n.find_all(lambda y: "data-action" in y.attrs))
            rows[int(n.attrs["data-ticket-id"])] = (n.attrs.get("data-ticket-status"), bool(n.attrs.get("data-admitted-at")), btn)
        want = {t1["FLOOR-B-1"]: ("valid", True, False), t1["FLOOR-B-2"]: ("valid", False, True), t1["FLOOR-B-3"]: ("refunded", False, False),
                t1["FLOOR-B-4"]: ("valid", False, True)}
        for tid, exp in want.items():
            ctx.eq(rows.get(tid), exp, f"door list entry of ticket {tid} (status, admitted, button)")
        tid = t1["FLOOR-B-4"]
        form = None
        for n in page.dom.attr_all("data-ticket-id", str(tid)):
            for f in n.find_all(lambda y: y.tag == "form"):
                form = f
        if form is None:
            for f in page.dom.find_all(lambda y: y.tag == "form" and f"/tickets/{tid}/checkin" in (y.attrs.get("action") or "")):
                form = f
        ctx.check(form is not None, "no door-checkin form for the ticket")
        t0 = now().replace(microsecond=0)
        scanner.post_form(f"/orgs/{w['org']}/tickets/{tid}/checkin", form_fields(form), follow=True)
        page = scanner.get(f"/orgs/{w['org']}/events/{e1}/door", follow=True)
        node = page.dom.attr_one("data-ticket-id", str(tid))
        at = parse_inst(node.attrs.get("data-admitted-at")) if node else None
        ctx.check(at and t0 - timedelta(seconds=1) <= at <= now() + timedelta(seconds=1), "the door check-in did not admit the ticket now")
        ctx.check(not any(x.attrs.get("data-action") == "door-checkin" for x in node.find_all(lambda y: "data-action" in y.attrs)),
                  "the door-checkin button remains after admission")
        H.model.tickets[tid].admitted_at, H.model.tickets[tid].gate = at, "Door"
        mt = H.model.tickets[tid]
        _r, rows_ = H.export(w["owner"], w["org"], e1)[:2]
        row = next((x for x in (rows_ or []) if x["ticket_id"] == str(tid)), None)
        ctx.eq((row or {}).get("gate"), "Door", "gate of a door check-in in the export")
        _ = mt


# ======================================================================================== W16
@workflow("W16", "Offline sync: earliest admission wins across gates and batches, ties, scan_id reuse, windows to the second across DST", 4,
          critical=True)
def w16(ctx):
    st = ctx.shared.setdefault("w16", {})
    with ctx.step("events on the next spring-forward (Chicago) and fall-back (Denver) mornings with tickets and devices"):
        w = H.quick_world("offline", "America/Chicago")
        den = H.create_venue(w["owner"], w["org"], "Offline Denver", "America/Denver", [("BALC", "Balcony", "A-B", 10, []),
                                                                                        ("FLOOR", "Floor", "A-D", 12, [])])
        on_sale = now() - timedelta(minutes=5)
        sp, fa = next_transition("America/Chicago", "spring"), next_transition("America/Denver", "fall")
        # 04:00 on the spring-forward morning: three hours earlier is still standard time (the hour 02:00-03:00 is skipped);
        # 03:00 on the fall-back morning: three hours earlier is the first 01:00 (the hour 01:00-02:00 repeats)
        r, ef = H.create_event(w["owner"], w["org"], w["venue"], "Spring Forward Sunrise", f"{sp}T04:00",
                               instant_to_wall(on_sale, "America/Chicago"), 100, {"BALC": 2000, "FLOOR": 3000})
        r2, eg = H.create_event(w["owner"], w["org"], den, "Fall Back Sunrise", f"{fa}T03:00",
                                instant_to_wall(on_sale, "America/Denver"), 100, {"BALC": 2000, "FLOOR": 3000})
        ctx.check(ef and eg, "creating the DST events failed")
        H.publish(w["owner"], w["org"], ef)
        H.publish(w["owner"], w["org"], eg)
        st["Sf"], st["Sg"] = H.model.events[ef].start, H.model.events[eg].start
        if (st["Sf"] != datetime(sp.year, sp.month, sp.day, 9, 0, tzinfo=UTC)
                or st["Sg"] != datetime(fa.year, fa.month, fa.day, 10, 0, tzinfo=UTC)):
            raise InfraError(f"verifier fixture: DST event starts {st['Sf']} / {st['Sg']} are not 09:00Z / 10:00Z")
        a, ca = H.account(tag="offa")
        b, cb = H.account(tag="offb")
        _o, ta = H.buy(ca, a, ef, [f"FLOOR-C-{n}" for n in range(1, 7)])
        _o, tb = H.buy(cb, b, ef, [f"FLOOR-D-{n}" for n in range(1, 4)])
        _o, tg = H.buy(ca, a, eg, ["BALC-A-1", "BALC-A-2", "BALC-A-3"])
        tix = [ta[f"FLOOR-C-{n}"] for n in range(1, 7)] + [tb[f"FLOOR-D-{n}"] for n in range(1, 4)]
        toks = {t: H.token(ca if i < 6 else cb, t) for i, t in enumerate(tix)}
        gtoks = {t: H.token(ca, t) for t in tg.values()}
        _d, kf = H.create_device(w["owner"], w["org"], ef, "Offline F")
        _d, kg = H.create_device(w["owner"], w["org"], eg, "Offline G")
        cemail, cc = H.account(tag="offc")
        _o, tc = H.buy(cc, cemail, ef, ["FLOOR-B-1", "FLOOR-B-2", "FLOOR-B-3"])
        ctoks = {seat: H.token(cc, tid) for seat, tid in tc.items()}
        rid = tc["FLOOR-B-1"]
        H.org_refund(w["owner"], w["org"], ef, rid)
        ctx.eq(H.wait_ticket(cc, rid, "refunded", 90).get("status"), "refunded", "status after an organizer refund")
        H.model.refund_requested(rid, H.model.tickets[rid].total)
        H.model.refund_completed(rid, H.model.tickets[rid].total)
        dmail = f"offd.{H.nonce}@verify.example"
        H.transfer(cc, tc["FLOOR-B-2"], dmail, "Dee Offline")
        H.model.transfer(tc["FLOOR-B-2"], dmail, "Dee Offline", None)
        dmail, dc = H.account(dmail, "Dee Offline")
        newtok = H.token(dc, tc["FLOOR-B-2"])
        ctx.check(newtok != ctoks["FLOOR-B-2"], "the transfer did not issue a new token")
        st.update(w=w, ef=ef, eg=eg, tix=tix, toks=toks, gt=list(tg.values()), gtoks=gtoks, kf=kf, kg=kg, ca=ca,
                  tc=tc, ctoks=ctoks, newtok=newtok)

    def run(ctx, key, event, rows, label):
        body = [{"scan_id": r["scan_id"], "token": r["token"], "gate": r["gate"], "scanned_at": r.get("raw") or inst(r["scanned_at"])}
                for r in rows]
        resp = H.scan_batch(key, body)
        want = H.model.scan(event, rows)
        ctx.eq(resp.status, 200, f"{label} status")
        got = batch_results(resp)
        ctx.eq(len(got), len(rows), f"{label} result count")
        probs = []
        for i, (g, wv) in enumerate(zip(got, want)):
            if g.get("scan_id") != rows[i]["scan_id"]:
                probs.append(f"row {i}: scan_id {g.get('scan_id')!r}")
            keys = ["result"] + (["reason"] if wv["result"] == "refused" else []) + \
                   (["ticket_id", "admitted_at", "gate"] if wv["result"] in ("admitted", "already_admitted") else [])
            for k in keys:
                if g.get(k) != wv.get(k):
                    probs.append(f"row {i} ({rows[i]['gate']} {inst(rows[i]['scanned_at'])}): {k} {g.get(k)!r} want {wv.get(k)!r}")
        ctx.check(not probs, f"{label}: " + " | ".join(probs[:6]))

    # T(h, m, s): the instant h:m:s UTC on the spring-forward day (the start S is 09:00Z); G likewise on the fall-back day (S 10:00Z)
    T = lambda h, m, s=0: st["Sf"] + timedelta(hours=h - 9, minutes=m, seconds=s)  # noqa: E731
    G = lambda h, m, s=0: st["Sg"] + timedelta(hours=h - 10, minutes=m, seconds=s)  # noqa: E731
    LF = lambda x: x.astimezone(ZoneInfo("America/Chicago")).isoformat()  # noqa: E731  (local time with its offset)
    LG = lambda x: x.astimezone(ZoneInfo("America/Denver")).isoformat()  # noqa: E731
    with ctx.step("batch 1 (North): window edges to the second, a DST-shifted early scan, two scans of one ticket"):
        t, k, n = st["tix"], st["toks"], H.nonce
        rows = [
            {"scan_id": f"n1-{n}", "token": k[t[0]], "gate": "North", "scanned_at": T(6, 20)},
            {"scan_id": f"n2-{n}", "token": k[t[1]], "gate": "North", "scanned_at": T(6, 0, 0)},
            {"scan_id": f"n3-{n}", "token": k[t[2]], "gate": "North", "scanned_at": T(5, 59, 59)},
            {"scan_id": f"n4-{n}", "token": k[t[3]], "gate": "North", "scanned_at": T(14, 59, 59)},
            {"scan_id": f"n5-{n}", "token": k[t[4]], "gate": "North", "scanned_at": T(15, 0, 0)},
            {"scan_id": f"n6-{n}", "token": k[t[5]], "gate": "North", "scanned_at": T(7, 10)},
            {"scan_id": f"n7-{n}", "token": k[t[0]], "gate": "North", "scanned_at": T(6, 25)},
            {"scan_id": f"n8-{n}", "token": k[t[6]], "gate": "North", "scanned_at": T(6, 30), "raw": LF(T(6, 30))},
        ]
        run(ctx, st["kf"], st["ef"], rows, "batch 1")
        st["b1"] = rows
    with ctx.step("batch 2 (South): an earlier scan takes the admission; equal times keep the first; repeated and reused scan_ids"):
        t, k, n = st["tix"], st["toks"], H.nonce
        b1 = st["b1"]
        rows = [
            {"scan_id": f"s1-{n}", "token": k[t[0]], "gate": "South", "scanned_at": T(6, 10)},
            {"scan_id": f"s2-{n}", "token": k[t[5]], "gate": "South", "scanned_at": T(7, 10)},
            dict(b1[1]),
            dict(b1[3], gate="South"),
            dict(b1[2]),
            {"scan_id": f"s3-{n}", "token": k[t[7]], "gate": "South", "scanned_at": T(6, 45)},
            {"scan_id": f"s4-{n}", "token": k[t[7]], "gate": "East", "scanned_at": T(6, 45)},
            {"scan_id": f"s5-{n}", "token": k[t[8]], "gate": "South", "scanned_at": T(13, 0), "raw": LF(T(13, 0))},
            {"scan_id": f"s6-{n}", "token": k[t[8]], "gate": "East", "scanned_at": T(12, 59, 59)},
        ]
        run(ctx, st["kf"], st["ef"], rows, "batch 2")
    with ctx.step("batch 3 (East): later scans report the earliest admission; an online scan now is outside the window"):
        t, k, n = st["tix"], st["toks"], H.nonce
        rows = [{"scan_id": f"e1-{n}", "token": k[t[0]], "gate": "East", "scanned_at": T(6, 15)},
                {"scan_id": f"e2-{n}", "token": k[t[1]], "gate": "East", "scanned_at": T(6, 0, 1)},
                {"scan_id": f"e3-{n}", "token": k[t[3]] + "x", "gate": "East", "scanned_at": T(7, 0)}]
        run(ctx, st["kf"], st["ef"], rows, "batch 3")
        res = H.scan(st["kf"], k[t[2]], "Online").json()
        ctx.eq((res.get("result"), res.get("reason")), ("refused", "outside_window"), "online scan long before the event")
    with ctx.step("batch 4: refunded, transferred-away and other-event tokens are refused for that reason, inside or outside the window"):
        t, n = st["tix"], H.nonce
        tc, ct, g, gk = st["tc"], st["ctoks"], st["gt"], st["gtoks"]
        early, inside = T(1, 0), T(7, 30)   # S - 8 h and S - 1.5 h
        rows = []
        for label, at in (("early", early), ("in", inside)):
            rows += [{"scan_id": f"r4-{label}-1-{n}", "token": ct["FLOOR-B-1"], "gate": "Gate 7", "scanned_at": at},
                     {"scan_id": f"r4-{label}-2-{n}", "token": ct["FLOOR-B-2"], "gate": "Gate 7", "scanned_at": at},
                     {"scan_id": f"r4-{label}-3-{n}", "token": gk[g[2]], "gate": "Gate 7", "scanned_at": at}]
        rows += [{"scan_id": f"r4-early-4-{n}", "token": ct["FLOOR-B-3"], "gate": "Gate 7", "scanned_at": early},
                 {"scan_id": f"r4-early-5-{n}", "token": st["newtok"], "gate": "Gate 7", "scanned_at": early},
                 {"scan_id": f"r4-early-6-{n}", "token": ct["FLOOR-B-3"][:-3] + "zzz", "gate": "Gate 7", "scanned_at": early},
                 {"scan_id": f"r4-in-4-{n}", "token": ct["FLOOR-B-3"], "gate": "Gate 7", "scanned_at": inside},
                 {"scan_id": f"r4-in-5-{n}", "token": st["newtok"], "gate": "Gate 7", "scanned_at": inside}]
        run(ctx, st["kf"], st["ef"], rows, "batch 4")
    with ctx.step("the fall-back event's window opens at the first 01:00 (07:00Z), not at local midnight"):
        g, gk, n = st["gt"], st["gtoks"], H.nonce
        rows = [{"scan_id": f"g1-{n}", "token": gk[g[0]], "gate": "West", "scanned_at": G(6, 30)},
                {"scan_id": f"g2-{n}", "token": gk[g[1]], "gate": "West", "scanned_at": G(7, 0, 0)},
                {"scan_id": f"g3-{n}", "token": gk[g[2]], "gate": "West", "scanned_at": G(15, 59, 59), "raw": LG(G(15, 59, 59))},
                {"scan_id": f"g4-{n}", "token": gk[g[0]], "gate": "West", "scanned_at": G(16, 0, 0)}]
        run(ctx, st["kg"], st["eg"], rows, "fall-back batch")
    with ctx.step("a later request with an equal scan time keeps the first admission; a scan_id belongs to one event only"):
        t, k, n = st["tix"], st["toks"], H.nonce
        g, gk = st["gt"], st["gtoks"]
        run(ctx, st["kf"], st["ef"], [{"scan_id": f"tie-{n}", "token": k[t[1]], "gate": "West", "scanned_at": T(6, 0, 0)}],
            "equal time in a later request")
        run(ctx, st["kg"], st["eg"], [{"scan_id": f"n1-{n}", "token": gk[g[0]], "gate": "West", "scanned_at": G(8, 0)}],
            "a scan_id first used at another event")
    with ctx.step("the export and dashboard show each ticket's earliest admission and its gate"):
        w, ef = st["w"], st["ef"]
        _r, rows, raw = H.export(w["owner"], w["org"], ef)
        probs = H.export_diff(rows, H.model.export_rows(ef))
        ctx.check(not probs, "export: " + " | ".join(probs[:5]))
        _r, dash = H.dashboard(w["owner"], w["org"], ef)
        want = H.model.dashboard(ef, now())
        ctx.check(not H.dash_diff(dash, want), "dashboard: " + "; ".join(H.dash_diff(dash, want)[:4]))
        big = [{"scan_id": f"big{i}", "token": "x", "gate": "G", "scanned_at": inst(T(7, 0))} for i in range(1001)]
        ctx.eq(H.scan_batch(st["kf"], big).status, 400, "a batch of 1001 rows")
