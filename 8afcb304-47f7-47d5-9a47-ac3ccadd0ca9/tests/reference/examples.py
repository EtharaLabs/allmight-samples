#!/usr/bin/env python3
"""The instruction's worked examples, reproduced by the reference engine.

Run: python3 tests/reference/examples.py   (exits nonzero on any mismatch; importing it does nothing)
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # importable standalone (Reward Kit)

from reference import engine as E  # noqa: E402

LA, HNL = "America/Los_Angeles", "Pacific/Honolulu"
FAR_PAST = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


def world(windows_by_version, exceptions=(), rooms=((1, "treatment"),), max_per_day=12, clinic_tz=LA):
    w = E.World()
    w.clinics = {1: clinic_tz}
    for rid, rt in rooms:
        w.rooms[rid] = E.Room(rid, 1, rt)
    w.add_type(E.ApptType(1, "FU30", "Follow-up", 30, 10, "treatment", fees=[(date(2024, 1, 1), 9500, 2000)]))
    w.add_type(E.ApptType(2, "FU45", "Extended follow-up", 45, 15, "treatment", fees=[(date(2024, 1, 1), 12550, 2000)]))
    w.add_type(E.ApptType(3, "INIT60", "Initial assessment", 60, 15, "treatment", fees=[(date(2024, 1, 1), 16500, 2000)]))
    w.add_type(E.ApptType(4, "REHAB45", "Gym rehab", 45, 15, "gym", fees=[(date(2024, 1, 1), 11000, 1750)]))
    w.add_type(E.ApptType(5, "T60", "Sixty", 60, 0, "treatment", fees=[(date(2024, 1, 1), 10000, 2000)]))
    for cid in (1, 2, 3):
        c = E.Clinician(cid, 100 + cid, 1, max_per_day)
        for i, (eff, text) in enumerate(windows_by_version):
            c.versions.append(E.Version(eff, i + 1, E.parse_windows(text)))
        c.exceptions = [E.Exception_(*x) for x in exceptions]
        w.clinicians[cid] = c
    return w


def local_hm(epochs, tz=LA):
    return [E.local_of(s, tz).strftime("%H:%M") for s in epochs]


def series(a, b):
    out, m = [], E.hm(a)
    while m <= E.hm(b):
        out.append(E.fmt_hm(m))
        m += 15
    return out


def check(label, got, want):
    if got != want:
        print(f"FAIL {label}: got {got} want {want}")
        return 1
    print(f"ok   {label}")
    return 0


def main() -> int:
    bad = 0
    # 1. grid, fit, buffers
    w = world([(date(2026, 1, 1), "TUE 09:10-12:00")])
    w.add_appt(E.Appt(1, 1, "FU30", 1, E.wall_to_epoch(date(2026, 12, 8), E.hm("10:30"), LA), "confirmed"))
    bad += check("ex1", local_hm(E.slots(w, 1, "FU45", date(2026, 12, 8), FAR_PAST, 120, 9999)), ["09:15", "09:30", "11:15"])
    # 2. versions and exceptions
    w = world([(date(2026, 12, 1), "MON 08:00-12:00\nMON 13:00-17:00"), (date(2027, 1, 4), "MON 09:00-15:00")],
              exceptions=[(date(2026, 12, 14), "off", E.hm("10:00"), E.hm("11:00")), (date(2026, 12, 21), "off", None, None),
                          (date(2026, 12, 28), "extra", E.hm("12:00"), E.hm("13:00"))])
    s = lambda d: local_hm(E.slots(w, 1, "INIT60", d, FAR_PAST, 120, 9999))  # noqa: E731
    bad += check("ex2 12-07", s(date(2026, 12, 7)), series("08:00", "11:00") + series("13:00", "16:00"))
    bad += check("ex2 12-14", s(date(2026, 12, 14)), series("08:00", "09:00") + ["11:00"] + series("13:00", "16:00"))
    bad += check("ex2 12-21", s(date(2026, 12, 21)), [])
    bad += check("ex2 12-28", s(date(2026, 12, 28)), series("08:00", "16:00"))
    bad += check("ex2 01-04", s(date(2027, 1, 4)), series("09:00", "14:00"))
    bad += check("ex2 counts", [len(s(date(2026, 12, 7))), len(s(date(2026, 12, 14))), len(s(date(2026, 12, 28))), len(s(date(2027, 1, 4)))],
                 [26, 19, 33, 21])
    # 3. rooms and the daily limit
    w = world([(date(2026, 1, 1), "WED 08:00-17:00")], rooms=((21, "gym"), (24, "gym")))
    d = date(2026, 12, 9)
    for aid, cid, t in ((1, 1, "10:00"), (2, 2, "10:30")):
        st = E.wall_to_epoch(d, E.hm(t), LA)
        room = E.pick_room(w, cid, "REHAB45", st, FAR_PAST)
        w.add_appt(E.Appt(aid, cid, "REHAB45", room, st, "confirmed"))
    bad += check("ex3 rooms", [w.appts[1].room_id, w.appts[2].room_id], [21, 24])
    c_slots = local_hm(E.slots(w, 3, "REHAB45", d, FAR_PAST, 120, 9999))
    bad += check("ex3 gap", [x for x in series("09:00", "11:15") if x not in c_slots], series("09:45", "10:45"))
    bad += check("ex3 pick", E.pick_room(w, 3, "REHAB45", E.wall_to_epoch(d, E.hm("11:00"), LA), FAR_PAST), 21)
    w2 = world([(date(2026, 1, 1), "WED 08:00-17:00")], max_per_day=2)
    for aid, t in ((1, "08:00"), (2, "15:00")):
        w2.add_appt(E.Appt(aid, 1, "FU30", 1, E.wall_to_epoch(d, E.hm(t), LA), "confirmed"))
    bad += check("ex3 daily limit", E.slots(w2, 1, "FU30", d, FAR_PAST, 120, 9999), [])
    # R4.4 on clock-change days (rule checks; the spec states the rule without these examples)
    w = world([(date(2026, 1, 1), "SUN 00:00-04:00")])
    got = E.slots(w, 1, "T60", date(2027, 3, 14), FAR_PAST, 120, 9999)
    bad += check("gap local", [E.local_of(x, LA).strftime("%H:%M%z") for x in got],
                 [f"{h}-0800" for h in series("00:00", "01:45")] + ["03:00-0700"])
    bad += check("gap utc", [datetime.fromtimestamp(x, timezone.utc).strftime("%H:%M") for x in (got[0], got[-1])], ["08:00", "10:00"])
    w = world([(date(2026, 1, 1), "SUN 02:30-05:00")])
    wt = E.working_time(w.clinicians[1], date(2027, 3, 14), LA)
    bad += check("gap window", [E.local_of(x, LA).strftime("%H:%M%z") for x in wt[0]], ["03:30-0700", "05:00-0700"])
    # R4.4, clocks set back
    w = world([(date(2026, 1, 1), "SUN 00:00-03:00")])
    got = E.slots(w, 1, "T60", date(2026, 11, 1), FAR_PAST, 120, 9999)
    bad += check("fold local", [E.local_of(x, LA).strftime("%H:%M%z") for x in got],
                 [f"{h}-0700" for h in series("00:00", "01:45")] + [f"{h}-0800" for h in series("01:00", "02:00")])
    bad += check("fold count", len(got), 13)
    w = world([(date(2026, 1, 1), "SUN 01:30-03:00")])
    wt = E.working_time(w.clinicians[1], date(2026, 11, 1), LA)
    bad += check("fold window", datetime.fromtimestamp(wt[0][0], timezone.utc).strftime("%H:%M"), "08:30")
    # 4. deposits and refund bands (R3.4, R7.1, R7.2)
    bad += check("ex4 deposit", E.deposit(12550, 2000), 2510)
    bad += check("ex4 refunds", [E.refund(2510, 30 * 3600), E.refund(2510, 5 * 3600), E.refund(2510, 90 * 60),
                                 E.refund(2510, 90 * 60, by_staff=True)], [2510, 1255, 0, 2510])
    bad += check("bands", [E.refund(1000, 86400), E.refund(1000, 86399), E.refund(1000, 7200), E.refund(1000, 7199)],
                 [1000, 500, 500, 0])
    # rule checks beyond the spec's examples: rounding ties, elapsed time across clock changes
    bad += check("tie deposits", [E.deposit(9900, 1750), E.refund(1733, 5 * 3600)], [1733, 867])
    appt = E.wall_to_epoch(date(2027, 3, 14), E.hm("09:30"), LA)
    cancel = E.wall_to_epoch(date(2027, 3, 13), E.hm("09:00"), LA)
    bad += check("elapsed forward", (appt - cancel) / 3600, 23.5)
    bad += check("elapsed forward refund", E.refund(2000, appt - cancel), 1000)
    appt = E.wall_to_epoch(date(2026, 11, 1), E.hm("09:30"), LA)
    cancel = E.wall_to_epoch(date(2026, 10, 31), E.hm("10:00"), LA)
    bad += check("elapsed back", (appt - cancel) / 3600, 24.5)
    bad += check("elapsed back refund", E.refund(2000, appt - cancel), 2000)
    # R8.4 reschedule carries the deposit
    bad += check("R8.4", [E.deposit(9500, 2000), E.deposit(10000, 2000), E.refund(1900, 5 * 3600)], [1900, 2000, 950])
    w = world([(date(2026, 1, 1), "WED 08:00-17:00")], rooms=((1, "treatment"),))
    st = E.wall_to_epoch(d, E.hm("10:00"), LA)
    w.add_appt(E.Appt(1, 1, "FU30", 1, st, "confirmed"))
    moved = E.wall_to_epoch(d, E.hm("10:15"), LA)
    bad += check("R8.4 self", (moved in E.slots(w, 1, "FU30", d, FAR_PAST, 120, 9999, ignore=1),
                               moved in E.slots(w, 1, "FU30", d, FAR_PAST, 120, 9999)), (True, False))
    print("all examples reproduce" if not bad else f"{bad} example(s) differ")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
