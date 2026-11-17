"""Availability (R4, R5): working time per local date, occupying appointments, rooms, slots."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db

UTC = timezone.utc
STEP = timedelta(minutes=15)
LEGACY_OCCUPYING = ("booked", "completed", "no_show")
_ZI: dict[str, ZoneInfo] = {}


def tzinfo(name: str) -> ZoneInfo:
    z = _ZI.get(name)
    if z is None:
        z = _ZI[name] = ZoneInfo(name)
    return z


def to_minutes(t) -> int | None:
    """'HH:MM[:SS]' text -> minutes after midnight ('24:00:00' -> 1440). Times are read as text because
    PostgreSQL's time type allows 24:00:00, which Python's time cannot hold."""
    if t is None:
        return None
    h, m = str(t).split(":")[:2]
    return int(h) * 60 + int(m)


def wall_to_utc(day: date, minutes: int, tz: ZoneInfo, fold: int = 0) -> datetime:
    """R4.4: a repeated wall time is its first occurrence (fold 0); a missing one keeps the earlier offset."""
    if minutes >= 1440:
        day, minutes = day + timedelta(days=1), minutes - 1440
    return datetime.combine(day, time(minutes // 60, minutes % 60)).replace(tzinfo=tz, fold=fold).astimezone(UTC)


def local_naive_fold(instant: datetime, tz: ZoneInfo) -> tuple[datetime, int]:
    loc = instant.astimezone(tz)
    return loc.replace(tzinfo=None), loc.fold


def legacy_instant(local_start: datetime, fold: int, tz: ZoneInfo) -> datetime:
    return local_start.replace(tzinfo=tz, fold=fold).astimezone(UTC)


def local_today(tz: ZoneInfo, now: datetime) -> date:
    return now.astimezone(tz).date()


# ---------------------------------------------------------------------------------- working time
def _union(ranges):
    out = []
    for a, b in sorted(r for r in ranges if r[1] > r[0]):
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def _minus(ranges, cuts):
    res = list(ranges)
    for c0, c1 in cuts:
        if c1 <= c0:
            continue
        nxt = []
        for a, b in res:
            if c1 <= a or c0 >= b:
                nxt.append((a, b))
                continue
            if a < c0:
                nxt.append((a, c0))
            if c1 < b:
                nxt.append((c1, b))
        res = nxt
    return _union(res)


def working_ranges(conn, clinician_id: int, day: date, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    ver = conn.execute(
        "SELECT id FROM schedule_versions WHERE clinician_id = %s AND effective_from <= %s"
        " ORDER BY effective_from DESC, id DESC LIMIT 1", (clinician_id, day)).fetchone()
    spans: list[tuple[int, int]] = []
    if ver:
        for w in conn.execute("SELECT start_time::text AS s, end_time::text AS e FROM schedule_windows"
                              " WHERE version_id = %s AND weekday = %s", (ver["id"], day.isoweekday())):
            spans.append((to_minutes(w["s"]), to_minutes(w["e"])))
    offs: list[tuple[int, int]] = []
    for x in conn.execute("SELECT kind, start_time::text AS s, end_time::text AS e FROM schedule_exceptions"
                          " WHERE clinician_id = %s AND on_date = %s", (clinician_id, day)):
        s = to_minutes(x["s"])
        e = to_minutes(x["e"])
        if x["kind"] == "off" and s is None and e is None:
            return []
        if s is None or e is None:
            continue
        (spans if x["kind"] == "extra" else offs).append((s, e))

    def inst(s, e):
        return (wall_to_utc(day, s, tz), wall_to_utc(day, e, tz))

    work = _union([inst(s, e) for s, e in spans if e > s])
    return _minus(work, [inst(s, e) for s, e in offs if e > s])


# ---------------------------------------------------------------------------- occupying appointments
def occupying_rows(conn, day: date, clinician_id: int | None, room_ids: list[int], now: datetime):
    """Occupying appointments of the clinician or in the rooms whose local start is within a day of `day`."""
    lo = datetime.combine(day - timedelta(days=2), time(0))
    hi = datetime.combine(day + timedelta(days=3), time(0))
    rows = conn.execute(
        """SELECT a.id, a.clinician_id, a.room_id, a.local_start, a.fold, a.status, t.duration_minutes, t.buffer_minutes,
                  c.clinic_id, cl.timezone, h.appointment_id IS NOT NULL AS app, h.start_at, h.expires_at
             FROM appointments a
             JOIN appointment_types t ON t.id = a.type_id
             JOIN clinicians c ON c.id = a.clinician_id
             JOIN clinics cl ON cl.id = c.clinic_id
             LEFT JOIN hv_appt h ON h.appointment_id = a.id
            WHERE a.local_start >= %s AND a.local_start < %s AND (a.clinician_id = %s OR a.room_id = ANY(%s))""",
        (lo, hi, clinician_id if clinician_id is not None else -1, room_ids)).fetchall()
    out = []
    for r in rows:
        if r["app"]:
            if r["status"] == "confirmed":
                pass
            elif r["status"] in ("held", "offered") and r["expires_at"] is not None and r["expires_at"] > now:
                pass
            else:
                continue
            start = r["start_at"].astimezone(UTC)
        else:
            if r["status"] not in LEGACY_OCCUPYING:
                continue
            start = legacy_instant(r["local_start"], r["fold"], tzinfo(r["timezone"]))
        end = start + timedelta(minutes=r["duration_minutes"] + r["buffer_minutes"])
        out.append({"id": r["id"], "clinician_id": r["clinician_id"], "room_id": r["room_id"], "start": start, "busy_end": end})
    return out


def context(conn, clinician_id: int, code: str):
    c = conn.execute("SELECT c.*, cl.timezone, cl.name AS clinic_name FROM clinicians c JOIN clinics cl ON cl.id = c.clinic_id"
                     " WHERE c.id = %s", (clinician_id,)).fetchone()
    t = conn.execute("SELECT * FROM appointment_types WHERE code = %s", (code,)).fetchone()
    return c, t


def fee_on(conn, type_id: int, day: date):
    return conn.execute("SELECT fee_cents, deposit_bp FROM type_fees WHERE type_id = %s AND effective_from <= %s"
                        " ORDER BY effective_from DESC LIMIT 1", (type_id, day)).fetchone()


def compute_slots(conn, clinician_id: int, code: str, day: date, now: datetime | None = None,
                  ignore_id: int | None = None) -> list[tuple[datetime, int]]:
    """[(start, room_id)] for every slot of R5.1 (room = the lowest-id free room, R6.1)."""
    now = now or datetime.now(UTC)
    c, t = context(conn, clinician_id, code)
    if c is None or t is None or not c["active"] or not t["active"]:
        return []
    tz = tzinfo(c["timezone"])
    if day > local_today(tz, now) + timedelta(days=db.HORIZON_DAYS):
        return []
    if fee_on(conn, t["id"], day) is None:
        return []
    if conn.execute("SELECT 1 FROM clinic_closures WHERE clinic_id = %s AND on_date = %s", (c["clinic_id"], day)).fetchone():
        return []
    rooms = [r["id"] for r in conn.execute("SELECT id FROM rooms WHERE clinic_id = %s AND room_type = %s AND active ORDER BY id",
                                           (c["clinic_id"], t["room_type"]))]
    if not rooms:
        return []
    occ = [o for o in occupying_rows(conn, day, clinician_id, rooms, now) if o["id"] != ignore_id]
    mine = [o for o in occ if o["clinician_id"] == clinician_id]
    if sum(1 for o in mine if o["start"].astimezone(tz).date() == day) >= c["max_per_day"]:
        return []
    by_room = {r: [o for o in occ if o["room_id"] == r] for r in rooms}
    dur = timedelta(minutes=t["duration_minutes"])
    busy = timedelta(minutes=t["duration_minutes"] + t["buffer_minutes"])
    earliest = now + timedelta(minutes=db.NOTICE_MIN)
    out = []
    for a, b in working_ranges(conn, clinician_id, day, tz):
        epoch = int(a.timestamp())
        s = datetime.fromtimestamp(-(-epoch // 900) * 900, UTC)
        while s + dur <= b:
            e = s + busy
            if s >= earliest and not any(o["start"] < e and s < o["busy_end"] for o in mine):
                for r in rooms:
                    if not any(o["start"] < e and s < o["busy_end"] for o in by_room[r]):
                        out.append((s, r))
                        break
            s += STEP
    return out


def iso_local(instant: datetime, tz: ZoneInfo) -> str:
    return instant.astimezone(tz).isoformat()


def label(instant: datetime, tz: ZoneInfo) -> str:
    loc = instant.astimezone(tz)
    return loc.strftime("%a %Y-%m-%d %H:%M ") + (loc.tzname() or "")
