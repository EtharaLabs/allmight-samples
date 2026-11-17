"""Database access: connections, idempotent schema additions (tables prefixed sw_), one-time import of
the old platform's rows into the application's working tables (the legacy tables stay untouched)."""

from __future__ import annotations

import secrets
import sys
import time

import psycopg
from psycopg.rows import dict_row

from .util import UTC, env, legacy_instant, log_exc, norm_email, now

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS sw_order_id_seq START 1000001;
CREATE SEQUENCE IF NOT EXISTS sw_ticket_id_seq START 1000001;
CREATE TABLE IF NOT EXISTS sw_meta (key text PRIMARY KEY, value text NOT NULL);
CREATE TABLE IF NOT EXISTS sw_accounts (
    id integer PRIMARY KEY REFERENCES users (id),
    email_norm text NOT NULL UNIQUE,
    full_name text NOT NULL,
    password_hash text,
    verified boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sw_sessions (
    sid_hash text PRIMARY KEY,
    account_id integer NOT NULL REFERENCES sw_accounts (id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sw_account_tokens (
    token_hash text PRIMARY KEY,
    account_id integer NOT NULL REFERENCES sw_accounts (id),
    kind text NOT NULL,
    created_at timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS sw_account_tokens_acct ON sw_account_tokens (account_id, kind);
CREATE TABLE IF NOT EXISTS sw_invites (
    id serial PRIMARY KEY,
    org_id integer NOT NULL REFERENCES organizations (id),
    email_norm text NOT NULL,
    role text NOT NULL,
    token_hash text NOT NULL UNIQUE,
    state text NOT NULL DEFAULT 'open',
    invited_by integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    accepted_by integer,
    accepted_at timestamptz
);
CREATE TABLE IF NOT EXISTS sw_events (
    event_id integer PRIMARY KEY REFERENCES events (id),
    org_id integer NOT NULL,
    venue_id integer NOT NULL,
    legacy boolean NOT NULL DEFAULT false,
    status text NOT NULL,
    starts_at timestamptz NOT NULL,
    original_starts_at timestamptz NOT NULL,
    on_sale_at timestamptz,
    off_sale_at timestamptz,
    presale_at timestamptz,
    presale_code text,
    rescheduled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    cancelled_at timestamptz
);
CREATE INDEX IF NOT EXISTS sw_events_venue ON sw_events (venue_id);
CREATE TABLE IF NOT EXISTS sw_orders (
    id bigint PRIMARY KEY,
    legacy boolean NOT NULL DEFAULT false,
    event_id integer NOT NULL,
    buyer_id integer,
    buyer_email text NOT NULL,
    buyer_email_norm text NOT NULL,
    buyer_name text NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL,
    hold_expires_at timestamptz,
    ended_at timestamptz,
    pay_pressed boolean NOT NULL DEFAULT false,
    session_id text UNIQUE,
    session_url text,
    session_amount bigint,
    session_expired boolean NOT NULL DEFAULT false,
    payment_intent text,
    amount_paid bigint,
    paid_at timestamptz,
    total_cents bigint,
    offer_id integer
);
CREATE INDEX IF NOT EXISTS sw_orders_event ON sw_orders (event_id);
CREATE INDEX IF NOT EXISTS sw_orders_buyer ON sw_orders (buyer_email_norm, event_id);
CREATE INDEX IF NOT EXISTS sw_orders_pending ON sw_orders (event_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS sw_orders_session_todo ON sw_orders (id) WHERE session_id IS NOT NULL AND NOT session_expired;
CREATE TABLE IF NOT EXISTS sw_order_seats (
    order_id bigint NOT NULL REFERENCES sw_orders (id),
    seat_id integer NOT NULL,
    label text NOT NULL,
    face bigint NOT NULL,
    discount bigint,
    service bigint,
    facility bigint,
    tax bigint,
    total bigint,
    PRIMARY KEY (order_id, seat_id)
);
CREATE TABLE IF NOT EXISTS sw_promo_codes (
    id serial PRIMARY KEY,
    event_id integer NOT NULL,
    code text NOT NULL,
    kind text NOT NULL,
    value bigint NOT NULL,
    min_subtotal bigint NOT NULL DEFAULT 0,
    max_redemptions integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, code)
);
CREATE TABLE IF NOT EXISTS sw_order_codes (
    order_id bigint NOT NULL REFERENCES sw_orders (id),
    code_id integer NOT NULL REFERENCES sw_promo_codes (id),
    PRIMARY KEY (order_id, code_id)
);
CREATE TABLE IF NOT EXISTS sw_tickets (
    id bigint PRIMARY KEY,
    legacy boolean NOT NULL DEFAULT false,
    order_id bigint NOT NULL,
    event_id integer NOT NULL,
    seat_id integer NOT NULL,
    holder_email_norm text NOT NULL,
    holder_name text NOT NULL,
    status text NOT NULL,
    legacy_status text,
    face bigint NOT NULL DEFAULT 0,
    discount bigint NOT NULL DEFAULT 0,
    service bigint NOT NULL DEFAULT 0,
    facility bigint NOT NULL DEFAULT 0,
    tax bigint NOT NULL DEFAULT 0,
    total bigint NOT NULL,
    legacy_refunded_cents bigint NOT NULL DEFAULT 0,
    barcode text UNIQUE,
    token_version integer NOT NULL,
    transferred boolean NOT NULL DEFAULT false,
    admitted_at timestamptz,
    admitted_gate text,
    issued_at timestamptz
);
CREATE INDEX IF NOT EXISTS sw_tickets_event ON sw_tickets (event_id);
CREATE INDEX IF NOT EXISTS sw_tickets_holder ON sw_tickets (holder_email_norm);
CREATE INDEX IF NOT EXISTS sw_tickets_order ON sw_tickets (order_id);
CREATE TABLE IF NOT EXISTS sw_transfers (
    id serial PRIMARY KEY,
    ticket_id bigint NOT NULL,
    from_email text NOT NULL,
    from_name text NOT NULL,
    to_email text NOT NULL,
    to_name text NOT NULL,
    new_version integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sw_refunds (
    id bigserial PRIMARY KEY,
    kind text NOT NULL,
    ticket_id bigint,
    order_id bigint NOT NULL,
    event_id integer NOT NULL,
    amount bigint NOT NULL,
    payment_intent text NOT NULL,
    state text NOT NULL DEFAULT 'requested',
    provider_refund_id text UNIQUE,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS sw_refunds_ticket ON sw_refunds (ticket_id);
CREATE INDEX IF NOT EXISTS sw_refunds_event ON sw_refunds (event_id);
CREATE INDEX IF NOT EXISTS sw_refunds_todo ON sw_refunds (next_attempt_at) WHERE state = 'requested';
CREATE TABLE IF NOT EXISTS sw_webhook_events (
    id text PRIMARY KEY,
    type text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sw_devices (
    id serial PRIMARY KEY,
    org_id integer NOT NULL,
    event_id integer NOT NULL,
    label text NOT NULL,
    key_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);
CREATE TABLE IF NOT EXISTS sw_scans (
    id bigserial PRIMARY KEY,
    event_id integer NOT NULL,
    device_id integer,
    scan_id text,
    token text NOT NULL,
    gate text NOT NULL,
    scanned_at_raw text,
    scanned_at timestamptz NOT NULL,
    ticket_id bigint,
    accepted boolean NOT NULL,
    reason text,
    received_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS sw_scans_scan_id ON sw_scans (event_id, scan_id) WHERE scan_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS sw_waitlist (
    id serial PRIMARY KEY,
    event_id integer NOT NULL,
    section_id integer NOT NULL,
    account_id integer NOT NULL,
    email_norm text NOT NULL,
    name text NOT NULL,
    quantity integer NOT NULL,
    status text NOT NULL DEFAULT 'waiting',
    created_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz
);
CREATE INDEX IF NOT EXISTS sw_waitlist_event ON sw_waitlist (event_id, section_id);
CREATE TABLE IF NOT EXISTS sw_offers (
    id serial PRIMARY KEY,
    entry_id integer NOT NULL,
    event_id integer NOT NULL,
    section_id integer NOT NULL,
    account_id integer NOT NULL,
    seat_ids integer[] NOT NULL,
    status text NOT NULL DEFAULT 'open',
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    order_id bigint,
    ended_at timestamptz
);
CREATE INDEX IF NOT EXISTS sw_offers_event ON sw_offers (event_id) WHERE status = 'open';
CREATE TABLE IF NOT EXISTS sw_reminders (
    event_id integer NOT NULL,
    key_hash text NOT NULL,
    sent_by integer,
    subject text NOT NULL,
    recipients integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, key_hash)
);
CREATE TABLE IF NOT EXISTS sw_outbox (
    id bigserial PRIMARY KEY,
    dedupe_key text NOT NULL UNIQUE,
    to_addr text NOT NULL,
    to_name text NOT NULL,
    subject text NOT NULL,
    body text NOT NULL,
    marker text,
    created_at timestamptz NOT NULL DEFAULT now(),
    attempts integer NOT NULL DEFAULT 0,
    sent_at timestamptz
);
CREATE INDEX IF NOT EXISTS sw_outbox_unsent ON sw_outbox (id) WHERE sent_at IS NULL;
"""

LOCK_SCHEMA = 77120001


def database_url() -> str:
    return env("DATABASE_URL")


def connect(**kw):
    return psycopg.connect(database_url(), row_factory=dict_row, connect_timeout=5, **kw)


def apply_schema() -> None:
    while True:
        try:
            with connect() as conn:
                conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_SCHEMA,))
                try:
                    conn.execute(SCHEMA)
                    conn.commit()
                    done = conn.execute("SELECT value FROM sw_meta WHERE key = 'legacy_import'").fetchone()
                    if not done:
                        t0 = time.time()
                        import_legacy(conn)
                        conn.execute("INSERT INTO sw_meta (key, value) VALUES ('legacy_import', 'v1')")
                        conn.execute("INSERT INTO sw_meta (key, value) VALUES ('install_id', %s) ON CONFLICT DO NOTHING",
                                     (secrets.token_hex(8),))
                        conn.commit()
                        sys.stderr.write(f"seatwise: imported old-platform records in {time.time() - t0:.1f}s\n")
                    conn.commit()
                finally:
                    try:
                        conn.rollback()
                        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_SCHEMA,))
                        conn.commit()
                    except Exception:
                        pass
            return
        except psycopg.OperationalError:
            log_exc("db")
            time.sleep(1)


def install_id(conn) -> str:
    row = conn.execute("SELECT value FROM sw_meta WHERE key = 'install_id'").fetchone()
    return row["value"] if row else "0"


def import_legacy(conn) -> None:
    """Copy the old platform's rows into the working tables once (amounts and times as recorded)."""
    ts = now()
    for u in conn.execute("SELECT id, email, full_name, created_at FROM users ORDER BY id").fetchall():
        conn.execute("INSERT INTO sw_accounts (id, email_norm, full_name, password_hash, verified, created_at) "
                     "VALUES (%s, %s, %s, NULL, false, %s) ON CONFLICT DO NOTHING",
                     (u["id"], norm_email(u["email"]), u["full_name"], u["created_at"]))
    tz_of_event: dict[int, str] = {}
    for e in conn.execute("SELECT e.*, v.timezone FROM events e JOIN venues v ON v.id = e.venue_id ORDER BY e.id").fetchall():
        tz = e["timezone"]
        tz_of_event[e["id"]] = tz
        start = legacy_instant(e["starts_local"], e["starts_fold"], tz)
        on_sale = legacy_instant(e["on_sale_local"], e["on_sale_fold"], tz)
        status = "cancelled" if e["status"] == "cancelled" else "published"
        conn.execute(
            "INSERT INTO sw_events (event_id, org_id, venue_id, legacy, status, starts_at, original_starts_at, on_sale_at, "
            "created_at, published_at, cancelled_at) VALUES (%s,%s,%s,true,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (e["id"], e["organization_id"], e["venue_id"], status, start, start, on_sale, ts, ts,
             ts if status == "cancelled" else None))
    orders = conn.execute("SELECT * FROM legacy_orders ORDER BY id").fetchall()
    with conn.cursor().copy(
            "COPY sw_orders (id, legacy, event_id, buyer_email, buyer_email_norm, buyer_name, status, created_at, paid_at, "
            "total_cents, amount_paid) FROM STDIN") as cp:
        for o in orders:
            placed = legacy_instant(o["placed_local"], o["placed_fold"], tz_of_event[o["event_id"]])
            cp.write_row((o["id"], True, o["event_id"], o["buyer_email"], norm_email(o["buyer_email"]), o["buyer_name"],
                          "paid", placed, placed, o["total_cents"], o["total_cents"]))
    event_of_order = {o["id"]: o["event_id"] for o in orders}
    transferred = {r["ticket_id"] for r in conn.execute("SELECT DISTINCT ticket_id FROM legacy_transfers")}
    tickets = conn.execute("SELECT * FROM legacy_tickets ORDER BY id").fetchall()
    with conn.cursor().copy(
            "COPY sw_tickets (id, legacy, order_id, event_id, seat_id, holder_email_norm, holder_name, status, legacy_status, "
            "face, discount, service, facility, tax, total, legacy_refunded_cents, barcode, token_version, transferred, "
            "admitted_at, admitted_gate, issued_at) FROM STDIN") as cp:
        for t in tickets:
            ev = event_of_order[t["order_id"]]
            tz = tz_of_event[ev]
            adm = legacy_instant(t["checked_in_local"], t["checked_in_fold"], tz) if t["checked_in_local"] else None
            status = "refunded" if t["status"] == "refunded" else "valid"
            cp.write_row((t["id"], True, t["order_id"], ev, t["seat_id"], norm_email(t["holder_email"]), t["holder_name"],
                          status, t["status"], t["face_cents"], t["discount_cents"], t["service_fee_cents"],
                          t["facility_fee_cents"], t["tax_cents"], t["total_cents"], t["refunded_cents"], t["barcode"], 0,
                          t["id"] in transferred, adm, t["checked_in_gate"] if adm else None, None))
    conn.execute("ANALYZE sw_tickets")
    conn.execute("ANALYZE sw_orders")


def utcnow():
    return now().astimezone(UTC)
