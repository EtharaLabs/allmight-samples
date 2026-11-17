"""Harborview domain layer: accounts, holds and confirmation, refunds, rescheduling, waitlist, outbox."""

from __future__ import annotations

import hashlib
import json
import secrets
import smtplib
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

import psycopg

from . import avail, db, provider
from .avail import UTC, tzinfo

WAKE = threading.Event()  # outbox
_PENDING = threading.local()  # offer emails to send once the transaction that made the offer commits (R14.5)


def _pending() -> list:
    if not hasattr(_PENDING, "items"):
        _PENDING.items = []
    return _PENDING.items


def clear_pending() -> None:
    _pending().clear()


def send_pending() -> None:
    items = list(_pending())
    _pending().clear()
    for to, name, subject, body in items:
        send_now(to, name, subject, body)
REFUND_WAKE = threading.Event()


def now_utc() -> datetime:
    return datetime.now(UTC)


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


def one_line(v: str) -> str:
    return " ".join(str(v).split())


def dollars(c: int) -> str:
    sign = "-" if c < 0 else ""
    c = abs(int(c))
    return f"{sign}${c // 100:,}.{c % 100:02d}"


def half_up(num: int, den: int) -> int:
    q, r = divmod(num, den)
    return q + (1 if 2 * r >= den else 0)


# ------------------------------------------------------------------------------------------ users
def account_for(conn, email: str):
    return conn.execute("SELECT * FROM users WHERE lower(btrim(email)) = %s AND active ORDER BY id LIMIT 1",
                        (norm_email(email),)).fetchone()


def any_user_with(conn, email: str) -> bool:
    return conn.execute("SELECT 1 FROM users WHERE lower(btrim(email)) = %s LIMIT 1", (norm_email(email),)).fetchone() is not None


def is_account(conn, user: dict) -> bool:
    acct = account_for(conn, user["email"])
    return acct is not None and acct["id"] == user["id"]


def clinician_of_user(conn, user_id: int):
    return conn.execute("SELECT * FROM clinicians WHERE user_id = %s", (user_id,)).fetchone()


