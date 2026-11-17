#!/usr/bin/env python3
"""Snapshot the pre-existing records before the submission runs (baseline for the model and R10)."""

import json
import sys

import psycopg


def main(db_url: str, out: str) -> None:
    with psycopg.connect(db_url, connect_timeout=10) as c:
        def rows(sql):
            cur = c.execute(sql)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        data = {
            "locations": {r["id"]: r for r in rows("SELECT id, name, timezone, ruleset FROM locations")},
            "employees": rows("SELECT id, email, full_name, location_id, manager_id, role, active, hired_on::text AS hired_on FROM employees ORDER BY id"),
            "job_rates": rows("SELECT id, employee_id, job_code, rate_cents, effective_from::text AS effective_from FROM job_rates ORDER BY id"),
            "legacy_punches": rows("SELECT id, employee_id, job_code, kind, to_char(local_time, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS local_time, fold FROM legacy_punches ORDER BY id"),
            "legacy_approvals": rows("SELECT employee_id, week_start::text AS week_start, approved_by FROM legacy_approvals"),
            "legacy_punch_hash": rows("SELECT coalesce(sum(hashtext(id::text||employee_id::text||coalesce(job_code,'')||kind||local_time::text||fold::text)),0) AS h FROM legacy_punches")[0]["h"],
        }
    with open(out, "w") as fh:
        json.dump(data, fh, default=str)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
