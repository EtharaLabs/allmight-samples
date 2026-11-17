"""Seatwise workflows W17-W26: waiting lists, dashboard, export, reminders, the old platform's data per
organization, the browser battery, deployment and health."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import CARD_OK, H, PASSWORD, REDIRECTS, batch_results, join, now, offer_page, section_node  # noqa: E402
from kitlib import browser as B  # noqa: E402
from kitlib.conc import simultaneously  # noqa: E402
from kitlib.web import Client, FormNotFound, form_fields, submit_form, visible_text  # noqa: E402
from kitlib.wf import InfraError, StepFailed, workflow  # noqa: E402
from reference import money as M  # noqa: E402
from reference.model import CSV_HEADER, DANGER, inst, instant_to_wall, norm, parse_inst  # noqa: E402
from wf_c import expect_refund, is_refund_mail  # noqa: E402

UTC = timezone.utc


# ======================================================================================== W17
@workflow("W17", "Waiting lists: join rules, offers in join order with skip-over, offer expiry and decline, reserved seats", 4, timeout=900)
def w17(ctx):
    st = ctx.shared.setdefault("w17", {})
    with ctx.step("a sold-out section offers a waiting list; a section with seats does not"):
        w = H.quick_world("waitlist", "America/New_York", [("BIG", "Big room", "A-B", 10, []), ("SMALL", "Small room", "A-A", 5, ["A5"])])
        eid = H.setup_event(w["owner"], w["org"], w["venue"], "Tiny Room Session", H.start_in(w["tz"], 60 * 24 * 10), {"BIG": 2000, "SMALL": 6500})
        buyers = {}
        for tag, seats, acc in (("p1", ["SMALL-A-1"], False), ("p2", ["SMALL-A-2"], False), ("p3", ["SMALL-A-3", "SMALL-A-4"], False),
                                ("p4", ["SMALL-A-5"], True)):
            e, c = H.account(tag=f"wl{tag}")
            _o, ids = H.buy(c, e, eid, seats, accessible=acc)
            buyers[tag] = (e, c, ids)
        e1, w1 = H.account(tag="wlwaiter1")
        page = H.event_page(w1, eid)
        small, big = section_node(page, "SMALL"), section_node(page, "BIG")
        ctx.eq((small.attrs.get("data-available") if small else None, big.attrs.get("data-available") if big else None), ("0", "20"),
               "data-available of SMALL and BIG")
        ctx.check(any(f.attrs.get("data-action") == "join-waitlist" for f in small.find_all(lambda n: "data-action" in n.attrs)),
                  "no join-waitlist form inside the sold-out section")
        ctx.check(not any(f.attrs.get("data-action") == "join-waitlist" for f in big.find_all(lambda n: "data-action" in n.attrs)),
                  "a join-waitlist form inside a section with available seats")
        st.update(w=w, eid=eid, buyers=buyers)
    with ctx.step("entries join in order; a second entry and a section with seats are refused"):
        eid = st["eid"]
        waiters = {}
        for tag, qty in (("w1", 2), ("w2", 1)):
            e, c = H.account(tag=f"wlwaiter{tag}")
            join(c, eid, "SMALL", qty)
            waiters[tag] = (e, c)
            time.sleep(1.1)
        join(waiters["w2"][1], eid, "SMALL", 1)
        join(waiters["w2"][1], eid, "BIG", 1)
        time.sleep(1.1)
        e3, c3 = H.account(tag="wlwaiterw3")
        join(c3, eid, "SMALL", 1)
        waiters["w3"] = (e3, c3)
        for tag, qty in (("w1", "2"), ("w2", "1"), ("w3", "1")):
            p = H.event_page(waiters[tag][1], eid)
            ctx.eq(section_node(p, "SMALL").attrs.get("data-my-waitlist"), qty, f"data-my-waitlist of {tag} on SMALL")
            ctx.check(section_node(p, "BIG").attrs.get("data-my-waitlist") is None, f"{tag} is on the BIG waiting list")
        st["waiters"] = waiters
        st["before"] = {t: H.mail_ids(e) for t, (e, _c) in waiters.items()}
    with ctx.step("a freed seat goes to the first entry it fits (skipping a larger one), is held, and the offer is emailed"):
        w, eid, buyers, waiters = st["w"], st["eid"], st["buyers"], st["waiters"]
        tid = buyers["p1"][2]["SMALL-A-1"]
        H.org_refund(w["owner"], w["org"], eid, tid)
        H.wait_ticket(buyers["p1"][1], tid, "refunded", 90)
        msgs = H.wait_mail(waiters["w2"][0], "seats available", st["before"]["w2"], 1, 75)
        ctx.eq(len(msgs), 1, "offer emails to the second entry")
        link = H.link_in(msgs, "/waitlist/offers/")
        ctx.check(link, "the offer email has no offer link")
        ctx.check(not H.new_mail(waiters["w1"][0], "seats available", st["before"]["w1"]), "the larger first entry got an offer for one seat")
        ctx.eq(H.seat_states(H.client(), eid).get("SMALL-A-1"), "held", "SMALL-A-1 while offered")
        e9, c9 = H.account(tag="wlpublic")
        ctx.check(H.hold(c9, eid, ["SMALL-A-1"])[1] is None, "the public held a seat under an offer")
        H.model.offers.append(__import__("reference.model", fromlist=["Offer"]).Offer(eid, ["SMALL-A-1"], now() + timedelta(seconds=H.offer_ttl - 3)))
        _r, dash = H.dashboard(w["owner"], w["org"], eid)
        ctx.eq(dash and dash["sections"].get("SMALL", {}).get("held"), "1", "dashboard held seats in SMALL during the offer")
        st["link2"] = link
        tid2 = buyers["p2"][2]["SMALL-A-2"]
        H.org_refund(w["owner"], w["org"], eid, tid2)
        H.wait_ticket(buyers["p2"][1], tid2, "refunded", 90)
        msgs3 = H.wait_mail(waiters["w3"][0], "seats available", st["before"]["w3"], 1, 75)
        ctx.eq(len(msgs3), 1, "offer emails to the third entry")
        st["link3"] = H.link_in(msgs3, "/waitlist/offers/")
        ctx.check(not H.new_mail(waiters["w1"][0], "seats available", st["before"]["w1"]), "the first entry got an offer it does not fit")
    with ctx.step("only the entrant may accept; accepting creates a pending order for exactly the offered seats"):
        eid, waiters = st["eid"], st["waiters"]
        o = offer_page(waiters["w2"][1], st["link2"])
        ctx.eq((o["status"], o["seats"], o["accept"], o["decline"]), ("open", "SMALL-A-1", True, True), "offer page of the second entry")
        _x, other = H.account(tag="wlintruder")
        p = other.get(st["link2"], follow=True)
        try:
            r = submit_form(other, st["link2"], "accept-offer", {}, follow=False, page=p)
        except FormNotFound:
            r = H.post_with_token(other, urllib.parse.urlsplit(st["link2"]).path + "/accept")
        if r.status in REDIRECTS and "/orders/" in (r.location or ""):
            ctx.breach("X1", "another account accepted someone else's waitlist offer")
            raise StepFailed("another account accepted the offer")
        page = waiters["w2"][1].get(st["link2"], follow=True)
        r = submit_form(waiters["w2"][1], st["link2"], "accept-offer", {}, follow=False, page=page)
        import re as _re
        m = _re.search(r"/orders/(\d+)", r.location or "")
        ctx.check(r.status in REDIRECTS and m, f"accepting answered {r.status} {r.location!r}")
        oid = int(m.group(1))
        o = H.order(waiters["w2"][1], oid)
        ctx.eq((o.get("status"), sorted(o.get("lines", {}))), ("pending", ["SMALL-A-1"]), "order created by accepting")
        from reference.model import Order
        H.model.offers[-1].status = "accepted"
        H.model.add_order(Order(oid, eid, waiters["w2"][0], H.names.get(waiters["w2"][0], ""), ["SMALL-A-1"],
                                H.model.quote_for(eid, ["SMALL-A-1"]), "pending", parse_inst(o.get("hold_expires_at"))))
        r, sid = H.pay_click(waiters["w2"][1], oid)
        H.pay.pay(sid, CARD_OK)
        H.settle_paid(waiters["w2"][1], oid, (now() - timedelta(seconds=5), now() + timedelta(seconds=30)))
        ctx.eq(offer_page(waiters["w2"][1], st["link2"])["status"], "accepted", "offer status after accepting")
    with ctx.step("an expired offer passes on; a seat no entry fits stays reserved (status waitlist) and cannot be held"):
        eid, waiters = st["eid"], st["waiters"]
        time.sleep(H.offer_ttl + 3)
        o = offer_page(waiters["w3"][1], st["link3"])
        ctx.eq((o["status"], o["accept"], o["decline"]), ("expired", False, False), "offer page after its expiry")
        ctx.eq(H.seat_states(H.client(), eid).get("SMALL-A-2"), "waitlist", "SMALL-A-2 reserved for the waiting list")
        e9, c9 = H.account(tag="wlpublic")
        ctx.check(H.hold(c9, eid, ["SMALL-A-2"])[1] is None, "the public held a reserved seat")
    with ctx.step("two reserved seats go to the entry that needed two; declining empties the list and the seats return to sale"):
        w, eid, buyers, waiters = st["w"], st["eid"], st["buyers"], st["waiters"]
        tid = buyers["p3"][2]["SMALL-A-3"]
        H.org_refund(w["owner"], w["org"], eid, tid)
        H.wait_ticket(buyers["p3"][1], tid, "refunded", 90)
        msgs = H.wait_mail(waiters["w1"][0], "seats available", st["before"]["w1"], 1, 75)
        ctx.eq(len(msgs), 1, "offer emails to the first entry")
        link = H.link_in(msgs, "/waitlist/offers/")
        o = offer_page(waiters["w1"][1], link)
        ctx.eq((o["status"], o["seats"]), ("open", "SMALL-A-2 SMALL-A-3"), "offer of two seats in seat order")
        submit_form(waiters["w1"][1], link, "decline-offer", {}, follow=True, page=o["page"])
        ctx.eq(offer_page(waiters["w1"][1], link)["status"], "declined", "offer status after declining")
        time.sleep(2)
        states = H.seat_states(H.client(), eid)
        ctx.eq([states.get("SMALL-A-2"), states.get("SMALL-A-3")], ["available", "available"], "seats once the list is empty")
        e9, c9 = H.account(tag="wlpublic")
        r, oid = H.hold(c9, eid, ["SMALL-A-2"])
        ctx.check(oid, f"the public could not hold a seat once the list was empty ({r.status})")
        H.release(c9, oid)
    with ctx.step("a two-seat offer lists its seats in seat order (seat 9 before seat 10)"):
        w = st["w"]
        vid = H.create_venue(w["owner"], w["org"], "Waitlist Annex", w["tz"], [("TEN", "Ten row", "A-A", 11, [])])
        eid = H.setup_event(w["owner"], w["org"], vid, "Eleven Seats", H.start_in(w["tz"], 60 * 24 * 10), {"TEN": 3300})
        ea, ca = H.account(tag="tena")
        eb, cb = H.account(tag="tenb")
        _o, ia = H.buy(ca, ea, eid, [f"TEN-A-{n}" for n in range(1, 7)])
        _o, ib = H.buy(cb, eb, eid, [f"TEN-A-{n}" for n in range(7, 12)])
        ew, cw = H.account(tag="tenwait")
        join(cw, eid, "TEN", 2)
        before = H.mail_ids(ew)
        for tid in (ib["TEN-A-10"], ib["TEN-A-9"]):
            H.org_refund(w["owner"], w["org"], eid, tid)
            H.wait_ticket(cb, tid, "refunded", 90)
        link = H.link_in(H.wait_mail(ew, "seats available", before, 1, 75), "/waitlist/offers/")
        ctx.check(link, "no offer email for the two freed seats")
        ctx.eq(offer_page(cw, link)["seats"], "TEN-A-9 TEN-A-10", "data-offer-seats of a two-seat offer")


# ======================================================================================== W18
@workflow("W18", "Dashboard figures at request time: sold, held, available, admitted, gross, refunded, net, per section", 4,
          timeout=900, critical=True)
def w18(ctx):
    st = ctx.shared.setdefault("w18", {})
    with ctx.step("an event with paid orders, 50 % and organizer refunds, a late payment refunded, and admissions"):
        w = H.quick_world("dash", "America/Chicago", [("BALC", "Balcony", "A-B", 10, []), ("BOX", "Boxes", "A-A", 4, []),
                                                       ("FLOOR", "Floor", "A-D", 12, ["A1"])])
        _b, box = H.add_member(w["owner"], w["org"], "box_office")
        _s, scanner = H.add_member(w["owner"], w["org"], "scanner")
        eid = H.setup_event(w["owner"], w["org"], w["venue"], "Numbers Night", H.start_in(w["tz"], 60 * 100),
                            {"BALC": 2760, "BOX": 11980, "FLOOR": 4020}, fee=275)
        H.create_code(w["owner"], w["org"], eid, "DASH17", "percent", 17)
        b1, c1 = H.account(tag="dash1")
        b2, c2 = H.account(tag="dash2")
        b3, c3 = H.account(tag="dash3")
        _o, t1 = H.buy(c1, b1, eid, ["BOX-A-1", "BOX-A-2", "FLOOR-B-9"], ["DASH17"])
        _o, t2 = H.buy(c2, b2, eid, ["BALC-A-1", "BALC-A-2"])
        _o, t3 = H.buy(c3, b3, eid, ["FLOOR-C-1"])
        tid = t1["BOX-A-2"]
        half = M.refund_amount(H.model.tickets[tid].total, 50)
        bf = H.mail_ids(b1)
        H.request_refund(c1, tid)
        H.model.refund_requested(tid, half)
        expect_refund(ctx, c1, tid, half, b1, bf, "BOX-A-2")
        tid = t2["BALC-A-2"]
        full = H.model.tickets[tid].total
        bf = H.mail_ids(b2)
        H.org_refund(w["owner"], w["org"], eid, tid)
        H.model.refund_requested(tid, full)
        expect_refund(ctx, c2, tid, full, b2, bf, "BALC-A-2")
        d_email, dc = H.account(tag="dashlate")
        r, doid = H.hold(dc, eid, ["FLOOR-D-12"], buyer=d_email)
        r, dsid = H.pay_click(dc, doid)
        exp = parse_inst(H.order(dc, doid).get("hold_expires_at"))
        time.sleep(max(0.0, (exp - now()).total_seconds()) + 1.6)
        e_email, ec = H.account(tag="dashtaker")
        H.buy(ec, e_email, eid, ["FLOOR-D-12"])
        H.pay.force_complete(dsid)
        ctx.eq(H.wait_status(dc, doid, "unavailable", 90).get("status"), "unavailable", "late payment status")
        pi = H.pay.intent_of(dsid)
        H.pay.wait(lambda s: H.pay.refunds_for_intent(pi, s) or None, 90)
        mo = H.model.orders[doid]
        mo.status, mo.late_paid, mo.late_refunded = "unavailable", 1, 1
        time.sleep(3)
        _d, key = H.create_device(w["owner"], w["org"], eid, "Dash gate")
        S = H.model.events[eid].start
        rows = [{"scan_id": f"d1-{H.nonce}", "token": H.token(c1, t1["BOX-A-1"]), "gate": "Main", "scanned_at": S - timedelta(minutes=50)},
                {"scan_id": f"d2-{H.nonce}", "token": H.token(c3, t3["FLOOR-C-1"]), "gate": "Main", "scanned_at": S - timedelta(minutes=40)}]
        H.model.scan(eid, rows)
        resp = H.scan_batch(key, [dict(r, scanned_at=inst(r["scanned_at"])) for r in rows])
        ctx.eq(resp.status, 200, "scan batch status")
        st.update(w=w, box=box, scanner=scanner, eid=eid, c3=c3, t3=t3)
    with ctx.step("with a refund pending and a hold active, every figure matches for owner and box office; a scanner gets 403"):
        w, eid = st["w"], st["eid"]
        tid = st["t3"]["FLOOR-C-1"]
        H.pay.hold(["refund.created", "charge.refunded"])
        try:
            H.org_refund(w["owner"], w["org"], eid, tid)
            H.pay.wait(lambda s: H.refunds_for_ticket(tid, s) or None, 90)
            H.model.refund_requested(tid, H.model.tickets[tid].total)
            h_email, hc = H.account(tag="dashholder")
            r, hoid = H.hold(hc, eid, ["BALC-B-3", "BALC-B-4"], buyer=h_email)
            ctx.check(hoid, "hold failed")
            want = H.model.dashboard(eid, now())
            for who, cl in (("owner", w["owner"]), ("box office", st["box"])):
                r, dash = H.dashboard(cl, w["org"], eid)
                ctx.eq(r.status, 200, f"{who} dashboard status")
                d = H.dash_diff(dash, want)
                ctx.check(not d, f"{who} dashboard differs: " + "; ".join(d[:6]))
            ctx.eq(st["scanner"].get(f"/orgs/{w['org']}/events/{eid}/dashboard").status, 403, "scanner dashboard")
            st["hold"] = (hc, hoid)
        finally:
            H.pay.release()
    with ctx.step("once the refund completes, refunded and net move; nothing else does"):
        w, eid = st["w"], st["eid"]
        tid = st["t3"]["FLOOR-C-1"]
        ctx.eq(H.wait_ticket(st["c3"], tid, "refunded", 90).get("status"), "refunded", "ticket after the refund webhook")
        H.model.refund_completed(tid, H.model.tickets[tid].total)
        r, dash = H.dashboard(w["owner"], w["org"], eid)
        d = H.dash_diff(dash, H.model.dashboard(eid, now()))
        ctx.check(not d, "dashboard differs: " + "; ".join(d[:6]))
    with ctx.step("the moment a hold expires its seats count as available, before any background work"):
        w, eid = st["w"], st["eid"]
        hc, hoid = st["hold"]
        exp = H.model.orders[hoid].hold_expires
        time.sleep(max(0.0, (exp - now()).total_seconds()) + 1.6)
        r, dash = H.dashboard(w["owner"], w["org"], eid)
        d = H.dash_diff(dash, H.model.dashboard(eid, now()))
        ctx.check(not d, "dashboard right after a hold expired: " + "; ".join(d[:6]))


# ======================================================================================== W19
@workflow("W19", "Attendee export: header, CRLF, seat order (numeric), resold seats, quoting, spreadsheet safety, who may download", 3)
def w19(ctx):
    st = ctx.shared.setdefault("w19", {})
    with ctx.step("tickets whose names, addresses and gates need quoting or neutralizing; a refunded and resold seat"):
        w = H.quick_world("export", "America/New_York")
        _b, box = H.add_member(w["owner"], w["org"], "box_office")
        _s, scanner = H.add_member(w["owner"], w["org"], "scanner")
        eid = H.setup_event(w["owner"], w["org"], w["venue"], "Export Night", H.start_in(w["tz"], 70), {"BALC": 2650, "FLOOR": 3820}, fee=125)
        people = [('Lee, "Sunny" Park', ["FLOOR-B-9", "FLOOR-B-10"]), ("=SUM(A1:A9)", ["FLOOR-B-2"]), ("+Plus One", ["FLOOR-B-11"]),
                  ("-Minus Two", ["BALC-A-1"]), ("@At Three", ["BALC-A-10", "BALC-A-2"])]
        bought = {}
        for name, seats in people:
            e, c = H.account(tag="exp", name=name)
            _o, ids = H.buy(c, e, eid, seats)
            bought[name] = (e, c, ids)
        e, c = H.account(tag="exprefund", name="Refund Then Resell")
        _o, ids = H.buy(c, e, eid, ["FLOOR-C-5"])
        H.org_refund(w["owner"], w["org"], eid, ids["FLOOR-C-5"])
        H.model.refund_requested(ids["FLOOR-C-5"], H.model.tickets[ids["FLOOR-C-5"]].total)
        H.wait_ticket(c, ids["FLOOR-C-5"], "refunded", 90)
        H.model.refund_completed(ids["FLOOR-C-5"], H.model.tickets[ids["FLOOR-C-5"]].total)
        e2, c2 = H.account(tag="expresell", name="Second Buyer")
        H.buy(c2, e2, eid, ["FLOOR-C-5"])
        le, lc, lids = bought['Lee, "Sunny" Park']
        to = f"MiXeD.{H.nonce}@Verify.Example"
        H.transfer(lc, lids["FLOOR-B-10"], to, "Zed, Transfer")
        H.model.transfer(lids["FLOOR-B-10"], to, "Zed, Transfer", None)
        _d, key = H.create_device(w["owner"], w["org"], eid, "Export gates")
        for (name, seat), gate in zip(((people[1][0], "FLOOR-B-2"), (people[3][0], "BALC-A-1"), (people[4][0], "BALC-A-10"),
                                       (people[2][0], "FLOOR-B-11")), ("-Side door", "@Upper", "=Gate", "North, Main")):
            be, bc, bids = bought[name]
            res = H.scan(key, H.token(bc, bids[seat]), gate).json()
            ctx.eq(res.get("result"), "admitted", f"scan at gate {gate!r}")
            H.model.tickets[bids[seat]].admitted_at, H.model.tickets[bids[seat]].gate = parse_inst(res.get("admitted_at")), gate
        avid = H.create_venue(w["owner"], w["org"], "Export Annex", w["tz"], [("ZED", "Zed side", "A-A", 12, []), ("ALP", "Alp side", "A-A", 12, [])])
        aeid = H.setup_event(w["owner"], w["org"], avid, "Export Annex Night", H.start_in(w["tz"], 60 * 24 * 9), {"ZED": 2500, "ALP": 2500})
        ae, ac = H.account(tag="expannex", name="Annex Buyer")
        H.buy(ac, ae, aeid, ["ALP-A-10", "ZED-A-2", "ALP-A-9", "ALP-A-1"])
        st.update(w=w, box=box, scanner=scanner, eid=eid, aeid=aeid)
    with ctx.step("the export has the exact header, CRLF line ends, and every row as the rules say"):
        w, eid = st["w"], st["eid"]
        r, rows, raw = H.export(w["owner"], w["org"], eid)
        ctx.eq(r.status, 200, "export status")
        ctx.check(raw.startswith(",".join(CSV_HEADER) + "\r\n"), "the header row differs or does not end in CRLF")
        body_lines = raw.split("\r\n")
        ctx.check(all("\n" not in ln for ln in body_lines if ln.count('"') == 0), "a row ends without CR")
        want = H.model.export_rows(eid)
        for row in rows or []:
            for k in ("holder_name", "buyer_name", "holder_email", "buyer_email", "gate"):
                if row[k] and row[k][0] in "=+-@" and not row[k].startswith("'"):
                    ctx.breach("X4", f"export wrote {row[k]!r} as a live spreadsheet formula")
        probs = H.export_diff(rows, want)
        ctx.check(not probs, f"{len(probs)} differences: " + " | ".join(probs[:6]))
        _r, arows, _raw = H.export(w["owner"], w["org"], st["aeid"])
        probs = H.export_diff(arows, H.model.export_rows(st["aeid"]))
        ctx.check(not probs, "export of a venue whose sections were created out of order: " + " | ".join(probs[:4]))
    with ctx.step("box office, scanners and other organizations cannot download it"):
        w, eid = st["w"], st["eid"]
        for who, cl in (("box office", st["box"]), ("scanner", st["scanner"])):
            r = cl.get(f"/orgs/{w['org']}/events/{eid}/attendees.csv")
            if r.status == 200 and b"ticket_id" in r.body:
                ctx.breach("X1", f"{who} downloaded the attendee export")
            ctx.eq(r.status, 403, f"{who} GET attendees.csv")
        p = H.quick_world("exportother", "America/New_York")
        for path in (f"/orgs/{p['org']}/events/{eid}/attendees.csv", f"/orgs/{w['org']}/events/{eid}/attendees.csv"):
            r = p["owner"].get(path)
            if r.status == 200 and b"ticket_id" in r.body:
                ctx.breach("X1", f"another organization's owner downloaded {path}")
            ctx.check(r.status in (403, 404), f"another organization's owner GET {path} answered {r.status}")


# ======================================================================================== W20
@workflow("W20", "Reminders and email safety: one email per holder address, one-time key, header injection, names in To", 3)
def w20(ctx):
    st = ctx.shared.setdefault("w20", {})
    with ctx.step("holders: one with two tickets, one who also received a transfer under another name, one transferred away, one refunded"):
        w = H.quick_world("remind", "America/Denver")
        _m, mgr = H.add_member(w["owner"], w["org"], "manager")
        _b, box = H.add_member(w["owner"], w["org"], "box_office")
        eid = H.setup_event(w["owner"], w["org"], w["venue"], "Reminder Night", H.start_in(w["tz"], 60 * 72), {"BALC": 2200, "FLOOR": 3300})
        h1, c1 = H.account(tag="rem1", name="Holder One")
        h2, c2 = H.account(tag="rem2", name="Holder Two")
        h3, c3 = H.account(tag="rem3", name="Holder Three")
        h4, c4 = H.account(tag="rem4", name="Holder Four")
        H.buy(c1, h1, eid, ["FLOOR-B-3", "FLOOR-A-7"])
        H.buy(c2, h2, eid, ["FLOOR-B-5"])
        _o, t3 = H.buy(c3, h3, eid, ["FLOOR-A-3"])
        H.transfer(c3, t3["FLOOR-A-3"], h2.upper(), "Second Name For Two")
        _o, t4 = H.buy(c4, h4, eid, ["FLOOR-C-1"])
        H.org_refund(w["owner"], w["org"], eid, t4["FLOOR-C-1"])
        H.wait_ticket(c4, t4["FLOOR-C-1"], "refunded", 90)
        st.update(w=w, mgr=mgr, box=box, eid=eid, h=(h1, h2, h3, h4))
        st["before"] = {h: H.mail_ids(h) for h in (h1, h2, h3, h4)}
    with ctx.step("a reminder submitted twice at once and again later reaches each holder address exactly once, by name"):
        w, mgr, eid = st["w"], st["mgr"], st["eid"]
        subject = f"Doors open at 7pm {H.nonce}"
        page = mgr.get(f"/orgs/{w['org']}/events/{eid}/reminders", follow=True)
        ctx.check(page.dom.attr_one("data-reminder-key") is not None, "the reminder form has no data-reminder-key input")
        outs = simultaneously([lambda: submit_form(mgr, "", "send-reminder", {"subject": subject, "body": f"Bring your QR code. {H.nonce}"},
                                                   page=page) for _ in range(2)])
        ctx.check(all(k == "ok" for k, _ in outs), "a reminder submission failed outright")
        h1, h2, h3, h4 = st["h"]
        time.sleep(1)
        for h, name in ((h1, "Holder One"), (h2, "Second Name For Two")):
            msgs = [m for m in H.wait_mail(h, subject, st["before"][h], 1, 35) if m["subject"] == subject]
            ctx.eq(len(msgs), 1, f"reminders to {h}")
            ctx.check(f"Bring your QR code. {H.nonce}" in (msgs[0]["text"] or msgs[0]["html"]), "the reminder body is missing")
            full = H.mail._get("/api/v1/message/" + str(msgs[0]["id"]))
            names = [str(t.get("Name", "")) for t in (full.get("To") or [])]
            ctx.check(name in names, f"reminder To names {names!r} for {h}, want {name!r}")
        submit_form(mgr, "", "send-reminder", {"subject": subject, "body": "again"}, page=page)
        time.sleep(5)
        for h in (h1, h2):
            ctx.eq(len([m for m in H.mail.to(h) if m["subject"] == subject]), 1, f"reminders to {h} after resubmitting the same form")
        for h in (h3, h4):
            ctx.eq(len([m for m in H.mail.to(h) if m["subject"] == subject]), 0, f"reminders to {h} (no valid ticket)")
    with ctx.step("typed text never adds headers or recipients; other roles and token-less posts send nothing"):
        w, mgr, box, eid = st["w"], st["mgr"], st["box"], st["eid"]
        evil = f"x{H.nonce}@evil.example"
        submit_form(mgr, f"/orgs/{w['org']}/events/{eid}/reminders", "send-reminder",
                    {"subject": f"Hi\r\nBcc: {evil}\r\nX-Injected: 1", "body": "body"}, follow=True)
        H.signup(f"inj.{H.nonce}@verify.example", f"Eve\r\nBcc: {evil}", PASSWORD)
        time.sleep(4)
        injected = [m for m in H.mail.summaries() if any("evil.example" in t for t in m["to"])]
        if injected:
            ctx.breach("X12", f"typed text added a recipient: {[m['subject'] for m in injected][:2]}")
        ctx.check(not injected, "typed text added an email recipient")
        subj2 = f"Box office probe {H.nonce}"
        r = H.post_with_token(box, f"/orgs/{w['org']}/events/{eid}/reminders", [("subject", subj2), ("body", "x")])
        ctx.eq(r.status, 403, "box office POST reminder")
        subj3 = f"Tokenless {H.nonce}"
        r = submit_form(mgr, f"/orgs/{w['org']}/events/{eid}/reminders", "send-reminder", {"subject": subj3, "body": "x"}, drop_csrf=True)
        time.sleep(4)
        sent = [m for m in H.mail.summaries() if m["subject"] in (subj2, subj3)]
        if any(m["subject"] == subj3 for m in sent):
            ctx.breach("X2", "a reminder was sent by a request without the session token")
        if any(m["subject"] == subj2 for m in sent):
            ctx.breach("X1", "box office sent a reminder")
        ctx.check(not sent and r.status == 403, f"token-less reminder answered {r.status}; probe emails sent: {len(sent)}")


# ======================================================================================== W21-W23
def legacy_segment(ctx, org: int, owner_email: str, quirky_staff: str):
    st = ctx.shared.setdefault(f"legacy{org}", {})
    events = H.model.legacy_events(org)
    with ctx.step("staff set passwords through reset links (addresses stored with odd case and spaces) and sign in"):
        st["owner"] = H.staff(owner_email)
        st["staff"] = H.staff(quirky_staff)
        r = st["owner"].get(f"/orgs/{org}", follow=True)
        ctx.check(r.dom.attr_one("data-my-role", "owner") is not None, "the owner's organization home lacks data-my-role=owner")
    with ctx.step(f"every one of the organization's {len(events)} old events has the dashboard the records imply"):
        probs = []
        for ev in events:
            r, dash = H.dashboard(st["staff"], org, ev.id)
            d = H.dash_diff(dash, H.model.dashboard(ev.id, now()))
            if d:
                probs.append(f"event {ev.id}: " + "; ".join(d[:3]))
        ctx.check(not probs, f"{len(probs)} dashboards differ: " + " | ".join(probs[:4]))
    with ctx.step("every old event's attendee export lists every ticket as recorded (DST folds, transfers, refunds, resold seats)"):
        probs = []
        for ev in events:
            r, rows, raw = H.export(st["owner"], org, ev.id)
            d = H.export_diff(rows, H.model.export_rows(ev.id))
            if d:
                probs.append(f"event {ev.id}: " + "; ".join(d[:2]))
        ctx.check(not probs, f"{len(probs)} exports differ: " + " | ".join(probs[:4]))
    with ctx.step("upcoming old events are on sale at their recorded start, with their sold seats shown sold"):
        probs = []
        for ev in events:
            if ev.start < now() or ev.status != "published":
                continue
            page = H.event_page(H.client(), ev.id)
            n = page.dom.attr_one("data-starts-at")
            if not n or n.attrs.get("data-starts-at") != inst(ev.start):
                probs.append(f"event {ev.id} data-starts-at {n.attrs.get('data-starts-at') if n else None} want {inst(ev.start)}")
            sold = {t.seat for t in H.model.tickets_of(ev.id) if t.status in ("valid", "refund_pending")}
            states = {x.attrs["data-seat"]: x.attrs.get("data-seat-status") for x in page.dom.attr_all("data-seat") if "data-seat-status" in x.attrs}
            got_sold = {s for s, v in states.items() if v == "sold"}
            if got_sold != sold:
                probs.append(f"event {ev.id}: sold seats differ (missing {sorted(sold - got_sold)[:3]}, extra {sorted(got_sold - sold)[:3]})")
            sale = page.dom.attr_one("data-sale-state")
            if not sale or sale.attrs.get("data-sale-state") != "on_sale":
                probs.append(f"event {ev.id} data-sale-state {sale.attrs.get('data-sale-state') if sale else None}")
        ctx.check(not probs, " | ".join(probs[:5]))
    with ctx.step("an old ticket holder signs up and sees exactly their tickets, with barcodes and no refund"):
        staff_emails = {norm(u["email"]) for u in H.baseline["users"]}
        raw = {x["id"]: x["holder_email"] for x in H.baseline["legacy_tickets"]}
        holders: dict = {}
        for t in H.model.tickets.values():
            if t.legacy:
                holders.setdefault(t.holder_email, []).append(t)
        cand = None
        for email, tl in sorted(holders.items()):
            if email in staff_emails or email in H.clients or "@" not in email:
                continue
            mine = [t for t in tl if H.model.events[t.event].org == org]
            if len(tl) >= 2 and any(raw.get(t.id) != t.holder_email for t in tl) and any(H.model.events[t.event].start > now() for t in mine) \
                    and any(H.model.events[t.event].start < now() for t in mine):
                cand = (email, tl)
                break
        if not cand:
            raise InfraError("verifier fixture: no quirky legacy holder")
        email, tl = cand
        _e, c = H.account(email, tl[0].holder_name)
        page = c.get("/tickets", follow=True)
        got = {int(n.attrs["data-ticket-id"]) for n in page.dom.attr_all("data-ticket-id") if n.attrs.get("data-ticket-id", "").isdigit()}
        want = {t.id for t in tl}
        ctx.eq(got, want, f"tickets listed for {email}")
        for t in tl[:6]:
            tk = H.ticket(c, t.id)
            ctx.eq((tk.get("token"), tk.get("status"), tk.get("seat")), (t.tokens[-1], t.status, t.seat), f"legacy ticket {t.id} page")
            ctx.check(not tk.get("has_refund") and tk.get("refund-percent") in (None, "0"), f"legacy ticket {t.id} offers a refund")
            upcoming = H.model.events[t.event].start > now() and t.status == "valid" and t.admitted_at is None
            ctx.eq(tk.get("has_transfer"), upcoming, f"legacy ticket {t.id} transfer form (upcoming valid: {upcoming})")
        tid = tl[0].id
        H.post_with_token(c, f"/tickets/{tid}/refund", token_from="/tickets")
        time.sleep(1)
        ctx.eq(H.ticket(c, tid).get("status"), tl[0].status, "a legacy ticket after a refund request")


@workflow("W21", "Old platform, Lantern Room Presents (Chicago): dashboards, exports, sale pages, holders", 4, timeout=1500, critical=True)
def w21(ctx):
    legacy_segment(ctx, 1, "nadia.brooks@lanternroom.example", "Theo.Marsh@LanternRoom.example")
    with ctx.step("the event the old platform cancelled shows as cancelled"):
        ev = next(e for e in H.model.legacy_events(1) if e.status == "cancelled")
        n = H.event_page(H.client(), ev.id).dom.attr_one("data-sale-state")
        ctx.eq(n.attrs.get("data-sale-state") if n else None, "cancelled", f"data-sale-state of cancelled event {ev.id}")


@workflow("W22", "Old platform, Copperline Live (Denver): dashboards, exports, sale pages, holders", 4, timeout=1500, critical=True)
def w22(ctx):
    legacy_segment(ctx, 2, "marcus.hale@copperline.example", "Leo.Brandt@Copperline.example")


@workflow("W23", "Old platform, Saguaro Sounds (Phoenix): dashboards, exports, sale pages, holders, old barcodes at the door", 4,
          timeout=1500, critical=True)
def w23(ctx):
    legacy_segment(ctx, 3, "lucia.ferreira@saguarosounds.example", "Grace.Yoon@SaguaroSounds.example ")
    with ctx.step("old barcodes admit at an upcoming event's door; a refunded one is refused"):
        ev = next(e for e in H.model.legacy_events(3) if e.start > now() + timedelta(days=30) and e.status == "published")
        mgr = H.staff("omar.saleh@saguarosounds.example")
        _d, key = H.create_device(mgr, 3, ev.id, f"Saguaro door {H.nonce}")
        valid = [t for t in H.model.tickets_of(ev.id) if t.legacy and t.status == "valid" and len(t.tokens) == 1 and t.admitted_at is None][:3]
        refunded = [t for t in H.model.tickets_of(ev.id) if t.legacy and t.status == "refunded"][:1]
        rows = [{"scan_id": f"sg-{H.nonce}-{i}", "token": t.tokens[0], "gate": "Saguaro", "scanned_at": ev.start - timedelta(minutes=20 + i)}
                for i, t in enumerate(valid + refunded)]
        want = H.model.scan(ev.id, rows)
        r = H.scan_batch(key, [dict(x, scanned_at=inst(x["scanned_at"])) for x in rows])
        ctx.eq(r.status, 200, "batch status")
        got = batch_results(r)
        ctx.eq([(g.get("result"), g.get("reason"), g.get("admitted_at")) for g in got],
               [(x.get("result"), x.get("reason"), x.get("admitted_at")) for x in want], "results for old barcodes")
        _r, dash = H.dashboard(mgr, 3, ev.id)
        d = H.dash_diff(dash, H.model.dashboard(ev.id, now()))
        ctx.check(not d, "dashboard after old barcodes were admitted: " + "; ".join(d[:4]))


# ======================================================================================== W24
HOLD_FORM = "form:has([data-action=hold-seats])"

@workflow("W24", "In a real browser: layout at 1280 and 390 px, typed text never runs as script, a click-through purchase", 3, timeout=1200)
def w24(ctx):
    st = ctx.shared.setdefault("w24", {})
    br = None
    try:
        with ctx.step("a plainly named event at Juniper Hall with two seats sold; its dashboard and manage page (experience evidence)"):
            c_email, cowner = H.account(tag="juniperowner", name="Avery Stone")
            corg = H.create_org(cowner, f"Juniper Hall Presents {H.nonce}")
            cvid = H.create_venue(cowner, corg, "Juniper Hall", "America/Chicago",
                                  [("BALC", "Balcony", "A-C", 10, []), ("FLOOR", "Floor", "A-E", 12, ["A1", "A2"])], address="2215 N Milwaukee Ave, Chicago")
            ceid = H.setup_event(cowner, corg, cvid, "Midnight Velvet Revue", H.start_in("America/Chicago", 60 * 24 * 10),
                                 {"BALC": 3200, "FLOOR": 4850}, fee=250, description="A late-night revue with a ten-piece band.")
            j_email, jc = H.account(tag="jordan", name="Jordan Reyes")
            H.buy(jc, j_email, ceid, ["FLOOR-C-4", "FLOOR-C-5"])
            st.update(cowner=cowner, corg=corg, ceid=ceid, cvid=cvid, j_email=j_email)
            br = B.Browser()
            cp = br.page(br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in cowner.cookies.items()]))
            st["cpage"] = cp
            cp.set_viewport_size({"width": 1280, "height": 900})
            cp.goto(H.base + f"/orgs/{corg}/events/{ceid}/dashboard")
            cp.wait_for_load_state()
            B.capture(cp, "dashboard", {"event_id": ceid, "figures": H.model.dashboard(ceid, now())})
            cp.goto(H.base + f"/orgs/{corg}/events/{ceid}")
            cp.wait_for_load_state()
            B.capture(cp, "cancel_before", {"event_id": ceid, "status": "published", "role": "owner", "tickets_sold_here": 2},
                      focus=["[data-action=cancel-event]"])
        with ctx.step("names, venues, sections, events, descriptions, holders and gates carry script payloads"):
            email, owner = H.account(tag="xssowner", name=B.payload("ownername", 0)[:90])
            org = H.create_org(owner, B.payload("orgname", 1)[:90])
            secs = [("XSS", B.payload("section", 2)[:90], "A-B", 10, []), ("SAFE", "Safe seats", "A-C", 12, ["A1"])]
            vid = H.create_venue(owner, org, B.payload("venue", 3)[:90], "America/New_York", secs, address=B.payload("addr", 0)[:90])
            eid = H.setup_event(owner, org, vid, B.payload("event", 1)[:140], H.start_in("America/New_York", 90), {"XSS": 2500, "SAFE": 3500},
                                description=B.payload("desc", 2))
            b_email, bc = H.account(tag="xssbuyer", name=B.payload("buyer", 3)[:90])
            _o, ids = H.buy(bc, b_email, eid, ["SAFE-B-4", "SAFE-B-5"])
            H.transfer(bc, ids["SAFE-B-5"], f"xssgift.{H.nonce}@verify.example", B.payload("holder", 0)[:90])
            _d, key = H.create_device(owner, org, eid, "Gate")
            H.scan(key, H.token(bc, ids["SAFE-B-4"]), "<img src=x onerror=__fk_xss('gate')>")
            st.update(owner=owner, org=org, vid=vid, eid=eid, bc=bc, ids=ids, b_email=b_email)
        with ctx.step("signing in through the browser form reaches a signed-in page"):
            br = br or B.Browser()
            page = br.page(br.context())
            page.goto(H.base + "/signin")
            page.fill("form:has([data-action=signin]) input[name=email]", st["j_email"])
            page.fill("form:has([data-action=signin]) input[name=password]", PASSWORD)
            page.click("form [data-action=signin]")
            page.wait_for_load_state()
            ctx.check(page.locator("[data-action=signout]").count() >= 1, "no sign-out button after a browser sign-in")
            st["page"] = page
            st["opage"] = br.page(br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in st["owner"].cookies.items()]))
            st["xpage"] = br.page(br.context(cookies=[{"name": k, "value": v, "url": H.base} for k, v in st["bc"].cookies.items()]))
        with ctx.step("a buyer clicks through: seats, hold, pay on the hosted page, back to a paid order"):
            page, ceid = st["page"], st["ceid"]
            S = H.model.events[ceid].start
            q_ = H.model.quote_for(ceid, ["FLOOR-C-6", "FLOOR-C-7"])
            order_truth = lambda status: {"status": status, "face_cents": [4850, 4850], "service_fee_cents": q_.service,  # noqa: E731
                                          "facility_fee_cents": q_.facility, "tax_cents": q_.tax, "total_cents": q_.total,
                                          "hold_seconds": H.hold_ttl}
            ticket_truth = {"refund_percent_now": 100, "venue_time_zone": "America/Chicago", "event_start_local": instant_to_wall(S, "America/Chicago"),
                            "full_refund_until_local": instant_to_wall(S - timedelta(hours=168), "America/Chicago"),
                            "half_refund_until_local": instant_to_wall(S - timedelta(hours=48), "America/Chicago")}
            oid = tid = None
            done: set = set()
            try:
                page.set_viewport_size({"width": 1280, "height": 900})
                page.goto(H.base + f"/events/{ceid}")
                page.wait_for_load_state()
                B.capture(page, "event_page", {"event_id": ceid, "venue_time_zone": "America/Chicago", "start_local": instant_to_wall(S, "America/Chicago"),
                                               "section_prices_cents": {"BALC": 3200, "FLOOR": 4850}, "accessible_seats": ["FLOOR-A-1", "FLOOR-A-2"],
                                               "sold_seats": ["FLOOR-C-4", "FLOOR-C-5"]}, focus=["[data-seat='FLOOR-C-6']"])
                page.locator(f"{HOLD_FORM} input[name=seats][value='FLOOR-C-6']").check(force=True)
                page.locator(f"{HOLD_FORM} input[name=seats][value='FLOOR-C-7']").check(force=True)
                page.click(f"{HOLD_FORM} [data-action=hold-seats]")
                page.wait_for_load_state()
                ctx.check("/orders/" in page.url, f"holding through the browser landed on {page.url}")
                oid = int(re.search(r"/orders/(\d+)", page.url).group(1))
                B.capture(page, "order_pending", dict(order_truth("pending"), order_id=oid), focus=["[data-action=pay]"])
                done.add("order_pending")
                page.click("form [data-action=pay]")
                page.wait_for_load_state()
                ctx.check(page.url.startswith(H.pay.api), f"Pay led to {page.url}, not the provider's page")
                page.fill("#card_number", CARD_OK)
                page.click("#pay")
                page.wait_for_load_state()
                ok = False
                for _ in range(60):
                    page.goto(H.base + f"/orders/{oid}")
                    page.wait_for_load_state()
                    if page.locator(f"[data-order-id='{oid}']").count() and page.locator("[data-order-status='paid']").count():
                        ok = True
                        break
                    time.sleep(0.5)
                ctx.check(ok, "the order did not become paid after paying on the hosted page")
                tid = int(page.locator("[data-ticket-id]").first.get_attribute("data-ticket-id"))
                st["coid"], st["ctid"] = oid, tid
                page.goto(H.base + f"/tickets/{tid}")
                page.wait_for_load_state()
                B.capture(page, "ticket_page", dict(ticket_truth, ticket_id=tid), focus=["[data-qr-token]", "[data-action=transfer-ticket]"])
                done.add("ticket_page")
            finally:   # evidence whenever an order or ticket exists, even when a check above failed
                try:
                    if oid and "order_pending" not in done:
                        page.goto(H.base + f"/orders/{oid}")
                        page.wait_for_load_state()
                        B.capture(page, "order_pending", dict(order_truth("as shown on the page"), order_id=oid), focus=["[data-action=pay]"])
                    if tid and "ticket_page" not in done:
                        page.goto(H.base + f"/tickets/{tid}")
                        page.wait_for_load_state()
                        B.capture(page, "ticket_page", dict(ticket_truth, ticket_id=tid), focus=["[data-qr-token]"])
                except Exception:
                    pass
        with ctx.step("a refused hold (6 more seats over the buyer's 4) for the experience review"):
            page, ceid = st["page"], st["ceid"]
            page.goto(H.base + f"/events/{ceid}")
            page.wait_for_load_state()
            try:
                for lab in ("BALC-A-1", "BALC-A-2", "BALC-A-3", "BALC-A-4", "BALC-A-5", "BALC-A-6"):
                    page.locator(f"{HOLD_FORM} input[name=seats][value='{lab}']").check(force=True)
                page.click(f"{HOLD_FORM} [data-action=hold-seats]")
                page.wait_for_load_state()
            except Exception:
                pass
            B.capture(page, "hold_refused", {"refused": True, "reason": "the buyer already has 4 tickets for this event; 6 more would pass the 6-ticket limit"})
        org, eid, vid, ids = st["org"], st["eid"], st["vid"], st["ids"]
        owner_urls = [f"/orgs/{org}", f"/orgs/{org}/members", f"/orgs/{org}/venues/{vid}", f"/orgs/{org}/events/{eid}",
                      f"/orgs/{org}/events/{eid}/dashboard", f"/orgs/{org}/events/{eid}/orders", f"/orgs/{org}/events/{eid}/door",
                      f"/orgs/{org}/events/{eid}/devices", f"/orgs/{org}/events/{eid}/codes", f"/orgs/{org}/events/{eid}/reminders"]
        buyer_urls = ["/", f"/events/{eid}", "/tickets", f"/tickets/{ids['SAFE-B-4']}", f"/tickets/{ids['SAFE-B-5']}"]
        with ctx.step("typed text and URL parts are shown as text on every page (no script runs)"):
            q = urllib.parse.quote
            reflected = [f"/events/{q(B.payload('evp', 0))}", f"/orders/{q(B.payload('ordp', 1))}", f"/tickets/{q(B.payload('tkp', 2))}",
                         f"/orgs/{q(B.payload('orgp', 3))}", "/signin?next=" + q(B.payload("next", 3)), f"/events/{eid}?x=" + q(B.payload("qx", 2)),
                         f"/waitlist/offers/{q(B.payload('offp', 0))}", f"/verify/{q(B.payload('verp', 1))}",
                         f"/password-reset/{q(B.payload('rstp', 2))}", f"/invites/{q(B.payload('invp', 3))}", "/" + q(B.payload("root", 0))]
            for u in owner_urls:
                st["opage"].goto(H.base + u)
                st["opage"].wait_for_load_state()
            for u in buyer_urls + reflected:
                st["xpage"].goto(H.base + u)
                st["xpage"].wait_for_load_state()
            st["xpage"].wait_for_timeout(400)
            if br.xss_hits:
                ctx.breach("X3", f"typed text ran as script: {sorted(set(br.xss_hits))[:5]}")
            ctx.check(not br.xss_hits, f"script ran from typed text: {sorted(set(br.xss_hits))[:5]}")
        with ctx.step("every page stays within 1280 and 390 CSS pixels without horizontal scrolling"):
            probs = []
            corg, ceid = st["corg"], st["ceid"]
            # the plainly named Juniper Hall pages (the script-payload pages carry long unbreakable strings)
            owner_pages = [f"/orgs/{corg}/events/{ceid}", f"/orgs/{corg}/events/{ceid}/dashboard", f"/orgs/{corg}/events/{ceid}/orders",
                           f"/orgs/{corg}/venues/{st['cvid']}"]
            buyer_pages = [f"/events/{ceid}", "/tickets"] + ([f"/orders/{st['coid']}", f"/tickets/{st['ctid']}"] if st.get("coid") else [])
            for pg, urls in ((st["cpage"], owner_pages), (st["page"], buyer_pages)):
                for u in urls:
                    for wdt in (1280, 390):
                        pg.set_viewport_size({"width": wdt, "height": 900})
                        pg.goto(H.base + u)
                        pg.wait_for_load_state()
                        ov = B.horizontal_overflow(pg)
                        if ov > 2:
                            probs.append(f"{u} overflows by {ov}px at {wdt}px")
            ctx.check(not probs, " | ".join(probs[:5]))
    finally:
        B.mark_unreached(["event_page", "order_pending", "ticket_page", "dashboard", "cancel_before", "hold_refused", "confirmation_email"])
        if br:
            br.close()


# ======================================================================================== W25
@workflow("W25", "Deployment: README, restart persistence (accounts, orders, QR tokens), nothing re-sent, old records kept", 2,
          timeout=900)
def w25(ctx):
    with ctx.step("README.md and an executable start.sh are in the workspace"):
        ws = Path(H.workspace)
        ctx.check((ws / "README.md").is_file() and (ws / "README.md").stat().st_size > 40, "README.md missing")
        ctx.check(os.access(ws / "start.sh", os.X_OK), "start.sh is not executable")
    with ctx.step("after a restart: accounts, orders and QR tokens still work, and no email is sent again"):
        time.sleep(10)
        before = {}
        for m in H.mail.summaries():
            for t in m["to"]:
                before[(t, m["subject"])] = before.get((t, m["subject"]), 0) + 1
        w14 = ctx.shared.get("w14") or {}
        ctx.check(H.restart_app(), "the application did not answer /healthz after a restart")
        time.sleep(15)
        after = {}
        for m in H.mail.summaries():
            for t in m["to"]:
                after[(t, m["subject"])] = after.get((t, m["subject"]), 0) + 1
        resent = {k: after[k] - before.get(k, 0) for k in after if after[k] > before.get(k, 0)}
        if resent:
            ctx.breach("X6", f"{len(resent)} emails were sent again after a restart: {list(resent)[:3]}")
        ctx.eq(resent, {}, "emails sent again after the restart")
        c, _ = H.signin(ctx.shared.get("w01", {}).get("a") or next(iter(H.clients)))
        ctx.check(H.signed_in(c), "a password did not survive the restart")
        w05 = ctx.shared.get("w05") or {}
        if w05.get("oid"):
            c2, _ = H.signin(w05["email"])
            ctx.eq(H.order(c2, w05["oid"]).get("status"), "paid", "a paid order after the restart")
        w15 = ctx.shared.get("w15") or {}
        if w15.get("t"):
            cb = H.clients.get(next((k for k in H.clients if k.startswith("scanbuyer")), ""), None)
            t2 = w15["t"][0]["FLOOR-B-2"]
            if cb:
                cb2, _ = H.signin(next(k for k in H.clients if k.startswith("scanbuyer")))
                tok = H.token(cb2, t2)
                _d, key = H.create_device(w15["w"]["owner"], w15["w"]["org"], w15["e"][0], "After restart")
                res = H.scan(key, tok, "Restart").json()
                ctx.check(res.get("result") in ("admitted", "already_admitted"), f"a token issued before the restart scanned as {res}")
        _ = w14
    with ctx.step("the old platform's records are all still there, unchanged"):
        b = H.baseline
        changed = []
        from snapshot import TABLES
        for table, sql in TABLES.items():
            if table == "memberships":
                cur = {(r["organization_id"], r["user_id"]): r for r in H.q(sql)}
                for r in b[table]:
                    if cur.get((r["organization_id"], r["user_id"])) != r:
                        changed.append(f"membership {r['organization_id']}/{r['user_id']}")
                continue
            if table == "event_prices":
                cur = {(r["event_id"], r["section_id"]): r for r in H.q(sql)}
                for r in b[table]:
                    if cur.get((r["event_id"], r["section_id"])) != r:
                        changed.append(f"event_price {r['event_id']}/{r['section_id']}")
                continue
            cur = {r["id"]: r for r in H.q(sql)}
            import json as _j
            for r in b[table]:
                now_r = cur.get(r["id"])
                if now_r is None or _j.loads(_j.dumps(now_r, default=str)) != r:
                    changed.append(f"{table} {r['id']}")
        if changed:
            ctx.breach("X10", f"pre-existing records changed: {changed[:8]}")
        ctx.check(not changed, f"pre-existing records changed: {changed[:8]}")


# ======================================================================================== W26
@workflow("W26", "Cross-cutting safety: secrets only hashed at rest, reset ends sessions, Host-proof links, headers, no stack traces",
          3, timeout=900)
def w26(ctx):
    from kitlib import xcut
    st = ctx.shared.setdefault("w26", {})
    with ctx.step("setting a new password through a reset link signs out every existing session"):
        email, c_old = H.account(tag="xcutreset")
        c_other, _ = H.signin(email)
        ctx.check(H.signed_in(c_old) and H.signed_in(c_other), "could not sign in twice")
        ctx.check(H.reset_password(email, "Rotated-Pass-77x"), "the reset flow failed")
        survivors = [n for n, c in (("first", c_old), ("second", c_other)) if H.signed_in(c)]
        if survivors:
            ctx.breach("X13", f"{len(survivors)} session(s) still signed in after the password was reset")
        ctx.check(not survivors, f"sessions survived the password reset: {survivors}")
        c_new, _ = H.signin(email, "Rotated-Pass-77x")
        ctx.check(H.signed_in(c_new), "the new password does not sign in")
        H.secrets.append("Rotated-Pass-77x")
        H.clients[email] = c_new
        st["email"], st["c"] = email, c_new
    with ctx.step("emailed links start with PUBLIC_BASE_URL whatever Host header the request carried"):
        from kitlib.mail import links as _links
        from kitlib.web import find_form
        evil = {"Host": "evil.example", "X-Forwarded-Host": "evil.example"}

        def probe(client, form_path, action, extra, post_path, addr, fragment):
            """POST the form with a forged Host. An app may refuse that request (>= 400): then the same request
            without the forged Host collects the link. Returns (links found, whether the forged request was refused)."""
            before = H.mail_ids(addr)
            page = client.get(form_path, follow=True)
            try:
                fields = form_fields(find_form(page.dom, action))
            except Exception:
                fields = []
            r = client.post_form(post_path, fields + extra, headers=evil)
            refused = r.status >= 400
            if refused:
                page = client.get(form_path, follow=True)
                try:
                    fields = form_fields(find_form(page.dom, action))
                except Exception:
                    fields = []
                client.post_form(post_path, fields + extra)
            msgs = H.mail.wait_for(addr, lambda m: m["id"] not in before, 1, 35)
            return [l for m in msgs for l in _links(m["text"] or m["html"]) if fragment in l], refused

        email = st["email"]
        found = []
        got, _ = probe(H.client(), "/password-reset", "request-reset", [("email", email)], "/password-reset", email, "/password-reset/")
        found += got
        newbie = f"hostsignup.{H.nonce}@verify.example"
        got, _ = probe(H.client(), "/signup", "signup", [("email", newbie), ("full_name", "Host Probe"), ("password", "Host-Probe-Pass-1")],
                       "/signup", newbie, "/verify/")
        found += got
        w = H.quick_world("xcuthost", "America/Chicago")
        inv = f"hostinvite.{H.nonce}@verify.example"
        got, _ = probe(w["owner"], f"/orgs/{w['org']}/members", "invite-member", [("email", inv), ("role", "scanner")],
                       f"/orgs/{w['org']}/members", inv, "/invites/")
        found += got
        H.secrets += [l.rstrip("/").rsplit("/", 1)[-1] for l in found]
        forged = [l for l in found if urllib.parse.urlsplit(l).hostname == "evil.example"]
        if forged:
            ctx.breach("X14", f"an emailed link was built from the request's Host header: {forged[0][:80]}")
        bad = [l for l in found if not l.startswith(H.base + "/")]
        ctx.check(len(found) >= 3 and not bad, f"emailed links {found[:3]} (each must start with PUBLIC_BASE_URL)")
        st["w"] = w
    with ctx.step("signed-in pages carry Cache-Control: no-store and every page an anti-framing header"):
        w, c = st["w"], st["c"]
        eid = H.setup_event(w["owner"], w["org"], w["venue"], "Header Night", H.start_in(w["tz"], 60 * 24 * 9), {"BALC": 2000, "FLOOR": 3000})
        oid, ids = H.buy(c, st["email"], eid, ["BALC-A-1"])
        problems = []
        for cl, path in ((w["owner"], f"/orgs/{w['org']}"), (w["owner"], f"/orgs/{w['org']}/members"),
                         (w["owner"], f"/orgs/{w['org']}/events/{eid}"), (w["owner"], f"/orgs/{w['org']}/events/{eid}/dashboard"),
                         (w["owner"], f"/orgs/{w['org']}/events/{eid}/attendees.csv"), (w["owner"], f"/orgs/{w['org']}/events/{eid}/devices"),
                         (c, "/tickets"), (c, f"/tickets/{ids['BALC-A-1']}"), (c, f"/orders/{oid}"), (c, f"/events/{eid}")):
            r = cl.get(path)
            ctx.check(r.status == 200, f"{path} answered {r.status}")
            hp = xcut.header_problems(r)
            if path.endswith(".csv"):   # a download is never shown in a frame: only no-store applies to it
                hp = [x for x in hp if "Cache-Control" in x]
            problems += hp
        for path in ("/signin", "/signup", f"/events/{eid}", "/"):
            problems += xcut.header_problems(H.client().get(path, follow=True), need_no_store=False)
        ctx.check(not problems, "; ".join(problems[:4]))
        st["eid"] = eid
    with ctx.step("malformed requests get a plain error page without stack traces, SQL, or file paths"):
        w, c, eid = st["w"], st["c"], st["eid"]
        o = w["org"]
        tok = H.csrf(w["owner"], f"/orgs/{o}/events/{eid}")
        ctok = H.csrf(c, f"/events/{eid}")
        _d, key = H.create_device(w["owner"], o, eid, "Probe gate")
        cl = H.client()
        probes = [
            c.get("/events/99999999999999999999999"), c.get("/orders/abc"), c.get("/tickets/%00"),
            w["owner"].get(f"/orgs/{o}/events/99999999999999999999/dashboard"), w["owner"].get(f"/orgs/abc/events/{eid}/attendees.csv"),
            w["owner"].post_form(f"/orgs/{o}/events/new", tok + [("venue_id", "1;DROP"), ("name", "X"), ("starts_at", "2027-13-45T25:61"),
                                                             ("on_sale_at", "never"), ("facility_fee_cents", "1e99"), ("price_BALC", "-0x1")]),
            w["owner"].post_form(f"/orgs/{o}/events/{eid}/codes", tok + [("code", "' OR 1=1--"), ("kind", "percent"), ("value", "99999999999999999999")]),
            c.post_form(f"/events/{eid}/hold", ctok + [("seats", "' OR 1=1--"), ("seats", "BALC-Z-999999999999")]),
            c.post_form(f"/events/{eid}/sections/%27%3B/waitlist", ctok + [("quantity", "NaN")]),
            cl.post_json("/api/scan", {"token": {"$gt": ""}, "gate": []}, headers={"Authorization": f"Bearer {key}"}),
            cl.post_json("/api/scan/batch", {"scans": [{"scan_id": [], "token": None, "gate": 5, "scanned_at": "2027-02-30T99:00:00Z"}]},
                         headers={"Authorization": f"Bearer {key}"}),
            cl.request("POST", "/webhooks/payments", body=b'{"type": "checkout.session.completed", "data": 7}',
                       headers={"Content-Type": "application/json", "Stripe-Signature": "t=x,v1=y"}),
        ]
        problems = [f"{r.url} ({r.status}): {p.pattern[:40]!r}" for r in probes for p in xcut.LEAK_PATTERNS
                    if p.search(visible_text(r.text))]   # what the page shows, not its markup or asset URLs
        ctx.check(not problems, "; ".join(problems[:4]))
    with ctx.step("no password, token, session id, device key or signing key is stored in plain text (database, workspace files)"):
        sk = os.environ.get("TICKET_SIGNING_KEY", "")
        names = H.session_cookie_names(st["c"])   # only the session cookie(s) are session identifiers (R2.6, R2.8)
        sess = [v for n, v in H.session_values if n in names][-12:]
        needles = [s for s in set(H.secrets) if len(s) >= 10] + sess + ([sk] if len(sk) >= 10 else [])
        hits = H.db_secret_hits(needles, skip=("legacy_tickets", "legacy_orders", "legacy_transfers", "seats"))
        if hits:
            ctx.breach("X8", f"plain-text secret in the database: {sorted(set(hits))}")
        files = xcut.scan_files_for_secrets([H.workspace], needles)
        if files:
            ctx.breach("X8", f"secrets stored in the application's files: {files[:3]}")
        ctx.check(not hits and not files, f"secrets at rest: database {sorted(set(hits))[:4]}, files {files[:3]}")


# ======================================================================================== W27
@workflow("W27", "Health follows the database: non-2xx within 10 s of a stop, 200 within 60 s of a start", 2, timeout=400)
def w27(ctx):
    stop, start = os.environ.get("FK_DB_STOP", ""), os.environ.get("FK_DB_START", "")

    def probe() -> int | None:
        try:
            return Client(H.base, timeout=5).get("/healthz").status
        except Exception:  # no answer within 5 s, or the connection failed
            return None

    with ctx.step("/healthz answers 200 while the database is up"):
        if not (stop and start):
            raise InfraError("verifier fixture: no database hooks")
        ctx.eq(probe(), 200, "/healthz status")
    try:
        with ctx.step("within 10 s of the database stopping, /healthz answers a non-2xx status within 5 s"):
            if subprocess.run([stop], check=False, capture_output=True).returncode != 0:
                raise InfraError("verifier fixture: the database did not stop")
            t0, seen = time.monotonic(), None
            while time.monotonic() - t0 < 10:
                s = probe()
                if s is not None and not (200 <= s < 300):
                    seen = round(time.monotonic() - t0, 1)
                    break
                time.sleep(0.5)
            ctx.observe("non2xx_after_s", seen)
            ctx.check(seen is not None, "no non-2xx answer within 10 s of the database stopping")
    finally:
        restarted = subprocess.run([start], check=False, capture_output=True).returncode == 0
    with ctx.step("within 60 s of the database starting again, /healthz answers 200 without a restart"):
        if not restarted:
            raise InfraError("verifier fixture: the database did not start again")
        t0, ok = time.monotonic(), False
        while time.monotonic() - t0 < 60:
            if probe() == 200:
                ok = True
                break
            time.sleep(1)
        ctx.observe("back_after_s", round(time.monotonic() - t0, 1))
        ctx.check(ok, "/healthz did not answer 200 within 60 s of the database starting")