def enqueue(conn, key: str, to: str, subject: str, body: str, name: str | None = None) -> None:
    if name is None:
        row = conn.execute("SELECT full_name FROM users WHERE lower(btrim(email)) = %s ORDER BY active DESC, id LIMIT 1",
                           (norm_email(to),)).fetchone()
        name = row["full_name"] if row else ""
    conn.execute("INSERT INTO hv_outbox (dedupe_key, to_addr, to_name, subject, body) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                 (key, norm_email(to), name, subject, body))


# ---------------------------------------------------------------------------------- appointments
APPT_SQL = """SELECT a.*, t.code, t.name AS type_name, t.duration_minutes, t.buffer_minutes, t.room_type,
                     c.clinic_id, c.user_id AS clinician_user_id, cu.full_name AS clinician_name, cl.name AS clinic_name,
                     cl.timezone, r.name AS room_name, pu.full_name AS patient_name, pu.email AS patient_email,
                     pu.active AS patient_active, h.appointment_id IS NOT NULL AS app, h.start_at, h.expires_at, h.session_id,
                     h.payment_intent, h.paid_cents, h.refund_cents, h.refund_status, h.rescheduled_to, h.rescheduled_from,
                     h.cancelled_at, h.cancelled_by_staff
                FROM appointments a
                JOIN appointment_types t ON t.id = a.type_id
                JOIN clinicians c ON c.id = a.clinician_id
                JOIN users cu ON cu.id = c.user_id
                JOIN clinics cl ON cl.id = c.clinic_id
                JOIN users pu ON pu.id = a.patient_id
                LEFT JOIN rooms r ON r.id = a.room_id
                LEFT JOIN hv_appt h ON h.appointment_id = a.id"""


def enrich(r: dict | None) -> dict | None:
    if r is None:
        return None
    tz = tzinfo(r["timezone"])
    r["tz"] = tz
    r["start"] = r["start_at"].astimezone(UTC) if r["app"] else avail.legacy_instant(r["local_start"], r["fold"], tz)
    r["end"] = r["start"] + timedelta(minutes=r["duration_minutes"])
    r["local_date"] = r["start"].astimezone(tz).date()
    if not r["app"]:
        r["paid_cents"], r["refund_cents"], r["refund_status"] = r["deposit_cents"], 0, "none"
    if r["app"] and r["status"] == "held" and r["expires_at"] is not None and r["expires_at"] <= now_utc():
        r["status"] = "expired"  # expired but not yet swept
    return r


def load_appt(conn, aid: int, lock: bool = False) -> dict | None:
    if lock:
        conn.execute("SELECT 1 FROM appointments WHERE id = %s FOR UPDATE", (aid,))
    return enrich(conn.execute(APPT_SQL + " WHERE a.id = %s", (aid,)).fetchone())


def when_text(a: dict) -> str:
    return avail.label(a["start"], a["tz"])


def _insert_appt(conn, patient_id: int, clinician_id: int, type_id: int, room_id: int | None, start: datetime, tz,
                 status: str, deposit: int, expires: datetime | None, **extra) -> int:
    naive, fold = avail.local_naive_fold(start, tz)
    aid = conn.execute(
        "INSERT INTO appointments (patient_id, clinician_id, type_id, room_id, local_start, fold, status, deposit_cents)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (patient_id, clinician_id, type_id, room_id, naive, fold, status, deposit)).fetchone()["id"]
    cols = {"appointment_id": aid, "start_at": start, "expires_at": expires, **extra}
    conn.execute(f"INSERT INTO hv_appt ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})", list(cols.values()))
    return aid


def parse_instant(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat((s or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(UTC)


class Refused(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


def _slot_room(conn, clinician_id: int, code: str, start: datetime, ignore: int | None = None) -> int:
    c, _ = avail.context(conn, clinician_id, code)
    day = start.astimezone(tzinfo(c["timezone"])).date()
    for s, room in avail.compute_slots(conn, clinician_id, code, day, now_utc(), ignore_id=ignore):
        if s == start:
            return room
    raise Refused(409, "That time is no longer available. Please pick another slot.")


def book(user: dict, clinician_id: int, code: str, start_s: str) -> tuple[int, str]:
    """Create a hold and its Checkout Session; returns (appointment id, checkout url)."""
    start = parse_instant(start_s)
    if start is None:
        raise Refused(400, "The start time is not a valid instant.")
    with db.connect() as conn:
        c, t = avail.context(conn, clinician_id, code)
        if c is None or t is None:
            raise Refused(400, "Unknown clinician or appointment type.")
        db.lock_clinic(conn, c["clinic_id"])
        room = _slot_room(conn, clinician_id, code, start)
        tz = tzinfo(c["timezone"])
        fee = avail.fee_on(conn, t["id"], start.astimezone(tz).date())
        deposit = half_up(fee["fee_cents"] * fee["deposit_bp"], 10000)
        now = now_utc()
        aid = _insert_appt(conn, user["id"], clinician_id, t["id"], room, start, tz, "held", deposit,
                           now + timedelta(seconds=db.HOLD_TTL))
        conn.commit()
    return aid, checkout_for(aid, user)


def checkout_for(aid: int, user: dict) -> str:
    with db.connect() as conn:
        a = load_appt(conn, aid)
    try:
        sess = provider.create_checkout(aid, a["deposit_cents"], f"{a['type_name']} deposit, {a['clinic_name']}",
                                        norm_email(user["email"]))
    except provider.ProviderError:
        with db.connect() as conn:
            conn.execute("UPDATE appointments SET status = 'released' WHERE id = %s AND status = 'held'", (aid,))
            conn.execute("UPDATE hv_appt SET expires_at = now() WHERE appointment_id = %s", (aid,))
            conn.commit()
        raise Refused(503, "The payment provider is not available right now, so the slot was not held. Please try again.")
    with db.connect() as conn:
        conn.execute("UPDATE hv_appt SET session_id = %s WHERE appointment_id = %s", (sess["id"], aid))
        conn.commit()
    return sess["url"]


def release(user: dict, aid: int) -> None:
    clear_pending()
    with db.connect() as conn:
        a = load_appt(conn, aid)
        if a is None or not a["app"] or a["patient_id"] != user["id"]:
            raise Refused(403, "You can only release your own holds.")
        db.lock_clinic(conn, a["clinic_id"])
        a = load_appt(conn, aid, lock=True)
        if a["status"] != "held":
            raise Refused(409, "This appointment is not held any more.")
        conn.execute("UPDATE appointments SET status = 'released' WHERE id = %s", (aid,))
        conn.execute("UPDATE hv_appt SET expire_session_pending = true WHERE appointment_id = %s", (aid,))
        waitlist_trigger(conn, a["clinician_id"], a["local_date"])
        conn.commit()
    send_pending()
    _expire_sessions()


# --------------------------------------------------------------------------------------- webhooks
def handle_event(event: dict) -> None:
    eid, etype = str(event.get("id") or ""), str(event.get("type") or "")
    obj = ((event.get("data") or {}).get("object")) or {}
    if not eid:
        return
    for attempt in range(5):
        try:
            with db.connect() as conn:
                if conn.execute("INSERT INTO hv_events (event_id, type) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING event_id",
                                (eid, etype)).fetchone() is None:
                    return
                clear_pending()
                if etype == "checkout.session.completed":
                    _on_completed(conn, obj)
                elif etype == "refund.created":
                    _on_refund(conn, obj)
                conn.commit()
            send_pending()
            WAKE.set()
            REFUND_WAKE.set()
            return
        except (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected):
            time.sleep(0.05 * (attempt + 1))


def _meta_appt(obj: dict) -> int | None:
    try:
        return int(str((obj.get("metadata") or {}).get("appointment_id")))
    except (TypeError, ValueError):
        return None


def _on_completed(conn, obj: dict) -> None:
    aid = _meta_appt(obj)
    if aid is None or obj.get("payment_status") != "paid":
        return
    a = load_appt(conn, aid, lock=True)
    if a is None or not a["app"]:
        return
    pi, amount = obj.get("payment_intent"), int(obj.get("amount_total") or 0)
    raw = conn.execute("SELECT status FROM appointments WHERE id = %s", (aid,)).fetchone()["status"]
    if raw == "held" and a["expires_at"] > now_utc():
        conn.execute("UPDATE appointments SET status = 'confirmed' WHERE id = %s", (aid,))
        conn.execute("UPDATE hv_appt SET payment_intent = %s, paid_cents = %s, expires_at = NULL WHERE appointment_id = %s",
                     (pi, amount, aid))
        enqueue(conn, f"confirm:{aid}", a["patient_email"], f"Appointment confirmed: {a['type_name']} on {when_text(a)}",
                f"Hello {a['patient_name']},\n\nYour appointment is confirmed.\n\n"
                f"  Clinic: {a['clinic_name']}\n  Clinician: {a['clinician_name']}\n  Type: {a['type_name']}\n"
                f"  When: {a['start'].astimezone(a['tz']).strftime('%Y-%m-%d %H:%M')} ({a['start'].astimezone(a['tz']).tzname()})\n"
                f"  Deposit paid: {dollars(amount)}\n\nHarborview Physio\n")
        return
    if raw in ("held", "expired", "released") and pi and amount > 0:
        if raw == "held":
            conn.execute("UPDATE appointments SET status = 'expired' WHERE id = %s", (aid,))
            waitlist_trigger(conn, a["clinician_id"], a["local_date"])
        h = conn.execute("SELECT refund_status FROM hv_appt WHERE appointment_id = %s", (aid,)).fetchone()
        if h["refund_status"] == "none":
            conn.execute("UPDATE hv_appt SET payment_intent = %s, paid_cents = %s, refund_cents = %s, refund_status = 'pending',"
                         " refund_key = %s, refund_reason = 'late payment', refund_next_at = now() WHERE appointment_id = %s",
                         (pi, amount, amount, f"late-refund-{aid}", aid))


def _on_refund(conn, obj: dict) -> None:
    aid = _meta_appt(obj)
    if aid is None or obj.get("status", "succeeded") != "succeeded":
        return
    a = load_appt(conn, aid, lock=True)
    if a is None or not a["app"]:
        return
    h = conn.execute("UPDATE hv_appt SET refund_status = 'refunded', refund_accepted = true WHERE appointment_id = %s"
                     " AND refund_status = 'pending' RETURNING refund_cents", (aid,)).fetchone()
    if h:
        enqueue(conn, f"refund:{aid}", a["patient_email"], f"Refund issued: {dollars(h['refund_cents'])}",
                f"Hello {a['patient_name']},\n\nWe have refunded {dollars(h['refund_cents'])} of your deposit for the "
                f"{a['type_name']} appointment on {when_text(a)} at {a['clinic_name']}.\n"
                "It can take a few days to appear on your card statement.\n\nHarborview Physio\n")


# ------------------------------------------------------------------------------------ cancellation
def refund_for(paid: int, seconds_before: float, staff: bool) -> int:
    if staff or seconds_before >= 86400:
        return paid
    if seconds_before >= 7200:
        return half_up(paid, 2)
    return 0


def band_text(seconds_before: float, staff: bool) -> str:
    if staff:
        return "cancelled by the clinic: the whole deposit is refunded"
    if seconds_before >= 86400:
        return "24 hours or more before the start: the whole deposit is refunded"
    if seconds_before >= 7200:
        return "between 2 and 24 hours before the start: half the deposit is refunded"
    return "less than 2 hours before the start: no refund"


def cancel(user: dict, aid: int) -> dict:
    clear_pending()
    staff = user["role"] in ("front_desk", "manager", "admin")
    with db.connect() as conn:
        a = load_appt(conn, aid)
        if a is None:
            raise Refused(404, "No such appointment.")
        if staff and user["role"] != "admin" and user.get("clinic_id") != a["clinic_id"]:
            raise Refused(403, "You can only cancel appointments of your own clinic.")
        if not staff and a["patient_id"] != user["id"]:
            raise Refused(403, "You can only cancel your own appointments.")
        if user["role"] not in ("patient", "front_desk", "manager", "admin"):
            raise Refused(403, "You cannot cancel appointments.")
        if not a["app"]:
            raise Refused(409, "Appointments made in our old system cannot be changed online. Please call the clinic.")
        db.lock_clinic(conn, a["clinic_id"])
        a = load_appt(conn, aid, lock=True)
        if a["status"] != "confirmed":
            raise Refused(409, "Only confirmed appointments can be cancelled.")
        now = now_utc()
        before = (a["start"] - now).total_seconds()
        if before <= 0:
            raise Refused(409, "This appointment has already started and can no longer be cancelled.")
        amount = refund_for(a["paid_cents"], before, staff)
        conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = %s", (aid,))
        conn.execute("UPDATE hv_appt SET cancelled_at = %s, cancelled_by_staff = %s, refund_cents = %s, refund_status = %s,"
                     " refund_key = %s, refund_reason = 'cancellation', refund_next_at = now() WHERE appointment_id = %s",
                     (now, staff, amount, "pending" if amount > 0 else "none", f"refund-{aid}", aid))
        refund_line = (f"We are refunding {dollars(amount)} of your {dollars(a['paid_cents'])} deposit "
                       f"({band_text(before, staff)}). You will get another email when the refund is issued."
                       if amount > 0 else f"There is no refund ({band_text(before, staff)}).")
        enqueue(conn, f"cancel:{aid}", a["patient_email"], f"Appointment cancelled: {a['type_name']} on {when_text(a)}",
                f"Hello {a['patient_name']},\n\nYour {a['type_name']} appointment with {a['clinician_name']} at "
                f"{a['clinic_name']} on {when_text(a)} has been cancelled.\n\n{refund_line}\n\nHarborview Physio\n")
        waitlist_trigger(conn, a["clinician_id"], a["local_date"])
        conn.commit()
    send_pending()
    WAKE.set()
    REFUND_WAKE.set()
    return {"refund": amount}


# -------------------------------------------------------------------------------------- reschedule
def reschedule(user: dict, aid: int, clinician_id: int, start_s: str) -> int:
    clear_pending()
    start = parse_instant(start_s)
    if start is None:
        raise Refused(400, "The new start time is not a valid instant.")
    with db.connect() as conn:
        a = load_appt(conn, aid)
        if a is None:
            raise Refused(404, "No such appointment.")
        if user["role"] != "patient" or a["patient_id"] != user["id"]:
            raise Refused(403, "You can only reschedule your own appointments.")
        if not a["app"]:
            raise Refused(409, "Appointments made in our old system cannot be changed online. Please call the clinic.")
        c = conn.execute("SELECT * FROM clinicians WHERE id = %s", (clinician_id,)).fetchone()
        if c is None or c["clinic_id"] != a["clinic_id"]:
            raise Refused(409, "Choose a clinician at the same clinic.")
        db.lock_clinic(conn, a["clinic_id"])
        a = load_appt(conn, aid, lock=True)
        if a["status"] != "confirmed":
            raise Refused(409, "Only confirmed appointments can be rescheduled.")
        if (a["start"] - now_utc()).total_seconds() < 86400:
            raise Refused(409, "Appointments can only be rescheduled 24 hours or more before they start. You can cancel instead.")
        room = _slot_room(conn, clinician_id, a["code"], start, ignore=aid)
        new = _insert_appt(conn, a["patient_id"], clinician_id, a["type_id"], room, start, a["tz"], "confirmed",
                           a["paid_cents"], None, payment_intent=a["payment_intent"], paid_cents=a["paid_cents"],
                           rescheduled_from=aid)
        conn.execute("UPDATE appointments SET status = 'rescheduled' WHERE id = %s", (aid,))
        conn.execute("UPDATE hv_appt SET rescheduled_to = %s WHERE appointment_id = %s", (new, aid))
        n = load_appt(conn, new)
        enqueue(conn, f"resched:{new}", a["patient_email"], f"Appointment rescheduled to {when_text(n)}",
                f"Hello {a['patient_name']},\n\nYour {a['type_name']} appointment has moved.\n\n"
                f"  Was: {when_text(a)} with {a['clinician_name']}\n  Now: {when_text(n)} with {n['clinician_name']}\n"
                f"  Clinic: {a['clinic_name']}\n  Deposit carried over: {dollars(a['paid_cents'])}\n\nHarborview Physio\n")
        waitlist_trigger(conn, a["clinician_id"], a["local_date"])
        conn.commit()
    send_pending()
    WAKE.set()
    return new


# ---------------------------------------------------------------------------------------- waitlist
def join_waitlist(user: dict, clinician_id: int, code: str, day: date) -> None:
    with db.connect() as conn:
        c, t = avail.context(conn, clinician_id, code)
        if c is None or t is None:
            raise Refused(400, "Unknown clinician or appointment type.")
        today = avail.local_today(tzinfo(c["timezone"]), now_utc())
        if day < today or day > today + timedelta(days=db.HORIZON_DAYS):
            raise Refused(400, "That date is outside the booking window.")
        try:
            conn.execute("INSERT INTO hv_waitlist (patient_id, clinician_id, type_code, on_date) VALUES (%s,%s,%s,%s)",
                         (user["id"], clinician_id, code, day))
        except psycopg.errors.UniqueViolation:
            raise Refused(409, "You are already on the waitlist for that day.") from None
        conn.commit()


def waitlist_trigger(conn, clinician_id: int, day: date) -> None:
    """R9.2: one offer for the first waiting entry (join order) whose type has a slot on that date now."""
    entries = conn.execute("SELECT * FROM hv_waitlist WHERE clinician_id = %s AND on_date = %s AND status = 'waiting'"
                           " ORDER BY joined_at, id FOR UPDATE", (clinician_id, day)).fetchall()
    for e in entries:
        slots = avail.compute_slots(conn, clinician_id, e["type_code"], day, now_utc())
        if not slots:
            continue
        start, room = slots[0]
        c, t = avail.context(conn, clinician_id, e["type_code"])
        tz = tzinfo(c["timezone"])
        fee = avail.fee_on(conn, t["id"], day)
        token = secrets.token_urlsafe(24)
        now = now_utc()
        aid = _insert_appt(conn, e["patient_id"], clinician_id, t["id"], room, start, tz, "offered",
                           half_up(fee["fee_cents"] * fee["deposit_bp"], 10000), now + timedelta(seconds=db.OFFER_TTL))
        conn.execute("UPDATE hv_waitlist SET status = 'offered', offer_appointment_id = %s, offer_token_hash = %s WHERE id = %s",
                     (aid, sha(token), e["id"]))
        a = load_appt(conn, aid)
        expires = (now + timedelta(seconds=db.OFFER_TTL)).astimezone(tz)
        # the link carries the offer token, which is stored only as a hash: the email is sent after commit, never stored
        _pending().append((a["patient_email"], a["patient_name"], f"A slot opened up: {a['type_name']} on {when_text(a)}",
                           f"Hello {a['patient_name']},\n\nA {a['type_name']} slot with {a['clinician_name']} at {a['clinic_name']} "
                           f"opened up on {when_text(a)}.\n\nIt is held for you until {expires.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
                           f"Accept it here: {db.PUBLIC_BASE_URL}/offers/{token}\n\nHarborview Physio\n"))
        return


def offer_by_token(conn, token: str):
    return conn.execute("SELECT * FROM hv_waitlist WHERE offer_token_hash = %s", (sha(token),)).fetchone()


def accept_offer(user: dict, token: str) -> str:
    with db.connect() as conn:
        e = offer_by_token(conn, token)
        if e is None:
            raise Refused(404, "No such offer.")
        if e["patient_id"] != user["id"]:
            raise Refused(403, "This offer belongs to someone else.")
        c = conn.execute("SELECT clinic_id FROM clinicians WHERE id = %s", (e["clinician_id"],)).fetchone()
        db.lock_clinic(conn, c["clinic_id"])
        e = offer_by_token(conn, token)
        a = load_appt(conn, e["offer_appointment_id"], lock=True)
        raw = conn.execute("SELECT status FROM appointments WHERE id = %s", (a["id"],)).fetchone()["status"]
        if e["status"] != "offered" or raw != "offered" or a["expires_at"] <= now_utc():
            raise Refused(409, "This offer has expired or was declined.")
        fee = avail.fee_on(conn, a["type_id"], a["local_date"])  # R3.4: the deposit is fixed when the hold is created
        deposit = half_up(fee["fee_cents"] * fee["deposit_bp"], 10000) if fee else a["deposit_cents"]
        conn.execute("UPDATE appointments SET status = 'held', deposit_cents = %s WHERE id = %s", (deposit, a["id"]))
        conn.execute("UPDATE hv_appt SET expires_at = %s WHERE appointment_id = %s",
                     (now_utc() + timedelta(seconds=db.HOLD_TTL), a["id"]))
        conn.execute("UPDATE hv_waitlist SET status = 'booked' WHERE id = %s", (e["id"],))
        conn.commit()
        aid = a["id"]
    return checkout_for(aid, user)


def decline_offer(user: dict, token: str) -> None:
    clear_pending()
    with db.connect() as conn:
        e = offer_by_token(conn, token)
        if e is None:
            raise Refused(404, "No such offer.")
        if e["patient_id"] != user["id"]:
            raise Refused(403, "This offer belongs to someone else.")
        c = conn.execute("SELECT clinic_id FROM clinicians WHERE id = %s", (e["clinician_id"],)).fetchone()
        db.lock_clinic(conn, c["clinic_id"])
        e = offer_by_token(conn, token)
        a = load_appt(conn, e["offer_appointment_id"], lock=True)
        raw = conn.execute("SELECT status FROM appointments WHERE id = %s", (a["id"],)).fetchone()["status"]
        if e["status"] != "offered" or raw != "offered" or a["expires_at"] <= now_utc():
            raise Refused(409, "This offer has already expired or been answered.")
        conn.execute("UPDATE appointments SET status = 'released' WHERE id = %s", (a["id"],))
        conn.execute("UPDATE hv_waitlist SET status = 'declined' WHERE id = %s", (e["id"],))
        waitlist_trigger(conn, e["clinician_id"], e["on_date"])
        conn.commit()
    send_pending()
    WAKE.set()


# ------------------------------------------------------------------------------------------ sweeper
def sweep_once() -> None:
    with db.connect() as conn:
        due = conn.execute("SELECT a.id, c.clinic_id FROM appointments a JOIN hv_appt h ON h.appointment_id = a.id"
                           " JOIN clinicians c ON c.id = a.clinician_id"
                           " WHERE a.status IN ('held', 'offered') AND h.expires_at <= now() ORDER BY h.expires_at LIMIT 50").fetchall()
    for d in due:
        clear_pending()
        with db.connect() as conn:
            db.lock_clinic(conn, d["clinic_id"])
            a = load_appt(conn, d["id"], lock=True)
            raw = conn.execute("SELECT status FROM appointments WHERE id = %s", (d["id"],)).fetchone()["status"]
            if raw not in ("held", "offered") or a["expires_at"] > now_utc():
                continue
            conn.execute("UPDATE appointments SET status = 'expired' WHERE id = %s", (d["id"],))
            if raw == "held":
                conn.execute("UPDATE hv_appt SET expire_session_pending = true WHERE appointment_id = %s", (d["id"],))
            else:
                conn.execute("UPDATE hv_waitlist SET status = 'expired' WHERE offer_appointment_id = %s AND status = 'offered'", (d["id"],))
            waitlist_trigger(conn, a["clinician_id"], a["local_date"])
            conn.commit()
        send_pending()
        WAKE.set()
    _expire_sessions()


def _expire_sessions() -> None:
    with db.connect() as conn:
        rows = conn.execute("SELECT appointment_id, session_id FROM hv_appt WHERE expire_session_pending").fetchall()
        for r in rows:
            if r["session_id"]:
                provider.expire_session(r["session_id"])
            conn.execute("UPDATE hv_appt SET expire_session_pending = false WHERE appointment_id = %s", (r["appointment_id"],))
            conn.commit()


def refunds_once() -> None:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM hv_appt WHERE refund_status = 'pending' AND NOT refund_accepted"
                            " AND refund_cents > 0 AND (refund_next_at IS NULL OR refund_next_at <= now())"
                            " ORDER BY appointment_id LIMIT 20").fetchall()
    for r in rows:
        try:
            provider.create_refund(r["payment_intent"], r["refund_cents"], r["appointment_id"], r["refund_key"])
            ok = True
        except provider.ProviderError:
            ok = False
        with db.connect() as conn:
            if ok:
                conn.execute("UPDATE hv_appt SET refund_accepted = true WHERE appointment_id = %s", (r["appointment_id"],))
            else:
                conn.execute("UPDATE hv_appt SET refund_next_at = now() + interval '3 seconds' WHERE appointment_id = %s",
                             (r["appointment_id"],))
            conn.commit()


def send_now(to: str, name: str, subject: str, body: str) -> None:
    """Send one email immediately, without storing it. Used for password links, whose token must not be
    written anywhere but the email itself (R14.5)."""
    msg = EmailMessage()
    msg["From"] = db.ENV.get("MAIL_FROM", "bookings@harborviewphysio.example")
    msg["To"] = formataddr((one_line(name or ""), norm_email(to)))
    msg["Subject"] = one_line(subject)
    msg["Message-ID"] = make_msgid(domain="harborview.local")
    msg.set_content(body)
    try:
        with smtplib.SMTP(db.ENV["SMTP_HOST"], int(db.ENV["SMTP_PORT"]), timeout=10) as smtp:
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException):
        pass  # the visitor can request another link; the response never reveals whether one was sent


def outbox_once() -> None:
    with db.connect() as conn:
        while True:
            rows = conn.execute("SELECT * FROM hv_outbox WHERE sent_at IS NULL ORDER BY id LIMIT 50 FOR UPDATE SKIP LOCKED").fetchall()
            if not rows:
                conn.commit()
                return
            with smtplib.SMTP(db.ENV["SMTP_HOST"], int(db.ENV["SMTP_PORT"]), timeout=10) as smtp:
                for r in rows:
                    msg = EmailMessage()
                    msg["From"] = db.ENV.get("MAIL_FROM", "bookings@harborviewphysio.example")
                    msg["To"] = formataddr((one_line(r["to_name"] or ""), r["to_addr"]))
                    msg["Subject"] = one_line(r["subject"])
                    msg["Message-ID"] = make_msgid(idstring=sha(r["dedupe_key"])[:16], domain="harborview.local")
                    msg.set_content(r["body"])
                    smtp.send_message(msg)
                    conn.execute("UPDATE hv_outbox SET sent_at = now() WHERE id = %s", (r["id"],))
            conn.commit()


def _loop(fn, event: threading.Event | None, period: float) -> None:
    while True:
        if event is not None:
            event.wait(period)
            event.clear()
        else:
            time.sleep(period)
        try:
            fn()
        except psycopg.OperationalError:
            time.sleep(1)
        except Exception:
            traceback.print_exc()
            time.sleep(1)


def start_workers() -> None:
    threading.Thread(target=provider.register_webhook, daemon=True).start()
    threading.Thread(target=_loop, args=(sweep_once, None, 0.5), daemon=True).start()
    threading.Thread(target=_loop, args=(refunds_once, REFUND_WAKE, 1.0), daemon=True).start()
    threading.Thread(target=_loop, args=(outbox_once, WAKE, 1.0), daemon=True).start()
    WAKE.set()
    REFUND_WAKE.set()


# ------------------------------------------------------------------------------------------ reminders
def day_sheet(conn, clinic_id: int, day: date, clinician_id: int | None = None) -> list[dict]:
    lo, hi = datetime.combine(day, datetime.min.time()), datetime.combine(day + timedelta(days=1), datetime.min.time())
    rows = conn.execute(APPT_SQL + " WHERE c.clinic_id = %s AND a.local_start >= %s AND a.local_start < %s"
                        " AND a.status IN ('booked', 'completed', 'no_show', 'confirmed')"
                        + (" AND a.clinician_id = %s" if clinician_id else ""),
                        (clinic_id, lo, hi) + ((clinician_id,) if clinician_id else ())).fetchall()
    out = []
    for r in rows:
        r = enrich(r)
        if (r["app"] and r["status"] == "confirmed") or (not r["app"] and r["status"] in avail.LEGACY_OCCUPYING):
            if r["local_date"] == day:
                out.append(r)
    return sorted(out, key=lambda r: (r["start"], r["id"]))


def send_reminders(clinic_id: int, day: date) -> int:
    with db.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(73200, %s)", (clinic_id,))
        n = 0
        for a in day_sheet(conn, clinic_id, day):
            if a["status"] not in ("booked", "confirmed") or not a["patient_active"]:
                continue
            if conn.execute("INSERT INTO hv_reminders (appointment_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING appointment_id",
                            (a["id"],)).fetchone() is None:
                continue
            loc = a["start"].astimezone(a["tz"])
            enqueue(conn, f"remind:{a['id']}", a["patient_email"], f"Appointment reminder: {loc.strftime('%Y-%m-%d %H:%M')}",
                    f"Hello {a['patient_name']},\n\nThis is a reminder of your {a['type_name']} appointment with "
                    f"{a['clinician_name']} at {a['clinic_name']} on {loc.strftime('%Y-%m-%d')} at {loc.strftime('%H:%M')} "
                    f"({loc.tzname()}).\n\nHarborview Physio\n")
            n += 1
        conn.commit()
    WAKE.set()
    return n


def book_for_patient(email: str, clinician_id: int, code: str, start_s: str) -> tuple[int, str]:
    """R6.7: front desk holds a slot for a patient account and emails the payment link."""
    with db.connect() as conn:
        p = account_for(conn, email)
    if p is None or p["role"] != "patient":
        raise Refused(400, "That address does not belong to a patient account.")
    aid, url = book(p, clinician_id, code, start_s)
    with db.connect() as conn:
        a = load_appt(conn, aid)
        exp = a["expires_at"].astimezone(a["tz"])
        enqueue(conn, f"paylink:{aid}", p["email"], f"Pay your deposit: {a['type_name']} on {when_text(a)}",
                f"Hello {p['full_name']},\n\nOur front desk has held a {a['type_name']} appointment for you with "
                f"{a['clinician_name']} at {a['clinic_name']} on {when_text(a)}.\n\nPay the {dollars(a['deposit_cents'])} deposit "
                f"here to confirm it: {url}\n\nThe slot is held until {exp.strftime('%Y-%m-%d %H:%M:%S %Z')}; after that it is "
                "released.\n\nHarborview Physio\n", name=p["full_name"])
        conn.commit()
    WAKE.set()
    return aid, url


EXPORT_HEADER = ["appointment_id", "local_date", "local_time", "utc_start", "clinician_id", "clinician_name", "patient_name",
                 "patient_email", "type", "status", "deposit_cents", "refund_cents"]


def export_rows(conn, clinic_id: int, d0: date, d1: date) -> list[list]:
    lo = datetime.combine(d0, datetime.min.time())
    hi = datetime.combine(d1 + timedelta(days=1), datetime.min.time())
    rows = conn.execute(APPT_SQL + " WHERE c.clinic_id = %s AND a.local_start >= %s AND a.local_start < %s", (clinic_id, lo, hi)).fetchall()
    out = []
    for r in rows:
        r = enrich(r)
        ok = r["status"] in ("confirmed", "cancelled", "rescheduled") if r["app"] else r["status"] in ("booked", "completed", "no_show", "cancelled")
        if not ok or not (d0 <= r["local_date"] <= d1):
            continue
        loc = r["start"].astimezone(r["tz"])
        out.append((r["start"], r["id"], [r["id"], loc.strftime("%Y-%m-%d"), loc.strftime("%H:%M"), r["start"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                                          r["clinician_id"], safe_cell(r["clinician_name"]), safe_cell(r["patient_name"]),
                                          norm_email(r["patient_email"]), r["code"], r["status"], r["deposit_cents"],
                                          (r["refund_cents"] or 0) if r["app"] else 0]))
    return [x[2] for x in sorted(out, key=lambda x: (x[0], x[1]))]


def safe_cell(v: str) -> str:
    return "'" + v if v and v[0] in "=+-@\t\r" else v


def json_dumps(x) -> str:
    return json.dumps(x, default=str)
