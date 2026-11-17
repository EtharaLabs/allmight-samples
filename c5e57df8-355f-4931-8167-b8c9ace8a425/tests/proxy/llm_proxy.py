#!/usr/bin/env python3
"""Verifier credential proxy for the model API (contract RL3, RL6, C7).

The proxy is the only process that holds the model-API key. tests/test.sh starts it as the
services user (uid 1001) and writes two lines to its standard input: the real key, then a per-run
token. Neither ever appears in an environment, an argument, a file, or a log.

It forwards only ``POST /v1/messages`` and ``POST /v1/messages/count_tokens`` to the upstream
(https://api.anthropic.com unless KH_PROXY_UPSTREAM names a test double), replacing the caller's
credential with the real key, through two listeners:

* a Unix socket (KH_PROXY_SOCKET, file mode 0600 in a 0700 directory) for the browser-experience
  executor: a connection is accepted only when SO_PEERCRED names uid 0 or the proxy's own uid;
* a loopback TCP port (KH_PROXY_PORT) for Reward Kit, whose model client cannot speak to a Unix
  socket: a connection is accepted only when the kernel's socket table names the connecting
  socket's owner as uid 0, and a request is forwarded only when it carries the per-run token as
  its x-api-key or Bearer credential.

Each listener has its own call budget (KH_PROXY_BUDGET_EXECUTOR, KH_PROXY_BUDGET_JUDGE). A request
beyond its budget, an upstream answer outside 2xx, and an upstream that does not answer within
KH_PROXY_DEADLINE seconds are all answered 502 or 503, never 408 or 504, so a model client sees
an error it does not score rather than a timeout it might. An upstream error answer keeps that
mapping, and the proxy's error body and log line carry the upstream's status, error type, error
message, and request id, so the failure is diagnosable. GET /healthz on either listener answers
with the age of the oldest request still in flight, which tests/proxy/supervise.py watches.

Before a judge-listener request is forwarded, tests/proxy/image_limits.py fits its images to the
model API's documented limits by lossless tiling; a request already within them is forwarded as
the same bytes. A request that cannot be fitted is answered 502 and never forwarded or counted
against the budget. Executor requests are never tiled: their screenshots answer computer-use
actions whose coordinates refer to the whole image.

One JSON line per request goes to KH_PROXY_LOG: time, listener, peer uid, method, path, status,
request and response byte counts, duration, the upstream request id, the upstream error type and
message when the upstream refused, and the tiling record (image sizes, sha256 digests, and tile
bounds) when images were tiled. No body, header, key, or token is logged.
"""

from __future__ import annotations

import hmac
import json
import os
import socket
import socketserver
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FORWARDED_PATHS = ("/v1/messages", "/v1/messages/count_tokens")
FORWARDED_HEADERS = ("content-type", "anthropic-version", "anthropic-beta")
ERROR_BODY_LIMIT = 1_000_000  # bytes of an upstream error answer the proxy reads
ERROR_MESSAGE_LIMIT = 600  # characters of an upstream error message the proxy repeats


class State:
    """Process-wide proxy state; filled by main() so that importing this module does nothing."""

    key = ""
    token = ""
    upstream = "https://api.anthropic.com"
    deadline = 240.0
    log_path: str | None = None
    budgets: dict[str, int] = {}
    used: dict[str, int] = {}
    inflight: dict[int, float] = {}
    lock = threading.Lock()
    serial = 0
    # Bound by main() from tests/proxy/image_limits.py. Reward Kit imports every Python file under
    # tests/ as a module, so this file imports nothing of its own beyond the standard library.
    normalize = None
    unrepresentable: type[Exception] = Exception


