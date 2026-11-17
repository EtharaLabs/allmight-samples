"""Verifier-side client for the payments provider mock (task-owned; kitlib has none).

Administrative surface (/_mock/...): in the verifier it is the Unix socket $ADMIN_SOCKET_DIR/payments.sock
(test.sh exports ADMIN_SOCKET_DIR; root may connect). Locally it is a loopback port named by
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
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 30.0):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def _request(conn, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    h = {"Connection": "close"}
    h.update(headers or {})
    try:
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        return r.status, r.read(), {k.lower(): v for k, v in r.getheaders()}
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
        body = urllib.parse.urlencode(fields or {}).encode() if method == "POST" else None
        hdr = {"Content-Type": "application/x-www-form-urlencoded"} if method == "POST" else {}
        status, data, _ = _request(conn, method, path, body, hdr)
        try:
            return status, json.loads(data.decode() or "{}")
        except ValueError:
            return status, {}

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

    def pay_redirect(self, sid: str, card: str = "4242424242424242") -> tuple[int, str | None]:
        status, _data, hdrs = self._public("POST", f"/pay/{sid}", {"card_number": card, "exp": "12/34", "cvc": "123"})
        return status, hdrs.get("location")

    # -- queries over the admin state ----------------------------------------------------------------
    def events(self, st: dict | None = None, etype: str | None = None) -> list[dict]:
        st = st or self.state()
        out = []
        for eid in st.get("event_order", []):
            ev = st["events"].get(eid)
            if ev and (etype is None or ev["type"] == etype):
                out.append(ev)
        return out

    def events_for_session(self, sid: str, etype: str | None = None, st: dict | None = None) -> list[dict]:
        return [e for e in self.events(st, etype) if e["data"]["object"].get("id") == sid]

    def intent_of(self, sid: str, st: dict | None = None) -> str | None:
        st = st or self.state()
        s = st.get("checkout_sessions", {}).get(sid) or {}
        return s.get("payment_intent")

    def refunds_for_intent(self, pi: str | None, st: dict | None = None) -> list[dict]:
        if not pi:
            return []
        st = st or self.state()
        return [r for r in st.get("refunds", {}).values() if r.get("payment_intent") == pi]

    def refund_requests(self, st: dict | None = None) -> list[dict]:
        st = st or self.state()
        return [r for r in st.get("requests", []) if r.get("path") == "/v1/refunds"]

    def wait(self, pred, timeout: float = 30.0, step: float = 0.5):
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            last = self.state()
            v = pred(last)
            if v:
                return v
            time.sleep(step)
        return None

    # -- signing ----------------------------------------------------------------------------------------
    def signing_key(self) -> str:
        return Path(self.key_file).read_text().strip()

    def sign(self, body: bytes, t: int | None = None, key: str | None = None) -> str:
        t = int(time.time()) if t is None else t
        k = (key if key is not None else self.signing_key()).encode()
        return f"t={t},v1=" + hmac.new(k, f"{t}.".encode() + body, hashlib.sha256).hexdigest()
