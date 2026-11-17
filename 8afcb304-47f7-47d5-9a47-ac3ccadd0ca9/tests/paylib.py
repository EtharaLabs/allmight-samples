"""Verifier-side client for the payments mock (task-owned; kitlib has none).

Administrative surface (/_mock/...): in the verifier it is a Unix socket, $ADMIN_SOCKET_DIR/payments.sock
(test.sh exports ADMIN_SOCKET_DIR; root may connect). Locally it is a loopback port given by
FK_PAYMENTS_ADMIN_URL. The public API is PAYMENTS_API_URL. Webhook signatures use the key in
PAYMENTS_WEBHOOK_SIGNING_FILE, so the verifier can sign (or mis-sign) deliveries of its own.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import socket
import time
import urllib.parse


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 30.0):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def _request(conn: http.client.HTTPConnection, method: str, path: str, body: bytes | None = None,
             headers: dict | None = None) -> tuple[int, bytes, dict]:
    h = {"Connection": "close"}
    h.update(headers or {})
    try:
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read()
        return r.status, data, {k.lower(): v for k, v in r.getheaders()}
    finally:
        conn.close()


class Payments:
    def __init__(self):
        e = os.environ
        self.api = e.get("PAYMENTS_API_URL", "").rstrip("/")
        self.key_file = e.get("PAYMENTS_WEBHOOK_SIGNING_FILE", "")
        sock_dir = e.get("ADMIN_SOCKET_DIR", "")
        self.admin_socket = os.path.join(sock_dir, "payments.sock") if sock_dir else ""
        self.admin_url = e.get("FK_PAYMENTS_ADMIN_URL", "").rstrip("/")

    # -- transport ---------------------------------------------------------------------------------
    def _public(self, method: str, path: str, fields: dict | None = None, timeout: float = 30.0):
        u = urllib.parse.urlsplit(self.api)
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        body = urllib.parse.urlencode(fields).encode() if fields is not None else None
        hdr = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
        return _request(conn, method, path, body, hdr)

    def _admin(self, method: str, path: str, fields: dict | None = None):
        if self.admin_socket and os.path.exists(self.admin_socket):
            conn = _UnixHTTPConnection(self.admin_socket)
        elif self.admin_url:
            u = urllib.parse.urlsplit(self.admin_url)
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=30)
        else:
            raise RuntimeError("no payments admin surface configured")
        body = urllib.parse.urlencode(fields).encode() if fields is not None else b""
        status, data, _ = _request(conn, method, path, body if method == "POST" else None,
                                   {"Content-Type": "application/x-www-form-urlencoded"} if method == "POST" else {})
        return status, json.loads(data.decode() or "{}")

    # -- admin ---------------------------------------------------------------------------------------
    def state(self) -> dict:
        return self._admin("GET", "/_mock/state")[1]

    def hold(self, types: list[str] | None = None) -> None:
        self._admin("POST", "/_mock/deliveries/hold", {"types": ",".join(types)} if types else {})

    def release(self, ids: list[str] | None = None) -> dict:
        return self._admin("POST", "/_mock/deliveries/release", {"ids": ",".join(ids)} if ids else {})[1]

    def fault(self, **kw) -> None:
        self._admin("POST", "/_mock/faults", {k: str(v) for k, v in kw.items()})

    def force_complete(self, session_id: str, card: str = "4242424242424242") -> dict:
        return self._admin("POST", f"/_mock/force_complete/{session_id}", {"card_number": card})[1]

    def redeliver(self, event_id: str) -> None:
        self._admin("POST", f"/_mock/redeliver/{event_id}", {})

    # -- public -------------------------------------------------------------------------------------
    def session(self, sid: str) -> dict:
        status, data, _ = self._public("GET", f"/v1/checkout/sessions/{sid}")
        return json.loads(data.decode()) if status == 200 else {}

    def pay(self, sid: str, card: str = "4242424242424242") -> tuple[int, dict]:
        status, data, _ = self._public("POST", f"/pay/{sid}", {"card_number": card, "exp": "12/34", "cvc": "123", "redirect": "0"})
        try:
            return status, json.loads(data.decode())
        except ValueError:
            return status, {}

    def events_for(self, appointment_id: int, etype: str | None = None, st: dict | None = None) -> list[dict]:
        st = st or self.state()
        out = []
        for eid in st.get("event_order", []):
            ev = st["events"][eid]
            md = (ev.get("data", {}).get("object", {}).get("metadata") or {})
            if str(md.get("appointment_id")) == str(appointment_id) and (etype is None or ev["type"] == etype):
                out.append(ev)
        return out

    def refunds_for(self, payment_intent: str, st: dict | None = None) -> list[dict]:
        st = st or self.state()
        return [r for r in st.get("refunds", {}).values() if r.get("payment_intent") == payment_intent]

    def refund_requests(self, payment_intent: str, st: dict | None = None) -> list[dict]:
        st = st or self.state()
        return [r for r in st.get("requests", []) if r.get("path") == "/v1/refunds" and r.get("payment_intent") == payment_intent]

    # -- signing ------------------------------------------------------------------------------------
    def signing_key(self) -> str:
        with open(self.key_file, encoding="utf-8") as fh:
            return fh.read().strip()

    def sign(self, body: bytes, t: int | None = None, key: str | None = None) -> str:
        t = int(time.time()) if t is None else t
        k = self.signing_key() if key is None else key
        return f"t={t},v1=" + hmac.new(k.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()


def event_body(event: dict) -> bytes:
    return json.dumps(event, separators=(",", ":"), sort_keys=True).encode()
