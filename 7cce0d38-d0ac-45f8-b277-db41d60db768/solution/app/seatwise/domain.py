"""Seatwise domain rules: seats and availability, holds, codes, payment, refunds, cancellation,
transfers, check-in, waiting lists, dashboard, export and reminders."""

from __future__ import annotations

import threading
from datetime import timedelta

from . import mail, money
from .db import install_id
from .util import (base_url, ceil_second, dollars, floor_second, hold_ttl, instant, label_key, local_text, offer_ttl,
                   parse_qr, qr_token, seat_label, sha)

WORK_KICK = threading.Event()      # housekeeping: expiries and waitlist offers
PROVIDER_KICK = threading.Event()  # provider calls: session expiry and refunds

ROLES = ("owner", "manager", "box_office", "scanner")
INVITE_ROLES = ("manager", "box_office", "scanner")
MAX_PER_BUYER = money.MAX_TICKETS_PER_BUYER

EVENT_SQL = """
SELECT se.*, e.name, e.description, e.facility_fee_cents, v.name AS venue_name, v.timezone, v.address AS venue_address,
       o.name AS org_name
  FROM sw_events se JOIN events e ON e.id = se.event_id JOIN venues v ON v.id = se.venue_id
  JOIN organizations o ON o.id = se.org_id"""


class Refused(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message = message
        self.status = status


def lock_event(conn, event_id: int) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(1, %s)", (int(event_id),))


def load_event(conn, event_id: int):
    return conn.execute(EVENT_SQL + " WHERE se.event_id = %s", (int(event_id),)).fetchone()


def sales_end(ev):
    off = ev["off_sale_at"]
    return min(off, ev["starts_at"]) if off is not None else ev["starts_at"]


def sale_state(ev, now) -> str:
    if ev["status"] == "cancelled":
        return "cancelled"
    if now >= sales_end(ev):
        return "ended"
    if ev["on_sale_at"] is None or now >= ev["on_sale_at"]:
        return "on_sale"
    if ev["presale_at"] is not None and now >= ev["presale_at"]:
        return "presale"
    return "not_on_sale"


# ---- seats -------------------------------------------------------------------------------------------
_SEAT_CACHE: dict[int, list[dict]] = {}
_SEAT_LOCK = threading.Lock()


def venue_seats(conn, venue_id: int, fixed: bool = True) -> list[dict]:
    """Every seat of the venue in seat order. Cached once the seat map is fixed (an event exists)."""
    if fixed:
        with _SEAT_LOCK:
            hit = _SEAT_CACHE.get(venue_id)
        if hit is not None:
            return hit
    rows = conn.execute(
        "SELECT s.id, s.section_id, sec.code, sec.name AS section_name, s.row_label, s.seat_number, s.accessible "
        "FROM seats s JOIN sections sec ON sec.id = s.section_id WHERE sec.venue_id = %s", (venue_id,)).fetchall()
    seats = []
    for r in rows:
        d = dict(r)
        d["label"] = seat_label(r["code"], r["row_label"], r["seat_number"])
        d["key"] = (r["code"], r["row_label"], r["seat_number"])
        seats.append(d)
    seats.sort(key=lambda s: s["key"])
    if fixed:
        with _SEAT_LOCK:
            _SEAT_CACHE[venue_id] = seats
    return seats


def venue_sections(conn, venue_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT sec.id, sec.code, sec.name, count(s.id) AS seat_count, min(s.row_label) AS first_row, "
        "max(s.row_label) AS last_row, max(s.seat_number) AS per_row, "
        "string_agg(CASE WHEN s.accessible THEN s.row_label || s.seat_number END, ' ' ORDER BY s.row_label, s.seat_number) AS acc "
        "FROM sections sec LEFT JOIN seats s ON s.section_id = sec.id WHERE sec.venue_id = %s GROUP BY sec.id", (venue_id,)).fetchall()
    return sorted((dict(r) for r in rows), key=lambda r: r["code"])


def event_prices(conn, event_id: int) -> dict[int, int]:
    return {r["section_id"]: r["price_cents"] for r in
            conn.execute("SELECT section_id, price_cents FROM event_prices WHERE event_id = %s", (event_id,))}


def seat_states(conn, ev, now, seats: list[dict] | None = None) -> dict[int, str]:
    eid = ev["event_id"]
    seats = seats if seats is not None else venue_seats(conn, ev["venue_id"])
    sold = {r["seat_id"] for r in conn.execute(
        "SELECT seat_id FROM sw_tickets WHERE event_id = %s AND status IN ('valid','refund_pending')", (eid,))}
    held = {r["seat_id"] for r in conn.execute(
        "SELECT os.seat_id FROM sw_order_seats os JOIN sw_orders o ON o.id = os.order_id "
        "WHERE o.event_id = %s AND o.status = 'pending' AND o.hold_expires_at > %s", (eid, now))}
    for r in conn.execute("SELECT seat_ids FROM sw_offers WHERE event_id = %s AND status = 'open' AND expires_at > %s",
                          (eid, now)):
        held.update(r["seat_ids"])
    waiting = {r["section_id"] for r in conn.execute(
        "SELECT DISTINCT section_id FROM sw_waitlist WHERE event_id = %s AND status = 'waiting'", (eid,))}
    out = {}
    for s in seats:
        sid = s["id"]
        if sid in sold:
            out[sid] = "sold"
        elif sid in held:
            out[sid] = "held"
        elif s["section_id"] in waiting and not s["accessible"]:
            out[sid] = "waitlist"
        else:
            out[sid] = "available"
    return out


def buyer_count(conn, event_id: int, email_norm: str, now) -> int:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM sw_tickets t JOIN sw_orders o ON o.id = t.order_id "
        "         WHERE o.event_id = %(e)s AND o.buyer_email_norm = %(b)s AND t.status IN ('valid','refund_pending')) "
        "     + (SELECT count(*) FROM sw_order_seats os JOIN sw_orders o ON o.id = os.order_id "
        "         WHERE o.event_id = %(e)s AND o.buyer_email_norm = %(b)s AND o.status = 'pending' "
        "           AND o.hold_expires_at > %(n)s) AS n", {"e": event_id, "b": email_norm, "n": now}).fetchone()
    return int(row["n"])