def _log(entry: dict) -> None:
    if not State.log_path:
        return
    line = json.dumps(
        {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry},
        sort_keys=True,
    )
    with State.lock:
        with open(State.log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _tcp_peer_uid(client: tuple[str, int], server_port: int) -> int | None:
    """Owner uid of the loopback socket at ``client`` connected to ``server_port``, from /proc/net/tcp."""
    host, port = client
    if host != "127.0.0.1":
        return None
    want_local = f"0100007F:{port:04X}"
    want_remote = f"0100007F:{server_port:04X}"
    try:
        with open("/proc/net/tcp", encoding="ascii") as handle:
            next(handle)
            for line in handle:
                fields = line.split()
                if fields[1] == want_local and fields[2] == want_remote:
                    return int(fields[7])
    except OSError:
        return None
    return None


def _fold_sse(response) -> tuple[dict | None, dict | None]:
    """Fold an Anthropic Messages event stream into the Message a non-streaming call returns."""
    message: dict | None = None
    blocks: dict[int, dict] = {}
    partial: dict[int, str] = {}
    event, data_lines = None, []
    stopped = False

    def handle(ev: str | None, payload: str):
        nonlocal message
        if not payload:
            return None
        data = json.loads(payload)
        kind = data.get("type") or ev
        if kind == "message_start":
            message = dict(data.get("message") or {})
            message["content"] = []
        elif kind == "content_block_start":
            blocks[data["index"]] = dict(data.get("content_block") or {})
        elif kind == "content_block_delta":
            block = blocks.setdefault(data["index"], {})
            delta = data.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif dtype == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get("signature", "")
            elif dtype == "input_json_delta":
                partial[data["index"]] = partial.get(data["index"], "") + delta.get("partial_json", "")
            elif dtype == "citations_delta":
                block.setdefault("citations", []).append(delta.get("citation"))
        elif kind == "content_block_stop":
            idx = data["index"]
            if idx in partial:
                blocks[idx]["input"] = json.loads(partial.pop(idx) or "{}")
        elif kind == "message_delta" and message is not None:
            for key, value in (data.get("delta") or {}).items():
                message[key] = value
            message.setdefault("usage", {}).update(data.get("usage") or {})
        elif kind == "message_stop":
            nonlocal stopped
            stopped = True
        elif kind == "error":
            return data.get("error") or {"type": "api_error", "message": "stream error"}
        return None

    for raw in response:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            err = handle(event, "\n".join(data_lines))
            if err is not None:
                return None, err
            event, data_lines = None, []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        err = handle(event, "\n".join(data_lines))
        if err is not None:
            return None, err
    if message is None or not stopped:
        return None, {"type": "truncated_stream", "message": "the upstream stream ended before message_stop"}
    message["content"] = [blocks[i] for i in sorted(blocks)]
    if not any(b.get("type") in ("text", "tool_use") for b in message["content"]):
        return None, {"type": "truncated_stream", "message": "the upstream answer carried no text or tool block"}
    return message, None


class UnixListener(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    listener = "executor"

    def __init__(self, path: str, handler):
        if os.path.exists(path):
            os.unlink(path)
        old = os.umask(0o177)
        try:
            super().__init__(path, handler)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        self.allowed = frozenset({0, os.getuid()})

    def verify_request(self, request, client_address) -> bool:  # noqa: ARG002
        try:
            raw = request.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _pid, uid, _gid = struct.unpack("3i", raw)
        except OSError:
            uid = -1
        if uid not in self.allowed:
            _log(
                {
                    "listener": self.listener,
                    "peer_uid": uid,
                    "method": None,
                    "path": None,
                    "status": "refused-peer",
                }
            )
            return False
        return True


class TCPListener(ThreadingHTTPServer):
    daemon_threads = True
    listener = "judge"

    def verify_request(self, request, client_address) -> bool:
        uid = _tcp_peer_uid(client_address, self.server_address[1])
        if uid != 0:
            _log(
                {
                    "listener": self.listener,
                    "peer_uid": uid,
                    "method": None,
                    "path": None,
                    "status": "refused-peer",
                }
            )
            return False
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "ForgeKitLLMProxy/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def address_string(self) -> str:
        return (
            str(self.client_address[0])
            if isinstance(self.client_address, tuple)
            else "unix"
        )

    def _reply(
        self, status: int, payload: bytes, content_type: str = "application/json"
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: int, kind: str, message: str) -> None:
        body = json.dumps(
            {"type": "error", "error": {"type": kind, "message": message}}
        ).encode()
        self._reply(status, body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/healthz":
            now = time.monotonic()
            with State.lock:
                oldest = max(
                    (now - started for started in State.inflight.values()), default=0.0
                )
                body = {
                    "ok": True,
                    "inflight": len(State.inflight),
                    "oldest_inflight_s": round(oldest, 1),
                    "deadline_s": State.deadline,
                    "used": dict(State.used),
                    "budgets": dict(State.budgets),
                }
            return self._reply(200, json.dumps(body).encode())
        return self._error(404, "not_found_error", "unknown path")

    def _authorized(self) -> bool:
        if self.server.listener == "executor":
            return True
        offered = (self.headers.get("x-api-key") or "").strip()
        bearer = (self.headers.get("Authorization") or "").strip()
        if not offered and bearer.lower().startswith("bearer "):
            offered = bearer[7:].strip()
        return bool(State.token) and hmac.compare_digest(
            offered.encode(), State.token.encode()
        )

    def do_POST(self) -> None:  # noqa: N802
        listener = self.server.listener
        started = time.monotonic()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = self.path.split("?", 1)[0].rstrip("/")
        entry = {
            "listener": listener,
            "peer_uid": None,
            "method": "POST",
            "path": path,
            "request_bytes": len(body),
            "response_bytes": 0,
            "status": None,
        }
        try:
            if path not in FORWARDED_PATHS:
                entry["status"] = 502
                return self._error(
                    502, "api_error", "the proxy forwards only the messages API"
                )
            if not self._authorized():
                entry["status"] = 502
                return self._error(
                    502, "authentication_error", "the proxy refused this caller"
                )
            if listener == "judge":
                try:
                    body, tiling = State.normalize(body)
                except State.unrepresentable as exc:
                    entry["status"] = 502
                    entry["images"] = {"unrepresentable": str(exc)}
                    return self._error(
                        502,
                        "api_error",
                        "the proxy cannot fit this request within the model API's image "
                        f"limits: {exc}",
                    )
                if tiling is not None:
                    entry["images"] = tiling
                    entry["forwarded_bytes"] = len(body)
            with State.lock:
                if State.used.get(listener, 0) >= State.budgets.get(listener, 0):
                    entry["status"] = 503
                    return self._error(
                        503, "overloaded_error", f"the {listener} call budget is spent"
                    )
                State.used[listener] = State.used.get(listener, 0) + 1
                State.serial += 1
                ticket = State.serial
                State.inflight[ticket] = started
            try:
                status, payload, content_type, upstream = self._forward(path, body)
            finally:
                with State.lock:
                    State.inflight.pop(ticket, None)
            entry["status"] = status
            entry.update(upstream)
            entry["response_bytes"] = len(payload)
            return self._reply(status, payload, content_type)
        finally:
            entry["duration_s"] = round(time.monotonic() - started, 3)
            _log(entry)

    def _forward(self, path: str, body: bytes) -> tuple[int, bytes, str, dict]:
        headers = {
            name: self.headers[name]
            for name in FORWARDED_HEADERS
            if self.headers.get(name)
        }
        headers.setdefault("anthropic-version", "2023-06-01")
        headers["x-api-key"] = State.key
        request = urllib.request.Request(
            State.upstream.rstrip("/") + path, data=body, method="POST", headers=headers
        )
        result: dict = {}

        # A non-streaming Messages call is sent upstream as a stream and folded back
        # into one Message, so bytes flow for the whole call. Egress proxies that cut connections
        # idle past a fixed limit (Harbor's allowlist sidecar cut silent calls at 15 s) then never
        # break a long max-effort judge call. The caller still receives one JSON Message.
        fold = False
        if path == "/v1/messages":
            try:
                parsed = json.loads(body.decode("utf-8"))
                if isinstance(parsed, dict) and not parsed.get("stream"):
                    parsed["stream"] = True
                    request = urllib.request.Request(
                        State.upstream.rstrip("/") + path, data=json.dumps(parsed).encode(), method="POST",
                        headers={**headers, "accept": "text/event-stream"})
                    fold = True
            except ValueError:
                pass

        def call_once() -> None:
            try:
                with urllib.request.urlopen(
                    request, timeout=State.deadline
                ) as response:
                    result["status"] = response.status
                    result["request_id"] = response.headers.get("request-id")
                    ctype = response.headers.get("Content-Type", "application/json")
                    if fold and "text/event-stream" in ctype:
                        message, error = _fold_sse(response)
                        if error is not None and error.get("type") in ("truncated_stream", "overloaded_error", "api_error"):
                            # transient: no complete answer arrived; let call() retry the whole request
                            result.clear()
                            result["error"] = "Stream:" + str(error.get("type"))
                            return
                        if error is not None:
                            result["status"] = 500
                            result["body"] = json.dumps({"type": "error", "error": error}).encode()
                        else:
                            result["body"] = json.dumps(message).encode()
                        result["type"] = "application/json"
                    else:
                        result["body"] = response.read()
                        result["type"] = ctype
            except urllib.error.HTTPError as exc:
                result["status"] = exc.code
                result["request_id"] = exc.headers.get("request-id") if exc.headers else None
                result["retry_after"] = exc.headers.get("retry-after") if exc.headers else None
                try:
                    result["body"] = exc.read(ERROR_BODY_LIMIT)
                except Exception:  # noqa: BLE001 - an unreadable error body leaves only the status
                    result["body"] = b""
            except Exception as exc:  # noqa: BLE001 - every transport failure becomes a 502 below
                result["error"] = type(exc).__name__

        def call() -> None:
            # Retry transport-level failures (connection reset or dropped before any HTTP answer) and
            # overloaded, rate-limited or server-error answers (429, 5xx, 529) while the deadline allows, so one
            # upstream hiccup does not void a whole evaluation. Rate limits honour Retry-After (else back off
            # 15 s, 30 s, ...); other failures back off 2 s, 4 s, ...
            started_call = time.monotonic()
            for attempt in range(6):
                result.clear()
                call_once()
                status = result.get("status")
                if "error" not in result and status not in (429, 500, 502, 503, 504, 529):
                    return
                if status in (429, 529):
                    try:
                        wait = float(result.get("retry_after") or 0)
                    except ValueError:
                        wait = 0.0
                    wait = max(wait, 15.0 * (attempt + 1))
                else:
                    wait = 2.0 * (attempt + 1)
                if attempt == 5 or time.monotonic() - started_call + wait > State.deadline - 60:
                    return
                time.sleep(wait)

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        worker.join(State.deadline)
        if worker.is_alive():
            return (
                503,
                _error_body(
                    "overloaded_error",
                    "the upstream did not answer before the proxy deadline",
                ),
                "application/json",
                {},
            )
        if "error" in result:
            return (
                502,
                _error_body(
                    "api_error",
                    f"the upstream could not be reached ({result['error']})",
                ),
                "application/json",
                {},
            )
        status = result["status"]
        if 200 <= status < 300:
            return status, result["body"], result["type"], _drop_empty(
                {"upstream_request_id": result.get("request_id")}
            )
        upstream = _upstream_error(status, result.get("body", b""), result.get("request_id"))
        kind = "overloaded_error" if status in (408, 429) or status >= 500 else "api_error"
        summary = f"the upstream answered {status}"
        if upstream.get("upstream_error_type"):
            summary += f" {upstream['upstream_error_type']}"
        if upstream.get("upstream_error_message"):
            summary += f": {upstream['upstream_error_message']}"
        if upstream.get("upstream_request_id"):
            summary += f" (request id {upstream['upstream_request_id']})"
        return (
            503 if kind == "overloaded_error" else 502,
            _error_body(kind, summary, upstream),
            "application/json",
            upstream,
        )


def _redact(text: str) -> str:
    for secret in (State.key, State.token):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _drop_empty(fields: dict) -> dict:
    return {key: value for key, value in fields.items() if value not in (None, "")}


def _upstream_error(status: int, raw: bytes, header_request_id: str | None) -> dict:
    """The upstream's status, error type, error message, and request id; never its headers or body."""
    kind = message = request_id = None
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        document = None
    if isinstance(document, dict):
        error = document.get("error") if isinstance(document.get("error"), dict) else {}
        kind = error.get("type") if isinstance(error.get("type"), str) else None
        message = error.get("message") if isinstance(error.get("message"), str) else None
        if isinstance(document.get("request_id"), str):
            request_id = document["request_id"]
    if message is not None:
        message = _redact(message)[:ERROR_MESSAGE_LIMIT]
    return _drop_empty(
        {
            "upstream_status": status,
            "upstream_error_type": kind,
            "upstream_error_message": message,
            "upstream_request_id": request_id or header_request_id,
        }
    )


def _error_body(kind: str, message: str, upstream: dict | None = None) -> bytes:
    document: dict = {"type": "error", "error": {"type": kind, "message": message}}
    if upstream:
        document["upstream"] = {
            key.removeprefix("upstream_"): value for key, value in upstream.items()
        }
    return json.dumps(document).encode()


def main() -> None:
    lines = sys.stdin.read().splitlines()
    if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
        sys.stderr.write(
            "llm proxy: expected the key and the token on standard input\n"
        )
        raise SystemExit(2)
    State.key, State.token = lines[0].strip(), lines[1].strip()
    from image_limits import Unrepresentable, normalize  # beside this file, on sys.path[0]

    State.normalize = staticmethod(normalize)
    State.unrepresentable = Unrepresentable
    State.upstream = os.environ.get("KH_PROXY_UPSTREAM") or State.upstream
    State.deadline = float(os.environ.get("KH_PROXY_DEADLINE", str(State.deadline)))
    State.log_path = os.environ.get("KH_PROXY_LOG") or None
    State.budgets = {
        "executor": int(os.environ.get("KH_PROXY_BUDGET_EXECUTOR", "280")),
        "judge": int(os.environ.get("KH_PROXY_BUDGET_JUDGE", "189")),
    }
    State.used = {"executor": 0, "judge": 0}
    if State.log_path:
        fd = os.open(State.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
    unix = UnixListener(os.environ["KH_PROXY_SOCKET"], Handler)
    tcp = TCPListener(("127.0.0.1", int(os.environ["KH_PROXY_PORT"])), Handler)
    threading.Thread(target=unix.serve_forever, daemon=True).start()
    sys.stderr.write(
        f"llm proxy: executor socket {os.environ['KH_PROXY_SOCKET']}, judge port {tcp.server_address[1]}, "
        f"deadline {State.deadline}s, budgets {State.budgets}\n"
    )
    tcp.serve_forever()


if __name__ == "__main__":
    main()
