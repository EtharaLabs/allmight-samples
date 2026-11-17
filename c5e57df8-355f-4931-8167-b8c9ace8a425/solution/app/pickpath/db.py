"""Pickpath data access: connections, schema additions, hashing, time, outbox mailer."""

from __future__ import annotations

import hashlib
import os
import secrets
import smtplib
import threading
import time
import traceback
from datetime import date, datetime, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import make_msgid
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

ENV = os.environ
DATABASE_URL = ENV.get("DATABASE_URL", "")
INV_LOCK = 777  # one advisory lock serializes every stock and reservation change

SCHEMA = """
CREATE TABLE IF NOT EXISTS pp_credentials (
    user_id integer PRIMARY KEY REFERENCES users (id),
    password_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pp_reset_tokens (
    token_hash text PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS pp_sessions (
    sid_hash text PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users (id),
    csrf text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS pp_api_keys (
    id serial PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users (id),
    label text NOT NULL,
    prefix text NOT NULL,
    salt text NOT NULL,
    key_hash text NOT NULL,
    revoked boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pp_lot_state (
    lot_id integer PRIMARY KEY REFERENCES lots (id),
    on_hand integer NOT NULL,
    version integer NOT NULL,
    count_hold boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS pp_movements (
    id bigserial PRIMARY KEY,
    lot_id integer NOT NULL REFERENCES lots (id),
    kind text NOT NULL,
    units integer NOT NULL,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ref text
);
CREATE INDEX IF NOT EXISTS pp_movements_lot ON pp_movements (lot_id);
CREATE TABLE IF NOT EXISTS pp_orders (
    id serial PRIMARY KEY,
    customer_id integer NOT NULL REFERENCES customers (id),
    created_by integer NOT NULL REFERENCES users (id),
    ship_date date NOT NULL,
    po_number text NOT NULL DEFAULT '',
    notes text NOT NULL DEFAULT '',
    status text NOT NULL,
    subtotal bigint NOT NULL,
    tax bigint NOT NULL,
    freight bigint NOT NULL,
    freight_tax bigint NOT NULL,
    total bigint NOT NULL,
    needs_approval boolean NOT NULL,
    approval_limit bigint,
    approved_by integer,
    rate numeric(7, 5) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    session_id text,
    session_url text,
    payment_intent text,
    paid_at timestamptz
);
CREATE TABLE IF NOT EXISTS pp_order_lines (
    order_id integer NOT NULL REFERENCES pp_orders (id),
    line_no integer NOT NULL,
    sku_id integer NOT NULL REFERENCES skus (id),
    qty integer NOT NULL,
    unit_price integer NOT NULL,
    amount bigint NOT NULL,
    tax bigint NOT NULL,
    taxable boolean NOT NULL,
    backordered integer NOT NULL DEFAULT 0,
    cancelled integer NOT NULL DEFAULT 0,
    shipped integer NOT NULL DEFAULT 0,
    returned integer NOT NULL DEFAULT 0,
    PRIMARY KEY (order_id, line_no)
);
CREATE TABLE IF NOT EXISTS pp_waves (
    id serial PRIMARY KEY,
    zone text NOT NULL,
    ship_date date NOT NULL,
    distance integer NOT NULL,
    route jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pp_tasks (
    id serial PRIMARY KEY,
    order_id integer NOT NULL REFERENCES pp_orders (id),
    line_no integer NOT NULL,
    lot_id integer NOT NULL REFERENCES lots (id),
    units integer NOT NULL,
    status text NOT NULL DEFAULT 'open',
    wave_id integer REFERENCES pp_waves (id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pp_tasks_lot ON pp_tasks (lot_id, status);
CREATE INDEX IF NOT EXISTS pp_tasks_order ON pp_tasks (order_id);
CREATE TABLE IF NOT EXISTS pp_shipments (
    id serial PRIMARY KEY,
    order_id integer NOT NULL REFERENCES pp_orders (id),
    tracking text NOT NULL UNIQUE,
    invoice_number text NOT NULL UNIQUE,
    lines jsonb NOT NULL,
    freight bigint NOT NULL,
    freight_tax bigint NOT NULL,
    total bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pp_refunds (
    id serial PRIMARY KEY,
    order_id integer NOT NULL REFERENCES pp_orders (id),
    amount bigint NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    idem_key text NOT NULL UNIQUE,
    payment_intent text NOT NULL,
    provider_id text,
    submitted boolean NOT NULL DEFAULT false,
    next_try timestamptz NOT NULL DEFAULT now(),
    last_error text,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pp_rmas (
    id serial PRIMARY KEY,
    order_id integer NOT NULL REFERENCES pp_orders (id),
    line_no integer NOT NULL,
    units integer NOT NULL,
    reason text NOT NULL,
    status text NOT NULL DEFAULT 'requested',
    disposition text NOT NULL DEFAULT '',
    lot_id integer,
    requested_by integer NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pp_credit_notes (
    id serial PRIMARY KEY,
    rma_id integer NOT NULL UNIQUE REFERENCES pp_rmas (id),
    order_id integer NOT NULL,
    amount bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pp_outbox (
    id serial PRIMARY KEY,
    dedupe_key text NOT NULL UNIQUE,
    to_addr text NOT NULL,
    to_name text NOT NULL DEFAULT '',
    subject text NOT NULL,
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);
CREATE TABLE IF NOT EXISTS pp_events (
    event_id text PRIMARY KEY,
    type text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pp_low_stock (
    sku_id integer NOT NULL,
    local_date date NOT NULL,
    PRIMARY KEY (sku_id, local_date)
);
INSERT INTO pp_lot_state (lot_id, on_hand, version)
    SELECT l.id, coalesce(sum(m.units), 0), count(m.id) FROM lots l LEFT JOIN legacy_movements m ON m.lot_id = l.id
    GROUP BY l.id
    ON CONFLICT DO NOTHING;
"""


