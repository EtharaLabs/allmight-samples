#!/usr/bin/env python3
"""Card-payment provider: a Stripe-compatible mock with signed webhooks.

Surfaces: Checkout Sessions (with a hosted payment page), Payment Intents, Charges, Refunds,
Customers, Subscriptions, Webhook Endpoints, and Events, all under /v1/ with Stripe's request
and response shapes for the fields an application uses. Every event is delivered to each registered
webhook endpoint as a POST whose Stripe-Signature header is `t=<unix>,v1=<hex>` where v1 is the
HMAC-SHA256 of `<t>.<body>` under the signing key this service writes to the file named by
PAYMENTS_WEBHOOK_SIGNING_FILE at start-up. GET / documents the surface and the test cards.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from adminsock import AdminAwareHandler, record, start_admin_listener  # noqa: E402

PORT = int(os.environ.get("PAYMENTS_PORT", "4242"))
PUBLIC_URL = os.environ.get("PAYMENTS_PUBLIC_URL", f"http://localhost:{PORT}").rstrip(
    "/"
)
SIGNING_FILE = os.environ.get(
    "PAYMENTS_WEBHOOK_SIGNING_FILE", "/run/payments/webhook-signing-key"
)
# The /_mock/ administrative surface is served only on the listener adminsock selects
# (PAYMENTS_ADMIN_SOCKET or PAYMENTS_ADMIN_PORT) and refused on the public port; the application
# never needs it.
ADMIN_LOG = os.environ.get("PAYMENTS_ADMIN_LOG", "").strip() or None
DELIVERY_ATTEMPTS = (0.0, 1.0, 3.0)
# How long an armed "timeout" or "lost_response" fault holds its reply before answering 503.
FAULT_DELAY = float(os.environ.get("PAYMENTS_FAULT_DELAY", "15"))
TEST_CARDS = {
    "4242424242424242": ("succeeds", None),
    "4000000000000002": ("declined", "Your card was declined."),
    "4000000000009995": ("declined", "Your card has insufficient funds."),
}

LOCK = threading.RLock()
STATE: dict[str, dict] = {
    "checkout_sessions": {},
    "payment_intents": {},
    "charges": {},
    "refunds": {},
    "customers": {},
    "subscriptions": {},
    "webhook_endpoints": {},
    "events": {},
}
DELIVERIES: list[dict] = []
ORDER: list[str] = []
# Webhook hold: while active, every event whose type matches is queued instead of delivered.
HOLD: dict = {"active": False, "types": None}
HELD: list[dict] = []
# One-shot faults armed through the administrative surface, keyed by operation.
FAULTS: dict[str, str] = {}
FAULT_MODES = {"checkout_create": ("error", "timeout"), "refund_create": ("error", "timeout", "lost_response", "slow")}
# Per-operation reply delay of the armed fault, in seconds (default FAULT_DELAY); "slow" performs the
# operation and answers 200 after the delay.
FAULT_DELAYS: dict[str, float] = {}
# Idempotency-Key replay store: key -> {"path", "fingerprint", "status", "payload"}.
IDEMPOTENCY: dict[str, dict] = {}
# Every POST to /v1/: path, Idempotency-Key header, status, time.
REQUESTS: list[dict] = []


def _load_signing_key() -> str:
    directory = os.path.dirname(SIGNING_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    if os.path.isfile(SIGNING_FILE):
        with open(SIGNING_FILE, encoding="utf-8") as handle:
            existing = handle.read().strip()
        if existing:
            return existing
    key = "whsec_" + secrets.token_hex(24)
    with open(SIGNING_FILE, "w", encoding="utf-8") as handle:
        handle.write(key + "\n")
    os.chmod(SIGNING_FILE, 0o644)
    return key


SIGNING_KEY = ""  # set by main(); importing this module has no side effect


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _now() -> int:
    return int(time.time())


def _event(event_type: str, obj: dict) -> dict:
    event = {
        "id": _new_id("evt"),
        "object": "event",
        "api_version": "2024-06-20",
        "created": _now(),
        "livemode": False,
        "type": event_type,
        "data": {"object": obj},
        "pending_webhooks": len(STATE["webhook_endpoints"]),
    }
    STATE["events"][event["id"]] = event
    ORDER.append(event["id"])
    if HOLD["active"] and (HOLD["types"] is None or event_type in HOLD["types"]):
        HELD.append(event)
    else:
        threading.Thread(target=_deliver, args=(event,), daemon=True).start()
    return event


def _deliver_in_order(events: list[dict]) -> None:
    for event in events:
        _deliver(event)


def _sign(timestamp: int, body: bytes) -> str:
    digest = hmac.new(
        SIGNING_KEY.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


def _deliver(event: dict, endpoints: list[dict] | None = None) -> None:
    body = json.dumps(event, separators=(",", ":"), sort_keys=True).encode()
    with LOCK:
        targets = (
            endpoints
            if endpoints is not None
            else list(STATE["webhook_endpoints"].values())
        )
    for endpoint in targets:
        for attempt, delay in enumerate(DELIVERY_ATTEMPTS, start=1):
            time.sleep(delay)
            timestamp = _now()
            request = urllib.request.Request(
                endpoint["url"],
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Stripe-Signature": _sign(timestamp, body),
                    "User-Agent": "Payments-Mock/1.0",
                },
            )
            record = {
                "event_id": event["id"],
                "event_type": event["type"],
                "endpoint_id": endpoint["id"],
                "url": endpoint["url"],
                "attempt": attempt,
                "at": timestamp,
                "status_code": None,
                "error": None,
            }
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    record["status_code"] = response.status
            except urllib.error.HTTPError as exc:
                record["status_code"] = exc.code
            except (urllib.error.URLError, OSError, ValueError) as exc:
                record["error"] = str(exc)
            with LOCK:
                DELIVERIES.append(record)
            if record["status_code"] is not None and 200 <= record["status_code"] < 300:
                break


def _parse_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b""
    content_type = (handler.headers.get("Content-Type") or "").split(";")[0].strip()
    if content_type == "application/json":
        try:
            data = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            data = {}
        return data if isinstance(data, dict) else {}
    flat = urllib.parse.parse_qs(raw.decode(errors="replace"), keep_blank_values=True)
    return _unflatten({key: values[-1] for key, values in flat.items()})


def _unflatten(flat: dict[str, str]) -> dict:
    """Stripe form encoding: a[b][0][c]=v becomes nested mappings and lists."""
    out: dict = {}
    for key, value in flat.items():
        parts = [key.split("[", 1)[0]] + [
            part.rstrip("]") for part in key.split("[")[1:]
        ]
        node = out
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            if last:
                node[part] = value
            else:
                node = node.setdefault(part, {})
    return _listify(out)


def _listify(node):
    if isinstance(node, dict):
        converted = {key: _listify(value) for key, value in node.items()}
        if converted and all(key.isdigit() for key in converted):
            return [converted[key] for key in sorted(converted, key=int)]
        return converted
    return node


def _total(items) -> int:
    total = 0
    for item in items or []:
        price_data = item.get("price_data", {}) if isinstance(item, dict) else {}
        amount = int(price_data.get("unit_amount", item.get("amount", 0)) or 0)
        total += amount * int(item.get("quantity", 1) or 1)
    return total


def _currency(items) -> str:
    for item in items or []:
        price_data = item.get("price_data", {}) if isinstance(item, dict) else {}
        if price_data.get("currency"):
            return str(price_data["currency"]).lower()
    return "usd"


def _complete_session(session: dict, card: str, force: bool = False) -> tuple[bool, str | None]:
    outcome, message = TEST_CARDS.get(
        card, ("declined", "Your card number is not a valid test card.")
    )
    if session["status"] == "complete" or (session["status"] != "open" and not force):
        return False, "This Checkout Session is no longer open."
    if outcome != "succeeds":
        return False, message
    intent = {
        "id": _new_id("pi"),
        "object": "payment_intent",
        "amount": session["amount_total"],
        "amount_received": session["amount_total"],
        "currency": session["currency"],
        "status": "succeeded",
        "created": _now(),
        "customer": session.get("customer"),
        "receipt_email": session.get("customer_email"),
        "metadata": dict(session.get("metadata") or {}),
        "latest_charge": None,
    }
    charge = {
        "id": _new_id("ch"),
        "object": "charge",
        "amount": session["amount_total"],
        "amount_refunded": 0,
        "currency": session["currency"],
        "paid": True,
        "refunded": False,
        "status": "succeeded",
        "created": _now(),
        "payment_intent": intent["id"],
        "payment_method_details": {
            "card": {"brand": "visa", "last4": card[-4:]},
            "type": "card",
        },
        "metadata": dict(session.get("metadata") or {}),
    }
    intent["latest_charge"] = charge["id"]
    STATE["payment_intents"][intent["id"]] = intent
    STATE["charges"][charge["id"]] = charge
    session.update(
        {"status": "complete", "payment_status": "paid", "payment_intent": intent["id"]}
    )
    _event("checkout.session.completed", dict(session))
    _event("payment_intent.succeeded", dict(intent))
    return True, None


def _refund(params: dict) -> tuple[int, dict]:
    intent_id = params.get("payment_intent")
    charge_id = params.get("charge")
    charge = None
    if charge_id:
        charge = STATE["charges"].get(charge_id)
    elif intent_id:
        intent = STATE["payment_intents"].get(intent_id)
        charge = STATE["charges"].get(intent["latest_charge"]) if intent else None
    if charge is None:
        return 400, _error("resource_missing", "No such charge or payment_intent.")
    remaining = charge["amount"] - charge["amount_refunded"]
    amount = int(params.get("amount") or remaining)
    if amount <= 0 or amount > remaining:
        return 400, _error(
            "charge_already_refunded", "The charge has no refundable amount left."
        )
    refund = {
        "id": _new_id("re"),
        "object": "refund",
        "amount": amount,
        "currency": charge["currency"],
        "charge": charge["id"],
        "payment_intent": charge["payment_intent"],
        "status": "succeeded",
        "reason": params.get("reason", "requested_by_customer"),
        "created": _now(),
        "metadata": params.get("metadata") or {},
    }
    STATE["refunds"][refund["id"]] = refund
    charge["amount_refunded"] += amount
    charge["refunded"] = charge["amount_refunded"] >= charge["amount"]
    _event("refund.created", dict(refund))
    _event("charge.refunded", dict(charge))
    return 200, refund


def _error(code: str, message: str, status: str = "invalid_request_error") -> dict:
    return {"error": {"type": status, "code": code, "message": message}}


DOCS = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Card-payment provider</title>
<style>body{font-family:system-ui,sans-serif;max-width:48rem;margin:2rem auto;padding:0 1rem;line-height:1.5}
code,pre{background:#f4f4f4;padding:.1rem .3rem}pre{padding:.6rem;overflow:auto}</style></head>
<body><h1>Card-payment provider</h1>
<p>A Stripe-compatible card-payment API. Requests take form-encoded or JSON bodies and
answer JSON with Stripe's object shapes. No API key is required.</p>
<h2>Surface</h2>
<ul>
<li><code>POST /v1/checkout/sessions</code> with <code>mode=payment</code>, <code>success_url</code>, <code>cancel_url</code>,
<code>customer_email</code>, <code>metadata[...]</code>, and <code>line_items[0][price_data][currency]</code>,
<code>line_items[0][price_data][unit_amount]</code>, <code>line_items[0][price_data][product_data][name]</code>,
<code>line_items[0][quantity]</code>. The response carries <code>id</code>, <code>url</code> (the hosted payment page),
<code>status</code> (<code>open</code>, <code>complete</code>, <code>expired</code>), <code>payment_status</code>, and <code>payment_intent</code>.
<code>{CHECKOUT_SESSION_ID}</code> in either URL is replaced on redirect.</li>
<li><code>GET /v1/checkout/sessions/{id}</code>, <code>POST /v1/checkout/sessions/{id}/expire</code></li>
<li><code>GET /v1/payment_intents/{id}</code>, <code>GET /v1/charges/{id}</code></li>
<li><code>POST /v1/refunds</code> with <code>payment_intent</code> or <code>charge</code> and an optional <code>amount</code>; <code>GET /v1/refunds/{id}</code></li>
<li><code>POST /v1/customers</code>, <code>GET /v1/customers/{id}</code></li>
<li><code>POST /v1/subscriptions</code> with <code>customer</code>, <code>GET /v1/subscriptions/{id}</code>, <code>DELETE /v1/subscriptions/{id}</code></li>
<li><code>POST /v1/webhook_endpoints</code> with <code>url</code>; <code>GET /v1/webhook_endpoints</code>; <code>DELETE /v1/webhook_endpoints/{id}</code>.
Registering the same URL twice returns the same endpoint.</li>
<li><code>GET /v1/events</code>, <code>GET /v1/events/{id}</code></li>
</ul>
<h2>Webhooks</h2>
<p>Each event is POSTed as JSON to every registered endpoint with a <code>Stripe-Signature</code> header
<code>t=&lt;unix seconds&gt;,v1=&lt;hex&gt;</code>, where <code>v1</code> is HMAC-SHA256 over <code>&lt;t&gt;.&lt;raw body&gt;</code>
under the signing key. The key is in the file the environment names in <code>PAYMENTS_WEBHOOK_SIGNING_FILE</code>.
A delivery that does not get a 2xx is retried twice. Event types raised: <code>checkout.session.completed</code>,
<code>payment_intent.succeeded</code>, <code>checkout.session.expired</code>, <code>refund.created</code>, <code>charge.refunded</code>,
<code>customer.subscription.created</code>, <code>customer.subscription.deleted</code>.</p>
<h2>Idempotency</h2>
<p><code>POST /v1/checkout/sessions</code> and <code>POST /v1/refunds</code> honour an <code>Idempotency-Key</code> header: a repeat
with the same key and parameters returns the first response with <code>Idempotent-Replayed: true</code> and creates nothing.</p>
<h2>Hosted payment page and test cards</h2>
<p>The session <code>url</code> serves a payment form. Any expiry in the future and any three-digit CVC are accepted.</p>
<ul><li><code>4242 4242 4242 4242</code> succeeds.</li>
<li><code>4000 0000 0000 0002</code> is declined.</li>
<li><code>4000 0000 0000 9995</code> is declined for insufficient funds.</li></ul>
</body></html>
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Pay {name}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem}}
label{{display:block;margin:.6rem 0 .2rem}}input{{width:100%;padding:.4rem}}button{{margin-top:1rem;padding:.5rem 1rem}}
.error{{color:#a00}}</style></head>
<body><h1>Card payment</h1>
<p>{name}: <strong>{amount}</strong></p>
{error}
<form method="post" action="/pay/{id}">
<label for="card_number">Card number</label><input id="card_number" name="card_number" required>
<label for="exp">Expiry (MM/YY)</label><input id="exp" name="exp" value="12/34" required>
<label for="cvc">CVC</label><input id="cvc" name="cvc" value="123" required>
<button type="submit" id="pay">Pay {amount}</button>
</form>
<p><a id="cancel" href="/pay/{id}/cancel">Cancel and return</a></p>
</body></html>
"""


