"""Background work: hold and offer expiry, waiting-list offers, provider session expiry, refund sending
(with retries under a stable Idempotency-Key), webhook endpoint registration."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

from . import domain, payments
from .db import connect, install_id
from .util import log_exc, now

_INFLIGHT: set[int] = set()
_INFLIGHT_LOCK = threading.Lock()
RETRY_SECONDS = 2


def housekeeping_once() -> None:
    t = now()
    with connect() as conn:
        conn.execute("UPDATE sw_orders SET status='expired', ended_at=hold_expires_at WHERE status='pending' "
                     "AND hold_expires_at <= %s", (t,))
        ended = conn.execute("UPDATE sw_offers SET status='expired', ended_at=expires_at WHERE status='open' "
                             "AND expires_at <= %s RETURNING entry_id", (t,)).fetchall()
        if ended:
            conn.execute("UPDATE sw_waitlist SET status='expired', ended_at=%s WHERE id = ANY(%s) AND status='offered'",
                         (t, [r["entry_id"] for r in ended]))
        conn.commit()
        events = [r["event_id"] for r in conn.execute(
            "SELECT DISTINCT event_id FROM sw_waitlist WHERE status = 'waiting' ORDER BY event_id")]
        conn.commit()
        for eid in events:
            try:
                domain.make_offers(conn, eid, now())
                conn.commit()
            except Exception:
                conn.rollback()
                log_exc("worker")
    from . import mail
    mail.KICK.set()


def housekeeping_loop() -> None:
    while True:
        domain.WORK_KICK.wait(1.0)
        domain.WORK_KICK.clear()
        try:
            housekeeping_once()
        except Exception:
            log_exc("worker")
            time.sleep(1)


def expire_sessions_once() -> None:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, session_id FROM sw_orders WHERE session_id IS NOT NULL AND NOT session_expired AND "
            "(status IN ('expired','released') OR (status = 'pending' AND hold_expires_at <= %s)) ORDER BY id LIMIT 50",
            (now(),)).fetchall()
        conn.commit()
        for r in rows:
            status, _ = payments.call("POST", f"/v1/checkout/sessions/{r['session_id']}/expire", [], timeout=10)
            if status is not None and (200 <= status < 300 or status in (400, 404)):
                conn.execute("UPDATE sw_orders SET session_expired = true WHERE id = %s", (r["id"],))
                conn.commit()


def refund_params(r) -> list[tuple[str, str]]:
    params = [("payment_intent", r["payment_intent"]), ("amount", str(r["amount"]))]
    if r["ticket_id"] is not None:
        params.append(("metadata[ticket_id]", str(r["ticket_id"])))
    params.append(("metadata[order_id]", str(r["order_id"])))
    params.append(("metadata[seatwise_refund_id]", str(r["id"])))
    params.append(("reason", "requested_by_customer"))
    return params


def send_refund(refund_id: int) -> None:
    try:
        with connect() as conn:
            r = conn.execute("SELECT * FROM sw_refunds WHERE id = %s", (refund_id,)).fetchone()
            key = f"seatwise-{install_id(conn)}-refund-{refund_id}"
            conn.commit()
            if r is None or r["state"] != "requested":
                return
            status, body = payments.call("POST", "/v1/refunds", refund_params(r), idem_key=key, timeout=20)
            if status is not None and 200 <= status < 300:
                rid = body.get("id") if isinstance(body, dict) else None
                conn.execute("UPDATE sw_refunds SET state = CASE WHEN state = 'requested' THEN 'sent' ELSE state END, "
                             "provider_refund_id = COALESCE(provider_refund_id, %s), attempts = attempts + 1, "
                             "last_error = NULL WHERE id = %s", (rid, refund_id))
            else:
                err = f"provider answered {status}" if status is not None else "no answer from the provider"
                wait = RETRY_SECONDS if status is None or status >= 500 or status == 429 else 10
                conn.execute("UPDATE sw_refunds SET attempts = attempts + 1, last_error = %s, next_attempt_at = %s "
                             "WHERE id = %s", (err, now() + timedelta(seconds=wait), refund_id))
            conn.commit()
    except Exception:
        log_exc("worker")
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(refund_id)
        domain.PROVIDER_KICK.set()


def dispatch_refunds_once() -> None:
    with connect() as conn:
        rows = conn.execute("SELECT id FROM sw_refunds WHERE state = 'requested' AND next_attempt_at <= %s "
                            "ORDER BY id LIMIT 200", (now(),)).fetchall()
        conn.commit()
    for r in rows:
        with _INFLIGHT_LOCK:
            if r["id"] in _INFLIGHT or len(_INFLIGHT) >= 16:
                continue
            _INFLIGHT.add(r["id"])
        threading.Thread(target=send_refund, args=(r["id"],), daemon=True, name=f"refund-{r['id']}").start()


def refund_loop() -> None:
    while True:
        domain.PROVIDER_KICK.wait(1.0)
        domain.PROVIDER_KICK.clear()
        try:
            dispatch_refunds_once()
        except Exception:
            log_exc("worker")
            time.sleep(1)


def session_loop() -> None:
    while True:
        time.sleep(1.0)
        try:
            expire_sessions_once()
        except Exception:
            log_exc("worker")
            time.sleep(1)


def start() -> None:
    for target, name in ((housekeeping_loop, "housekeeping"), (refund_loop, "refunds"), (session_loop, "sessions"),
                         (payments.registrar_loop, "webhook-registrar")):
        threading.Thread(target=target, daemon=True, name=name).start()
    domain.WORK_KICK.set()
    domain.PROVIDER_KICK.set()
