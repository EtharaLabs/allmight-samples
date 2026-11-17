#!/usr/bin/env python3
"""Outbound mail sink: an SMTP server that keeps every message and serves it over HTTP.

SMTP on MAILSINK_SMTP_PORT (1025) accepts any sender and recipient without authentication or
TLS. HTTP on MAILSINK_HTTP_PORT (8025) lists what arrived: GET /api/v1/messages (newest first),
GET /api/v1/message/<id> (full text and HTML bodies), DELETE /api/v1/messages (clear), GET /
(this documentation), and GET /healthz. Clearing the store is administrative: it is served only on
the listener adminsock selects (MAILSINK_ADMIN_SOCKET or MAILSINK_ADMIN_PORT), never on the public port.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from adminsock import AdminAwareHandler, start_admin_listener  # noqa: E402

SMTP_PORT = int(os.environ.get("MAILSINK_SMTP_PORT", "1025"))
HTTP_PORT = int(os.environ.get("MAILSINK_HTTP_PORT", "8025"))
LOCK = threading.Lock()
MESSAGES: list[dict] = []


def _address(value) -> list[dict]:
    out = []
    for item in value or []:
        out.append({"Name": item.display_name or "", "Address": item.addr_spec})
    return out


def _store(sender: str, recipients: list[str], raw: bytes) -> None:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    text = html = ""
    if message.is_multipart():
        for part in message.walk():
            kind = part.get_content_type()
            if kind == "text/plain" and not text:
                text = part.get_content()
            elif kind == "text/html" and not html:
                html = part.get_content()
    else:
        content = (
            message.get_content() if message.get_content_maintype() == "text" else ""
        )
        if message.get_content_type() == "text/html":
            html = content
        else:
            text = content
    snippet = " ".join((text or html).split())[:200]
    record = {
        "ID": secrets.token_hex(8),
        "Created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "From": (
            _address([message["from"].addresses[0]])[0]
            if message["from"] and message["from"].addresses
            else {"Name": "", "Address": sender}
        ),
        "To": _address(message["to"].addresses if message["to"] else [])
        or [{"Name": "", "Address": r} for r in recipients],
        "Cc": _address(message["cc"].addresses if message["cc"] else []),
        "Subject": str(message["subject"] or ""),
        "Date": str(message["date"] or ""),
        "MessageID": str(message["message-id"] or ""),
        "Envelope": {"From": sender, "To": recipients},
        "Snippet": snippet,
        "Text": text,
        "HTML": html,
        "Size": len(raw),
    }
    with LOCK:
        MESSAGES.append(record)


class SMTPSession:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.sender = ""
        self.recipients: list[str] = []

    async def reply(self, line: str) -> None:
        self.writer.write((line + "\r\n").encode())
        await self.writer.drain()

    def reset(self) -> None:
        self.sender = ""
        self.recipients = []

    async def run(self) -> None:
        await self.reply("220 mailsink ESMTP")
        try:
            while True:
                raw = await self.reader.readline()
                if not raw:
                    return
                line = raw.decode(errors="replace").rstrip("\r\n")
                verb, _, argument = line.partition(" ")
                verb = verb.upper()
                if verb in ("EHLO", "HELO"):
                    self.reset()
                    await self.reply(
                        f"250-mailsink greets {argument.strip() or 'you'}\r\n250-8BITMIME\r\n250-SIZE 10485760\r\n250 OK"
                    )
                elif verb == "MAIL":
                    self.sender = _bracketed(argument)
                    self.recipients = []
                    await self.reply("250 OK")
                elif verb == "RCPT":
                    if not self.sender and self.sender != "":
                        await self.reply("503 MAIL first")
                        continue
                    self.recipients.append(_bracketed(argument))
                    await self.reply("250 OK")
                elif verb == "DATA":
                    if not self.recipients:
                        await self.reply("503 RCPT first")
                        continue
                    await self.reply("354 End data with <CR><LF>.<CR><LF>")
                    body = bytearray()
                    while True:
                        chunk = await self.reader.readline()
                        if not chunk:
                            return
                        if chunk in (b".\r\n", b".\n"):
                            break
                        if chunk.startswith(b".."):
                            chunk = chunk[1:]
                        body += chunk
                    _store(self.sender, list(self.recipients), bytes(body))
                    self.reset()
                    await self.reply("250 OK queued")
                elif verb == "RSET":
                    self.reset()
                    await self.reply("250 OK")
                elif verb == "NOOP":
                    await self.reply("250 OK")
                elif verb == "QUIT":
                    await self.reply("221 Bye")
                    return
                elif verb in ("STARTTLS", "AUTH"):
                    await self.reply("502 Command not implemented")
                else:
                    await self.reply("500 Unrecognized command")
        finally:
            self.writer.close()


def _bracketed(argument: str) -> str:
    value = argument.split(":", 1)[1] if ":" in argument else argument
    value = value.strip().split(" ")[0]
    return value.strip("<>")


async def _serve_smtp() -> None:
    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await SMTPSession(reader, writer).run()

    server = await asyncio.start_server(handle, "0.0.0.0", SMTP_PORT)
    async with server:
        await server.serve_forever()


DOCS = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Mail sink</title></head>
<body style="font-family:system-ui,sans-serif;max-width:44rem;margin:2rem auto"><h1>Mail sink</h1>
<p>Every message delivered to the SMTP port is kept here and never forwarded anywhere.</p>
<ul><li><code>GET /api/v1/messages</code> newest first: ID, From, To, Subject, Created, Snippet</li>
<li><code>GET /api/v1/message/{ID}</code> with Text and HTML bodies</li>
<li><code>DELETE /api/v1/messages</code> clears the store; it is administrative and refused here</li></ul></body></html>
"""


class Handler(AdminAwareHandler):
    service_name = "mailsink"
    log_env = "MAILSINK_ADMIN_LOG"

    def _send(
        self, status: int, payload, content_type: str = "application/json"
    ) -> None:
        body = (
            payload.encode()
            if isinstance(payload, str)
            else json.dumps(payload, indent=2).encode()
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/":
            return self._send(200, DOCS, "text/html; charset=utf-8")
        if path == "/healthz":
            return self._send(200, {"ok": True})
        if path == "/api/v1/messages":
            with LOCK:
                summaries = [
                    {
                        key: value
                        for key, value in m.items()
                        if key not in ("Text", "HTML")
                    }
                    for m in reversed(MESSAGES)
                ]
            return self._send(
                200,
                {
                    "total": len(summaries),
                    "count": len(summaries),
                    "messages": summaries,
                },
            )
        if path.startswith("/api/v1/message/"):
            wanted = path.rsplit("/", 1)[1]
            with LOCK:
                found = next((m for m in MESSAGES if m["ID"] == wanted), None)
            if found is None:
                return self._send(404, {"error": "no such message"})
            return self._send(200, found)
        return self._send(404, {"error": "unknown path"})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if path == "/api/v1/messages":
            if not self.is_admin_request():
                self._send(403, {"error": "the administrative surface is not served here"})
                return self.audit(False, "public")
            with LOCK:
                MESSAGES.clear()
            self._send(200, {"ok": True})
            return self.audit(True, "admin")
        return self._send(404, {"error": "unknown path"})


def main() -> None:
    threading.Thread(target=lambda: asyncio.run(_serve_smtp()), daemon=True).start()
    admin = start_admin_listener(Handler, service="mailsink", prefix="MAILSINK")
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    server.daemon_threads = True
    sys.stderr.write(f"mail sink: smtp on {SMTP_PORT}, http on {HTTP_PORT}, {admin}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
