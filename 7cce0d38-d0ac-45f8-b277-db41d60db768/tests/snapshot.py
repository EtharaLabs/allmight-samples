#!/usr/bin/env python3
"""Snapshot the pre-existing records before the submission runs (baseline for the model and R18)."""

import json
import sys

import psycopg

TABLES = {
    "organizations": "SELECT id, name FROM organizations ORDER BY id",
    "users": "SELECT id, email, full_name FROM users ORDER BY id",
    "memberships": "SELECT organization_id, user_id, role FROM memberships ORDER BY organization_id, user_id",
    "venues": "SELECT id, organization_id, name, timezone, address FROM venues ORDER BY id",
    "sections": "SELECT id, venue_id, code, name FROM sections ORDER BY id",
    "seats": "SELECT id, section_id, row_label, seat_number, accessible FROM seats ORDER BY id",
    "events": ("SELECT id, organization_id, venue_id, name, description, to_char(starts_local, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS starts_local,"
               " starts_fold, to_char(on_sale_local, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS on_sale_local, on_sale_fold, status,"
               " facility_fee_cents FROM events ORDER BY id"),
    "event_prices": "SELECT event_id, section_id, price_cents FROM event_prices ORDER BY event_id, section_id",
    "legacy_orders": ("SELECT id, event_id, buyer_email, buyer_name, to_char(placed_local, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS placed_local,"
                      " placed_fold, total_cents, old_payment_ref FROM legacy_orders ORDER BY id"),
    "legacy_tickets": ("SELECT id, order_id, seat_id, holder_email, holder_name, status, face_cents, discount_cents,"
                       " service_fee_cents, facility_fee_cents, tax_cents, total_cents, refunded_cents,"
                       " to_char(refunded_local, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS refunded_local, refunded_fold, barcode,"
                       " to_char(checked_in_local, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS checked_in_local, checked_in_fold, checked_in_gate"
                       " FROM legacy_tickets ORDER BY id"),
    "legacy_transfers": ("SELECT id, ticket_id, from_email, to_email, to_char(transferred_local, 'YYYY-MM-DD\"T\"HH24:MI:SS')"
                         " AS transferred_local, transferred_fold FROM legacy_transfers ORDER BY id"),
}
def main(db_url: str, out: str) -> None:
    with psycopg.connect(db_url, connect_timeout=10) as c:
        def rows(sql):
            cur = c.execute(sql)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        data = {k: rows(v) for k, v in TABLES.items()}
        data["max_ids"] = {k: rows(f"SELECT coalesce(max(id), 0) AS m FROM {k}")[0]["m"]
                           for k in ("organizations", "users", "venues", "sections", "seats", "events", "legacy_orders",
                                     "legacy_tickets", "legacy_transfers")}
    with open(out, "w") as fh:
        json.dump(data, fh, default=str)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
