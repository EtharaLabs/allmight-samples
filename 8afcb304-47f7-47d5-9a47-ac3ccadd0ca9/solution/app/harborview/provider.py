"""Card-payment provider client (Stripe-compatible) and webhook signature verification (R6.6)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db


class ProviderError(Exception):
    pass


def _post(path: str, fields: list[tuple[str, str]], idem: str | None = None, timeout: float = 25.0) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(db.PAYMENTS_API_URL + path, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    if idem:
        req.add_header("Idempotency-Key", idem)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise ProviderError(f"{e.code}: {body[:300]}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ProviderError(str(e)) from None


def register_webhook() -> None:
    while True:
        try:
            _post("/v1/webhook_endpoints", [("url", db.PUBLIC_BASE_URL + "/webhooks/payments")], timeout=10)
            return
        except ProviderError:
            time.sleep(2)


def create_checkout(appt_id: int, amount: int, product: str, email: str) -> dict:
    base = db.PUBLIC_BASE_URL
    return _post("/v1/checkout/sessions", [
        ("mode", "payment"),
        ("success_url", f"{base}/appointments/{appt_id}?checkout=success"),
        ("cancel_url", f"{base}/appointments/{appt_id}?checkout=cancelled"),
        ("customer_email", email),
        ("metadata[appointment_id]", str(appt_id)),
        ("line_items[0][price_data][currency]", "usd"),
        ("line_items[0][price_data][unit_amount]", str(amount)),
        ("line_items[0][price_data][product_data][name]", product),
        ("line_items[0][quantity]", "1"),
    ], idem=f"checkout-{appt_id}", timeout=25)


def expire_session(session_id: str) -> None:
    try:
        _post(f"/v1/checkout/sessions/{urllib.parse.quote(session_id)}/expire", [], timeout=10)
    except ProviderError:
        pass


def create_refund(payment_intent: str, amount: int, appt_id: int, key: str) -> dict:
    return _post("/v1/refunds", [("payment_intent", payment_intent), ("amount", str(amount)),
                                 ("metadata[appointment_id]", str(appt_id))], idem=key, timeout=25)


_KEY: dict = {"key": None}


def _signing_key(reload: bool = False) -> str:
    if reload or not _KEY["key"]:
        try:
            with open(db.SIGNING_FILE, encoding="utf-8") as fh:
                _KEY["key"] = fh.read().strip()
        except OSError:
            _KEY["key"] = ""
    return _KEY["key"] or ""


def verify(header: str | None, body: bytes, tolerance: int = 300) -> bool:
    if not header:
        return False
    t, sigs = None, []
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            t = v
        elif k == "v1":
            sigs.append(v)
    if t is None or not t.isdigit() or not sigs:
        return False
    if abs(time.time() - int(t)) > tolerance:
        return False
    for reload in (False, True):
        key = _signing_key(reload)
        if not key:
            continue
        want = hmac.new(key.encode(), t.encode() + b"." + body, hashlib.sha256).hexdigest()
        if any(hmac.compare_digest(want, s) for s in sigs):
            return True
    return False
