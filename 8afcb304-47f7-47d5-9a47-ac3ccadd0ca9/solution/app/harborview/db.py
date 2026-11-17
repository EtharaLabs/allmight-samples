"""Configuration, database connection, and the application's own schema additions."""

from __future__ import annotations

import os
import time

import psycopg
from psycopg.rows import dict_row

ENV = os.environ


def env_int(name: str, default: int) -> int:
    try:
        return int(ENV.get(name, str(default)))
    except ValueError:
        return default


DATABASE_URL = ENV.get("DATABASE_URL", "")
PUBLIC_BASE_URL = ENV.get("PUBLIC_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
PAYMENTS_API_URL = ENV.get("PAYMENTS_API_URL", "").rstrip("/")
SIGNING_FILE = ENV.get("PAYMENTS_WEBHOOK_SIGNING_FILE", "")
RESET_TTL = env_int("RESET_LINK_TTL_SECONDS", 3600)
HOLD_TTL = env_int("HOLD_TTL_SECONDS", 900)
OFFER_TTL = env_int("OFFER_TTL_SECONDS", 1800)
NOTICE_MIN = env_int("MIN_NOTICE_MINUTES", 120)
HORIZON_DAYS = env_int("BOOKING_HORIZON_DAYS", 60)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hv_credentials (
    user_id integer PRIMARY KEY REFERENCES users (id),
    password_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hv_reset_tokens (
    token_hash text PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS hv_sessions (
    sid_hash text PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users (id),
    csrf text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS hv_appt (
    appointment_id integer PRIMARY KEY REFERENCES appointments (id),
    start_at timestamptz NOT NULL,
    expires_at timestamptz,
    session_id text,
    payment_intent text,
    paid_cents integer NOT NULL DEFAULT 0,
    refund_cents integer NOT NULL DEFAULT 0,
    refund_status text NOT NULL DEFAULT 'none',
    refund_key text,
    refund_accepted boolean NOT NULL DEFAULT false,
    refund_next_at timestamptz,
    refund_reason text,
    rescheduled_from integer,
    rescheduled_to integer,
    cancelled_at timestamptz,
    cancelled_by_staff boolean NOT NULL DEFAULT false,
    expire_session_pending boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS hv_appt_refund ON hv_appt (refund_status) WHERE refund_status = 'pending';
CREATE INDEX IF NOT EXISTS hv_appt_expiry ON hv_appt (expires_at) WHERE expires_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS hv_waitlist (
    id serial PRIMARY KEY,
    patient_id integer NOT NULL REFERENCES users (id),
    clinician_id integer NOT NULL REFERENCES clinicians (id),
    type_code text NOT NULL,
    on_date date NOT NULL,
    joined_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    status text NOT NULL DEFAULT 'waiting',
    offer_appointment_id integer,
    offer_token_hash text UNIQUE
);
CREATE UNIQUE INDEX IF NOT EXISTS hv_waitlist_one_waiting ON hv_waitlist (patient_id, clinician_id, type_code, on_date)
    WHERE status = 'waiting';
CREATE TABLE IF NOT EXISTS hv_events (
    event_id text PRIMARY KEY,
    type text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hv_reminders (
    appointment_id integer PRIMARY KEY,
    sent_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hv_outbox (
    id serial PRIMARY KEY,
    dedupe_key text NOT NULL UNIQUE,
    to_addr text NOT NULL,
    to_name text NOT NULL DEFAULT '',
    subject text NOT NULL,
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);
ALTER TABLE hv_outbox ADD COLUMN IF NOT EXISTS to_name text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS hv_users_norm_email ON users (lower(btrim(email)));
CREATE INDEX IF NOT EXISTS hv_appointments_local_start ON appointments (local_start);
"""


def connect(**kw):
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=4, **kw)


def apply_schema() -> None:
    while True:
        try:
            with connect() as conn:
                conn.execute("SELECT pg_advisory_lock(73001)")
                try:
                    conn.execute(SCHEMA)
                    conn.commit()
                finally:
                    conn.execute("SELECT pg_advisory_unlock(73001)")
                    conn.commit()
            return
        except psycopg.OperationalError:
            time.sleep(1)


def lock_clinic(conn, clinic_id: int) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(73100, %s)", (clinic_id,))