def _money(amount: int, currency: str) -> str:
    return f"{amount / 100:.2f} {currency.upper()}"


class Handler(AdminAwareHandler):
    server_version = "PaymentsMock/1.0"
    service_name = "payments"
    log_env = "PAYMENTS_ADMIN_LOG"

    def _send(
        self, status: int, payload, content_type: str = "application/json", replayed: bool = False
    ) -> None:
        body = (
            payload.encode()
            if isinstance(payload, str)
            else json.dumps(payload, indent=2).encode()
        )
        self.send_response(status)
        self.send_header(
            "Content-Type",
            content_type
            + ("; charset=utf-8" if content_type.startswith("text/") else ""),
        )
        if replayed:
            self.send_header("Idempotent-Replayed", "true")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, url: str) -> None:
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _refuse_public_admin(self) -> None:
        self._send(403, _error("admin_required", "The administrative surface is not served here."))
        self.audit(False, "public")

    def _lookup(self, collection: str, identifier: str):
        obj = STATE[collection].get(identifier)
        if obj is None:
            self._send(
                404,
                _error(
                    "resource_missing", f"No such {collection[:-1]}: '{identifier}'"
                ),
            )
        return obj

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        parts = path.strip("/").split("/")
        with LOCK:
            if path == "/":
                return self._send(200, DOCS, "text/html")
            if path == "/healthz":
                return self._send(200, {"ok": True})
            if len(parts) == 2 and parts[0] == "pay":
                session = STATE["checkout_sessions"].get(parts[1])
                if session is None:
                    return self._send(
                        404, "<h1>No such Checkout Session</h1>", "text/html"
                    )
                return self._send(200, self._page(session, None), "text/html")
            if len(parts) == 3 and parts[0] == "pay" and parts[2] == "cancel":
                session = STATE["checkout_sessions"].get(parts[1])
                if session is None:
                    return self._send(
                        404, "<h1>No such Checkout Session</h1>", "text/html"
                    )
                return self._redirect(
                    session["cancel_url"].replace(
                        "{CHECKOUT_SESSION_ID}", session["id"]
                    )
                )
            if path.startswith("/_mock/") and not self.is_admin_request():
                return self._refuse_public_admin()
            if path == "/_mock/state":
                self._send(
                    200,
                    {**STATE, "deliveries": DELIVERIES, "event_order": ORDER, "hold": HOLD,
                     "held": [event["id"] for event in HELD], "faults": FAULTS, "requests": REQUESTS},
                )
                return self.audit(True, "admin")
            if parts[0] == "v1" and len(parts) >= 2:
                collection = {
                    "checkout": "checkout_sessions",
                    "payment_intents": "payment_intents",
                    "charges": "charges",
                    "refunds": "refunds",
                    "customers": "customers",
                    "subscriptions": "subscriptions",
                    "webhook_endpoints": "webhook_endpoints",
                    "events": "events",
                }.get(parts[1])
                if collection is None:
                    return self._send(
                        404,
                        _error(
                            "unknown_endpoint", f"Unrecognized request URL ({path})."
                        ),
                    )
                offset = 3 if parts[1] == "checkout" else 2
                if len(parts) == offset:
                    items = (
                        [STATE[collection][key] for key in ORDER]
                        if collection == "events"
                        else list(STATE[collection].values())
                    )
                    return self._send(
                        200,
                        {
                            "object": "list",
                            "data": items,
                            "has_more": False,
                            "url": path,
                        },
                    )
                if len(parts) == offset + 1:
                    obj = self._lookup(collection, parts[offset])
                    if obj is not None:
                        self._send(200, obj)
                    return None
            return self._send(
                404, _error("unknown_endpoint", f"Unrecognized request URL ({path}).")
            )

    def do_DELETE(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        parts = path.strip("/").split("/")
        with LOCK:
            if len(parts) == 3 and parts[:2] == ["v1", "webhook_endpoints"]:
                endpoint = STATE["webhook_endpoints"].pop(parts[2], None)
                if endpoint is None:
                    return self._send(
                        404, _error("resource_missing", "No such webhook endpoint.")
                    )
                return self._send(
                    200, {"id": parts[2], "object": "webhook_endpoint", "deleted": True}
                )
            if len(parts) == 3 and parts[:2] == ["v1", "subscriptions"]:
                subscription = self._lookup("subscriptions", parts[2])
                if subscription is None:
                    return None
                subscription["status"] = "canceled"
                subscription["canceled_at"] = _now()
                _event("customer.subscription.deleted", dict(subscription))
                return self._send(200, subscription)
            return self._send(
                404, _error("unknown_endpoint", f"Unrecognized request URL ({path}).")
            )

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        parts = path.strip("/").split("/")
        params = _parse_body(self)
        if path.startswith("/_mock/"):
            if not self.is_admin_request():
                return self._refuse_public_admin()
            with LOCK:
                self._admin_post(path, parts, params)
            return self.audit(True, "admin")
        if path in ("/v1/checkout/sessions", "/v1/refunds"):
            return self._idempotent_create(path, params)
        with LOCK:
            if parts[0] == "pay" and len(parts) == 2:
                return self._pay(parts[1], params)
            if (
                len(parts) == 5
                and parts[:3] == ["v1", "checkout", "sessions"]
                and parts[4] == "expire"
            ):
                session = self._lookup("checkout_sessions", parts[3])
                if session is None:
                    return None
                if session["status"] != "open":
                    return self._send(
                        400,
                        _error(
                            "checkout_session_not_open",
                            "Only open sessions can be expired.",
                        ),
                    )
                session["status"] = "expired"
                _event("checkout.session.expired", dict(session))
                return self._send(200, session)
            if path == "/v1/customers":
                customer = {
                    "id": _new_id("cus"),
                    "object": "customer",
                    "email": params.get("email"),
                    "name": params.get("name"),
                    "created": _now(),
                    "metadata": params.get("metadata") or {},
                }
                STATE["customers"][customer["id"]] = customer
                return self._send(200, customer)
            if path == "/v1/subscriptions":
                if params.get("customer") not in STATE["customers"]:
                    return self._send(
                        400, _error("resource_missing", "No such customer.")
                    )
                subscription = {
                    "id": _new_id("sub"),
                    "object": "subscription",
                    "customer": params["customer"],
                    "status": "active",
                    "created": _now(),
                    "canceled_at": None,
                    "items": params.get("items") or [],
                    "metadata": params.get("metadata") or {},
                }
                STATE["subscriptions"][subscription["id"]] = subscription
                _event("customer.subscription.created", dict(subscription))
                return self._send(200, subscription)
            if path == "/v1/webhook_endpoints":
                url = str(params.get("url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    return self._send(
                        400,
                        _error(
                            "parameter_invalid_empty",
                            "A webhook endpoint needs an http(s) url.",
                        ),
                    )
                for endpoint in STATE["webhook_endpoints"].values():
                    if endpoint["url"] == url:
                        return self._send(200, endpoint)
                endpoint = {
                    "id": _new_id("we"),
                    "object": "webhook_endpoint",
                    "url": url,
                    "status": "enabled",
                    "enabled_events": params.get("enabled_events") or ["*"],
                    "created": _now(),
                }
                STATE["webhook_endpoints"][endpoint["id"]] = endpoint
                return self._send(200, endpoint)
            return self._send(
                404, _error("unknown_endpoint", f"Unrecognized request URL ({path}).")
            )

    def _admin_post(self, path: str, parts: list[str], params: dict) -> None:
        """The administrative operations; the caller holds LOCK and audits the request."""
        if path == "/_mock/reset":
            for collection in STATE.values():
                collection.clear()
            for store in (DELIVERIES, ORDER, HELD, REQUESTS):
                store.clear()
            FAULTS.clear()
            FAULT_DELAYS.clear()
            IDEMPOTENCY.clear()
            HOLD.update({"active": False, "types": None})
            return self._send(200, {"ok": True})
        if len(parts) == 3 and parts[:2] == ["_mock", "redeliver"]:
            event = STATE["events"].get(parts[2])
            if event is None:
                return self._send(404, _error("resource_missing", "No such event."))
            threading.Thread(target=_deliver, args=(event,), daemon=True).start()
            return self._send(200, {"ok": True, "event": event["id"]})
        if path == "/_mock/deliveries/hold":
            types = params.get("types")
            if isinstance(types, str):
                types = [value for value in types.split(",") if value]
            HOLD.update({"active": True, "types": sorted(types) if types else None})
            return self._send(200, {"ok": True, "hold": HOLD})
        if path == "/_mock/deliveries/release":
            wanted = [value for value in str(params.get("ids") or "").split(",") if value]
            if wanted:
                by_id = {event["id"]: event for event in HELD}
                missing = [value for value in wanted if value not in by_id]
                if missing:
                    return self._send(404, _error("resource_missing", f"Not held: {missing}."))
                released = [by_id[value] for value in wanted]
                HELD[:] = [event for event in HELD if event["id"] not in wanted]
            else:
                released = list(HELD)
                HELD.clear()
                HOLD.update({"active": False, "types": None})
            threading.Thread(target=_deliver_in_order, args=(released,), daemon=True).start()
            return self._send(200, {"ok": True, "released": [event["id"] for event in released]})
        if path == "/_mock/faults":
            armed = {}
            for operation, modes in FAULT_MODES.items():
                mode = params.get(operation)
                if mode is None:
                    continue
                if mode not in modes:
                    return self._send(400, _error("parameter_invalid", f"{operation} takes one of {list(modes)}."))
                FAULTS[operation] = mode
                FAULT_DELAYS[operation] = float(params.get(f"{operation}_delay") or FAULT_DELAY)
                armed[operation] = mode
            return self._send(200, {"ok": True, "armed": armed, "faults": FAULTS})
        if len(parts) == 3 and parts[:2] == ["_mock", "force_complete"]:
            session = STATE["checkout_sessions"].get(parts[2])
            if session is None:
                return self._send(404, _error("resource_missing", "No such Checkout Session."))
            card = "".join(ch for ch in str(params.get("card_number") or "4242424242424242") if ch.isdigit())
            ok, message = _complete_session(session, card, force=True)
            return self._send(200 if ok else 400, {"ok": ok, "message": message, "session": session})
        return self._send(404, _error("unknown_endpoint", f"Unrecognized request URL ({path})."))

    def _idempotent_create(self, path: str, params: dict) -> None:
        """POST /v1/checkout/sessions and /v1/refunds: armed faults, then Idempotency-Key replay."""
        key = (self.headers.get("Idempotency-Key") or "").strip() or None
        operation = "checkout_create" if path == "/v1/checkout/sessions" else "refund_create"
        fingerprint = json.dumps(params, sort_keys=True, default=str)
        with LOCK:
            entry = {"path": path, "idempotency_key": key, "at": _now(), "status": None, "fault": None,
                     "payment_intent": params.get("payment_intent"), "amount": params.get("amount")}
            REQUESTS.append(entry)
            if key is not None and key in IDEMPOTENCY:
                stored = IDEMPOTENCY[key]
                if stored["path"] != path or stored["fingerprint"] != fingerprint:
                    entry["status"] = 400
                    return self._send(400, _error("idempotency_key_in_use",
                                                  "This Idempotency-Key was used with different parameters."))
                entry["status"] = stored["status"]
                entry["replayed"] = True
                return self._send(stored["status"], stored["payload"], replayed=True)
            fault = FAULTS.pop(operation, None)
            delay = FAULT_DELAYS.pop(operation, FAULT_DELAY)
            entry["fault"] = fault
            if fault == "error":
                entry["status"] = 500
                return self._send(500, _error("api_error", "The provider failed to process the request.", "api_error"))
            if fault == "timeout":
                status, payload = None, None
            elif path == "/v1/checkout/sessions":
                status, payload = self._create_session(params)
            else:
                status, payload = _refund(params)
            if status is not None and key is not None and status < 500:
                IDEMPOTENCY[key] = {"path": path, "fingerprint": fingerprint, "status": status, "payload": payload}
        if fault == "slow":
            time.sleep(delay)
            entry["status"] = status
            return self._send(status, payload)
        if fault in ("timeout", "lost_response"):
            time.sleep(delay)
            entry["status"] = 503
            return self._send(503, _error("api_connection_error", "The provider did not answer in time.", "api_error"))
        entry["status"] = status
        return self._send(status, payload)

    def _create_session(self, params: dict) -> tuple[int, dict]:
        items = params.get("line_items") or []
        for field in ("success_url", "cancel_url"):
            if not str(params.get(field) or "").startswith(("http://", "https://")):
                return 400, _error("parameter_missing", f"Missing or invalid {field}.")
        if not items or _total(items) <= 0:
            return 400, _error(
                "parameter_missing",
                "line_items with a positive unit_amount are required.",
            )
        session = {
            "id": _new_id("cs_test"),
            "object": "checkout.session",
            "mode": params.get("mode", "payment"),
            "status": "open",
            "payment_status": "unpaid",
            "amount_total": _total(items),
            "currency": _currency(items),
            "customer": params.get("customer"),
            "customer_email": params.get("customer_email"),
            "success_url": params["success_url"],
            "cancel_url": params["cancel_url"],
            "metadata": params.get("metadata") or {},
            "line_items": items,
            "payment_intent": None,
            "created": _now(),
            "url": None,
        }
        session["url"] = f"{PUBLIC_URL}/pay/{session['id']}"
        STATE["checkout_sessions"][session["id"]] = session
        return 200, session

    def _page(self, session: dict, error: str | None) -> str:
        items = session.get("line_items") or []
        name = "your order"
        if items and isinstance(items[0], dict):
            name = (items[0].get("price_data") or {}).get("product_data", {}).get(
                "name"
            ) or name
        return PAGE.format(
            id=session["id"],
            name=html.escape(str(name)),
            amount=_money(session["amount_total"], session["currency"]),
            error=f'<p class="error" id="error">{html.escape(error)}</p>'
            if error
            else "",
        )

    def _pay(self, session_id: str, params: dict) -> None:
        session = STATE["checkout_sessions"].get(session_id)
        if session is None:
            return self._send(404, "<h1>No such Checkout Session</h1>", "text/html")
        card = "".join(
            ch for ch in str(params.get("card_number") or "") if ch.isdigit()
        )
        ok, message = _complete_session(session, card)
        wants_json = str(params.get("redirect", "1")) in ("0", "false", "no")
        if wants_json:
            return self._send(
                200 if ok else 402, {"ok": ok, "message": message, "session": session}
            )
        if not ok:
            return self._send(402, self._page(session, message), "text/html")
        return self._redirect(
            session["success_url"].replace("{CHECKOUT_SESSION_ID}", session["id"])
        )


def main() -> None:
    global SIGNING_KEY  # noqa: PLW0603 - the one process-wide key
    SIGNING_KEY = _load_signing_key()
    admin = start_admin_listener(Handler, service="payments", prefix="PAYMENTS")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    sys.stderr.write(
        f"payments mock listening on {PORT}, {admin}, "
        f"public url {PUBLIC_URL}, signing key at {SIGNING_FILE}\n"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
