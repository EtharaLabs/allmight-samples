"""Payment provider client (Stripe-compatible), webhook verification, and the refund worker (R5.5-R5.9)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

from . import core, db

TOLERANCE = 300


def _api() -> str:
    return db.ENV["PAYMENTS_API_URL"].rstrip("/")


def call(method: str, path: str, fields: dict | None = None, idem: str | None = None, timeout: float = 25.0) -> tuple[int, dict]:
    data = urllib.parse.urlencode(fields).encode() if fields is not None else None
    req = urllib.request.Request(_api() + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if idem:
        req.add_header("Idempotency-Key", idem)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:  # noqa: BLE001 - no answer
        return 0, {"error": str(e)}


def register_webhook() -> None:
    url = db.ENV["PUBLIC_BASE_URL"].rstrip("/") + "/webhooks/payments"
    for _ in range(60):
        st, _ = call("POST", "/v1/webhook_endpoints", {"url": url}, timeout=10)
        if st == 200:
            return
        time.sleep(1)


def create_session(order, user_email: str) -> tuple[int, dict]:
    base = db.ENV["PUBLIC_BASE_URL"].rstrip("/")
    oid = order["id"]
    fields = {
        "mode": "payment",
        "success_url": f"{base}/orders/{oid}?checkout=success",
        "cancel_url": f"{base}/orders/{oid}?checkout=cancelled",
        "customer_email": db.norm_email(user_email),
        "metadata[order_id]": str(oid),
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(int(order["total"])),
        "line_items[0][price_data][product_data][name]": f"Order {core.num(oid)}",
        "line_items[0][quantity]": "1",
    }
    return call("POST", "/v1/checkout/sessions", fields, idem=f"pickpath-checkout-{oid}")


def expire_session(sid: str) -> None:
    try:
        call("POST", f"/v1/checkout/sessions/{urllib.parse.quote(sid)}/expire", {}, timeout=10)
    except Exception:
        pass


def signing_key() -> str:
    with open(db.ENV["PAYMENTS_WEBHOOK_SIGNING_FILE"], encoding="utf-8") as fh:
        return fh.read().strip()


def verify(header: str | None, body: bytes) -> bool:
    if not header:
        return False
    ts, sigs = None, []
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            ts = v
        elif k == "v1":
            sigs.append(v)
    if not ts or not ts.isdigit() or not sigs:
        return False
    if abs(time.time() - int(ts)) > TOLERANCE:
        return False
    want = hmac.new(signing_key().encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(want, s) for s in sigs)


def handle(event: dict) -> None:
    """A verified event: act at most once per event id."""
    eid, etype = str(event.get("id") or ""), str(event.get("type") or "")
    obj = (event.get("data") or {}).get("object") or {}
    if etype not in ("checkout.session.completed", "refund.created") or not eid:
        return
    with db.connect() as conn:
        if conn.execute("INSERT INTO pp_events (event_id, type) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING event_id",
                        (eid, etype)).fetchone() is None:
            return
        if etype == "checkout.session.completed":
            core.on_session_completed(conn, obj)
        else:
            core.on_refund_created(conn, obj)
        conn.commit()
    db.OUTBOX_EVENT.set()


def refund_loop() -> None:
    """Submit pending refunds; retry failures with the same Idempotency-Key (never holding a row lock while calling)."""
    while True:
        core.REFUND_EVENT.wait(1.0)
        core.REFUND_EVENT.clear()
        try:
            with db.connect() as conn:
                rows = conn.execute("SELECT * FROM pp_refunds WHERE NOT submitted AND next_try <= now() ORDER BY id LIMIT 20").fetchall()
            for r in rows:
                st, body = call("POST", "/v1/refunds", {"payment_intent": r["payment_intent"], "amount": str(int(r["amount"])),
                                                        "metadata[order_id]": str(r["order_id"]),
                                                        "metadata[refund_id]": str(r["id"])}, idem=r["idem_key"])
                with db.connect() as conn:
                    if st == 200 and body.get("id"):
                        conn.execute("UPDATE pp_refunds SET submitted = true, provider_id = %s, last_error = NULL WHERE id = %s",
                                     (body["id"], r["id"]))
                    elif 400 <= st < 500 and st not in (409, 429):
                        conn.execute("UPDATE pp_refunds SET submitted = true, last_error = %s WHERE id = %s",
                                     (json.dumps(body)[:500], r["id"]))
                    else:
                        conn.execute("UPDATE pp_refunds SET next_try = now() + interval '2 seconds', last_error = %s WHERE id = %s",
                                     (json.dumps(body)[:500], r["id"]))
                        core.REFUND_EVENT.set()
                    conn.commit()
        except Exception:
            traceback.print_exc()
            time.sleep(1)
