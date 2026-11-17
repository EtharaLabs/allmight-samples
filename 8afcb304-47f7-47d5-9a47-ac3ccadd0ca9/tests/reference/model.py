"""Verifier-side model of Harborview Physio: what every slot list, room, deposit and refund must be.

Built from the baseline snapshot taken before the submission ran, plus every clinician, schedule,
type, fee, room and booking the verifier itself created through the application. It never reads
the submission's own tables.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # importable standalone (Reward Kit)

from reference import engine as E  # noqa: E402


def norm(email: str) -> str:
    return (email or "").strip().lower()


class Model:
    def __init__(self, baseline: dict, notice_min: int, horizon_days: int):
        self.notice, self.horizon = notice_min, horizon_days
        self.w = E.World()
        self.clinic_names = {}
        for c in baseline["clinics"]:
            self.w.clinics[c["id"]] = c["timezone"]
            self.clinic_names[c["id"]] = c["name"]
        for x in baseline.get("clinic_closures", []):
            self.w.closures.add((x["clinic_id"], date.fromisoformat(x["on_date"])))
        for r in baseline["rooms"]:
            self.w.rooms[r["id"]] = E.Room(r["id"], r["clinic_id"], r["room_type"], r["active"], r["name"])
        for t in baseline["appointment_types"]:
            self.w.add_type(E.ApptType(t["id"], t["code"], t["name"], t["duration_minutes"], t["buffer_minutes"],
                                       t["room_type"], t["active"]))
        for f in baseline["type_fees"]:
            self.w.type_by_id[f["type_id"]].fees.append((date.fromisoformat(f["effective_from"]), f["fee_cents"], f["deposit_bp"]))
        self.users = {u["id"]: dict(u) for u in baseline["users"]}
        for c in baseline["clinicians"]:
            self.w.clinicians[c["id"]] = E.Clinician(c["id"], c["user_id"], c["clinic_id"], c["max_per_day"], c["active"],
                                                     name=self.users[c["user_id"]]["full_name"])
        self.legacy_clinicians = {c["id"] for c in baseline["clinicians"]}
        vers = {}
        for v in baseline["schedule_versions"]:
            vers[v["id"]] = E.Version(date.fromisoformat(v["effective_from"]), v["id"], [])
            self.w.clinicians[v["clinician_id"]].versions.append(vers[v["id"]])
        for x in baseline["schedule_windows"]:
            vers[x["version_id"]].windows.append((x["weekday"], E.hm(x["start_time"]), E.hm(x["end_time"])))
        for x in baseline["schedule_exceptions"]:
            self.w.clinicians[x["clinician_id"]].exceptions.append(E.Exception_(
                date.fromisoformat(x["on_date"]), x["kind"],
                E.hm(x["start_time"]) if x["start_time"] else None, E.hm(x["end_time"]) if x["end_time"] else None))
        self.legacy_appts = {}
        for a in baseline["appointments"]:
            c = self.w.clinicians[a["clinician_id"]]
            tz = self.w.clinics[c.clinic_id]
            ls = datetime.fromisoformat(a["local_start"])
            start = int(ls.replace(tzinfo=E.zone(tz), fold=a["fold"]).timestamp())
            ap = E.Appt(a["id"], a["clinician_id"], self.w.type_by_id[a["type_id"]].code, a["room_id"], start, a["status"],
                        legacy=True, patient_id=a["patient_id"], deposit=a["deposit_cents"])
            self.w.add_appt(ap)
            self.legacy_appts[a["id"]] = a
        self.max_legacy_appt = max(self.legacy_appts) if self.legacy_appts else 0
        self.notes = {}
        for n in baseline["clinical_notes"]:
            self.notes.setdefault(n["appointment_id"], []).append(n)
        self._vid = 10**8

    # -- accounts ------------------------------------------------------------------------------
    def account_for(self, email: str) -> int | None:
        n = norm(email)
        ids = sorted(u["id"] for u in self.users.values() if norm(u["email"]) == n and u["active"])
        return ids[0] if ids else None

    def tz_of(self, cid: int) -> str:
        return self.w.clinics[self.w.clinicians[cid].clinic_id]

    # -- verifier-created reference data -------------------------------------------------------
    def add_clinician(self, cid: int, uid: int, clinic_id: int, max_per_day: int, email: str, name: str) -> None:
        self.w.clinicians[cid] = E.Clinician(cid, uid, clinic_id, max_per_day, True, name=name)
        self.users[uid] = {"id": uid, "email": email, "full_name": name, "role": "clinician", "active": True}

    def add_version(self, cid: int, eff: date, windows_text: str) -> None:
        self._vid += 1
        self.w.clinicians[cid].versions.append(E.Version(eff, self._vid, E.parse_windows(windows_text)))

    def add_exception(self, cid: int, on: date, kind: str, start: str | None, end: str | None) -> None:
        self.w.clinicians[cid].exceptions.append(E.Exception_(on, kind, E.hm(start) if start else None, E.hm(end) if end else None))

    def add_type(self, tid: int, code: str, name: str, dur: int, buf: int, room_type: str, fee: int, bp: int, eff: date) -> None:
        self.w.add_type(E.ApptType(tid, code, name, dur, buf, room_type, True, [(eff, fee, bp)]))

    def add_fee(self, code: str, eff: date, fee: int, bp: int) -> None:
        self.w.types[code].fees.append((eff, fee, bp))

    def add_closure(self, clinic_id: int, on: date) -> None:
        self.w.closures.add((clinic_id, on))

    def add_room(self, rid: int, clinic_id: int, room_type: str, name: str = "") -> None:
        self.w.rooms[rid] = E.Room(rid, clinic_id, room_type, True, name)

    # -- availability --------------------------------------------------------------------------
    def slots(self, cid: int, code: str, day: date, now: float, ignore: int | None = None) -> list[int]:
        return E.slots(self.w, cid, code, day, now, self.notice, self.horizon, ignore=ignore)

    def deposit_on(self, code: str, day: date) -> int:
        fee, bp = self.w.types[code].fee_on(day)
        return E.deposit(fee, bp)

    def predict_room(self, cid: int, code: str, start: int, now: float, ignore: int | None = None) -> int | None:
        return E.pick_room(self.w, cid, code, start, now, ignore=ignore)

    def local_day(self, cid: int, epoch: float) -> date:
        return E.local_date(int(epoch), self.tz_of(cid))

    def today(self, cid: int, now: float) -> date:
        return self.local_day(cid, now)

    # -- bookings ------------------------------------------------------------------------------
    def add_booking(self, aid: int, cid: int, code: str, room: int | None, start: int, status: str,
                    patient_id: int | None, deposit: int, expires: float | None = None) -> E.Appt:
        a = E.Appt(aid, cid, code, room, start, status, legacy=False, expires=expires, patient_id=patient_id, deposit=deposit)
        self.w.add_appt(a)
        return a

    def set_status(self, aid: int, status: str, expires: float | None = None) -> None:
        a = self.w.appts[aid]
        a.status = status
        a.expires = expires

    def appt(self, aid: int) -> E.Appt:
        return self.w.appts[aid]

    def day_sheet(self, clinic_id: int, day: date) -> list[E.Appt]:
        tz = self.w.clinics[clinic_id]
        out = []
        for a in self.w.appts.values():
            c = self.w.clinicians[a.clinician_id]
            if c.clinic_id != clinic_id or E.local_date(a.start, tz) != day:
                continue
            if (a.legacy and a.status in ("booked", "completed", "no_show")) or (not a.legacy and a.status == "confirmed"):
                out.append(a)
        return sorted(out, key=lambda a: (a.start, a.aid))


def utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, timezone.utc)


def days_from(day: date, n: int) -> date:
    return day + timedelta(days=n)
