"""Card-payment provider (Stripe-compatible) client and webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .util import base_url, env, log_exc


def api_url(path: str) -> str:
    return env("PAYMENTS_API_URL").strip().rstrip("/") + path


def call(method: str, path: str, params: list[tuple[str, str]] | None = None, idem_key: str | None = None,
         timeout: float = 20.0) -> tuple[int | None, dict | None]:
    """-> (status, json body); (None, None) when the provider did not answer."""
    data = urllib.parse.urlencode(params or []).encode() if method == "POST" else None
    req = urllib.request.Request(api_url(path), data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if idem_key:
        req.add_header("Idempotency-Key", idem_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read()
        except Exception:
            raw = b""
    except Exception:
        return None, None
    try:
        body = json.loads(raw.decode() or "{}")
    except Exception:
        body = None
    return status, body if isinstance(body, dict) else None


def webhook_url() -> str:
    return base_url() + "/webhooks/payments"


def ensure_webhook() -> bool:
    status, body = call("GET", "/v1/webhook_endpoints", timeout=10)
    if status == 200 and body and any(ep.get("url") == webhook_url() for ep in body.get("data") or []):
        return True
    status, body = call("POST", "/v1/webhook_endpoints", [("url", webhook_url())], timeout=10)
    return status is not None and 200 <= status < 300


def signing_key() -> str | None:
    path = env("PAYMENTS_WEBHOOK_SIGNING_FILE")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            key = fh.read().strip()
        return key or None
    except OSError:
        return None


def verify(raw: bytes, header: str | None, tolerance: int = 300) -> bool:
    if not header:
        return False
    key = signing_key()
    if not key:
        return False
    ts = None
    sigs = []
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            ts = v.strip()
        elif k == "v1":
            sigs.append(v.strip())
    if ts is None or not ts.isdigit() or not sigs:
        return False
    if abs(time.time() - int(ts)) > tolerance:
        return False
    expected = hmac.new(key.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def registrar_loop() -> None:
    """Register PUBLIC_BASE_URL/webhooks/payments at start-up (retrying until the provider answers) and
    re-check it periodically so a provider restart does not silently drop deliveries."""
    delay = 1.0
    registered = False
    while True:
        try:
            ok = ensure_webhook()
        except Exception:
            log_exc("payments")
            ok = False
        if ok:
            registered = True
        time.sleep(3.0 if registered and ok else delay)
        delay = min(delay * 1.5, 5.0)
