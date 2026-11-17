#!/usr/bin/env python3
"""Snapshot the pre-existing records before the submission runs (baseline for the reference world and R10)."""

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
            "tz": rows("SELECT timezone FROM sites ORDER BY id LIMIT 1")[0]["timezone"],
            "tax_rates": rows("SELECT state, rate::text AS rate, freight_taxable FROM tax_rates"),
            "bins": rows("SELECT id, code, zone, aisle, bay, level FROM bins ORDER BY id"),
            "skus": rows("SELECT id, code, name, case_pack, unit_weight_g, length_cm, width_cm, height_cm, storage, taxable,"
                         " reorder_point, active FROM skus ORDER BY id"),
            "price_list": rows("SELECT id, tier, sku_id, unit_price_cents, effective_from::text AS effective_from FROM price_list ORDER BY id"),
            "customers": rows("SELECT id, name, tier, ship_to_state, approval_limit_cents, min_shelf_life_days, allow_split_cases,"
                              " backorders_allowed FROM customers ORDER BY id"),
            "users": rows("SELECT id, email, full_name, role, customer_id, zone, active FROM users ORDER BY id"),
            "lots": rows("SELECT id, sku_id, lot_code, expiry_date::text AS expiry_date, bin_id, unit_cost::text AS unit_cost, qa_hold"
                         " FROM lots ORDER BY id"),
            "legacy_movements": rows("SELECT id, lot_id, kind, units, to_char(local_time, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS local_time,"
                                     " fold, ref FROM legacy_movements ORDER BY id"),
            "legacy_orders": rows("SELECT id, number, customer_id, placed_by, to_char(placed_local, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS placed_local,"
                                  " fold, ship_date::text AS ship_date, po_number, status, subtotal_cents, tax_cents, freight_cents,"
                                  " freight_tax_cents, total_cents FROM legacy_orders ORDER BY id"),
            "legacy_hashes": {
                "mv": rows("SELECT coalesce(sum(hashtext(id::text||'/'||lot_id::text||'/'||kind||'/'||units::text||'/'||local_time::text||'/'||fold::text)),0) AS h"
                           " FROM legacy_movements")[0]["h"],
                "orders": rows("SELECT coalesce(sum(hashtext(id::text||'/'||number||'/'||customer_id::text||'/'||placed_local::text||'/'||fold::text||'/'"
                               "||coalesce(po_number,'')||'/'||status||'/'||total_cents::text)),0) AS h FROM legacy_orders")[0]["h"],
            },
            "legacy_lines_hash": rows("SELECT count(*) AS n, coalesce(sum(hashtext(order_id::text||'/'||line_no::text||'/'||sku_id::text||'/'"
                                      "||qty_units::text||'/'||unit_price_cents::text)), 0) AS h FROM legacy_order_lines")[0],
        }
    with open(out, "w") as fh:
        json.dump(data, fh, default=str)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
