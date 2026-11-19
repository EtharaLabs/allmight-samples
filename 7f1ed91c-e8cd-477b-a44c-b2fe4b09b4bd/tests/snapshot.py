#!/usr/bin/env python3
"""Snapshot the pre-existing records before the submission runs (baseline for the model and R10.1)."""

import json
import sys

import psycopg

TABLES = {
    "households": "SELECT id, name FROM households ORDER BY id",
    "users": "SELECT id, household_id, email, full_name, role, active FROM users ORDER BY id",
    "assignments": "SELECT user_id, household_id FROM assignments ORDER BY 1, 2",
    "accounts": ("SELECT id, household_id, name, broker, account_number, lot_method, opened_on::text AS opened_on, account_type "
                 "FROM accounts ORDER BY id"),
    "corporate_actions": ("SELECT id, household_id, security_id, kind, action_date::text AS action_date, new_security_id, ratio::text AS ratio, "
                          "parent_fmv::text AS parent_fmv, new_fmv::text AS new_fmv, cash_in_lieu_price::text AS cash_in_lieu_price, "
                          "acquirer_security_id, shares_per_share::text AS shares_per_share, cash_per_share::text AS cash_per_share, "
                          "acquirer_fmv::text AS acquirer_fmv, amount_per_share::text AS amount_per_share FROM corporate_actions ORDER BY id"),
    "lot_method_changes": "SELECT id, account_id, effective_from::text AS effective_from, lot_method FROM lot_method_changes ORDER BY id",
    "securities": "SELECT id, household_id, symbol, isin, name, currency FROM securities ORDER BY id",
    "fx_rates": "SELECT currency, rate_date::text AS rate_date, rate::text AS rate, quote FROM fx_rates ORDER BY 1, 2",
    "security_prices": "SELECT security_id, price_date::text AS price_date, close::text AS close FROM security_prices ORDER BY 1, 2",
    "legacy_transactions": ("SELECT id, account_id, security_id, kind, trade_date::text AS trade_date, quantity::text AS quantity, "
                            "amount::text AS amount, currency, split_new, split_old, to_account_id, ref, memo "
                            "FROM legacy_transactions ORDER BY id"),
    "legacy_year_locks": "SELECT household_id, year FROM legacy_year_locks ORDER BY 1, 2",
    "legacy_locked_figures": ("SELECT household_id, year, security_id, short_term_gain_cents, long_term_gain_cents, "
                              "wash_disallowed_cents, dividends_cents FROM legacy_locked_figures ORDER BY 1, 2, 3"),
}
HASHES = {
    "legacy_transactions": "SELECT count(*) AS n, coalesce(sum(hashtext(id::text||account_id::text||security_id::text||kind||trade_date::text"
                           "||coalesce(quantity::text,'')||coalesce(amount::text,'')||currency||coalesce(split_new::text,'')"
                           "||coalesce(split_old::text,'')||coalesce(to_account_id::text,'')||coalesce(ref,'')||coalesce(memo,''))),0) AS h "
                           "FROM legacy_transactions",
    "fx_rates": "SELECT count(*) AS n, coalesce(sum(hashtext(currency||rate_date::text||rate::text||quote)),0) AS h FROM fx_rates",
    "security_prices": "SELECT count(*) AS n, coalesce(sum(hashtext(security_id::text||price_date::text||close::text)),0) AS h FROM security_prices",
    "legacy_locked_figures": "SELECT count(*) AS n, coalesce(sum(hashtext(household_id::text||year::text||security_id::text||"
                             "short_term_gain_cents::text||long_term_gain_cents::text||wash_disallowed_cents::text||dividends_cents::text)),0) AS h "
                             "FROM legacy_locked_figures",
}


def main(db_url: str, out: str) -> None:
    with psycopg.connect(db_url, connect_timeout=10) as c:
        def rows(sql):
            cur = c.execute(sql)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        data = {name: rows(sql) for name, sql in TABLES.items()}
        data["hashes"] = {name: {k: int(v) for k, v in rows(sql)[0].items()} for name, sql in HASHES.items()}
    with open(out, "w") as fh:
        json.dump(data, fh, default=str)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
