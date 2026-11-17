"""Reference availability and money engine for Harborview Physio (hidden from the agent).

A literal reading of instruction.md R3-R8, written independently of solution/app. Instants are
integer epoch seconds. Wall-clock times are minutes after local midnight (0..1440).

    wall_to_epoch(day, minute, tz)            R4.4
    working_time(clinician, day, tz)          R4.1-R4.3 -> sorted disjoint [(a, b)]
    slots(world, clinician_id, code, day, now) R5.1   -> [epoch seconds]
    deposit(fee, bp), refund(paid, seconds_before, by_staff)   R3.4, R7.1-R7.2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

GRID = 900  # 15 minutes
LEGACY_OCCUPYING = {"booked", "completed", "no_show"}
DAY_NAMES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]

_ZONES: dict[str, ZoneInfo] = {}


def zone(tz: str) -> ZoneInfo:
    if tz not in _ZONES:
        _ZONES[tz] = ZoneInfo(tz)
    return _ZONES[tz]


def wall_to_epoch(day: date, minute: int, tz: str, fold: int = 0) -> int:
    """R4.4: first occurrence of a repeated time; gap times read with the offset before the change.

    zoneinfo with fold=0 does exactly that: for a repeated time it picks the earlier offset, and
    for a non-existent time it applies the offset in effect before the transition."""
    if minute == 1440:
        day, minute = day + timedelta(days=1), 0
    dt = datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=zone(tz), fold=fold)
    return int(dt.timestamp())


def local_of(epoch: int, tz: str) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone(zone(tz))


def local_date(epoch: int, tz: str) -> date:
    return local_of(epoch, tz).date()


def iso(epoch: int, tz: str) -> str:
    return local_of(epoch, tz).isoformat()


def parse_instant(s: str) -> int:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("instant without offset")
    return int(dt.timestamp())


# ---------------------------------------------------------------------------------------------- data
@dataclass
class Version:
    effective_from: date
    vid: int
    windows: list  # [(weekday 1..7, start_min, end_min)]


@dataclass
class Exception_:
    on_date: date
    kind: str  # off | extra
    start: int | None
    end: int | None


@dataclass
class Clinician:
    cid: int
    user_id: int
    clinic_id: int
    max_per_day: int
    active: bool = True
    versions: list = field(default_factory=list)
    exceptions: list = field(default_factory=list)
    name: str = ""


@dataclass
class ApptType:
    tid: int
    code: str
    name: str
    duration: int  # minutes
    buffer: int
    room_type: str
    active: bool = True
    fees: list = field(default_factory=list)  # [(effective_from, fee_cents, bp)]

    def fee_on(self, day: date):
        best = None
        for eff, fee, bp in sorted(self.fees):
            if eff <= day:
                best = (fee, bp)
        return best


@dataclass
class Room:
    rid: int
    clinic_id: int
    room_type: str
    active: bool = True
    name: str = ""


@dataclass
class Appt:
    aid: int
    clinician_id: int
    type_code: str
    room_id: int | None
    start: int
    status: str
    legacy: bool = False
    expires: float | None = None  # epoch seconds for held/offered
    patient_id: int | None = None
    deposit: int = 0
    refund: int = 0
    patient_email: str = ""
    patient_name: str = ""


@dataclass
class World:
    clinics: dict = field(default_factory=dict)  # id -> tz
    rooms: dict = field(default_factory=dict)  # id -> Room
    types: dict = field(default_factory=dict)  # code -> ApptType
    clinicians: dict = field(default_factory=dict)  # id -> Clinician
    appts: dict = field(default_factory=dict)  # id -> Appt
    by_clin: dict = field(default_factory=dict)  # clinician id -> [aid]
    by_room: dict = field(default_factory=dict)  # room id -> [aid]
    type_by_id: dict = field(default_factory=dict)
    closures: set = field(default_factory=set)  # (clinic id, local date)

    def add_type(self, t: ApptType) -> None:
        self.types[t.code] = t
        self.type_by_id[t.tid] = t

    def add_appt(self, a: Appt) -> None:
        self.appts[a.aid] = a
        self.by_clin.setdefault(a.clinician_id, []).append(a.aid)
        if a.room_id is not None:
            self.by_room.setdefault(a.room_id, []).append(a.aid)

    def set_room(self, a: Appt, room_id: int | None) -> None:
        if a.room_id is not None and a.aid in self.by_room.get(a.room_id, []):
            self.by_room[a.room_id].remove(a.aid)
        a.room_id = room_id
        if room_id is not None:
            self.by_room.setdefault(room_id, []).append(a.aid)

    def occupying(self, a: Appt, now: float) -> bool:
        if a.legacy:
            return a.status in LEGACY_OCCUPYING
        if a.status == "confirmed":
            return True
        if a.status in ("held", "offered"):
            return a.expires is None or a.expires > now
        return False

    def busy(self, a: Appt) -> tuple[int, int]:
        t = self.types[a.type_code]
        return a.start, a.start + (t.duration + t.buffer) * 60


# ------------------------------------------------------------------------------------- working time
def _merge(iv):
    out = []
    for a, b in sorted(iv):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _subtract(iv, cut):
    out = []
    for a, b in iv:
        pieces = [(a, b)]
        for c, d in cut:
            if d <= c:
                continue
            nxt = []
            for x, y in pieces:
                if d <= x or c >= y:
                    nxt.append((x, y))
                else:
                    if x < c:
                        nxt.append((x, c))
                    if d < y:
                        nxt.append((d, y))
            pieces = nxt
        out.extend(pieces)
    return _merge(out)


def version_on(c: Clinician, day: date):
    best = None
    for v in c.versions:
        if v.effective_from <= day and (best is None or (v.effective_from, v.vid) > (best.effective_from, best.vid)):
            best = v
    return best


def working_time(c: Clinician, day: date, tz: str) -> list[tuple[int, int]]:
    def conv(s, e):
        if s is None or e is None or e <= s:
            return None
        return (wall_to_epoch(day, s, tz), wall_to_epoch(day, e, tz))

    add = []
    v = version_on(c, day)
    if v is not None:
        wd = day.isoweekday()
        for w, s, e in v.windows:
            if w == wd:
                r = conv(s, e)
                if r:
                    add.append(r)
    off, whole_off = [], False
    for x in c.exceptions:
        if x.on_date != day:
            continue
        if x.kind == "extra":
            r = conv(x.start, x.end)
            if r:
                add.append(r)
        elif x.kind == "off":
            if x.start is None and x.end is None:
                whole_off = True
            else:
                r = conv(x.start, x.end)
                if r:
                    off.append(r)
    if whole_off:
        return []
    return _subtract(_merge(add), off)


# ------------------------------------------------------------------------------------------ slots
def _overlap(a, b, c, d) -> bool:
    return a < d and c < b


def slots(w: World, cid: int, code: str, day: date, now: float, notice_min: int, horizon_days: int,
          ignore: int | None = None) -> list[int]:
    c = w.clinicians.get(cid)
    t = w.types.get(code)
    if c is None or t is None or not c.active or not t.active:
        return []
    tz = w.clinics[c.clinic_id]
    if day > local_date(int(now), tz) + timedelta(days=horizon_days):
        return []
    if (c.clinic_id, day) in w.closures:
        return []
    if t.fee_on(day) is None:
        return []
    rooms = sorted(r.rid for r in w.rooms.values() if r.clinic_id == c.clinic_id and r.room_type == t.room_type and r.active)
    if not rooms:
        return []
    mine = [w.appts[a] for a in w.by_clin.get(cid, []) if a != ignore and w.occupying(w.appts[a], now)]
    if sum(1 for a in mine if local_date(a.start, tz) == day) >= c.max_per_day:
        return []
    mine_busy = [w.busy(a) for a in mine]
    room_busy = {r: [w.busy(w.appts[a]) for a in w.by_room.get(r, []) if a != ignore and w.occupying(w.appts[a], now)]
                 for r in rooms}
    dur, total = t.duration * 60, (t.duration + t.buffer) * 60
    earliest = now + notice_min * 60
    out = []
    for a, b in working_time(c, day, tz):
        s = -(-a // GRID) * GRID
        while s + dur <= b:
            if s >= earliest:
                e = s + total
                if not any(_overlap(s, e, x, y) for x, y in mine_busy):
                    if any(not any(_overlap(s, e, x, y) for x, y in room_busy[r]) for r in rooms):
                        out.append(s)
            s += GRID
    return out


def pick_room(w: World, cid: int, code: str, start: int, now: float, ignore: int | None = None) -> int | None:
    c, t = w.clinicians[cid], w.types[code]
    e = start + (t.duration + t.buffer) * 60
    for r in sorted(r.rid for r in w.rooms.values() if r.clinic_id == c.clinic_id and r.room_type == t.room_type and r.active):
        if not any(_overlap(start, e, *w.busy(w.appts[a])) for a in w.by_room.get(r, [])
                   if a != ignore and w.occupying(w.appts[a], now)):
            return r
    return None


# ------------------------------------------------------------------------------------------ money
def half_up_div(num: int, den: int) -> int:
    q, r = divmod(num, den)
    return q + (1 if 2 * r >= den else 0)


def deposit(fee: int, bp: int) -> int:
    return half_up_div(fee * bp, 10000)


def refund(paid: int, seconds_before: float, by_staff: bool = False) -> int:
    if by_staff:
        return paid
    if seconds_before >= 86400:
        return paid
    if seconds_before >= 7200:
        return half_up_div(paid, 2)
    return 0


def parse_windows(text: str) -> list[tuple[int, int, int]]:
    out = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        day, span = line.split()
        a, b = span.split("-")
        out.append((DAY_NAMES.index(day) + 1, hm(a), hm(b)))
    return out


def hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def fmt_hm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"