# ---- orders ------------------------------------------------------------------------------------------
def _new_order(conn, ev, acct, seats: list[dict], now, offer_id=None) -> int:
    prices = event_prices(conn, ev["event_id"])
    oid = conn.execute("SELECT nextval('sw_order_id_seq') AS id").fetchone()["id"]
    conn.execute(
        "INSERT INTO sw_orders (id, event_id, buyer_id, buyer_email, buyer_email_norm, buyer_name, status, created_at, "
        "hold_expires_at, offer_id) VALUES (%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s)",
        (oid, ev["event_id"], acct["id"], acct["email_norm"], acct["email_norm"], acct["full_name"], now,
         ceil_second(now + timedelta(seconds=hold_ttl())), offer_id))
    for s in sorted(seats, key=lambda s: s["key"]):
        conn.execute("INSERT INTO sw_order_seats (order_id, seat_id, label, face) VALUES (%s,%s,%s,%s)",
                     (oid, s["id"], s["label"], prices[s["section_id"]]))
    return oid


def create_hold(conn, acct, ev, labels: list[str], presale_code: str, accessible: bool, now) -> int:
    """R6.1: hold seats; raises Refused with the reason. Caller holds the event lock."""
    if ev["status"] == "cancelled":
        raise Refused("This event is cancelled, so no more seats can be held. Browse other events on the home page.")
    if ev["status"] != "published":
        raise Refused("This event is not published yet, so its seats cannot be held.")
    state = sale_state(ev, now)
    tz = ev["timezone"]
    if state == "not_on_sale":
        if ev["presale_at"] is not None:
            raise Refused(f"Sales have not started. The presale opens {local_text(ev['presale_at'], tz)} and general sale "
                          f"{local_text(ev['on_sale_at'], tz)}. Come back then.")
        raise Refused(f"Sales have not started. Tickets go on sale {local_text(ev['on_sale_at'], tz)}. Come back then.")
    if state == "ended":
        raise Refused("Sales for this event have ended, so seats can no longer be held.")
    if state == "presale":
        typed = (presale_code or "").strip().upper()
        if not typed:
            raise Refused(f"This event is in its presale until {local_text(ev['on_sale_at'], tz)}: enter the presale code "
                          "to hold seats, or wait for the general sale.", 403)
        if typed != (ev["presale_code"] or ""):
            raise Refused("That presale code is not right for this event. Check the code and try again, or wait for the "
                          f"general sale ({local_text(ev['on_sale_at'], tz)}).", 403)
    picked: list[str] = []
    for lab in labels:
        lab = (lab or "").strip()
        if lab and lab not in picked:
            picked.append(lab)
    if not picked:
        raise Refused("Choose at least one seat to hold.", 400)
    if len(picked) > MAX_PER_BUYER:
        raise Refused(f"You can hold at most {MAX_PER_BUYER} seats at a time. Choose fewer seats.", 400)
    seats = venue_seats(conn, ev["venue_id"])
    by_label = {s["label"]: s for s in seats}
    unknown = [lab for lab in picked if lab not in by_label]
    if unknown:
        raise Refused("These seats do not exist at this venue: " + ", ".join(unknown[:6]) + ".", 400)
    chosen = sorted((by_label[lab] for lab in picked), key=lambda s: s["key"])
    states = seat_states(conn, ev, now, seats)
    taken = [s["label"] for s in chosen if states[s["id"]] != "available"]
    if taken:
        why = {"sold": "sold", "held": "held by someone else", "waitlist": "reserved for the waiting list"}
        detail = ", ".join(f"{s['label']} ({why[states[s['id']]]})" for s in chosen if states[s["id"]] != "available")
        raise Refused(f"Some seats are no longer available: {detail}. Choose other seats.")
    acc = [s["label"] for s in chosen if s["accessible"]]
    if acc and not accessible:
        raise Refused("These are accessible seats: " + ", ".join(acc) + ". Tick “I need accessible seating” to hold "
                      "them, or choose other seats.", 400)
    have = buyer_count(conn, ev["event_id"], acct["email_norm"], now)
    if have + len(chosen) > MAX_PER_BUYER:
        left = max(0, MAX_PER_BUYER - have)
        raise Refused(f"Each buyer may have at most {MAX_PER_BUYER} tickets for an event. You already have {have} "
                      f"(tickets bought and seats on hold), so you can hold {left} more.")
    return _new_order(conn, ev, acct, chosen, now)


def load_order(conn, order_id: int, lock: bool = False):
    return conn.execute("SELECT * FROM sw_orders WHERE id = %s" + (" FOR UPDATE" if lock else ""),
                        (order_id,)).fetchone()


def order_status(order, now) -> str:
    if order["status"] == "pending" and order["hold_expires_at"] is not None and now >= order["hold_expires_at"]:
        return "expired"
    return order["status"]


def hold_active(order, now) -> bool:
    return order_status(order, now) == "pending"


def codes_can_change(order, now) -> bool:
    return hold_active(order, now) and not order["pay_pressed"]


def order_lines(conn, order_id: int) -> list[dict]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM sw_order_seats WHERE order_id = %s", (order_id,))]
    rows.sort(key=lambda r: label_key(r["label"]))
    return rows


def order_codes(conn, order_id: int) -> list[dict]:
    rows = conn.execute("SELECT pc.* FROM sw_order_codes oc JOIN sw_promo_codes pc ON pc.id = oc.code_id "
                        "WHERE oc.order_id = %s", (order_id,)).fetchall()
    return sorted((dict(r) for r in rows), key=lambda c: (c["kind"] != "percent", c["code"]))


def order_quote(conn, order, ev=None) -> money.Quote:
    ev = ev or load_event(conn, order["event_id"])
    lines = order_lines(conn, order["id"])
    codes = [money.Code(c["code"], c["kind"], int(c["value"]), int(c["min_subtotal"]), c["max_redemptions"])
             for c in order_codes(conn, order["id"])]
    return money.quote([(l["label"], int(l["face"])) for l in lines], int(ev["facility_fee_cents"]), codes)


def code_in_use(conn, code_id: int, now) -> int:
    return int(conn.execute(
        "SELECT count(*) AS n FROM sw_order_codes oc JOIN sw_orders o ON o.id = oc.order_id WHERE oc.code_id = %s "
        "AND (o.status = 'paid' OR (o.status = 'pending' AND o.hold_expires_at > %s))", (code_id, now)).fetchone()["n"])


