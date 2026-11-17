"""Email: an outbox (unique dedupe keys, sent once, survives restarts) and direct sending for messages
that carry account links, so no link token is ever stored. Recipients come from the application's
records and are passed explicitly; typed names go through email.headerregistry.Address."""

from __future__ import annotations

import re
import secrets
import smtplib
import threading
import time
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .util import base_url, env, env_int, log_exc, offer_token, qr_token, sha

KICK = threading.Event()


def _clean_name(name: str) -> str:
    return "".join(ch for ch in (name or "") if ord(ch) >= 0x20 and ord(ch) != 0x7F)


def build(to_addr: str, to_name: str, subject: str, body: str, key: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = env("MAIL_FROM")
    local, _, domain = to_addr.partition("@")
    name = _clean_name(to_name)
    try:
        msg["To"] = Address(display_name=name, username=local, domain=domain)
    except Exception:
        msg["To"] = Address(username=local, domain=domain)
    msg["Subject"] = _clean_name(subject)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(idstring=sha(key)[:16], domain="seatwise.local")
    msg.set_content(body)
    return msg


def _smtp():
    return smtplib.SMTP(env("SMTP_HOST"), env_int("SMTP_PORT", 25), timeout=15)


def send_direct(to_addr: str, to_name: str, subject: str, body: str) -> bool:
    """Send now without storing the message (used for verification, reset and invitation links)."""
    for attempt in range(3):
        try:
            with _smtp() as smtp:
                smtp.send_message(build(to_addr, to_name, subject, body, secrets.token_hex(8)),
                                  from_addr=env("MAIL_FROM"), to_addrs=[to_addr])
            return True
        except Exception:
            log_exc("mail")
            time.sleep(0.5 * (attempt + 1))
    return False


def enqueue(conn, key: str, to_addr: str, to_name: str, subject: str, body: str, marker: str | None = None) -> None:
    conn.execute(
        "INSERT INTO sw_outbox (dedupe_key, to_addr, to_name, subject, body, marker) VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (dedupe_key) DO NOTHING", (key, to_addr, to_name, subject, body, marker))


def new_marker() -> str:
    return secrets.token_hex(6)


def qr_ph(marker: str, ticket_id: int, version: int) -> str:
    """Placeholder rendered into the ticket's QR token at send time (tokens are never stored)."""
    return f"[[{marker}:qr:{ticket_id}:{version}]]"


def offer_ph(marker: str, offer_id: int) -> str:
    return f"[[{marker}:offer:{offer_id}]]"


def render(conn, body: str, marker: str | None) -> str:
    if not marker:
        return body
    pat = re.compile(r"\[\[" + re.escape(marker) + r":(qr|offer):(\d+)(?::(\d+))?\]\]")
    barcodes: dict[int, str | None] = {}

    def sub(m):
        if m[1] == "offer":
            return f"{base_url()}/waitlist/offers/{offer_token(int(m[2]))}"
        tid, ver = int(m[2]), int(m[3] or 0)
        if ver == 0:
            if tid not in barcodes:
                row = conn.execute("SELECT barcode FROM sw_tickets WHERE id = %s", (tid,)).fetchone()
                barcodes[tid] = row["barcode"] if row else None
            return qr_token(tid, 0, barcodes[tid])
        return qr_token(tid, ver)

    return pat.sub(sub, body)


def outbox_loop(connect) -> None:
    while True:
        KICK.wait(0.5)
        KICK.clear()
        try:
            with connect() as conn:
                while True:
                    rows = conn.execute(
                        "SELECT * FROM sw_outbox WHERE sent_at IS NULL AND attempts < 200 ORDER BY id LIMIT 100").fetchall()
                    conn.commit()
                    if not rows:
                        break
                    with _smtp() as smtp:
                        for r in rows:
                            claimed = conn.execute(
                                "SELECT id FROM sw_outbox WHERE id = %s AND sent_at IS NULL FOR UPDATE SKIP LOCKED",
                                (r["id"],)).fetchone()
                            if not claimed:
                                conn.commit()
                                continue
                            try:
                                body = render(conn, r["body"], r["marker"])
                                smtp.send_message(build(r["to_addr"], r["to_name"], r["subject"], body, r["dedupe_key"]),
                                                  from_addr=env("MAIL_FROM"), to_addrs=[r["to_addr"]])
                                conn.execute("UPDATE sw_outbox SET sent_at = now(), attempts = attempts + 1 WHERE id = %s",
                                             (r["id"],))
                                conn.commit()
                            except smtplib.SMTPRecipientsRefused:
                                conn.execute("UPDATE sw_outbox SET attempts = attempts + 50 WHERE id = %s", (r["id"],))
                                conn.commit()
                            except Exception:
                                conn.rollback()
                                conn.execute("UPDATE sw_outbox SET attempts = attempts + 1 WHERE id = %s", (r["id"],))
                                conn.commit()
                                raise
        except Exception:
            log_exc("mail")
            time.sleep(1)


def start(connect) -> None:
    threading.Thread(target=outbox_loop, args=(connect,), daemon=True, name="outbox").start()
    KICK.set()
