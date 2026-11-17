"""Pickpath domain layer: orders, payment events, allocation, waves, picks, counts, shipping, returns, reports."""

from __future__ import annotations

import csv
import io
import json
import re
import secrets
import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from . import db, rules
from .db import dollars, enqueue_mail, inv_lock, norm_email

SKU_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,23}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ZONE_OF = {"ambient": "A", "chilled": "C", "frozen": "F"}
REFUND_EVENT = threading.Event()


def parse_date(s) -> date | None:
    if not isinstance(s, str) or not DATE_RE.match(s.strip()):
        return None
    try:
        return date.fromisoformat(s.strip())
    except ValueError:
        return None


def parse_int(v, lo: int, hi: int) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        n = v
    elif isinstance(v, str) and re.fullmatch(r"\s*\d+\s*", v):
        n = int(v)
    else:
        return None
    return n if lo <= n <= hi else None


def num(order_id: int) -> str:
    return f"PP-{order_id:06d}"


# ---- lookups -------------------------------------------------------------------------------------
def get_user(conn, uid: int):
    return conn.execute("SELECT * FROM users WHERE id = %s", (uid,)).fetchone()


def get_customer(conn, cid: int):
    return conn.execute("SELECT * FROM customers WHERE id = %s", (cid,)).fetchone()


def sku_by_code(conn, code: str):
    c = (code or "").strip().upper()
    return conn.execute("SELECT * FROM skus WHERE upper(code) = %s", (c,)).fetchone()


def tax_info(conn, state: str) -> tuple[Decimal, bool]:
    r = conn.execute("SELECT rate, freight_taxable FROM tax_rates WHERE state = %s", (state,)).fetchone()
    return Decimal(r["rate"]), bool(r["freight_taxable"])


def price_for(conn, tier: str, sku_id: int, day: date) -> int | None:
    r = conn.execute("SELECT unit_price_cents FROM price_list WHERE tier = %s AND sku_id = %s AND effective_from <= %s"
                     " ORDER BY effective_from DESC LIMIT 1", (tier, sku_id, day)).fetchone()
    return r["unit_price_cents"] if r else None


def sku_info(s) -> rules.SkuInfo:
    return rules.SkuInfo(s["code"], s["case_pack"], s["unit_weight_g"], s["length_cm"], s["width_cm"], s["height_cm"],
                         s["storage"], bool(s["taxable"]))


def get_order(conn, oid: int, lock: bool = False):
    return conn.execute("SELECT * FROM pp_orders WHERE id = %s" + (" FOR UPDATE" if lock else ""), (oid,)).fetchone()


def order_lines(conn, oid: int):
    return conn.execute("SELECT l.*, s.code AS sku_code, s.case_pack, s.name AS sku_name FROM pp_order_lines l"
                        " JOIN skus s ON s.id = l.sku_id WHERE l.order_id = %s ORDER BY l.line_no", (oid,)).fetchall()