def apply_code(conn, order, typed: str, now) -> str:
    """Caller locked the order row. Returns the applied code."""
    if not codes_can_change(order, now):
        raise Refused("Codes can no longer change on this order (its hold ended or Pay was pressed).")
    code = (typed or "").strip().upper()
    if not code:
        raise Refused("Enter a promo code.", 400)
    pc = conn.execute("SELECT * FROM sw_promo_codes WHERE event_id = %s AND code = %s FOR UPDATE",
                      (order["event_id"], code)).fetchone()
    if not pc:
        raise Refused(f"{code} is not a promo code for this event. Check the spelling.", 400)
    applied = order_codes(conn, order["id"])
    if any(c["id"] == pc["id"] for c in applied):
        raise Refused(f"{code} is already applied to this order.")
    if any(c["kind"] == pc["kind"] for c in applied):
        kind = "percentage" if pc["kind"] == "percent" else "fixed-amount"
        raise Refused(f"This order already carries a {kind} code; an order can carry one percentage code and one "
                      "fixed-amount code. Remove the other code first to use this one.")
    subtotal = sum(int(l["face"]) for l in order_lines(conn, order["id"]))
    if subtotal < int(pc["min_subtotal"]):
        raise Refused(f"{code} needs a face subtotal of at least {dollars(pc['min_subtotal'])}; this order's is "
                      f"{dollars(subtotal)}.")
    if pc["max_redemptions"] is not None and code_in_use(conn, pc["id"], now) >= int(pc["max_redemptions"]):
        raise Refused(f"{code} has no redemptions left.")
    conn.execute("INSERT INTO sw_order_codes (order_id, code_id) VALUES (%s, %s)", (order["id"], pc["id"]))
    return code


def remove_code(conn, order, code: str, now) -> None:
    if not codes_can_change(order, now):
        raise Refused("Codes can no longer change on this order (its hold ended or Pay was pressed).")
    code = (code or "").strip().upper()
    hit = [c for c in order_codes(conn, order["id"]) if c["code"] == code]
    if not hit:
        raise Refused(f"{code} is not applied to this order.", 404)
    conn.execute("DELETE FROM sw_order_codes WHERE order_id = %s AND code_id = %s", (order["id"], hit[0]["id"]))


def checkout_params(conn, order, ev, total: int) -> list[tuple[str, str]]:
    url = f"{base_url()}/orders/{order['id']}"
    n = len(order_lines(conn, order["id"]))
    return [("mode", "payment"), ("success_url", url), ("cancel_url", url),
            ("metadata[order_id]", str(order["id"])), ("customer_email", order["buyer_email_norm"]),
            ("line_items[0][price_data][currency]", "usd"),
            ("line_items[0][price_data][unit_amount]", str(total)),
            ("line_items[0][price_data][product_data][name]",
             f"{ev['name']} - {n} ticket{'s' if n != 1 else ''} (order {order['id']})"),
            ("line_items[0][quantity]", "1")]


def checkout_key(conn, order_id: int) -> str:
    return f"seatwise-{install_id(conn)}-checkout-{order_id}"


# ---- payment confirmation (webhooks) -------------------------------------------------------------------
def handle_webhook(conn, event: dict, now) -> None:
    eid = event.get("id")
    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") if isinstance(event.get("data"), dict) else None
    if not isinstance(eid, str) or not eid or not isinstance(etype, str) or not isinstance(obj, dict):
        return
    fresh = conn.execute("INSERT INTO sw_webhook_events (id, type) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING id",
                         (eid, etype)).fetchone()
    if not fresh:
        return
    if etype == "checkout.session.completed":
        _session_completed(conn, obj, now)
    elif etype == "refund.created":
        _refund_created(conn, obj, now)


def seats_still_available(conn, order, ev, now) -> bool:
    states = seat_states(conn, ev, now)
    return all(states.get(l["seat_id"]) == "available" for l in order_lines(conn, order["id"]))


def _session_completed(conn, obj: dict, now) -> None:
    sid = obj.get("id")
    if not isinstance(sid, str):
        return
    row = conn.execute("SELECT id, event_id FROM sw_orders WHERE session_id = %s", (sid,)).fetchone()
    if not row:
        return
    lock_event(conn, row["event_id"])
    order = load_order(conn, row["id"], lock=True)
    if order["status"] not in ("pending", "expired", "released"):
        return
    if obj.get("payment_status") not in (None, "paid"):
        return
    pi = obj.get("payment_intent")
    if not isinstance(pi, str) or not pi:
        return
    try:
        amount = int(obj.get("amount_total"))
    except (TypeError, ValueError):
        amount = int(order["session_amount"] or 0)
    ev = load_event(conn, order["event_id"])
    if hold_active(order, now) and ev["status"] != "cancelled":
        _mark_paid(conn, order, ev, amount, pi, now)
    elif ev["status"] != "cancelled" and seats_still_available(conn, order, ev, now):
        _mark_paid(conn, order, ev, amount, pi, now)
    else:
        _mark_unavailable(conn, order, ev, amount, pi, now)


def _store_line_amounts(conn, order_id: int, q: money.Quote, lines: list[dict]) -> None:
    for l, ql in zip(lines, q.lines):
        conn.execute("UPDATE sw_order_seats SET discount=%s, service=%s, facility=%s, tax=%s, total=%s "
                     "WHERE order_id=%s AND seat_id=%s", (ql.discount, ql.service, ql.facility, ql.tax, ql.total,
                                                          order_id, l["seat_id"]))


def _mark_paid(conn, order, ev, amount: int, pi: str, now) -> None:
    lines = order_lines(conn, order["id"])
    q = order_quote(conn, order, ev)
    _store_line_amounts(conn, order["id"], q, lines)
    conn.execute("UPDATE sw_orders SET status='paid', paid_at=%s, payment_intent=%s, amount_paid=%s, total_cents=%s "
                 "WHERE id=%s", (now, pi, amount, q.total, order["id"]))
    marker = mail.new_marker()
    parts = []
    for l, ql in zip(lines, q.lines):
        tid = conn.execute("SELECT nextval('sw_ticket_id_seq') AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO sw_tickets (id, legacy, order_id, event_id, seat_id, holder_email_norm, holder_name, status, face, "
            "discount, service, facility, tax, total, token_version, issued_at) "
            "VALUES (%s,false,%s,%s,%s,%s,%s,'valid',%s,%s,%s,%s,%s,%s,1,%s)",
            (tid, order["id"], ev["event_id"], l["seat_id"], order["buyer_email_norm"], order["buyer_name"], ql.face,
             ql.discount, ql.service, ql.facility, ql.tax, ql.total, now))
        parts.append(f"  {l['label']}  ticket {tid}  ({dollars(ql.total)})\n    QR token: {mail.qr_ph(marker, tid, 1)}\n"
                     f"    {base_url()}/tickets/{tid}\n")
    body = (f"Hello {order['buyer_name']},\n\nThank you for your order {order['id']}. Your tickets for {ev['name']} at "
            f"{ev['venue_name']}, {local_text(ev['starts_at'], ev['timezone'])}:\n\n" + "".join(parts) +
            f"\nOrder total: {dollars(q.total)}\n\nShow each ticket's QR token at the door. Your tickets: "
            f"{base_url()}/tickets\n")
    mail.enqueue(conn, f"order-confirmation-{order['id']}", order["buyer_email_norm"], order["buyer_name"],
                 f"Your tickets for {ev['name']}", body, marker)


