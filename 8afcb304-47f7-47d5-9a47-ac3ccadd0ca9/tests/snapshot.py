#!/usr/bin/env python3
"""Snapshot the pre-existing records before the submission runs (baseline for the model and R12)."""

import json
import sys

import psycopg

TABLES = {
    "clinics": "SELECT id, name, timezone FROM clinics ORDER BY id",
    "clinic_closures": "SELECT id, clinic_id, on_date::text AS on_date, reason FROM clinic_closures ORDER BY id",
    "rooms": "SELECT id, clinic_id, name, room_type, active FROM rooms ORDER BY id",
    "users": "SELECT id, email, full_name, role, clinic_id, active FROM users ORDER BY id",
    "clinicians": "SELECT id, user_id, clinic_id, max_per_day, active FROM clinicians ORDER BY id",
    "appointment_types": "SELECT id, code, name, duration_minutes, buffer_minutes, room_type, active FROM appointment_types ORDER BY id",
    "type_fees": "SELECT id, type_id, effective_from::text AS effective_from, fee_cents, deposit_bp FROM type_fees ORDER BY id",
    "schedule_versions": "SELECT id, clinician_id, effective_from::text AS effective_from FROM schedule_versions ORDER BY id",
    "schedule_windows": "SELECT id, version_id, weekday, to_char(start_time, 'HH24:MI') AS start_time, "
                        "CASE WHEN end_time = '24:00' THEN '24:00' ELSE to_char(end_time, 'HH24:MI') END AS end_time "
                        "FROM schedule_windows ORDER BY id",
    "schedule_exceptions": "SELECT id, clinician_id, on_date::text AS on_date, kind, to_char(start_time, 'HH24:MI') AS start_time, "
                           "CASE WHEN end_time = '24:00' THEN '24:00' ELSE to_char(end_time, 'HH24:MI') END AS end_time "
                           "FROM schedule_exceptions ORDER BY id",
    "appointments": "SELECT id, patient_id, clinician_id, type_id, room_id, to_char(local_start, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS local_start, "
                    "fold, status, deposit_cents FROM appointments ORDER BY id",
    "clinical_notes": "SELECT id, appointment_id, author_id, body FROM clinical_notes ORDER BY id",
}


def main(db_url: str, out: str) -> None:
    with psycopg.connect(db_url, connect_timeout=10) as c:
        data = {}
        for name, sql in TABLES.items():
            cur = c.execute(sql)
            cols = [d.name for d in cur.description]
            data[name] = [dict(zip(cols, r)) for r in cur.fetchall()]
    with open(out, "w") as fh:
        json.dump(data, fh, default=str)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