# ---- order creation (R4.1-R4.3, R5.1-R5.4) -----------------------------------------------------------
def create_order(conn, user, ship_s, po, notes, lines_in) -> tuple[int | None, list[str]]:
    errors: list[str] = []
    cust = get_customer(conn, user["customer_id"])
    d = parse_date(ship_s)
    t = db.today(conn)
    if d is None:
        errors.append("ship_date must be a date (YYYY-MM-DD).")
    elif not (t <= d <= t + timedelta(days=365)):
        errors.append("ship_date must be between today and 365 days from today.")
    po = "" if po is None else po
    notes = "" if notes is None else notes
    if not isinstance(po, str) or len(po) > 40 or db.has_ctrl(po):
        errors.append("po_number must be at most 40 characters with no control characters.")
    if not isinstance(notes, str) or len(notes) > 500 or db.has_ctrl(notes, allow_newlines=True):
        errors.append("notes must be at most 500 characters.")
    if not isinstance(lines_in, list) or not (1 <= len(lines_in) <= 12):
        errors.append("An order needs 1 to 12 lines.")
        lines_in = lines_in if isinstance(lines_in, list) else []
    parsed, seen = [], set()
    for i, (code, qty_raw) in enumerate(lines_in, start=1):
        if not isinstance(code, str) or not code.strip():
            errors.append(f"Line {i}: a SKU code is required.")
            continue
        s = sku_by_code(conn, code)
        qty = parse_int(qty_raw, 1, 100000)
        if s is None or not s["active"]:
            errors.append(f"Line {i}: unknown or inactive SKU.")
            continue
        if s["id"] in seen:
            errors.append(f"Line {i}: SKU {s['code']} appears more than once.")
            continue
        seen.add(s["id"])
        if qty is None:
            errors.append(f"Line {i}: qty must be a whole number from 1 to 100000.")
            continue
        if not cust["allow_split_cases"] and qty % s["case_pack"]:
            errors.append(f"Line {i}: {s['code']} is sold in whole cases of {s['case_pack']}.")
            continue
        price = price_for(conn, cust["tier"], s["id"], d) if d else None
        if d and price is None:
            errors.append(f"Line {i}: {s['code']} has no {cust['tier']} price on {d}.")
            continue
        parsed.append((s, qty, price))
    if errors:
        return None, errors
    rate, ftax = tax_info(conn, cust["ship_to_state"])
    tot = rules.order_totals([(sku_info(s), q, p) for s, q, p in parsed], rate, ftax)
    limit = cust["approval_limit_cents"]
    needs = limit is not None and tot["total_cents"] > limit
    oid = conn.execute(
        "INSERT INTO pp_orders (customer_id, created_by, ship_date, po_number, notes, status, subtotal, tax, freight,"
        " freight_tax, total, needs_approval, approval_limit, rate) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (cust["id"], user["id"], d, po, notes, "awaiting_approval" if needs else "awaiting_payment", tot["subtotal_cents"],
         tot["tax_cents"], tot["freight_cents"], tot["freight_tax_cents"], tot["total_cents"], needs, limit, rate)).fetchone()["id"]
    for n, ((s, q, p), l) in enumerate(zip(parsed, tot["lines"]), start=1):
        conn.execute("INSERT INTO pp_order_lines (order_id, line_no, sku_id, qty, unit_price, amount, tax, taxable)"
                     " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (oid, n, s["id"], q, p, l["amount_cents"], l["tax_cents"], bool(s["taxable"])))
    if needs:
        for a in conn.execute("SELECT * FROM users WHERE customer_id = %s AND role = 'approver' AND active AND id <> %s",
                              (cust["id"], user["id"])).fetchall():
            enqueue_mail(conn, f"approval:{oid}:{a['id']}", a["email"], a["full_name"], f"Approval needed: order {num(oid)}",
                         f"Hello {a['full_name']},\n\nOrder {num(oid)} for {cust['name']} totals {dollars(tot['total_cents'])}, "
                         f"above your company's approval limit of {dollars(limit)}. Approve it at "
                         f"{db.ENV['PUBLIC_BASE_URL'].rstrip('/')}/orders/{oid} so it can be paid.\n\nLarder & Line Provisions\n")
    return oid, []


# ---- order JSON (R4.9) -------------------------------------------------------------------------------------
def order_json(conn, oid: int) -> dict:
    o = get_order(conn, oid)
    lines = order_lines(conn, oid)
    alloc: dict[int, dict[int, int]] = {}
    for r in conn.execute("SELECT line_no, lot_id, sum(units) AS u FROM pp_tasks WHERE order_id = %s AND status IN ('open','picked')"
                          " GROUP BY 1, 2", (oid,)).fetchall():
        if r["u"]:
            alloc.setdefault(r["line_no"], {})[r["lot_id"]] = int(r["u"])
    out_lines = []
    for l in lines:
        a = alloc.get(l["line_no"], {})
        out_lines.append({"line_no": l["line_no"], "sku": l["sku_code"], "qty": l["qty"], "unit_price_cents": l["unit_price"],
                          "amount_cents": int(l["amount"]), "tax_cents": int(l["tax"]), "reserved": sum(a.values()),
                          "shipped": l["shipped"], "backordered": l["backordered"], "cancelled": l["cancelled"],
                          "returned": l["returned"], "allocations": [{"lot_id": k, "units": v} for k, v in sorted(a.items())]})
    refunds = [{"id": r["id"], "amount_cents": int(r["amount"]), "status": r["status"]} for r in
               conn.execute("SELECT * FROM pp_refunds WHERE order_id = %s ORDER BY id", (oid,)).fetchall()]
    ships = [{"id": s["id"], "tracking_number": s["tracking"], "invoice_number": s["invoice_number"], "invoice_total_cents": int(s["total"])}
             for s in conn.execute("SELECT * FROM pp_shipments WHERE order_id = %s ORDER BY id", (oid,)).fetchall()]
    credits = [{"id": c["id"], "rma_id": c["rma_id"], "amount_cents": int(c["amount"])} for c in
               conn.execute("SELECT * FROM pp_credit_notes WHERE order_id = %s ORDER BY id", (oid,)).fetchall()]
    return {"id": o["id"], "number": num(o["id"]), "status": o["status"], "customer_id": o["customer_id"],
            "ship_date": o["ship_date"].isoformat(), "po_number": o["po_number"], "subtotal_cents": int(o["subtotal"]),
            "tax_cents": int(o["tax"]), "freight_cents": int(o["freight"]), "freight_tax_cents": int(o["freight_tax"]),
            "total_cents": int(o["total"]), "lines": out_lines, "refunds": refunds, "shipments": ships, "credit_notes": credits}


# ---- approvals and cancellation (R4.4, R4.5) -------------------------------------------------------------
def approve(conn, user, oid: int) -> tuple[int, str]:
    o = get_order(conn, oid, lock=True)
    if o["created_by"] == user["id"]:
        return 403, "Nobody approves an order they created. Ask another approver."
    if o["status"] != "awaiting_approval":
        return 409, "This order is not waiting for approval."
    conn.execute("UPDATE pp_orders SET status = 'awaiting_payment', approved_by = %s WHERE id = %s", (user["id"], oid))
    return 200, "Order approved. A buyer can now pay for it."


def cancel_order(conn, user, oid: int) -> tuple[int, str]:
    from . import payments
    o = get_order(conn, oid, lock=True)
    if o["status"] not in ("awaiting_approval", "awaiting_payment"):
        return 409, "Only an order that is not paid yet can be cancelled."
    conn.execute("UPDATE pp_orders SET status = 'cancelled' WHERE id = %s", (oid,))
    if o["session_id"]:
        payments.expire_session(o["session_id"])
    return 200, "Order cancelled."


# ---- refunds (R5.9, R5.10) -------------------------------------------------------------------------------
def due(conn, oid: int) -> int:
    o = get_order(conn, oid)
    lines = [{"qty": l["qty"], "cancelled": l["cancelled"], "unit_price_cents": l["unit_price"], "taxable": l["taxable"]}
             for l in order_lines(conn, oid)]
    return rules.due_of(lines, int(o["freight"]), int(o["freight_tax"]), Decimal(o["rate"]))


def create_refund(conn, oid: int, amount: int, reason: str, payment_intent: str, key: str | None = None) -> None:
    if amount <= 0:
        return
    key = key or "pickpath-refund-" + secrets.token_hex(12)
    conn.execute("INSERT INTO pp_refunds (order_id, amount, idem_key, payment_intent, reason) VALUES (%s,%s,%s,%s,%s)"
                 " ON CONFLICT (idem_key) DO NOTHING", (oid, amount, key, payment_intent, reason))
    REFUND_EVENT.set()


def refund_difference(conn, oid: int, before: int, reason: str) -> None:
    after = due(conn, oid)
    if before - after > 0:
        o = get_order(conn, oid)
        create_refund(conn, oid, before - after, reason, o["payment_intent"])


def refresh_status(conn, oid: int) -> None:
    o = get_order(conn, oid)
    if o["status"] not in ("in_fulfilment", "complete", "cancelled") or not o["payment_intent"]:
        return
    lines = order_lines(conn, oid)
    if all(l["cancelled"] == l["qty"] for l in lines):
        st = "cancelled"
    elif all(l["shipped"] + l["cancelled"] == l["qty"] for l in lines):
        st = "complete"
    else:
        st = "in_fulfilment"
    if st != o["status"]:
        conn.execute("UPDATE pp_orders SET status = %s WHERE id = %s", (st, oid))


# ---- allocation (R6) ------------------------------------------------------------------------------------
def reserved_of(conn, lot_id: int, statuses=("open", "picked")) -> int:
    r = conn.execute("SELECT coalesce(sum(units), 0) AS u FROM pp_tasks WHERE lot_id = %s AND status = ANY(%s)",
                     (lot_id, list(statuses))).fetchone()
    return int(r["u"])


def candidate_lots(conn, sku_id: int, ship: date, min_days: int):
    return conn.execute(
        "SELECT l.id, l.expiry_date, st.on_hand, coalesce(r.u, 0) AS reserved FROM lots l"
        " JOIN pp_lot_state st ON st.lot_id = l.id JOIN bins b ON b.id = l.bin_id"
        " LEFT JOIN (SELECT lot_id, sum(units) AS u FROM pp_tasks WHERE status IN ('open','picked') AND lot_id IN"
        "   (SELECT id FROM lots WHERE sku_id = %s) GROUP BY lot_id) r ON r.lot_id = l.id"
        " WHERE l.sku_id = %s AND b.code <> 'QA-01' AND NOT l.qa_hold AND NOT st.count_hold AND l.expiry_date >= %s"
        " ORDER BY l.expiry_date, l.id", (sku_id, sku_id, ship + timedelta(days=min_days))).fetchall()


def add_task(conn, oid: int, line_no: int, lot_id: int, units: int) -> None:
    r = conn.execute("SELECT id FROM pp_tasks WHERE order_id = %s AND line_no = %s AND lot_id = %s AND status = 'open'"
                     " AND wave_id IS NULL LIMIT 1", (oid, line_no, lot_id)).fetchone()
    if r:
        conn.execute("UPDATE pp_tasks SET units = units + %s WHERE id = %s", (units, r["id"]))
    else:
        conn.execute("INSERT INTO pp_tasks (order_id, line_no, lot_id, units) VALUES (%s,%s,%s,%s)", (oid, line_no, lot_id, units))


def allocate(conn, order, line_no: int, sku, n: int, cust) -> int:
    """R6.2. Returns the units that could not be allocated."""
    lots = [rules.LotView(r["id"], r["expiry_date"], int(r["on_hand"]) - int(r["reserved"]), True)
            for r in candidate_lots(conn, sku["id"], order["ship_date"], cust["min_shelf_life_days"])]
    got, rem = rules.fefo(n, lots, order["ship_date"], cust["min_shelf_life_days"], sku["case_pack"], cust["allow_split_cases"])
    for lot_id, units in got:
        add_task(conn, order["id"], line_no, lot_id, units)
    return rem


def settle(conn, oid: int, line_no: int, rem: int, cust) -> None:
    if rem <= 0:
        return
    col = "backordered" if cust["backorders_allowed"] else "cancelled"
    conn.execute(f"UPDATE pp_order_lines SET {col} = {col} + %s WHERE order_id = %s AND line_no = %s", (rem, oid, line_no))


def reallocate(conn, oid: int, line_no: int, n: int) -> None:
    """Allocate n units of a line anew; the rest backorders or cancels (the caller refunds per event)."""
    order = get_order(conn, oid)
    cust = get_customer(conn, order["customer_id"])
    sku = conn.execute("SELECT s.* FROM skus s JOIN pp_order_lines l ON l.sku_id = s.id WHERE l.order_id = %s AND l.line_no = %s",
                       (oid, line_no)).fetchone()
    settle(conn, oid, line_no, allocate(conn, order, line_no, sku, n, cust), cust)


def fill_backorders(conn, sku_id: int) -> None:
    """R6.4."""
    rows = conn.execute("SELECT l.order_id, l.line_no, l.backordered FROM pp_order_lines l JOIN pp_orders o ON o.id = l.order_id"
                        " WHERE l.sku_id = %s AND l.backordered > 0 AND o.status = 'in_fulfilment' ORDER BY l.order_id, l.line_no",
                        (sku_id,)).fetchall()
    sku = conn.execute("SELECT * FROM skus WHERE id = %s", (sku_id,)).fetchone()
    for r in rows:
        order = get_order(conn, r["order_id"])
        cust = get_customer(conn, order["customer_id"])
        rem = allocate(conn, order, r["line_no"], sku, r["backordered"], cust)
        conn.execute("UPDATE pp_order_lines SET backordered = %s WHERE order_id = %s AND line_no = %s", (rem, r["order_id"], r["line_no"]))


def check_low_stock(conn, sku_ids) -> None:
    """R9.2."""
    t = db.today(conn)
    for sid in sorted(set(sku_ids)):
        s = conn.execute("SELECT * FROM skus WHERE id = %s", (sid,)).fetchone()
        if not s or s["reorder_point"] <= 0:
            continue
        r = conn.execute(
            "SELECT coalesce(sum(st.on_hand - coalesce(r.u, 0)), 0) AS a FROM lots l JOIN pp_lot_state st ON st.lot_id = l.id"
            " JOIN bins b ON b.id = l.bin_id LEFT JOIN (SELECT lot_id, sum(units) AS u FROM pp_tasks WHERE status IN ('open','picked')"
            " GROUP BY lot_id) r ON r.lot_id = l.id WHERE l.sku_id = %s AND b.code <> 'QA-01' AND NOT l.qa_hold"
            " AND NOT st.count_hold AND l.expiry_date >= %s", (sid, t)).fetchone()
        avail = int(r["a"])
        if avail >= s["reorder_point"]:
            continue
        if conn.execute("INSERT INTO pp_low_stock VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING sku_id", (sid, t)).fetchone() is None:
            continue
        for u in conn.execute("SELECT * FROM users WHERE role = 'supervisor' AND active ORDER BY id").fetchall():
            enqueue_mail(conn, f"lowstock:{sid}:{t}:{u['id']}", u["email"], u["full_name"], f"Low stock: {s['code']}",
                         f"Hello {u['full_name']},\n\nSKU {s['code']} ({s['name']}) has {avail} units available, below its reorder "
                         f"point of {s['reorder_point']} units, as of {t.isoformat()}.\n\nLarder & Line Provisions\n")


def sku_ids_of_order(conn, oid: int) -> list[int]:
    return [r["sku_id"] for r in conn.execute("SELECT sku_id FROM pp_order_lines WHERE order_id = %s", (oid,)).fetchall()]


def lot_sku(conn, lot_id: int) -> int:
    return conn.execute("SELECT sku_id FROM lots WHERE id = %s", (lot_id,)).fetchone()["sku_id"]


# ---- payment events (R5.7, R5.8) -----------------------------------------------------------------------------
def on_session_completed(conn, s: dict) -> None:
    try:
        oid = int((s.get("metadata") or {}).get("order_id"))
    except (TypeError, ValueError):
        return
    inv_lock(conn)  # same lock order as every other stock change: inventory lock, then the order row
    order = get_order(conn, oid, lock=True)
    if order is None:
        return
    paid = s.get("payment_status") == "paid"
    if (order["status"] == "awaiting_payment" and s.get("id") == order["session_id"] and paid
            and int(s.get("amount_total") or -1) == int(order["total"])):
        conn.execute("UPDATE pp_orders SET status = 'in_fulfilment', payment_intent = %s, paid_at = now() WHERE id = %s",
                     (s.get("payment_intent"), oid))
        order = get_order(conn, oid)
        cust = get_customer(conn, order["customer_id"])
        before = due(conn, oid)
        for l in order_lines(conn, oid):
            sku = conn.execute("SELECT * FROM skus WHERE id = %s", (l["sku_id"],)).fetchone()
            settle(conn, oid, l["line_no"], allocate(conn, order, l["line_no"], sku, l["qty"], cust), cust)
        refund_difference(conn, oid, before, "units not available")
        refresh_status(conn, oid)
        check_low_stock(conn, sku_ids_of_order(conn, oid))
        u = get_user(conn, order["created_by"])
        enqueue_mail(conn, f"confirmed:{oid}", u["email"], u["full_name"], f"Order confirmed: {num(oid)}",
                     f"Hello {u['full_name']},\n\nWe received your payment of {dollars(order['total'])} for order {num(oid)}. "
                     f"Requested ship date: {order['ship_date'].isoformat()}.\n\nLarder & Line Provisions\n")
        return
    if paid and s.get("payment_intent") and (order["status"] == "cancelled" or
                                             (order["payment_intent"] and s.get("payment_intent") != order["payment_intent"])):
        create_refund(conn, oid, int(s.get("amount_total") or 0), "late payment", s["payment_intent"], key=f"late-{s.get('id')}")


def on_refund_created(conn, r: dict) -> None:
    md = r.get("metadata") or {}
    row = None
    if r.get("id"):
        row = conn.execute("SELECT * FROM pp_refunds WHERE provider_id = %s FOR UPDATE", (r["id"],)).fetchone()
    if row is None and str(md.get("refund_id") or "").isdigit():
        row = conn.execute("SELECT * FROM pp_refunds WHERE id = %s FOR UPDATE", (int(md["refund_id"]),)).fetchone()
    if row is None or row["status"] != "pending":
        return
    conn.execute("UPDATE pp_refunds SET status = 'refunded' WHERE id = %s", (row["id"],))
    o = get_order(conn, row["order_id"])
    u = get_user(conn, o["created_by"])
    enqueue_mail(conn, f"refund:{row['id']}", u["email"], u["full_name"], f"Refund issued: {num(o['id'])}",
                 f"Hello {u['full_name']},\n\nWe refunded {dollars(row['amount'])} for order {num(o['id'])} to your card.\n\n"
                 "Larder & Line Provisions\n")


# ---- backorder cancellation (R6.5) ------------------------------------------------------------------------
def cancel_backorder(conn, oid: int, line_no: int, units) -> tuple[int, str]:
    inv_lock(conn)
    o = get_order(conn, oid, lock=True)
    l = conn.execute("SELECT * FROM pp_order_lines WHERE order_id = %s AND line_no = %s", (oid, line_no)).fetchone()
    if l is None:
        return 404, "No such line."
    n = parse_int(units, 1, max(1, l["backordered"]))
    if o["status"] != "in_fulfilment" or l["backordered"] <= 0 or n is None:
        return 409, "Only backordered units can be cancelled, from 1 to the units backordered."
    before = due(conn, oid)
    conn.execute("UPDATE pp_order_lines SET backordered = backordered - %s, cancelled = cancelled + %s WHERE order_id = %s AND line_no = %s",
                 (n, n, oid, line_no))
    refund_difference(conn, oid, before, "backorder cancelled")
    refresh_status(conn, oid)
    return 200, f"{n} backordered units cancelled; the refund is on its way."


# ---- setup (R3) ---------------------------------------------------------------------------------------------
def receive(conn, f: dict) -> tuple[int, str, int | None]:
    inv_lock(conn)
    s = sku_by_code(conn, f.get("sku", ""))
    code = (f.get("lot_code") or "")
    exp = parse_date(f.get("expiry_date"))
    b = conn.execute("SELECT * FROM bins WHERE code = %s", ((f.get("bin") or "").strip().upper(),)).fetchone()
    units = parse_int(f.get("units"), 1, 1000000)
    cost_s = (f.get("unit_cost") or "").strip()
    if s is None:
        return 422, "Unknown SKU.", None
    if not code.strip() or len(code.strip()) > 32 or db.has_ctrl(code):
        return 422, "The lot code must be 1 to 32 characters.", None
    if exp is None:
        return 422, "The expiry date must be a date (YYYY-MM-DD).", None
    if b is None or b["zone"] != ZONE_OF[s["storage"]]:
        return 422, f"The bin must exist and be in zone {ZONE_OF[s['storage']]} for {s['storage']} stock.", None
    if units is None:
        return 422, "Units must be a whole number from 1 to 1000000.", None
    if not re.fullmatch(r"\d+(\.\d{1,4})?", cost_s) or Decimal(cost_s) <= 0:
        return 422, "The unit cost must be a positive amount in dollars with at most 4 decimals.", None
    cost = Decimal(cost_s)
    lot = conn.execute("SELECT * FROM lots WHERE sku_id = %s AND upper(btrim(lot_code)) = %s ORDER BY id LIMIT 1",
                       (s["id"], code.strip().upper())).fetchone()
    if lot:
        if lot["expiry_date"] != exp or lot["bin_id"] != b["id"] or Decimal(lot["unit_cost"]) != cost:
            return 409, "That lot already exists with a different expiry date, bin or unit cost.", lot["id"]
        lot_id = lot["id"]
    else:
        lot_id = conn.execute("INSERT INTO lots (sku_id, lot_code, expiry_date, bin_id, unit_cost) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                              (s["id"], code.strip(), exp, b["id"], cost)).fetchone()["id"]
        conn.execute("INSERT INTO pp_lot_state (lot_id, on_hand, version) VALUES (%s, 0, 0)", (lot_id,))
    add_movement(conn, lot_id, "receipt", units)
    fill_backorders(conn, s["id"])
    check_low_stock(conn, [s["id"]])
    return 200, "Received.", lot_id


def add_movement(conn, lot_id: int, kind: str, units: int, ref: str = "") -> None:
    conn.execute("INSERT INTO pp_movements (lot_id, kind, units, ref) VALUES (%s,%s,%s,%s)", (lot_id, kind, units, ref))
    conn.execute("UPDATE pp_lot_state SET on_hand = on_hand + %s, version = version + 1 WHERE lot_id = %s", (units, lot_id))


# ---- waves (R7.2, R7.3) -------------------------------------------------------------------------------------
def create_wave(conn, zone: str, ship: date, k: int) -> tuple[int, str, int | None]:
    inv_lock(conn)
    rows = conn.execute(
        "SELECT t.id, b.code FROM pp_tasks t JOIN pp_orders o ON o.id = t.order_id JOIN lots l ON l.id = t.lot_id"
        " JOIN bins b ON b.id = l.bin_id WHERE t.status = 'open' AND t.wave_id IS NULL AND t.units > 0 AND b.zone = %s"
        " AND o.ship_date = %s ORDER BY t.order_id, t.line_no, l.expiry_date, l.id, t.id LIMIT %s", (zone, ship, k)).fetchall()
    if not rows:
        return 409, "No unpicked tasks for that zone and ship date are waiting for a wave.", None
    distance, seq = rules.route([r["code"] for r in rows])
    wid = conn.execute("INSERT INTO pp_waves (zone, ship_date, distance, route) VALUES (%s,%s,%s,%s) RETURNING id",
                       (zone, ship, distance, json.dumps(seq))).fetchone()["id"]
    conn.execute("UPDATE pp_tasks SET wave_id = %s WHERE id = ANY(%s)", (wid, [r["id"] for r in rows]))
    return 200, "Wave created.", wid


# ---- picking (R7.4, R7.5) ------------------------------------------------------------------------------------
def confirm_pick(conn, user, task_id: int, f: dict) -> tuple[int, str]:
    inv_lock(conn)
    t = conn.execute("SELECT t.*, l.lot_code, l.sku_id, b.code AS bin_code, b.zone FROM pp_tasks t JOIN lots l ON l.id = t.lot_id"
                     " JOIN bins b ON b.id = l.bin_id WHERE t.id = %s FOR UPDATE OF t", (task_id,)).fetchone()
    if t is None:
        return 404, "No such pick task."
    if user["role"] == "picker" and user["zone"] != t["zone"]:
        return 403, "This pick is in another zone."
    if t["status"] != "open" or t["wave_id"] is None:
        return 409, "This pick was already confirmed."
    if (f.get("bin_code") or "").strip().upper() != t["bin_code"].strip().upper():
        return 409, f"Wrong bin: this pick is in bin {t['bin_code']}. Go to that bin, scan it, and try again."
    if (f.get("lot_code") or "").strip().upper() != t["lot_code"].strip().upper():
        return 409, f"Wrong lot: this pick takes lot {t['lot_code'].strip()}. Scan that lot's label and try again."
    order = get_order(conn, t["order_id"])
    cust = get_customer(conn, order["customer_id"])
    pack = conn.execute("SELECT case_pack FROM skus WHERE id = %s", (t["sku_id"],)).fetchone()["case_pack"]
    n = parse_int(f.get("units"), 0, t["units"])
    if n is None or (not cust["allow_split_cases"] and n % pack):
        return 422, f"Units must be a whole number from 0 to {t['units']}" + ("" if cust["allow_split_cases"] else f", in whole cases of {pack}") + "."
    if n == t["units"]:
        conn.execute("UPDATE pp_tasks SET status = 'picked' WHERE id = %s", (task_id,))
        check_low_stock(conn, [t["sku_id"]])
        return 200, "Pick confirmed."
    before = due(conn, order["id"])
    conn.execute("UPDATE pp_tasks SET status = 'picked', units = %s WHERE id = %s", (n, task_id))
    conn.execute("UPDATE pp_lot_state SET count_hold = true WHERE lot_id = %s", (t["lot_id"],))
    reallocate(conn, order["id"], t["line_no"], t["units"] - n)
    refund_difference(conn, order["id"], before, "short pick")
    refresh_status(conn, order["id"])
    check_low_stock(conn, [t["sku_id"]])
    return 200, f"Short pick recorded: {n} of {t['units']} units. The lot is held for a cycle count and the rest is being re-allocated."


# ---- cycle counts (R7.7) --------------------------------------------------------------------------------------
def count_lot(conn, lot_id: int, f: dict) -> tuple[int, str]:
    inv_lock(conn)
    st = conn.execute("SELECT * FROM pp_lot_state WHERE lot_id = %s FOR UPDATE", (lot_id,)).fetchone()
    if st is None:
        return 404, "No such lot."
    counted = parse_int(f.get("counted_units"), 0, 100000000)
    if counted is None:
        return 422, "Counted units must be a whole number from 0."
    if str(f.get("version", "")).strip() != str(st["version"]):
        return 409, "This lot changed since you opened it (its version is now different). Reload the lot page, count again, and submit."
    picked = reserved_of(conn, lot_id, ("picked",))
    new = counted + picked
    add_movement(conn, lot_id, "count", new - st["on_hand"])
    conn.execute("UPDATE pp_lot_state SET count_hold = false WHERE lot_id = %s", (lot_id,))
    open_res = reserved_of(conn, lot_id, ("open",))
    excess = open_res - counted
    touched: dict[int, int] = {}
    released: list[tuple[int, int, int]] = []
    if excess > 0:
        for t in conn.execute("SELECT * FROM pp_tasks WHERE lot_id = %s AND status = 'open' ORDER BY order_id DESC, line_no DESC, id DESC",
                              (lot_id,)).fetchall():
            if excess <= 0:
                break
            take = min(excess, t["units"])
            if take == t["units"]:
                conn.execute("DELETE FROM pp_tasks WHERE id = %s", (t["id"],))
            else:
                conn.execute("UPDATE pp_tasks SET units = units - %s WHERE id = %s", (take, t["id"]))
            excess -= take
            released.append((t["order_id"], t["line_no"], take))
    for oid, line_no, n in released:
        if oid not in touched:
            touched[oid] = due(conn, oid)
        reallocate(conn, oid, line_no, n)
    for oid, before in touched.items():
        refund_difference(conn, oid, before, "stock count")
        refresh_status(conn, oid)
    sid = lot_sku(conn, lot_id)
    fill_backorders(conn, sid)
    check_low_stock(conn, [sid])
    return 200, "Count recorded."


# ---- shipping and invoices (R8.1-R8.3) ----------------------------------------------------------------------
def ship(conn, oid: int) -> tuple[int, str, int | None]:
    inv_lock(conn)
    o = get_order(conn, oid, lock=True)
    tasks = conn.execute("SELECT * FROM pp_tasks WHERE order_id = %s AND status IN ('open','picked')", (oid,)).fetchall()
    picked = [t for t in tasks if t["status"] == "picked" and t["units"] > 0]
    if o["status"] != "in_fulfilment" or not picked or any(t["status"] == "open" for t in tasks):
        return 409, "This order cannot ship now: it needs picked units and no picks still open.", None
    first = conn.execute("SELECT 1 FROM pp_shipments WHERE order_id = %s LIMIT 1", (oid,)).fetchone() is None
    rate = Decimal(o["rate"])
    per_line: dict[int, int] = {}
    per_lot: dict[int, int] = {}
    for t in picked:
        per_line[t["line_no"]] = per_line.get(t["line_no"], 0) + t["units"]
        per_lot[t["lot_id"]] = per_lot.get(t["lot_id"], 0) + t["units"]
    inv_lines, total = [], 0
    for l in order_lines(conn, oid):
        u = per_line.get(l["line_no"], 0)
        if not u:
            continue
        amt = u * l["unit_price"]
        tx = rules.shipment_line_tax(l["shipped"], l["shipped"] + u, l["unit_price"], l["taxable"], rate)
        inv_lines.append({"line_no": l["line_no"], "sku": l["sku_code"], "units": u, "unit_price_cents": l["unit_price"],
                          "amount_cents": amt, "tax_cents": tx})
        total += amt + tx
        conn.execute("UPDATE pp_order_lines SET shipped = shipped + %s WHERE order_id = %s AND line_no = %s", (u, oid, l["line_no"]))
    fr, ftx = (int(o["freight"]), int(o["freight_tax"])) if first else (0, 0)
    total += fr + ftx
    for lot_id, u in sorted(per_lot.items()):
        add_movement(conn, lot_id, "ship", -u, num(oid))
    conn.execute("UPDATE pp_tasks SET status = 'shipped' WHERE order_id = %s AND status = 'picked'", (oid,))
    while True:
        tracking = "PP" + "".join(secrets.choice("0123456789") for _ in range(10))
        if conn.execute("SELECT 1 FROM pp_shipments WHERE tracking = %s", (tracking,)).fetchone() is None:
            break
    sid = conn.execute("INSERT INTO pp_shipments (order_id, tracking, invoice_number, lines, freight, freight_tax, total)"
                       " VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                       (oid, tracking, "pending-" + secrets.token_hex(8), json.dumps(inv_lines), fr, ftx, total)).fetchone()["id"]
    inv_no = f"INV-{sid:06d}"
    conn.execute("UPDATE pp_shipments SET invoice_number = %s WHERE id = %s", (inv_no, sid))
    refresh_status(conn, oid)
    check_low_stock(conn, sku_ids_of_order(conn, oid))
    u = get_user(conn, o["created_by"])
    enqueue_mail(conn, f"shipped:{sid}", u["email"], u["full_name"], f"Shipped: {num(oid)}",
                 f"Hello {u['full_name']},\n\nPart or all of order {num(oid)} has shipped.\n\nTracking number: {tracking}\n"
                 f"Invoice: {inv_no}\nInvoice total: {dollars(total)}\n\nLarder & Line Provisions\n")
    return 200, "Shipped.", sid


# ---- returns and credits (R8.4-R8.7) ---------------------------------------------------------------------------
def request_rma(conn, user, oid: int, f: dict) -> tuple[int, str, int | None]:
    o = get_order(conn, oid, lock=True)
    ln = parse_int(f.get("line_no"), 1, 12)
    l = conn.execute("SELECT * FROM pp_order_lines WHERE order_id = %s AND line_no = %s", (oid, ln)).fetchone() if ln else None
    if l is None:
        return 422, "Choose a line of this order.", None
    used = conn.execute("SELECT coalesce(sum(units), 0) AS u FROM pp_rmas WHERE order_id = %s AND line_no = %s", (oid, ln)).fetchone()["u"]
    room = l["shipped"] - int(used)
    n = parse_int(f.get("units"), 1, max(room, 0))
    reason = f.get("reason") or ""
    if n is None or room <= 0:
        return 422, f"You can return 1 to {max(room, 0)} units of this line.", None
    if not reason.strip() or len(reason) > 500:
        return 422, "Give a reason of 1 to 500 characters.", None
    rid = conn.execute("INSERT INTO pp_rmas (order_id, line_no, units, reason, requested_by) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                       (oid, ln, n, reason, user["id"])).fetchone()["id"]
    return 200, "Return requested.", rid


def receive_rma(conn, rma_id: int) -> tuple[int, str]:
    r = conn.execute("SELECT * FROM pp_rmas WHERE id = %s FOR UPDATE", (rma_id,)).fetchone()
    if r["status"] != "requested":
        return 409, "This return was already received."
    o = get_order(conn, r["order_id"], lock=True)
    l = conn.execute("SELECT * FROM pp_order_lines WHERE order_id = %s AND line_no = %s", (r["order_id"], r["line_no"])).fetchone()
    amount = rules.credit_of(l["shipped"] - l["returned"], r["units"], l["unit_price"], l["taxable"], Decimal(o["rate"]))
    conn.execute("UPDATE pp_rmas SET status = 'quarantined' WHERE id = %s", (rma_id,))
    conn.execute("UPDATE pp_order_lines SET returned = returned + %s WHERE order_id = %s AND line_no = %s",
                 (r["units"], r["order_id"], r["line_no"]))
    cid = conn.execute("INSERT INTO pp_credit_notes (rma_id, order_id, amount) VALUES (%s,%s,%s) RETURNING id",
                       (rma_id, r["order_id"], amount)).fetchone()["id"]
    u = get_user(conn, r["requested_by"])
    enqueue_mail(conn, f"credit:{cid}", u["email"], u["full_name"], f"Credit note for {num(o['id'])}",
                 f"Hello {u['full_name']},\n\nWe received your return of {r['units']} units for order {num(o['id'])}. "
                 f"Credit note CN-{cid:06d} for {dollars(amount)} has been issued to your account.\n\nLarder & Line Provisions\n")
    return 200, "Return received into quarantine; credit note issued."


def inspect_rma(conn, rma_id: int, outcome: str) -> tuple[int, str]:
    inv_lock(conn)
    r = conn.execute("SELECT * FROM pp_rmas WHERE id = %s FOR UPDATE", (rma_id,)).fetchone()
    if outcome not in ("restock", "damaged"):
        return 422, "Choose restock or damaged."
    if r["status"] != "quarantined":
        return 409, "Only a return in quarantine can be inspected."
    lot = conn.execute("SELECT l.* FROM pp_movements m JOIN lots l ON l.id = m.lot_id JOIN pp_order_lines ol ON ol.sku_id = l.sku_id"
                       " WHERE m.kind = 'ship' AND m.ref = %s AND ol.order_id = %s AND ol.line_no = %s"
                       " ORDER BY l.expiry_date, l.id LIMIT 1", (num(r["order_id"]), r["order_id"], r["line_no"])).fetchone()
    if outcome == "restock" and lot is not None and lot["expiry_date"] >= db.today(conn) + timedelta(days=7):
        add_movement(conn, lot["id"], "restock", r["units"], f"RMA-{rma_id}")
        disp = "restocked"
        conn.execute("UPDATE pp_rmas SET status = 'closed', disposition = %s, lot_id = %s WHERE id = %s", (disp, lot["id"], rma_id))
        fill_backorders(conn, lot["sku_id"])
        check_low_stock(conn, [lot["sku_id"]])
    else:
        disp = "written_off"
        conn.execute("UPDATE pp_rmas SET status = 'closed', disposition = %s, lot_id = %s WHERE id = %s",
                     (disp, lot["id"] if lot else None, rma_id))
    return 200, f"Inspection recorded: {disp.replace('_', ' ')}."


# ---- legacy wall-clock times (R10.2) -----------------------------------------------------------------------------
_LEGACY: dict = {}
_LEGACY_LOCK = threading.Lock()


def legacy_cache(conn) -> dict:
    with _LEGACY_LOCK:
        if not _LEGACY:
            tz = str(db.site_tz(conn))
            mv = []
            for r in conn.execute("SELECT m.id, m.lot_id, m.kind, m.units, m.local_time, m.fold, l.sku_id FROM legacy_movements m"
                                  " JOIN lots l ON l.id = m.lot_id").fetchall():
                mv.append((r["sku_id"], r["lot_id"], r["kind"], r["units"], rules.legacy_instant(r["local_time"], r["fold"], tz), r["id"]))
            _LEGACY["movements"] = mv
            by_sku: dict[int, list] = {}
            for m in mv:
                by_sku.setdefault(m[0], []).append(m)
            _LEGACY["by_sku"] = by_sku
            _LEGACY["orders"] = {r["id"]: rules.legacy_instant(r["placed_local"], r["fold"], tz) for r in
                                 conn.execute("SELECT id, placed_local, fold FROM legacy_orders").fetchall()}
        return _LEGACY


# ---- CSV (R9.7) ---------------------------------------------------------------------------------------------------
def csv_text(header: list[str], rows: list[list], text_cols: set[int]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow([rules.csv_safe(str(v)) if i in text_cols and v is not None else ("" if v is None else v) for i, v in enumerate(r)])
    return buf.getvalue()


def report_on_hand(conn) -> str:
    t = db.today(conn)
    rows = conn.execute(
        "SELECT l.id, s.code, l.lot_code, b.code AS bin, l.expiry_date, st.on_hand, coalesce(r.u, 0) AS reserved, l.qa_hold,"
        " st.count_hold FROM lots l JOIN skus s ON s.id = l.sku_id JOIN bins b ON b.id = l.bin_id JOIN pp_lot_state st ON st.lot_id = l.id"
        " LEFT JOIN (SELECT lot_id, sum(units) AS u FROM pp_tasks WHERE status IN ('open','picked') GROUP BY lot_id) r ON r.lot_id = l.id"
        " WHERE st.on_hand > 0 ORDER BY l.id").fetchall()
    out = []
    for r in rows:
        if r["bin"] == "QA-01":
            status = "quarantine"
        elif r["qa_hold"] or r["count_hold"]:
            status = "hold"
        elif r["expiry_date"] < t:
            status = "expired"
        else:
            status = "available"
        avail = int(r["on_hand"]) - int(r["reserved"]) if status == "available" else 0
        out.append([r["id"], r["code"], r["lot_code"], r["bin"], r["expiry_date"].isoformat(), r["on_hand"], int(r["reserved"]), avail, status])
    return csv_text(["lot_id", "sku", "lot_code", "bin", "expiry_date", "on_hand", "reserved", "available", "status"], out, {1, 2, 3})


def utcz(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def report_ledger(conn, code: str) -> str | None:
    s = sku_by_code(conn, code)
    if s is None:
        return None
    cache = legacy_cache(conn)
    rows = [(m[4], 0, m[5], m[1], m[2], m[3]) for m in cache["by_sku"].get(s["id"], [])]
    for m in conn.execute("SELECT m.* FROM pp_movements m JOIN lots l ON l.id = m.lot_id WHERE l.sku_id = %s", (s["id"],)).fetchall():
        rows.append((m["at"], 1, m["id"], m["lot_id"], m["kind"], m["units"]))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return csv_text(["lot_id", "kind", "units", "at"], [[r[3], r[4], r[5], utcz(r[0])] for r in rows], set())


def report_valuation(conn, as_of: datetime) -> str:
    cache = legacy_cache(conn)
    cost = {r["id"]: (r["sku_id"], Decimal(r["unit_cost"])) for r in conn.execute("SELECT id, sku_id, unit_cost FROM lots").fetchall()}
    by_sku: dict[int, list] = {}
    for m in cache["movements"]:
        by_sku.setdefault(m[0], []).append((m[4], (0, m[5]), m[3], cost[m[1]][1]))
    for m in conn.execute("SELECT * FROM pp_movements WHERE at <= %s", (as_of,)).fetchall():
        sid, c = cost[m["lot_id"]]
        by_sku.setdefault(sid, []).append((m["at"], (1, m["id"]), m["units"], c))
    codes = {r["id"]: r["code"] for r in conn.execute("SELECT id, code FROM skus").fetchall()}
    out = []
    for sid, mv in by_sku.items():
        on, val = rules.fifo_value(mv, as_of)
        if on > 0:
            out.append([codes[sid], on, val])
    out.sort(key=lambda r: r[0])
    return csv_text(["sku", "on_hand", "fifo_value_cents"], out, {0})


def report_orders(conn, customer_id: int | None) -> str:
    cache = legacy_cache(conn)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM customers").fetchall()}
    rows = []
    q = "SELECT * FROM legacy_orders" + (" WHERE customer_id = %s" if customer_id else "")
    for r in conn.execute(q, (customer_id,) if customer_id else ()).fetchall():
        rows.append((cache["orders"][r["id"]], r["number"], r["customer_id"], r["status"], r["po_number"] or "", r["ship_date"], r["total_cents"]))
    q = "SELECT * FROM pp_orders" + (" WHERE customer_id = %s" if customer_id else "")
    for r in conn.execute(q, (customer_id,) if customer_id else ()).fetchall():
        rows.append((r["created_at"], num(r["id"]), r["customer_id"], r["status"], r["po_number"], r["ship_date"], r["total"]))
    rows.sort(key=lambda r: (utcz(r[0]), r[1]))
    return csv_text(["number", "customer_id", "customer_name", "placed_at", "status", "po_number", "ship_date", "total_cents"],
                    [[r[1], r[2], names.get(r[2], ""), utcz(r[0]), r[3], r[4], r[5].isoformat(), int(r[6])] for r in rows], {0, 2, 5})