def _mark_unavailable(conn, order, ev, amount: int, pi: str, now) -> None:
    lines = order_lines(conn, order["id"])
    q = order_quote(conn, order, ev)
    _store_line_amounts(conn, order["id"], q, lines)
    conn.execute("UPDATE sw_orders SET status='unavailable', paid_at=%s, payment_intent=%s, amount_paid=%s, "
                 "total_cents=%s WHERE id=%s", (now, pi, amount, q.total, order["id"]))
    conn.execute("INSERT INTO sw_refunds (kind, order_id, event_id, amount, payment_intent) VALUES ('late',%s,%s,%s,%s)",
                 (order["id"], ev["event_id"], amount, pi))
    seats = ", ".join(l["label"] for l in lines)
    body = (f"Hello {order['buyer_name']},\n\nYour payment for order {order['id']} ({ev['name']}) arrived after the hold "
            f"on your seats had ended, and the seats ({seats}) were no longer available"
            f"{' because the event was cancelled' if ev['status'] == 'cancelled' else ''}.\n\n"
            f"We have refunded the whole payment of {dollars(amount)} to your card. No tickets were issued.\n")
    mail.enqueue(conn, f"late-payment-{order['id']}", order["buyer_email_norm"], order["buyer_name"],
                 "Seats no longer available - your payment was refunded", body)
    PROVIDER_KICK.set()


def _refund_created(conn, obj: dict, now) -> None:
    rid = obj.get("id") if isinstance(obj.get("id"), str) else None
    md = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    row = None
    if rid:
        row = conn.execute("SELECT * FROM sw_refunds WHERE provider_refund_id = %s FOR UPDATE", (rid,)).fetchone()
    if row is None and str(md.get("seatwise_refund_id", "")).isdigit():
        row = conn.execute("SELECT * FROM sw_refunds WHERE id = %s AND (provider_refund_id IS NULL OR provider_refund_id = %s) "
                           "FOR UPDATE", (int(md["seatwise_refund_id"]), rid)).fetchone()
    if row is None and str(md.get("ticket_id", "")).isdigit():
        try:
            amt = int(obj.get("amount"))
        except (TypeError, ValueError):
            amt = -1
        row = conn.execute("SELECT * FROM sw_refunds WHERE ticket_id = %s AND state <> 'completed' AND provider_refund_id "
                           "IS NULL ORDER BY (amount = %s) DESC, id LIMIT 1 FOR UPDATE",
                           (int(md["ticket_id"]), amt)).fetchone()
    if row is None and str(md.get("order_id", "")).isdigit():
        row = conn.execute("SELECT * FROM sw_refunds WHERE order_id = %s AND kind = 'late' AND state <> 'completed' AND "
                           "provider_refund_id IS NULL ORDER BY id LIMIT 1 FOR UPDATE", (int(md["order_id"]),)).fetchone()
    if row is None or row["state"] == "completed":
        return
    pi = obj.get("payment_intent")
    if isinstance(pi, str) and pi and pi != row["payment_intent"]:
        return
    conn.execute("UPDATE sw_refunds SET state='completed', completed_at=%s, provider_refund_id=COALESCE(provider_refund_id, %s) "
                 "WHERE id=%s", (now, rid, row["id"]))
    if row["ticket_id"] is None:
        return
    t = conn.execute("SELECT * FROM sw_tickets WHERE id = %s FOR UPDATE", (row["ticket_id"],)).fetchone()
    left = conn.execute("SELECT count(*) AS n FROM sw_refunds WHERE ticket_id = %s AND state <> 'completed'",
                        (row["ticket_id"],)).fetchone()["n"]
    if left == 0 and t["status"] in ("valid", "refund_pending"):
        conn.execute("UPDATE sw_tickets SET status='refunded' WHERE id=%s", (t["id"],))
    if row["kind"] in ("buyer", "organizer"):
        order = load_order(conn, t["order_id"])
        ev = load_event(conn, t["event_id"])
        label = ticket_label(conn, t)
        body = (f"Hello {order['buyer_name']},\n\nYour refund of {dollars(row['amount'])} for seat {label} "
                f"(ticket {t['id']}, {ev['name']}) has been completed and paid back to your card.\n")
        mail.enqueue(conn, f"refund-completed-{row['id']}", order["buyer_email_norm"], order["buyer_name"],
                     f"Refund completed: {label}", body)
    WORK_KICK.set()


# ---- tickets -----------------------------------------------------------------------------------------
def ticket_label(conn, t) -> str:
    r = conn.execute("SELECT sec.code, s.row_label, s.seat_number FROM seats s JOIN sections sec ON sec.id = s.section_id "
                     "WHERE s.id = %s", (t["seat_id"],)).fetchone()
    return seat_label(r["code"], r["row_label"], r["seat_number"])


def current_token(t) -> str:
    return qr_token(t["id"], t["token_version"], t["barcode"])


def refund_terms(ev, order, now) -> dict:
    """R10.2/R10.3 deadlines and the percentage a request now would return (ignoring who asks)."""
    start = ev["starts_at"]
    if order["paid_at"] is not None and ev["rescheduled_at"] is not None and order["paid_at"] < ev["rescheduled_at"]:
        return {"full_until": start, "half_until": start, "percent": 100 if now < start else 0, "rescheduled": True}
    full, half = start - timedelta(hours=168), start - timedelta(hours=48)
    t = floor_second(now)
    pct = 100 if t <= full else 50 if t <= half else 0
    return {"full_until": full, "half_until": half, "percent": pct, "rescheduled": False}


