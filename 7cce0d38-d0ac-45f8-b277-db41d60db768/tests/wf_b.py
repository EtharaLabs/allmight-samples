"""Seatwise workflows W06-W10: prices at scale, promo codes, hold expiry and late payments, webhook
integrity, seat races and the per-buyer limit. Each workflow is strict."""

from __future__ import annotations

import json
import random
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import CARD_DECLINED, CARD_OK, H, REDIRECTS, join, now, offer_page, section_node  # noqa: E402
from kitlib.conc import simultaneously  # noqa: E402
from kitlib.web import FormNotFound, submit_form, visible_text  # noqa: E402
from kitlib.wf import InfraError, StepFailed, workflow  # noqa: E402
from reference import money as M  # noqa: E402
from reference.model import Order, inst, norm, parse_inst, seat_parts  # noqa: E402

UTC = timezone.utc
TIES = Path(__file__).resolve().parent / "reference" / "ties.json"
PRICING_SECTIONS = [(c, f"Pricing {c}", "A-B", 8, []) for c in ("PA", "PB", "PC", "PD", "PE", "PF")]


# ======================================================================================== W06
# W06 quotes 116 fixture and ordinary orders plus 25 seat-order orders, and pays 4. Its budget is about 40 times the reference's time, so a correct but slow
# application finishes (the spec sets no response-time limit, so slowness alone must not fail it).
@workflow("W06", "Prices at scale: fees, tax, stacked codes, largest-remainder allocation and rounding ties on every line", 5,
          timeout=3600, critical=True)
def w06(ctx):
    st = ctx.shared.setdefault("w06", {})
    fixture = json.loads(TIES.read_text())
    with ctx.step("pricing events with the fixture's section prices, facility fees and promo codes"):
        w = H.quick_world("pricing", "America/New_York", PRICING_SECTIONS)
        events = []
        for i, fe in enumerate(fixture["events"]):
            eid = H.setup_event(w["owner"], w["org"], w["venue"], f"Pricing Night {i + 1}", H.start_in(w["tz"], 60 * 24 * (20 + i)),
                                fe["prices"], fee=fe["fee"])
            for cd in fe["codes"]:
                r = H.create_code(w["owner"], w["org"], eid, cd["code"], cd["kind"], cd["value"])
                ctx.check(r.status < 400, f"creating code {cd['code']} answered {r.status}")
            for od in fe["orders"]:
                if od.get("fixed"):
                    r = H.create_code(w["owner"], w["org"], eid, od["fixed"]["code"], "fixed", od["fixed"]["value"])
                    ctx.check(r.status < 400, f"creating code {od['fixed']['code']} answered {r.status}")
            events.append(eid)
        email, c = H.account(tag="pricebuyer")
        st.update(w=w, events=events, email=email, c=c)
    rnd = random.Random(606)
    plans = []
    for i, fe in enumerate(fixture["events"]):
        for od in fe["orders"]:
            seats = [f"{s}-A-{k + 1}" for s, n in sorted(od["seats"].items()) for k in range(n)]
            codes = [x for x in (od.get("percent"), (od.get("fixed") or {}).get("code")) if x]
            plans.append((i, seats, codes, "fixture " + "/".join(od["breaks"])))
        for _ in range(6):
            secs = rnd.sample(["PA", "PB", "PC", "PD", "PE", "PF"], rnd.randint(1, 3))
            seats = []
            for s in secs:
                row = rnd.choice("AB")
                seats += [f"{s}-{row}-{k}" for k in rnd.sample(range(1, 9), rnd.randint(1, 2))]
            seats = seats[:6]
            pct = rnd.choice([None] + [cd["code"] for cd in fe["codes"]])
            plans.append((i, seats, [pct] if pct else [], "ordinary"))
    with ctx.step(f"each of {len(plans)} orders shows every line's face, discount share, fees, tax share and total exactly"):
        c, email, events = st["c"], st["email"], st["events"]
        problems, checked = [], 0
        for i, seats, codes, kind in plans:
            eid = events[i]
            r, oid = H.hold(c, eid, seats, buyer=email)
            if not oid:
                problems.append(f"hold {seats} on event {eid} answered {r.status}")
                if len(problems) > 5:
                    break
                continue
            cobjs = []
            for code in codes:
                rr = H.apply_code(c, oid, code.lower() if checked % 3 == 0 else code)
                if rr.status >= 400:
                    problems.append(f"order {oid}: applying {code} answered {rr.status}")
                cobjs.append(H.codes[(eid, code.upper())])
            o = H.order(c, oid)
            q = H.model.quote_for(eid, seats, cobjs)
            d = H.quote_diff(o, q)
            if d:
                problems.append(f"order {oid} ({kind}, {seats}, {codes}): " + "; ".join(d[:4]))
            checked += 1
            H.release(c, oid)
            if len(problems) > 5:
                break
        ctx.observe("orders_checked", checked)
        ctx.check(not problems, f"{len(problems)} orders differ: " + " | ".join(problems[:5]))
    with ctx.step("seat order decides ties: sections created out of order (ZED, MID, ALP), seat 9 before seat 10"):
        w = st["w"]
        order_secs = [("ZED", "Zed side", "A-B", 12, []), ("MID", "Middle", "A-B", 12, []), ("ALP", "Alp side", "A-B", 12, [])]
        vid = H.create_venue(w["owner"], w["org"], "Seat Order Hall", w["tz"], order_secs)
        eid = H.setup_event(w["owner"], w["org"], vid, "Seat Order Night", H.start_in(w["tz"], 60 * 24 * 25),
                            {"ZED": 2100, "MID": 2100, "ALP": 2100}, fee=200)
        H.create_code(w["owner"], w["org"], eid, "ORDER10", "percent", 10)
        H.create_code(w["owner"], w["org"], eid, "ORDER333", "fixed", 333)
        created = {"ZED": 0, "MID": 1, "ALP": 2}

        def totals(labels, codes, key):
            ordered = sorted(labels, key=key)
            q = M.quote([(x, 2100) for x in ordered], 200, codes)
            return {l.key: l.total for l in q.lines}
        num_key = M.seat_key
        id_key = lambda x: (created[x.split("-")[0]], x.split("-")[1], int(x.split("-")[2]))  # noqa: E731
        rnd2 = random.Random(808)
        orders = [(["ALP-A-1", "ZED-A-1"], [])]
        tries = 0
        while len(orders) < 25 and tries < 20000:
            tries += 1
            row = rnd2.choice("AB")
            sec = rnd2.choice(["ALP", "MID"])
            seats = {f"{sec}-{row}-9", f"{sec}-{row}-{rnd2.choice([10, 11, 12])}", f"ZED-{rnd2.choice('AB')}-{rnd2.randint(1, 12)}"}
            while len(seats) < rnd2.randint(3, 6):
                seats.add(f"{rnd2.choice(['ZED', 'MID', 'ALP'])}-{rnd2.choice('AB')}-{rnd2.randint(1, 12)}")
            codes = rnd2.choice([[], [H.codes[(eid, "ORDER10")]], [H.codes[(eid, "ORDER333")]],
                                 [H.codes[(eid, "ORDER10")], H.codes[(eid, "ORDER333")]]])
            exact = totals(seats, codes, num_key)
            if exact != totals(seats, codes, lambda x: x) and exact != totals(seats, codes, id_key):
                orders.append((sorted(seats), codes))
        q0 = H.model.quote_for(eid, ["ALP-A-1", "ZED-A-1"])
        if {l.key: (l.tax, l.total) for l in q0.lines} != {"ALP-A-1": (201, 2659), "ZED-A-1": (200, 2658)}:
            raise InfraError("verifier fixture: worked check of the seat-order tie")
        if len(orders) < 20:
            raise InfraError(f"verifier fixture: only {len(orders)} seat-order orders")
        email, c = H.account(tag="seatorder")
        problems = []
        for seats, codes in orders:
            r, oid = H.hold(c, eid, seats, buyer=email)
            if not oid:
                problems.append(f"hold {seats} answered {r.status}")
                break
            for code in codes:
                H.apply_code(c, oid, code.code)
            d = H.quote_diff(H.order(c, oid), H.model.quote_for(eid, seats, codes))
            if d:
                problems.append(f"order {oid} ({seats}, {[x.code for x in codes]}): " + "; ".join(d[:3]))
            H.release(c, oid)
            if len(problems) > 4:
                break
        ctx.observe("seat_order_orders", len(orders))
        ctx.check(not problems, f"{len(problems)} orders differ: " + " | ".join(problems[:4]))
    with ctx.step("paid orders charge exactly the quoted total and keep their breakdown"):
        events = st["events"]
        fx, used = [], set()
        for p in plans:
            if p[3] != "ordinary" and len(p[2]) == 2 and p[0] not in used:
                fx.append(p)
                used.add(p[0])
        for n, (i, seats, codes, kind) in enumerate(fx):
            eid = events[i]
            email, c = H.account(tag=f"pricepaid{n}")
            oid, ids = H.buy(c, email, eid, seats, codes)
            o = H.order(c, oid)
            q = H.model.orders[oid].quote
            d = H.quote_diff(o, q)
            ctx.check(not d, f"paid order {oid} breakdown: " + "; ".join(d[:4]))
            sid = H.model.orders[oid].session
            ctx.eq(H.pay.session(sid).get("amount_total"), q.total, f"session amount for order {oid}")
            for line in q.lines:
                tk = H.ticket(c, ids[line.key])
                ctx.eq(tk.get("seat"), line.key, f"ticket {ids[line.key]} seat")