def connect(**kw):
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=4, **kw)


def apply_schema() -> None:
    while True:
        try:
            with connect() as conn:
                conn.execute("SELECT pg_advisory_lock(424242)")
                try:
                    conn.execute(SCHEMA)
                    conn.commit()
                finally:
                    conn.execute("SELECT pg_advisory_unlock(424242)")
                    conn.commit()
            return
        except psycopg.OperationalError:
            time.sleep(1)


def inv_lock(conn) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (INV_LOCK,))


# ---- small helpers ------------------------------------------------------------------------------
def norm_email(s: str) -> str:
    return (s or "").strip().lower()


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + salt.hex() + "$" + dk.hex()


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return secrets.compare_digest(got.hex(), dk)
    except Exception:
        return False


_TZ: dict = {}


def site_tz(conn=None) -> ZoneInfo:
    if "tz" not in _TZ:
        if conn is None:
            with connect() as c:
                row = c.execute("SELECT timezone FROM sites ORDER BY id LIMIT 1").fetchone()
        else:
            row = conn.execute("SELECT timezone FROM sites ORDER BY id LIMIT 1").fetchone()
        _TZ["tz"] = ZoneInfo(row["timezone"])
    return _TZ["tz"]


def today(conn=None) -> date:
    return datetime.now(site_tz(conn)).date()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}${c // 100:,}.{c % 100:02d}"


def has_ctrl(s: str, allow_newlines: bool = False) -> bool:
    for ch in s:
        o = ord(ch)
        if o < 0x20 or o == 0x7F:
            if allow_newlines and ch in "\r\n":
                continue
            return True
    return False


# ---- outbox ----------------------------------------------------------------------------------------
OUTBOX_EVENT = threading.Event()


def enqueue_mail(conn, key: str, to: str, to_name: str, subject: str, body: str) -> None:
    conn.execute(
        "INSERT INTO pp_outbox (dedupe_key, to_addr, to_name, subject, body) VALUES (%s,%s,%s,%s,%s)"
        " ON CONFLICT DO NOTHING", (key, norm_email(to), to_name or "", subject, body))


def _clean(s: str) -> str:
    return "".join(ch for ch in (s or "") if ord(ch) >= 0x20 and ord(ch) != 0x7F)


def build_message(to: str, to_name: str, subject: str, body: str, key: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = ENV["MAIL_FROM"]
    name = _clean(to_name)
    msg["To"] = Address(display_name=name, addr_spec=to) if name else to
    msg["Subject"] = _clean(subject)
    msg["Message-ID"] = make_msgid(idstring=sha(key)[:16], domain="pickpath.local")
    msg.set_content(body)
    return msg


def send_now(to: str, to_name: str, subject: str, body: str) -> None:
    """Immediate send (reset links: the plain token is never stored)."""
    try:
        with smtplib.SMTP(ENV["SMTP_HOST"], int(ENV["SMTP_PORT"]), timeout=10) as smtp:
            smtp.send_message(build_message(to, to_name, subject, body, secrets.token_hex(8)))
    except Exception:
        traceback.print_exc()


def outbox_loop() -> None:
    while True:
        OUTBOX_EVENT.wait(1.0)
        OUTBOX_EVENT.clear()
        try:
            with connect() as conn:
                while True:
                    rows = conn.execute(
                        "SELECT * FROM pp_outbox WHERE sent_at IS NULL ORDER BY id LIMIT 50 FOR UPDATE SKIP LOCKED").fetchall()
                    if not rows:
                        conn.commit()
                        break
                    with smtplib.SMTP(ENV["SMTP_HOST"], int(ENV["SMTP_PORT"]), timeout=10) as smtp:
                        for r in rows:
                            smtp.send_message(build_message(r["to_addr"], r["to_name"], r["subject"], r["body"], r["dedupe_key"]))
                            conn.execute("UPDATE pp_outbox SET sent_at = now() WHERE id = %s", (r["id"],))
                    conn.commit()
        except Exception:
            time.sleep(1)