def buyer_refund_check(acct, t, order, ev, now) -> tuple[int, str]:
    """-> (percent, reason it is not possible)."""
    if t["legacy"]:
        return 0, "Tickets sold by the old platform cannot be refunded here."
    if acct is None or acct["email_norm"] != order["buyer_email_norm"]:
        return 0, "Only the buyer can request a refund."
    if t["transferred"] or t["holder_email_norm"] != acct["email_norm"]:
        return 0, "This ticket was transferred, so it can no longer be refunded to the buyer."
    if t["status"] != "valid":
        return 0, "This ticket is not valid any more (a refund was already requested)."
    if t["admitted_at"] is not None:
        return 0, "This ticket has already been used to enter."
    if ev["status"] == "cancelled":
        return 0, "The event is cancelled; every ticket is refunded automatically."
    if now >= ev["starts_at"]:
        return 0, "The event has started."
    pct = refund_terms(ev, order, now)["percent"]
    if pct == 0:
        return 0, "The refund deadlines have passed (the last one was 48 hours before the start)."
    return pct, ""


def _add_refund(conn, kind: str, t, order, amount: int) -> int:
    return conn.execute("INSERT INTO sw_refunds (kind, ticket_id, order_id, event_id, amount, payment_intent) "
                        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                        (kind, t["id"], order["id"], t["event_id"], amount, order["payment_intent"])).fetchone()["id"]


def request_buyer_refund(conn, acct, ticket_id: int, now) -> int:
    t = conn.execute("SELECT * FROM sw_tickets WHERE id = %s FOR UPDATE", (ticket_id,)).fetchone()
    order = load_order(conn, t["order_id"])
    ev = load_event(conn, t["event_id"])
    pct, why = buyer_refund_check(acct, t, order, ev, now)
    if pct == 0:
        raise Refused(why)
    amount = money.refund_amount(int(t["total"]), pct)
    _add_refund(conn, "buyer", t, order, amount)
    conn.execute("UPDATE sw_tickets SET status='refund_pending' WHERE id=%s", (t["id"],))
    PROVIDER_KICK.set()
    return amount


def organizer_refund(conn, ticket_id: int) -> int:
    t = conn.execute("SELECT * FROM sw_tickets WHERE id = %s FOR UPDATE", (ticket_id,)).fetchone()
    if t["legacy"]:
        raise Refused("Tickets sold by the old platform cannot be refunded here: there is no payment to refund.")
    if t["status"] != "valid":
        raise Refused("Only valid tickets can be refunded; this one is already refunded or has a refund pending.")
    order = load_order(conn, t["order_id"])
    _add_refund(conn, "organizer", t, order, int(t["total"]))
    conn.execute("UPDATE sw_tickets SET status='refund_pending' WHERE id=%s", (t["id"],))
    PROVIDER_KICK.set()
    return int(t["total"])


def transfer_check(acct, t, ev, now) -> str:
    if acct is None or t["holder_email_norm"] != acct["email_norm"]:
        return "Only the ticket's current holder can transfer it."
    if t["status"] != "valid":
        return "Only valid tickets can be transferred."
    if t["admitted_at"] is not None:
        return "This ticket has already been used to enter."
    if ev["status"] == "cancelled":
        return "The event is cancelled."
    if now >= ev["starts_at"]:
        return "The event has started."
    return ""