# ======================================================================================== W07
@workflow("W07", "Promo codes: case-insensitive entry, one percent plus one fixed, refusals, locking at Pay, caps under concurrency", 4,
          timeout=900, critical=True)
def w07(ctx):
    st = ctx.shared.setdefault("w07", {})
    with ctx.step("an owner creates codes; malformed or duplicate codes are refused"):
        w = H.quick_world("promo", "America/New_York")
        eid = H.setup_event(w["owner"], w["org"], w["venue"], "Promo Night", H.start_in(w["tz"], 60 * 24 * 14), {"BALC": 3000, "FLOOR": 5000},
                            fee=150)
        other = H.setup_event(w["owner"], w["org"], w["venue"], "Other Night", H.start_in(w["tz"], 60 * 24 * 15), {"BALC": 3000, "FLOOR": 5000})
        for args in (("SPRING20", "percent", 20), ("TAKE4", "fixed", 400), ("ALSO15", "percent", 15), ("BIG10", "percent", 10, 8001),
                     ("CAP3", "fixed", 300, 0, 3), ("lowcase5", "percent", 5)):
            r = H.create_code(w["owner"], w["org"], eid, *args)
            ctx.check(r.status < 400, f"creating {args[0]} answered {r.status}")
        H.create_code(w["owner"], w["org"], other, "OTHER10", "percent", 10)
        for bad in (("BAD CODE", "percent", 10), ("PCT101", "percent", 101), ("ZERO0", "fixed", 0), ("SPRING20", "fixed", 100),
                    ("ABC", "percent", 10)):
            H.codes.pop((eid, bad[0].upper()), None) if bad[0] != "SPRING20" else None
            submit_form(w["owner"], f"/orgs/{w['org']}/events/{eid}/codes", "create-code",
                        {"code": bad[0], "kind": bad[1], "value": str(bad[2]), "min_subtotal_cents": "0", "max_redemptions": ""}, follow=True)
        email, c = H.account(tag="promobuyer")
        for bad in ("BAD CODE", "PCT101", "ZERO0", "ABC"):
            r, oid = H.hold(c, eid, ["BALC-B-1"])
            rr = H.apply_code(c, oid, bad)
            o = H.order(c, oid)
            ctx.check(not o.get("codes"), f"malformed code {bad!r} was created and applies")
            H.release(c, oid)
        st.update(w=w, eid=eid, other=other, email=email, c=c)
    with ctx.step("codes apply in any case; one percent and one fixed stack; repeats, a second percent code and other events' codes are refused"):
        c, eid, email = st["c"], st["eid"], st["email"]
        seats = ["BALC-A-3", "BALC-A-4", "FLOOR-B-7"]
        r, oid = H.hold(c, eid, seats, buyer=email)
        ctx.check(oid, f"hold answered {r.status}")
        H.apply_code(c, oid, "  spring20 ")
        o = H.order(c, oid)
        ctx.eq(o.get("codes"), ["SPRING20"], "applied codes after ' spring20 '")
        q = H.model.quote_for(eid, seats, [H.codes[(eid, "SPRING20")]])
        ctx.check(not H.quote_diff(o, q), "breakdown with SPRING20: " + "; ".join(H.quote_diff(o, q)[:4]))
        for code in ("SPRING20", "ALSO15", "OTHER10", "NOPE99", "LOWCASE5"):
            H.apply_code(c, oid, code)
        ctx.eq(H.order(c, oid).get("codes"), ["SPRING20"], "applied codes after refused codes")
        H.apply_code(c, oid, "take4")
        o = H.order(c, oid)
        ctx.eq(o.get("codes"), ["SPRING20", "TAKE4"], "applied codes after TAKE4")
        q = H.model.quote_for(eid, seats, [H.codes[(eid, "SPRING20")], H.codes[(eid, "TAKE4")]])
        ctx.check(not H.quote_diff(o, q), "breakdown with SPRING20 + TAKE4: " + "; ".join(H.quote_diff(o, q)[:4]))
        ctx.eq(o.get("total"), "10494", "order total of the instruction's example 3")
        H.remove_code(c, oid, "SPRING20")
        o = H.order(c, oid)
        ctx.eq(o.get("codes"), ["TAKE4"], "applied codes after removing SPRING20")
        q = H.model.quote_for(eid, seats, [H.codes[(eid, "TAKE4")]])
        ctx.check(not H.quote_diff(o, q), "breakdown after removing SPRING20: " + "; ".join(H.quote_diff(o, q)[:4]))
        H.apply_code(c, oid, "LOWCASE5")
        ctx.eq(H.order(c, oid).get("codes"), ["LOWCASE5", "TAKE4"], "a code created in lower case applies (stored in capitals)")
        r2, o2 = H.hold(c, eid, ["BALC-B-5"], buyer=email)
        H.apply_code(c, o2, "BIG10")
        ctx.eq(H.order(c, o2).get("codes"), [], "BIG10 applied below its minimum subtotal")
        H.release(c, o2)
        st["oid"], st["seats"] = oid, seats
    with ctx.step("extra form fields never change amounts; codes lock once Pay is pressed"):
        c, eid, seats, email = st["c"], st["eid"], st["seats"], st["email"]
        # a fresh hold, so nothing here depends on how long the previous step took against the hold's lifetime
        H.release(c, st["oid"])
        r, oid = H.hold(c, eid, seats, buyer=email)
        ctx.check(oid, f"holding the seats again answered {r.status}")
        for code in ("LOWCASE5", "TAKE4"):
            H.apply_code(c, oid, code)
        ctx.eq(H.order(c, oid).get("codes"), ["LOWCASE5", "TAKE4"], "codes on the fresh hold")
        st["oid"] = oid
        page = c.get(f"/orders/{oid}", follow=True)
        submit_form(c, f"/orders/{oid}", "apply-code", {"code": "SPRING20"}, follow=True, page=page,
                    override={"discount_cents": "99999", "total_cents": "1", "price_cents": "1", "percent": "100"})
        o = H.order(c, oid)
        codes = [H.codes[(eid, x)] for x in o.get("codes")]
        q = H.model.quote_for(eid, seats, codes)
        if o.get("total") != str(q.total):
            ctx.breach("X5", f"extra fields changed the order total to {o.get('total')}")
        ctx.check(not H.quote_diff(o, q), "breakdown after a request with extra fields: " + "; ".join(H.quote_diff(o, q)[:3]))
        r, sid = H.pay_click(c, oid)
        ctx.check(sid, f"Pay answered {r.status}")
        o = H.order(c, oid)
        ctx.check(not o["has_apply"], "the apply-code form is still offered after Pay")
        H.post_with_token(c, f"/orders/{oid}/codes", [("code", "ALSO15")], token_from=f"/orders/{oid}")
        for code in list(o.get("codes")):
            H.post_with_token(c, f"/orders/{oid}/codes/{code}/remove", token_from=f"/orders/{oid}")
        ctx.eq(H.order(c, oid).get("codes"), o.get("codes"), "codes after changes attempted once Pay was pressed")
        ctx.eq(H.pay.session(sid).get("amount_total"), q.total, "session amount")
    with ctx.step("applied at the same moment to eight orders, a code with three redemptions lands on exactly three"):
        eid = st["eid"]
        buyers = [H.account(tag=f"capbuyer{i}") for i in range(4)]
        orders = []
        seat_iter = iter([f"FLOOR-{r}-{n}" for r in "CD" for n in range(1, 13)])
        for email, c in buyers:
            for _ in range(2):
                s = next(seat_iter)
                r, oid = H.hold(c, eid, [s], buyer=email)
                ctx.check(oid, f"hold for the cap race answered {r.status}")
                orders.append((c, oid, email))
        pages = [(c, oid, c.get(f"/orders/{oid}", follow=True)) for c, oid, _e in orders]
        outs = simultaneously([lambda c=c, oid=oid, p=p: submit_form(c, f"/orders/{oid}", "apply-code", {"code": "CAP3"}, page=p)
                               for c, oid, p in pages])
        ctx.check(all(k == "ok" for k, _ in outs), "a racing apply-code request failed outright")
        carrying = [(c, oid, e) for c, oid, e in orders if "CAP3" in H.order(c, oid).get("codes", [])]
        if len(carrying) > 3:
            ctx.breach("X6", f"a code with 3 redemptions was applied to {len(carrying)} orders")
        ctx.eq(len(carrying), 3, "orders carrying CAP3 after the race")
        st["orders"], st["carrying"] = orders, carrying
    with ctx.step("paid orders keep their redemption; released and expired holds give theirs back"):
        eid = st["eid"]
        carrying, orders = st["carrying"], st["orders"]
        (pc, poid, pe), (rc, roid, _re), (xc, xoid, _xe) = carrying
        H.model.orders[poid].quote = H.model.quote_for(eid, H.model.orders[poid].seats, [H.codes[(eid, "CAP3")]])
        r, sid = H.pay_click(pc, poid)
        H.pay.pay(sid, CARD_OK)
        H.settle_paid(pc, poid, (now() - timedelta(seconds=5), now() + timedelta(seconds=30)))
        H.release(rc, roid)
        free = [(c, oid) for c, oid, _e in orders if oid not in (poid, roid, xoid)]
        H.apply_code(free[0][0], free[0][1], "CAP3")
        H.apply_code(free[1][0], free[1][1], "CAP3")
        got = ["CAP3" in H.order(c, oid).get("codes", []) for c, oid in free[:2]]
        ctx.eq(got, [True, False], "CAP3 on two more orders after one release (one paid, one pending, one free)")
        exps = [parse_inst(H.order(c, oid).get("hold_expires_at")) for c, oid, _e in orders if oid not in (poid, roid)]
        ctx.check(all(exps), "no hold expiry on a pending order")
        time.sleep(max(0.0, (max(exps) - now()).total_seconds()) + 1.6)
        fresh = []
        for i in range(3):
            email, c = H.account(tag=f"capafter{i}")
            r, oid = H.hold(c, eid, [f"FLOOR-B-{i + 1}"], buyer=email)
            H.apply_code(c, oid, "CAP3")
            fresh.append("CAP3" in H.order(c, oid).get("codes", []))
        ctx.eq(fresh, [True, True, False], "CAP3 on new orders once the pending ones expired")