def transfer(conn, acct, ticket_id: int, to_email: str, to_name: str, now) -> int:
    t = conn.execute("SELECT * FROM sw_tickets WHERE id = %s FOR UPDATE", (ticket_id,)).fetchone()
    ev = load_event(conn, t["event_id"])
    why = transfer_check(acct, t, ev, now)
    if why:
        raise Refused(why)
    if to_email == t["holder_email_norm"]:
        raise Refused("You already hold this ticket; enter someone else's email address.", 400)
    ver = int(t["token_version"]) + 1
    conn.execute("UPDATE sw_tickets SET holder_email_norm=%s, holder_name=%s, token_version=%s, transferred=true WHERE id=%s",
                 (to_email, to_name, ver, t["id"]))
    tr = conn.execute("INSERT INTO sw_transfers (ticket_id, from_email, from_name, to_email, to_name, new_version) "
                      "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                      (t["id"], t["holder_email_norm"], t["holder_name"], to_email, to_name, ver)).fetchone()["id"]
    label = ticket_label(conn, t)
    marker = mail.new_marker()
    when = local_text(ev["starts_at"], ev["timezone"])
    mail.enqueue(conn, f"transfer-{tr}-received", to_email, to_name, f"You have a ticket for {ev['name']}",
                 f"Hello {to_name},\n\n{t['holder_name']} transferred a ticket to you.\n\nEvent: {ev['name']} at "
                 f"{ev['venue_name']}, {when}\nSeat: {label}\nTicket: {t['id']}\nQR token: {mail.qr_ph(marker, t['id'], ver)}\n"
                 f"Ticket page: {base_url()}/tickets/{t['id']}\n\nSign in with this email address to see it at "
                 f"{base_url()}/tickets.\n", marker)
    mail.enqueue(conn, f"transfer-{tr}-sent", t["holder_email_norm"], t["holder_name"], f"Ticket transferred: {label}",
                 f"Hello {t['holder_name']},\n\nYour ticket for seat {label} ({ev['name']}, {when}) was transferred to "
                 f"{to_name}. Its old QR code no longer admits anyone.\n")
    return ver


# ---- cancellation -------------------------------------------------------------------------------------
def can_cancel(conn, ev) -> tuple[bool, str]:
    if ev["status"] != "published":
        return False, "Only published events can be cancelled."
    if ev["legacy"] or conn.execute("SELECT 1 FROM sw_tickets WHERE event_id = %s AND legacy LIMIT 1",
                                    (ev["event_id"],)).fetchone():
        return False, ("This event has tickets sold by the old platform, which cannot be refunded here, so it cannot be "
                       "cancelled in Seatwise. Contact Seatwise support to cancel it and refund those buyers.")
    return True, ""


def cancel_event(conn, ev, now) -> int:
    ok, why = can_cancel(conn, ev)
    if not ok:
        raise Refused(why)
    eid = ev["event_id"]
    conn.execute("UPDATE sw_events SET status='cancelled', cancelled_at=%s WHERE event_id=%s", (now, eid))
    conn.execute("UPDATE sw_orders SET status='expired', ended_at=%s WHERE event_id=%s AND status='pending'", (now, eid))
    conn.execute("UPDATE sw_waitlist SET status='closed', ended_at=%s WHERE event_id=%s AND status IN ('waiting','offered')",
                 (now, eid))
    conn.execute("UPDATE sw_offers SET status='expired', ended_at=%s WHERE event_id=%s AND status='open'", (now, eid))
    tickets = conn.execute("SELECT * FROM sw_tickets WHERE event_id = %s ORDER BY id FOR UPDATE", (eid,)).fetchall()
    sums = {r["ticket_id"]: int(r["s"]) for r in conn.execute(
        "SELECT ticket_id, sum(amount) AS s FROM sw_refunds WHERE event_id = %s AND ticket_id IS NOT NULL GROUP BY ticket_id",
        (eid,))}
    orders = {r["id"]: r for r in conn.execute("SELECT * FROM sw_orders WHERE event_id = %s AND status = 'paid'", (eid,))}
    n = 0
    for t in tickets:
        left = int(t["total"]) - sums.get(t["id"], 0)
        if left > 0:
            _add_refund(conn, "cancel", t, orders[t["order_id"]], left)
            n += 1
            if t["status"] == "valid":
                conn.execute("UPDATE sw_tickets SET status='refund_pending' WHERE id=%s", (t["id"],))
    seats = {s["id"]: s for s in venue_seats(conn, ev["venue_id"])}
    people: dict[str, str] = {}
    for o in sorted(orders.values(), key=lambda o: o["id"]):
        people.setdefault(o["buyer_email_norm"], o["buyer_name"])
    for t in sorted(tickets, key=lambda t: (seats[t["seat_id"]]["key"], t["id"])):
        people.setdefault(t["holder_email_norm"], t["holder_name"])
    when = local_text(ev["starts_at"], ev["timezone"])
    for addr, name in people.items():
        mail.enqueue(conn, f"event-cancelled-{eid}-{addr}", addr, name, f"Cancelled: {ev['name']}",
                     f"Hello {name},\n\n{ev['name']} at {ev['venue_name']} ({when}) has been cancelled by the organizer.\n\n"
                     "Every ticket is refunded in full to the card that paid for it; you do not need to do anything. "
                     "Refunds can take a few days to appear on your statement.\n")
    PROVIDER_KICK.set()
    WORK_KICK.set()
    return n


# ---- check-in ------------------------------------------------------------------------------------------
def run_scans(conn, event_id: int, rows: list[dict], device_id: int | None, now) -> list[dict]:
    """R13.3/R13.4. rows: {scan_id|None, token, gate, scanned_at (datetime), raw (str|None)}. One transaction."""
    conn.execute("SELECT pg_advisory_xact_lock(2, %s)", (int(event_id),))
    ev = load_event(conn, event_id)
    start = ev["starts_at"]
    win_lo, win_hi = start - timedelta(hours=3), start + timedelta(hours=6)
    ids, barcodes, parsed = set(), set(), []
    for r in rows:
        p = parse_qr(r["token"])
        parsed.append(p)
        if p:
            ids.add(p[0])
        else:
            barcodes.add(r["token"])
    tickets = {}
    by_barcode = {}
    if ids or barcodes:
        for t in conn.execute("SELECT * FROM sw_tickets WHERE id = ANY(%s) OR barcode = ANY(%s) ORDER BY id FOR UPDATE",
                              (list(ids), list(barcodes))):
            tickets[t["id"]] = t
            if t["barcode"]:
                by_barcode[t["barcode"]] = t
    sids = [r["scan_id"] for r in rows if r["scan_id"] is not None]
    seen: dict[str, tuple] = {}
    if sids:
        for s in conn.execute("SELECT scan_id, token, gate, scanned_at_raw FROM sw_scans WHERE event_id = %s AND "
                              "scan_id = ANY(%s)", (event_id, sids)):
            seen[s["scan_id"]] = (s["token"], s["gate"], s["scanned_at_raw"])
    adm = {tid: (t["admitted_at"], t["admitted_gate"], None) for tid, t in tickets.items() if t["admitted_at"] is not None}
    changed: set[int] = set()
    out: list[dict] = []
    inserts = []
    for i, (r, p) in enumerate(zip(rows, parsed)):
        res = {"scan_id": r["scan_id"], "result": None, "reason": None, "ticket_id": None, "admitted_at": None,
               "gate": None}
        # resolve the token
        t, reason = None, None
        if p:
            t = tickets.get(p[0])
            if t is None or t["token_version"] == 0 or p[1] > t["token_version"]:
                t, reason = None, "invalid"
            elif p[1] < t["token_version"]:
                reason = "transferred"
        else:
            t = by_barcode.get(r["token"])
            if t is None:
                reason = "invalid"
            elif t["token_version"] > 0:
                reason = "transferred"
        if t is not None:
            res["ticket_id"] = t["id"]
        if r["scan_id"] is not None:
            fields = (sha("scan:" + r["token"]), r["gate"], r["raw"])
            if r["scan_id"] in seen:
                if seen[r["scan_id"]] == fields:
                    res["result"] = "duplicate"
                else:
                    res["result"], res["reason"] = "refused", "scan_id_reused"
                out.append(res)
                continue
            seen[r["scan_id"]] = fields
        final_reason = None
        if reason == "invalid":
            final_reason = "invalid"
        elif t["event_id"] != event_id:
            final_reason = "wrong_event"
        elif ev["status"] == "cancelled":
            final_reason = "cancelled"
        elif t["status"] in ("refund_pending", "refunded"):
            final_reason = "refunded"
        elif reason == "transferred":
            final_reason = "transferred"
        elif r["scanned_at"] < win_lo or r["scanned_at"] >= win_hi:
            final_reason = "outside_window"
        accepted = final_reason is None
        if accepted:
            cur = adm.get(t["id"])
            if cur is None or r["scanned_at"] < cur[0]:
                adm[t["id"]] = (r["scanned_at"], r["gate"], i)
                changed.add(t["id"])
            res["result"] = "accepted"
        else:
            res["result"], res["reason"] = "refused", final_reason
        inserts.append((event_id, device_id, r["scan_id"], sha("scan:" + r["token"]), r["gate"], r["raw"], r["scanned_at"],
                        t["id"] if t is not None else None, accepted, final_reason, now))
        out.append(res)
    for i, res in enumerate(out):
        tid = res["ticket_id"]
        a = adm.get(tid) if tid is not None else None
        if res["result"] == "accepted":
            res["result"] = "admitted" if a and a[2] == i else "already_admitted"
        if a:
            res["admitted_at"], res["gate"] = instant(a[0]), a[1]
    if changed:
        with conn.cursor() as cur:
            cur.executemany("UPDATE sw_tickets SET admitted_at=%s, admitted_gate=%s WHERE id=%s",
                            [(adm[tid][0], adm[tid][1], tid) for tid in sorted(changed)])
    if inserts:
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO sw_scans (event_id, device_id, scan_id, token, gate, scanned_at_raw, scanned_at, "
                            "ticket_id, accepted, reason, received_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", inserts)
    return out


# ---- waiting lists -------------------------------------------------------------------------------------
def offer_status(offer, now) -> str:
    if offer["status"] == "open" and now >= offer["expires_at"]:
        return "expired"
    return offer["status"]


def join_waitlist(conn, acct, ev, section, quantity_text: str, now) -> int:
    from .util import parse_int
    if ev["status"] != "published" or sale_state(ev, now) != "on_sale":
        raise Refused("Waiting lists are open only while the event is on sale.")
    seats = venue_seats(conn, ev["venue_id"])
    states = seat_states(conn, ev, now, seats)
    if any(states[s["id"]] == "available" for s in seats if s["section_id"] == section["id"]):
        raise Refused("This section still has available seats: hold one of them instead of joining the waiting list.")
    q = parse_int(quantity_text, 1, 4)
    if q is None:
        raise Refused("Ask for 1 to 4 seats.", 400)
    mine = conn.execute(
        "SELECT w.id FROM sw_waitlist w WHERE w.event_id = %s AND w.section_id = %s AND w.account_id = %s AND "
        "(w.status = 'waiting' OR (w.status = 'offered' AND EXISTS (SELECT 1 FROM sw_offers f WHERE f.entry_id = w.id "
        "AND f.status = 'open' AND f.expires_at > %s)))", (ev["event_id"], section["id"], acct["id"], now)).fetchone()
    if mine:
        raise Refused("You are already on this section's waiting list.")
    have = buyer_count(conn, ev["event_id"], acct["email_norm"], now)
    if have + q > MAX_PER_BUYER:
        raise Refused(f"Each buyer may have at most {MAX_PER_BUYER} tickets for an event; you already have {have}, so "
                      f"you can ask for at most {max(0, MAX_PER_BUYER - have)}.")
    wid = conn.execute("INSERT INTO sw_waitlist (event_id, section_id, account_id, email_norm, name, quantity, created_at) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                       (ev["event_id"], section["id"], acct["id"], acct["email_norm"], acct["full_name"], q, now)
                       ).fetchone()["id"]
    WORK_KICK.set()
    return wid


def make_offers(conn, event_id: int, now) -> int:
    lock_event(conn, event_id)
    ev = load_event(conn, event_id)
    if ev is None or ev["status"] != "published" or now >= ev["starts_at"]:
        return 0
    entries = conn.execute("SELECT * FROM sw_waitlist WHERE event_id = %s AND status = 'waiting' ORDER BY created_at, id",
                           (event_id,)).fetchall()
    if not entries:
        return 0
    seats = venue_seats(conn, ev["venue_id"])
    states = seat_states(conn, ev, now, seats)
    made = 0
    for section_id in sorted({e["section_id"] for e in entries}):
        reserved = [s for s in seats if s["section_id"] == section_id and states[s["id"]] == "waitlist"]
        for e in (e for e in entries if e["section_id"] == section_id):
            if not reserved:
                break
            if e["quantity"] > len(reserved):
                continue
            take, reserved = reserved[:e["quantity"]], reserved[e["quantity"]:]
            expires = ceil_second(now + timedelta(seconds=offer_ttl()))
            oid = conn.execute(
                "INSERT INTO sw_offers (entry_id, event_id, section_id, account_id, seat_ids, created_at, expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (e["id"], event_id, section_id, e["account_id"], [s["id"] for s in take], now, expires)).fetchone()["id"]
            conn.execute("UPDATE sw_waitlist SET status='offered' WHERE id=%s", (e["id"],))
            marker = mail.new_marker()
            labels = ", ".join(s["label"] for s in take)
            mail.enqueue(conn, f"waitlist-offer-{oid}", e["email_norm"], e["name"], f"Seats available for {ev['name']}",
                         f"Hello {e['name']},\n\nSeats came free for {ev['name']} ({local_text(ev['starts_at'], ev['timezone'])}) "
                         f"and they are offered to you from the waiting list: {labels}.\n\nThe offer is open until "
                         f"{local_text(expires, ev['timezone'])} ({instant(expires)}). Accept or decline it here:\n"
                         f"{mail.offer_ph(marker, oid)}\n", marker)
            made += 1
    return made


def accept_offer(conn, acct, offer, now) -> int:
    lock_event(conn, offer["event_id"])
    offer = conn.execute("SELECT * FROM sw_offers WHERE id = %s FOR UPDATE", (offer["id"],)).fetchone()
    if offer_status(offer, now) != "open":
        raise Refused("This offer is no longer open.")
    ev = load_event(conn, offer["event_id"])
    if ev["status"] != "published":
        raise Refused("The event is no longer on sale.")
    have = buyer_count(conn, ev["event_id"], acct["email_norm"], now)
    if have + len(offer["seat_ids"]) > MAX_PER_BUYER:
        raise Refused(f"Each buyer may have at most {MAX_PER_BUYER} tickets for an event; you already have {have}. "
                      "Decline the offer so the seats go to the next person.")
    seats = [s for s in venue_seats(conn, ev["venue_id"]) if s["id"] in set(offer["seat_ids"])]
    oid = _new_order(conn, ev, acct, seats, now, offer_id=offer["id"])
    conn.execute("UPDATE sw_offers SET status='accepted', order_id=%s, ended_at=%s WHERE id=%s", (oid, now, offer["id"]))
    conn.execute("UPDATE sw_waitlist SET status='accepted', ended_at=%s WHERE id=%s", (now, offer["entry_id"]))
    return oid


def decline_offer(conn, offer, now) -> None:
    offer = conn.execute("SELECT * FROM sw_offers WHERE id = %s FOR UPDATE", (offer["id"],)).fetchone()
    if offer_status(offer, now) != "open":
        raise Refused("This offer is no longer open.")
    conn.execute("UPDATE sw_offers SET status='declined', ended_at=%s WHERE id=%s", (now, offer["id"]))
    conn.execute("UPDATE sw_waitlist SET status='declined', ended_at=%s WHERE id=%s", (now, offer["entry_id"]))
    WORK_KICK.set()


# ---- dashboard and export ----------------------------------------------------------------------------------
FIGURES = ("capacity", "sold", "held", "available", "admitted", "gross", "refunded", "net")


def dashboard(conn, ev, now) -> tuple[dict, list[tuple[dict, dict]]]:
    eid = ev["event_id"]
    seats = venue_seats(conn, ev["venue_id"])
    sec_of = {s["id"]: s["section_id"] for s in seats}
    sections = venue_sections(conn, ev["venue_id"])
    fig = {sec["id"]: dict.fromkeys(FIGURES, 0) for sec in sections}
    for s in seats:
        fig[s["section_id"]]["capacity"] += 1
    sold_seats = set()
    for t in conn.execute("SELECT seat_id, status, admitted_at, total, legacy, legacy_refunded_cents FROM sw_tickets "
                          "WHERE event_id = %s", (eid,)):
        f = fig[sec_of[t["seat_id"]]]
        if t["status"] in ("valid", "refund_pending"):
            f["sold"] += 1
            sold_seats.add(t["seat_id"])
        if t["admitted_at"] is not None:
            f["admitted"] += 1
        f["gross"] += int(t["total"])
        if t["legacy"]:
            f["refunded"] += int(t["legacy_refunded_cents"])
    held = {r["seat_id"] for r in conn.execute(
        "SELECT os.seat_id FROM sw_order_seats os JOIN sw_orders o ON o.id = os.order_id "
        "WHERE o.event_id = %s AND o.status = 'pending' AND o.hold_expires_at > %s", (eid, now))}
    for r in conn.execute("SELECT seat_ids FROM sw_offers WHERE event_id = %s AND status = 'open' AND expires_at > %s",
                          (eid, now)):
        held.update(r["seat_ids"])
    for sid in held - sold_seats:
        fig[sec_of[sid]]["held"] += 1
    for r in conn.execute("SELECT os.seat_id, os.total FROM sw_order_seats os JOIN sw_orders o ON o.id = os.order_id "
                          "WHERE o.event_id = %s AND o.status = 'unavailable'", (eid,)):
        fig[sec_of[r["seat_id"]]]["gross"] += int(r["total"] or 0)
    for r in conn.execute("SELECT t.seat_id, r.amount FROM sw_refunds r JOIN sw_tickets t ON t.id = r.ticket_id "
                          "WHERE r.event_id = %s AND r.state = 'completed'", (eid,)):
        fig[sec_of[r["seat_id"]]]["refunded"] += int(r["amount"])
    for r in conn.execute("SELECT os.seat_id, os.total FROM sw_refunds r JOIN sw_order_seats os ON os.order_id = r.order_id "
                          "WHERE r.event_id = %s AND r.kind = 'late' AND r.state = 'completed'", (eid,)):
        fig[sec_of[r["seat_id"]]]["refunded"] += int(r["total"] or 0)
    total = dict.fromkeys(FIGURES, 0)
    for f in fig.values():
        f["available"] = f["capacity"] - f["sold"] - f["held"]
        f["net"] = f["gross"] - f["refunded"]
        for k in FIGURES:
            total[k] += f[k]
    return total, [(sec, fig[sec["id"]]) for sec in sections]


def _csv_field(v) -> str:
    s = "" if v is None else str(v)
    if any(c in s for c in (",", '"', "\r", "\n")):
        return '"' + s.replace('"', '""') + '"'
    return s


def _neutral(s: str | None) -> str:
    s = s or ""
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


CSV_HEADER = ("ticket_id,order_id,section,row,seat,holder_name,holder_email,buyer_name,buyer_email,status,total_cents,"
              "refunded_cents,purchased_at,checked_in_at,gate")


def attendees_csv(conn, ev) -> str:
    eid = ev["event_id"]
    seats = {s["id"]: s for s in venue_seats(conn, ev["venue_id"])}
    refunded = {r["ticket_id"]: int(r["s"]) for r in conn.execute(
        "SELECT ticket_id, sum(amount) AS s FROM sw_refunds WHERE event_id = %s AND ticket_id IS NOT NULL "
        "AND state = 'completed' GROUP BY ticket_id", (eid,))}
    rows = conn.execute(
        "SELECT t.id, t.order_id, t.seat_id, t.holder_name, t.holder_email_norm, t.status, t.total, t.legacy, "
        "t.legacy_refunded_cents, t.admitted_at, t.admitted_gate, o.buyer_name, o.buyer_email_norm, o.paid_at "
        "FROM sw_tickets t JOIN sw_orders o ON o.id = t.order_id WHERE t.event_id = %s", (eid,)).fetchall()
    rows.sort(key=lambda r: (seats[r["seat_id"]]["key"], r["id"]))
    out = [CSV_HEADER]
    for r in rows:
        s = seats[r["seat_id"]]
        ref = int(r["legacy_refunded_cents"]) if r["legacy"] else refunded.get(r["id"], 0)
        adm = r["admitted_at"]
        vals = [r["id"], r["order_id"], s["code"], s["row_label"], s["seat_number"], _neutral(r["holder_name"]),
                _neutral(r["holder_email_norm"]), _neutral(r["buyer_name"]), _neutral(r["buyer_email_norm"]),
                r["status"], int(r["total"]), ref, instant(r["paid_at"]), instant(adm) if adm else "",
                _neutral(r["admitted_gate"]) if adm else ""]
        out.append(",".join(_csv_field(v) for v in vals))
    return "\r\n".join(out) + "\r\n"


# ---- reminders ---------------------------------------------------------------------------------------------
def send_reminder(conn, ev, key: str, subject: str, body: str, sender_id: int) -> int | None:
    """-> number of recipients, or None when this form (one-time key) was already submitted."""
    kh = sha("reminder:" + key)
    fresh = conn.execute("INSERT INTO sw_reminders (event_id, key_hash, sent_by, subject) VALUES (%s,%s,%s,%s) "
                         "ON CONFLICT DO NOTHING RETURNING event_id", (ev["event_id"], kh, sender_id, subject)).fetchone()
    if not fresh:
        return None
    seats = {s["id"]: s for s in venue_seats(conn, ev["venue_id"])}
    tickets = conn.execute("SELECT id, seat_id, holder_email_norm, holder_name FROM sw_tickets WHERE event_id = %s "
                           "AND status = 'valid'", (ev["event_id"],)).fetchall()
    tickets.sort(key=lambda t: (seats[t["seat_id"]]["key"], t["id"]))
    people: dict[str, str] = {}
    for t in tickets:
        people.setdefault(t["holder_email_norm"], t["holder_name"])
    for addr, name in people.items():
        mail.enqueue(conn, f"reminder-{ev['event_id']}-{kh[:32]}-{addr}", addr, name, subject, body)
    conn.execute("UPDATE sw_reminders SET recipients=%s WHERE event_id=%s AND key_hash=%s", (len(people), ev["event_id"], kh))
    return len(people)