# ======================================================================================== W08
@workflow("W08", "Holds: expiry from creation, release, provider sessions expired, late payments confirmed or refunded", 5,
          timeout=1500, critical=True)
def w08(ctx):
    st = ctx.shared.setdefault("w08", {})
    with ctx.step("a hold expires HOLD_TTL_SECONDS after it was created, even when Pay was pressed later"):
        w = H.quick_world("expiry", "America/Denver")
        eid = H.setup_event(w["owner"], w["org"], w["venue"], "Expiry Night", H.start_in(w["tz"], 60 * 24 * 10), {"BALC": 2600, "FLOOR": 3900},
                            fee=175)
        a_email, a = H.account(tag="expirya")
        t0 = now()
        r, oid = H.hold(a, eid, ["FLOOR-A-5", "FLOOR-A-6"], buyer=a_email)
        ctx.check(oid, f"hold answered {r.status}")
        ex0 = H.order(a, oid).get("hold_expires_at")
        time.sleep(H.hold_ttl / 2)
        r, sid = H.pay_click(a, oid)
        ctx.check(sid, f"Pay answered {r.status}")
        ex1 = H.order(a, oid).get("hold_expires_at")
        ctx.eq(ex1, ex0, "data-hold-expires-at after pressing Pay")
        exp = parse_inst(ex0)
        ctx.check(exp and abs((exp - t0).total_seconds() - H.hold_ttl) <= 3, f"expiry {ex0} is not {H.hold_ttl} s after the hold")
        st.update(w=w, eid=eid, a=a, oid=oid, sid=sid, exp=exp)
    with ctx.step("at expiry the order is expired and its seats are available at once to the next buyer"):
        a, oid, eid, exp = st["a"], st["oid"], st["eid"], st["exp"]
        time.sleep(max(0.0, (exp - now()).total_seconds()) + 1.6)
        o = H.order(a, oid)
        ctx.eq(o.get("status"), "expired", "order status just after the hold expired")
        ctx.check(not o["has_pay"] and not o["has_release"], "an expired order still offers pay/release")
        b_email, b = H.account(tag="expiryb")
        r, boid = H.hold(b, eid, ["FLOOR-A-5"], buyer=b_email)
        ctx.check(boid, f"the next buyer could not hold a seat whose hold had expired ({r.status})")
        H.release(b, boid)
    with ctx.step("the expired order's Checkout Session is expired at the provider within 30 s"):
        sid = st["sid"]
        ok = H.pay.wait(lambda s: s.get("checkout_sessions", {}).get(sid, {}).get("status") == "expired", 32)
        ctx.check(ok, f"session {sid} is {H.pay.session(sid).get('status')!r} 30 s after the hold expired")
    with ctx.step("a payment completing after expiry, with the seats still free, is confirmed as normal"):
        eid = st["eid"]
        c_email, c = H.account(tag="latefree")
        r, oid = H.hold(c, eid, ["BALC-B-7", "BALC-B-8"], buyer=c_email)
        r, sid = H.pay_click(c, oid)
        exp = parse_inst(H.order(c, oid).get("hold_expires_at"))
        time.sleep(max(0.0, (exp - now()).total_seconds()) + 1.6)
        ctx.eq(H.order(c, oid).get("status"), "expired", "status before the late payment")
        H.pay.wait(lambda s: s.get("checkout_sessions", {}).get(sid, {}).get("status") == "expired", 32)
        before = H.mail_ids(c_email)
        t0 = now()
        res = H.pay.force_complete(sid)
        ctx.check(res.get("ok"), f"the provider did not complete the late payment: {res}")
        ids = H.settle_paid(c, oid, (t0 - timedelta(seconds=2), now() + timedelta(seconds=30)))
        ctx.eq(sorted(ids), ["BALC-B-7", "BALC-B-8"], "tickets of the late-paid order")
        ctx.eq(len(H.wait_mail(c_email, "your tickets", before)), 1, "confirmation emails for the late payment")
    with ctx.step("a payment completing after expiry, when a seat was sold meanwhile, is refunded in full once and emailed once"):
        eid = st["eid"]
        d_email, d = H.account(tag="latetaken")
        r, doid = H.hold(d, eid, ["FLOOR-D-11"], buyer=d_email)
        r, dsid = H.pay_click(d, doid)
        total = int(H.order(d, doid)["total"])
        exp = parse_inst(H.order(d, doid).get("hold_expires_at"))
        time.sleep(max(0.0, (exp - now()).total_seconds()) + 1.6)
        e_email, e = H.account(tag="takesseat")
        H.buy(e, e_email, eid, ["FLOOR-D-11"])
        before = H.mail_ids(d_email)
        res = H.pay.force_complete(dsid)
        ctx.check(res.get("ok"), f"the provider did not complete the late payment: {res}")
        o = H.wait_status(d, doid, ("unavailable", "paid"), 90)
        if o.get("status") == "paid" and any((l.get("ticket-id") or "") for l in (o.get("lines") or {}).values()):
            ctx.breach("X6", "a late payment issued a ticket for a seat already sold to another buyer (seat sold twice)")
        ctx.eq(o.get("status"), "unavailable", "status of a late payment whose seat was sold")
        pi = H.pay.intent_of(dsid)
        refunds = H.pay.wait(lambda s: H.pay.refunds_for_intent(pi, s) or None, 90) or []
        ctx.eq([x["amount"] for x in refunds], [total], "provider refunds for the late payment")
        msgs = H.wait_mail(d_email, "seats no longer available", before)
        ctx.eq(len(msgs), 1, "'Seats no longer available' emails")
        ctx.check(f"${total / 100:,.2f}" in (msgs[0]["text"] or msgs[0]["html"]), f"the email does not state ${total / 100:,.2f}")
        for ev in H.pay.events_for_session(dsid, "checkout.session.completed"):
            H.pay.redeliver(ev["id"])
        time.sleep(5)
        refunds = H.pay.refunds_for_intent(pi)
        if len(refunds) > 1:
            ctx.breach("X6", f"a late payment was refunded {len(refunds)} times")
        ctx.eq(len(refunds), 1, "refunds after the completed event was delivered again")
        ctx.eq(len(H.new_mail(d_email, "seats no longer available", before)), 1, "emails after redelivery")
        mo = H.model.orders[doid]
        mo.status, mo.late_paid, mo.late_refunded = "unavailable", 1, 1
        states = H.seat_states(H.client(), eid)
        ctx.eq(states.get("FLOOR-D-11"), "sold", "the seat stays with the buyer who paid first")
    def late_unavailable(ctx, c, email, oid, sid, label):
        total = int(H.order(c, oid)["total"])
        before = H.mail_ids(email)
        res = H.pay.force_complete(sid)
        ctx.check(res.get("ok"), f"the provider did not complete the late payment: {res}")
        o = H.wait_status(c, oid, ("unavailable", "paid"), 90)
        if o.get("status") == "paid":
            ctx.breach("X6", f"a late payment took a seat that was {label}")
        ctx.eq(o.get("status"), "unavailable", f"status of a late payment whose seat was {label}")
        pi = H.pay.intent_of(sid)
        refunds = H.pay.wait(lambda s_: H.pay.refunds_for_intent(pi, s_) or None, 90) or []
        ctx.eq([x["amount"] for x in refunds], [total], f"provider refunds of a late payment whose seat was {label}")
        ctx.eq(len(H.wait_mail(email, "seats no longer available", before)), 1, "'Seats no longer available' emails")
        mo = H.model.orders[oid]
        mo.status, mo.late_paid, mo.late_refunded = "unavailable", 1, 1

    with ctx.step("a late payment whose seat is now held by another buyer is refunded; the other hold is untouched"):
        eid = st["eid"]
        d_email, d = H.account(tag="latehold")
        r, doid = H.hold(d, eid, ["FLOOR-D-10"], buyer=d_email)
        r, dsid = H.pay_click(d, doid)
        exp = parse_inst(H.order(d, doid).get("hold_expires_at"))
        time.sleep(max(0.0, (exp - now()).total_seconds()) + 1.6)
        e_email, e = H.account(tag="holdsnow")
        r, eoid = H.hold(e, eid, ["FLOOR-D-10"], buyer=e_email)
        ctx.check(eoid, f"the next buyer could not hold the expired seat ({r.status})")
        late_unavailable(ctx, d, d_email, doid, dsid, "held by another buyer")
        o = H.order(e, eoid)
        ctx.check(o.get("status") == "pending" and o["has_pay"], f"the other buyer's hold became {o.get('status')}")
        ctx.eq(H.seat_states(H.client(), eid).get("FLOOR-D-10"), "held", "the seat after the late payment")
        H.release(e, eoid)
    with ctx.step("a late payment whose seat is now under an open waitlist offer is refunded; the offer stays open"):
        w = st["w"]
        vid = H.create_venue(w["owner"], w["org"], "Expiry Annex", w["tz"], [("DUO", "Duo", "A-A", 2, [])])
        e3 = H.setup_event(w["owner"], w["org"], vid, "Duo Expiry Night", H.start_in(w["tz"], 60 * 24 * 10), {"DUO": 4150})
        f_email, f = H.account(tag="lateoffer")
        r, foid = H.hold(f, e3, ["DUO-A-1"], buyer=f_email)
        r, fsid = H.pay_click(f, foid)
        g_email, g = H.account(tag="duosold")
        H.buy(g, g_email, e3, ["DUO-A-2"])
        wt_email, wt = H.account(tag="duowait")
        join(wt, e3, "DUO", 1)
        ctx.eq(section_node(H.event_page(wt, e3), "DUO").attrs.get("data-my-waitlist"), "1", "the waiting entry")
        before = H.mail_ids(wt_email)
        msgs = H.wait_mail(wt_email, "seats available", before, 1, H.hold_ttl + 70)
        link = H.link_in(msgs, "/waitlist/offers/")
        ctx.check(link, "no waitlist offer after the hold expired")
        ctx.eq(H.order(f, foid).get("status"), "expired", "the late buyer's order before paying")
        late_unavailable(ctx, f, f_email, foid, fsid, "under an open waitlist offer")
        op = offer_page(wt, link)
        ctx.eq((op["status"], op["accept"]), ("open", True), "the waitlist offer after the late payment")
    with ctx.step("releasing a hold frees its seats at once and expires its session"):
        eid = st["eid"]
        f_email, f = H.account(tag="releaser")
        r, oid = H.hold(f, eid, ["FLOOR-C-1", "FLOOR-C-2"], buyer=f_email)
        r, sid = H.pay_click(f, oid)
        H.release(f, oid)
        ctx.eq(H.order(f, oid).get("status"), "released", "status after release")
        states = H.seat_states(H.client(), eid)
        ctx.eq([states.get("FLOOR-C-1"), states.get("FLOOR-C-2")], ["available", "available"], "seats after release")
        ok = H.pay.wait(lambda s: s.get("checkout_sessions", {}).get(sid, {}).get("status") == "expired", 32)
        ctx.check(ok, "the released order's session was not expired at the provider within 30 s")


# ======================================================================================== W09
def forged_completed(sid: str, oid: int, amount: int) -> bytes:
    sess = {"id": sid, "object": "checkout.session", "status": "complete", "payment_status": "paid", "amount_total": amount,
            "currency": "usd", "payment_intent": "pi_forged_" + secrets.token_hex(6), "metadata": {"order_id": str(oid)}}
    ev = {"id": "evt_forged_" + secrets.token_hex(8), "object": "event", "api_version": "2024-06-20", "created": int(time.time()),
          "livemode": False, "type": "checkout.session.completed", "data": {"object": sess}}
    return json.dumps(ev, separators=(",", ":")).encode()


@workflow("W09", "Webhooks: forged, unsigned and stale deliveries refused; duplicates and out-of-order events harmless; declined cards",
          4, critical=True)
def w09(ctx):
    st = ctx.shared.setdefault("w09", {})
    with ctx.step("forged, unsigned, stale or mis-signed completion events are answered 400 and change nothing"):
        w = H.quick_world("hooks", "America/Chicago")
        eid = H.setup_event(w["owner"], w["org"], w["venue"], "Webhook Night", H.start_in(w["tz"], 60 * 24 * 11), {"BALC": 2800, "FLOOR": 4100})
        email, c = H.account(tag="hookbuyer")
        r, oid = H.hold(c, eid, ["FLOOR-B-3", "FLOOR-B-4"], buyer=email)
        r, sid = H.pay_click(c, oid)
        ctx.check(sid, f"Pay answered {r.status}")
        total = int(H.order(c, oid)["total"])
        before = H.mail_ids(email)
        cl = H.client()
        key = H.pay.signing_key()
        variants = []
        b = forged_completed(sid, oid, total)
        variants.append(("no signature", b, {}))
        variants.append(("wrong secret", b, {"Stripe-Signature": H.pay.sign(b, key="whsec_" + secrets.token_hex(24))}))
        variants.append(("stale timestamp", b, {"Stripe-Signature": H.pay.sign(b, t=int(time.time()) - 400)}))
        other = forged_completed(sid, oid, total)
        variants.append(("signature of another body", b, {"Stripe-Signature": H.pay.sign(other)}))
        variants.append(("garbage header", b, {"Stripe-Signature": "t=1,v1=deadbeef"}))
        variants.append(("future timestamp", b, {"Stripe-Signature": H.pay.sign(b, t=int(time.time()) + 400)}))
        bad = []
        for label, body, hdr in variants:
            rr = cl.request("POST", "/webhooks/payments", body=body, headers={"Content-Type": "application/json", **hdr})
            if rr.status != 400:
                bad.append(f"{label}: {rr.status}")
        time.sleep(2)
        o = H.order(c, oid)
        if o.get("status") == "paid" or any((l.get("ticket-id") or "") for l in (o.get("lines") or {}).values()):
            ctx.breach("X6", "an order was marked paid by a webhook that did not verify")
            raise StepFailed("a forged webhook marked the order paid")
        ctx.check(not bad, "webhook answers: " + ", ".join(bad))
        ctx.check(not H.new_mail(email, "your tickets", before), "a confirmation email followed a forged webhook")
        st.update(w=w, eid=eid, c=c, email=email, oid=oid, sid=sid)
    with ctx.step("a declined card leaves the order pending; paying again with a good card confirms it"):
        c, oid, sid, email = st["c"], st["oid"], st["sid"], st["email"]
        status, body = H.pay.pay(sid, CARD_DECLINED)
        ctx.eq(status, 402, "the provider's answer to a declined card")
        time.sleep(2)
        o = H.order(c, oid)
        ctx.check(o.get("status") == "pending" and o["has_pay"], f"after a decline the order is {o.get('status')} (pay button: {o['has_pay']})")
        t0 = now()
        status, body = H.pay.pay(sid, CARD_OK)
        ctx.eq(status, 200, "the provider's answer to a good card")
        st["ids"] = H.settle_paid(c, oid, (t0 - timedelta(seconds=2), now() + timedelta(seconds=30)))
    with ctx.step("the completion event delivered again changes nothing and sends nothing"):
        c, oid, sid, email = st["c"], st["oid"], st["sid"], st["email"]
        H.wait_mail(email, "your tickets", set(), 1, 30)
        n0 = len([m for m in H.mail.to(email) if "your tickets" in m["subject"].lower()])
        for ev in H.pay.events_for_session(sid, "checkout.session.completed"):
            H.pay.redeliver(ev["id"])
            H.pay.redeliver(ev["id"])
        time.sleep(6)
        n1 = len([m for m in H.mail.to(email) if "your tickets" in m["subject"].lower()])
        ctx.eq(n1, n0, "confirmation emails after duplicate deliveries")
        ctx.eq(n0, 1, "confirmation emails")
        o = H.order(c, oid)
        ids = {s: int(l["ticket-id"]) for s, l in o["lines"].items()}
        ctx.eq(ids, st["ids"], "ticket ids after duplicate deliveries")
        page = c.get("/tickets", follow=True)
        n_t = len([n for n in page.dom.attr_all("data-ticket-id") if n.attrs.get("data-event-id") == str(st["eid"])])
        if n_t > 2:
            ctx.breach("X6", f"duplicate deliveries issued {n_t} tickets for 2 seats")
        ctx.eq(n_t, 2, "tickets listed after duplicate deliveries")
    with ctx.step("events delivered out of order (payment_intent.succeeded before checkout.session.completed) confirm once"):
        eid = st["eid"]
        email, c = H.account(tag="ooobuyer")
        r, oid = H.hold(c, eid, ["BALC-A-9"], buyer=email)
        r, sid = H.pay_click(c, oid)
        H.pay.hold(["checkout.session.completed", "payment_intent.succeeded"])
        try:
            t0 = now()
            H.pay.pay(sid, CARD_OK)
            st_ = H.pay.state()
            held = {e: st_["events"][e]["type"] for e in st_.get("held", [])}
            mine = [e for e in st_.get("held", []) if st_["events"][e]["data"]["object"].get("id") == sid
                    or st_["events"][e]["data"]["object"].get("id") == H.pay.intent_of(sid, st_)]
            order = sorted(mine, key=lambda e: 0 if held[e] == "payment_intent.succeeded" else 1)
            time.sleep(2)
            ctx.eq(H.order(c, oid).get("status"), "pending", "status while the provider's events are held")
            H.pay.release(order)
        finally:
            H.pay.release()
        H.settle_paid(c, oid, (t0 - timedelta(seconds=2), now() + timedelta(seconds=30)))
        before = set()
        ctx.eq(len(H.wait_mail(email, "your tickets", before)), 1, "confirmation emails")
    with ctx.step("returning from the hosted page's cancel link leaves the order pending; the webhook path refuses junk"):
        eid = st["eid"]
        email, c = H.account(tag="canceller")
        r, oid = H.hold(c, eid, ["BALC-A-10"], buyer=email)
        r, sid = H.pay_click(c, oid)
        status, _d, hdrs = H.pay._public("GET", f"/pay/{sid}/cancel")
        ctx.check(status == 303 and (hdrs.get("location") or "").startswith(H.base), f"cancel link answered {status}")
        back = c.get(hdrs["location"], follow=True)
        ctx.eq(back.status, 200, "cancel_url status")
        o = H.order(c, oid)
        ctx.check(o.get("status") == "pending" and o["has_pay"], f"after cancel the order is {o.get('status')}")
        cl = H.client()
        for body in (b"{not json", b"", b"[]"):
            rr = cl.request("POST", "/webhooks/payments", body=body, headers={"Content-Type": "application/json"})
            ctx.eq(rr.status, 400, f"webhook answer to {body!r}")
        H.release(c, oid)


# ======================================================================================== W10
@workflow("W10", "Seat races and the per-buyer limit: one winner per seat, six tickets per buyer across orders and old orders", 4,
          critical=True)
def w10(ctx):
    st = ctx.shared.setdefault("w10", {})
    with ctx.step("eight buyers holding the same last seat at the same moment: exactly one gets it"):
        w = H.quick_world("race", "America/New_York")
        eid = H.setup_event(w["owner"], w["org"], w["venue"], "Last Seat Night", H.start_in(w["tz"], 60 * 24 * 12), {"BALC": 3300, "FLOOR": 4400})
        buyers = [H.account(tag=f"racer{i}") for i in range(8)]
        pages = [(c, H.event_page(c, eid)) for _e, c in buyers]
        outs = simultaneously([lambda c=c, p=p: submit_form(c, f"/events/{eid}", "hold-seats", {"seats": ["BALC-B-10"]}, page=p)
                               for c, p in pages])
        wins = [v for k, v in outs if k == "ok" and v.status in REDIRECTS and "/orders/" in (v.location or "")]
        if len(wins) > 1:
            ctx.breach("X6", f"{len(wins)} buyers held the same seat")
        ctx.eq(len(wins), 1, "buyers holding the raced seat")
        ctx.eq(H.seat_states(H.client(), eid).get("BALC-B-10"), "held", "the raced seat's status")
        st.update(w=w, eid=eid)
    with ctx.step("two simultaneous holds of four seats by one buyer cannot together pass six"):
        eid = st["eid"]
        email, c = H.account(tag="doublehold")
        p1, p2 = H.event_page(c, eid), H.event_page(c, eid)
        outs = simultaneously([lambda: submit_form(c, f"/events/{eid}", "hold-seats", {"seats": [f"FLOOR-A-{n}" for n in range(3, 7)]}, page=p1),
                               lambda: submit_form(c, f"/events/{eid}", "hold-seats", {"seats": [f"FLOOR-B-{n}" for n in range(3, 7)]}, page=p2)])
        wins = [v for k, v in outs if k == "ok" and v.status in REDIRECTS and "/orders/" in (v.location or "")]
        if len(wins) > 1:
            ctx.breach("X6", "one buyer held 8 seats of an event at once")
        ctx.eq(len(wins), 1, "successful simultaneous holds")
    with ctx.step("paid tickets and unexpired holds count toward six; a refund gives room; a transfer does not"):
        eid, w = st["eid"], st["w"]
        email, c = H.account(tag="limitbuyer")
        oid, ids = H.buy(c, email, eid, ["FLOOR-C-1", "FLOOR-C-2", "FLOOR-C-3", "FLOOR-C-4"])
        ctx.check(H.hold(c, eid, ["FLOOR-D-1", "FLOOR-D-2", "FLOOR-D-3"])[1] is None, "a hold taking the buyer to 7 succeeded")
        r, h2 = H.hold(c, eid, ["FLOOR-D-1", "FLOOR-D-2"], buyer=email)
        ctx.check(h2, f"a hold taking the buyer to 6 answered {r.status}")
        ctx.check(H.hold(c, eid, ["FLOOR-D-3"])[1] is None, "a hold beyond 6 (counting a pending hold) succeeded")
        H.release(c, h2)
        tid = ids["FLOOR-C-1"]
        H.pay.hold(["refund.created", "charge.refunded"])
        try:
            H.org_refund(w["owner"], w["org"], eid, tid)
            ctx.check(H.pay.wait(lambda s_: H.refunds_for_ticket(tid, s_) or None, 90), "the organizer refund never reached the provider")
            ctx.eq(H.ticket(c, tid).get("status"), "refund_pending", "status while the refund webhook is held")
            ctx.check(H.hold(c, eid, ["FLOOR-D-1", "FLOOR-D-2", "FLOOR-D-3"])[1] is None,
                      "a refund_pending ticket stopped counting toward the limit (a hold to 7 succeeded)")
            r, hp = H.hold(c, eid, ["FLOOR-D-1", "FLOOR-D-2"], buyer=email)
            ctx.check(hp, f"with 4 tickets (one refund_pending) the buyer could not hold 2 ({r.status})")
            H.release(c, hp)
        finally:
            H.pay.release()
        t = H.wait_ticket(c, tid, "refunded", 90)
        ctx.eq(t.get("status"), "refunded", "status after an organizer refund")
        r, h3 = H.hold(c, eid, ["FLOOR-D-1", "FLOOR-D-2", "FLOOR-D-3"], buyer=email)
        ctx.check(h3, f"after a refund the buyer could not hold up to 6 ({r.status})")
        H.release(c, h3)
        r_email = f"recipient.{H.nonce}@verify.example"
        H.transfer(c, ids["FLOOR-C-2"], r_email, "Rae Cipient")
        ctx.check(H.hold(c, eid, ["FLOOR-D-4", "FLOOR-D-5", "FLOOR-D-6", "FLOOR-D-7"])[1] is None,
                  "a transfer away freed room under the buyer's limit")
        r_email, rc = H.account(r_email, "Rae Cipient")
        r, h4 = H.hold(rc, eid, [f"FLOOR-D-{n}" for n in range(6, 12)], buyer=r_email)
        ctx.check(h4, f"a transfer recipient could not hold 6 seats ({r.status})")
        H.release(rc, h4)
    with ctx.step("the old platform's orders count, matched by address whatever their case and spacing"):
        at = now()
        staff_emails = {norm(u["email"]) for u in H.baseline["users"]}
        cands, over = [], []
        for ev in H.model.events.values():
            if not ev.legacy or ev.status != "published" or ev.start < at + timedelta(days=3):
                continue
            counts: dict = {}
            raw: dict = {}
            for o in H.baseline["legacy_orders"]:
                if o["event_id"] == ev.id:
                    raw.setdefault(norm(o["buyer_email"]), set()).add(o["buyer_email"])
            for t in H.model.tickets_of(ev.id):
                if t.legacy and t.status != "refunded":
                    counts[t.buyer_email] = counts.get(t.buyer_email, 0) + 1
            for e, n in counts.items():
                if e in staff_emails or e in H.clients:
                    continue
                quirky = any(r != e for r in raw.get(e, ()))
                if 2 <= n <= 4 and quirky and e.count("@") == 1:
                    cands.append((ev.id, e, n))
                if n >= 6:
                    over.append((ev.id, e, n))
        if not (cands and over):
            raise InfraError("verifier fixture: no legacy buyers for the limit check")
        eid, e, n = sorted(cands)[0]
        email, c = H.account(e, "Legacy Buyer")
        ev = H.model.events[eid]
        taken = {t.seat for t in H.model.tickets_of(eid) if t.status != "refunded"}
        free = [s for code in sorted(ev.sections) for s in ev.sections[code].seats if s not in taken and s not in ev.sections[code].accessible]
        ctx.check(H.hold(c, eid, free[:7 - n])[1] is None, f"a legacy buyer with {n} old tickets held {7 - n} more")
        r, oid = H.hold(c, eid, free[:6 - n], buyer=email)
        ctx.check(oid, f"a legacy buyer with {n} old tickets could not hold {6 - n} more ({r.status}: {visible_text(r.text)[:120]})")
        H.release(c, oid)
        # complimentary tickets count like any other (R18.3): staff no other workflow signs in as, holding comps for an
        # upcoming old event, one under the limit and one at or over it
        busy = {"nadia.brooks@lanternroom.example", "theo.marsh@lanternroom.example", "jo.feld@gatecrew.example",
                "marcus.hale@copperline.example", "leo.brandt@copperline.example", "lucia.ferreira@saguarosounds.example",
                "grace.yoon@saguarosounds.example", "omar.saleh@saguarosounds.example"}
        comp = []
        for u in sorted({norm(x["email"]) for x in H.baseline["users"]} - busy):
            for ev_ in sorted(H.model.events.values(), key=lambda e: e.id):
                if ev_.legacy and ev_.status == "published" and ev_.start > at + timedelta(days=3):
                    n_ = H.model.buyer_count(ev_.id, u, at)
                    if n_:
                        comp.append((n_ >= 6, u, ev_.id, n_))
        under = [x for x in comp if not x[0]]
        atover = [x for x in comp if x[0]]
        if not under or not atover:
            raise InfraError("verifier fixture: no staff with complimentary tickets on upcoming old events")
        for _o, staff_email, eid_, have in (under[0], next(x for x in atover if x[1] != under[0][1]) if any(x[1] != under[0][1] for x in atover) else atover[0]):
            sc = H.staff(staff_email)
            ev_ = H.model.events[eid_]
            taken_ = {t.seat for t in H.model.tickets_of(eid_) if t.status != "refunded"}
            free_ = [x for code in sorted(ev_.sections) for x in ev_.sections[code].seats if x not in taken_ and x not in ev_.sections[code].accessible]
            room = max(0, 6 - have)
            ctx.check(H.hold(sc, eid_, free_[:room + 1])[1] is None,
                      f"{staff_email} holds {room + 1} more seats although {have} complimentary tickets count toward the limit")
            if room:
                r, hid = H.hold(sc, eid_, free_[:room])
                ctx.check(hid, f"{staff_email} could not hold {room} seats ({r.status}: {visible_text(r.text)[:120]})")
                H.release(sc, hid)
        oeid, oe, on = sorted(over)[0]
        oemail, oc = H.account(oe, "Over Limit")
        oev = H.model.events[oeid]
        taken = {t.seat for t in H.model.tickets_of(oeid) if t.status != "refunded"}
        ofree = [s for code in sorted(oev.sections) for s in oev.sections[code].seats if s not in taken and s not in oev.sections[code].accessible]
        ctx.check(H.hold(oc, oeid, ofree[:1])[1] is None, f"a buyer who already has {on} old tickets held one more")
