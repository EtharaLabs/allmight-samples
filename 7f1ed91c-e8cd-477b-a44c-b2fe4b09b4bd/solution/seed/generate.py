#!/usr/bin/env python3
"""Deterministic generator for Lotbook's pre-existing data (environment and verifier seed).

Writes 001_schema.sql and 002_records.sql. The same bytes ship in environment/services/db/init/ and
tests/services/db/init/. Re-running with the same SEED reproduces them exactly.

All households, securities, prices and exchange rates are fictional.

The old system's locked 2023 figures (legacy_locked_figures) are what the rules give for 2023 *without*
the wash-sale rule, because the old system never applied it; tests/reference/engine.py computes them.

Usage: python3 generate.py <out_dir>
"""

from __future__ import annotations

import heapq
import math
from decimal import Decimal, getcontext
import random
import sys
from datetime import date, timedelta
from fractions import Fraction as F
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))  # assemble.py runs this with python3 -I

from reference import engine as E  # noqa: E402

SEED = 20261110
START, END = date(2023, 1, 3), date(2025, 12, 31)
FX_START, FX_END = date(2022, 12, 1), date(2026, 10, 30)
PRICE_DAYS = (date(2026, 9, 30), date(2026, 10, 30))
HOLIDAYS = {date(2022, 12, 26), date(2023, 1, 2), date(2023, 4, 7), date(2023, 12, 25), date(2023, 12, 26),
            date(2024, 1, 1), date(2024, 3, 29), date(2024, 12, 25), date(2024, 12, 26), date(2025, 1, 1),
            date(2025, 4, 18), date(2025, 12, 25), date(2025, 12, 26), date(2026, 1, 1), date(2026, 4, 3)}
CAD_SWITCH = date(2024, 7, 1)  # CAD rows before this date are quoted units_per_usd

SCHEMA = """\
CREATE TABLE households (
    id integer PRIMARY KEY,
    name text NOT NULL
);
CREATE TABLE users (
    id serial PRIMARY KEY,
    household_id integer REFERENCES households (id),
    email text NOT NULL,
    full_name text NOT NULL,
    role text NOT NULL CHECK (role IN ('owner', 'member', 'viewer', 'advisor', 'preparer')),
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE assignments (
    user_id integer NOT NULL REFERENCES users (id),
    household_id integer NOT NULL REFERENCES households (id),
    PRIMARY KEY (user_id, household_id)
);
CREATE TABLE accounts (
    id serial PRIMARY KEY,
    household_id integer NOT NULL REFERENCES households (id),
    name text NOT NULL,
    broker text NOT NULL CHECK (broker IN ('northwind', 'harbor')),
    account_number text NOT NULL,
    lot_method text NOT NULL CHECK (lot_method IN ('FIFO', 'HIFO')),
    opened_on date NOT NULL,
    account_type text NOT NULL DEFAULT 'taxable' CHECK (account_type IN ('taxable', 'ira')),
    UNIQUE (broker, account_number)
);
CREATE TABLE lot_method_changes (
    id serial PRIMARY KEY,
    account_id integer NOT NULL REFERENCES accounts (id),
    effective_from date NOT NULL,
    lot_method text NOT NULL CHECK (lot_method IN ('FIFO', 'HIFO')),
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE securities (
    id serial PRIMARY KEY,
    household_id integer NOT NULL REFERENCES households (id),
    symbol text NOT NULL,
    isin text NOT NULL,
    name text NOT NULL,
    currency text NOT NULL CHECK (currency IN ('USD', 'CAD', 'EUR'))
);
CREATE UNIQUE INDEX securities_symbol ON securities (household_id, upper(symbol));
CREATE UNIQUE INDEX securities_isin ON securities (household_id, upper(isin));
CREATE TABLE fx_rates (
    currency text NOT NULL,
    rate_date date NOT NULL,
    rate numeric(12, 6) NOT NULL,
    quote text NOT NULL CHECK (quote IN ('usd_per_unit', 'units_per_usd')),
    PRIMARY KEY (currency, rate_date)
);
CREATE TABLE security_prices (
    security_id integer NOT NULL REFERENCES securities (id),
    price_date date NOT NULL,
    close numeric(14, 4) NOT NULL,
    PRIMARY KEY (security_id, price_date)
);
CREATE TABLE legacy_transactions (
    id bigint PRIMARY KEY,
    account_id integer NOT NULL REFERENCES accounts (id),
    security_id integer NOT NULL REFERENCES securities (id),
    kind text NOT NULL,
    trade_date date NOT NULL,
    quantity numeric(20, 8),
    amount numeric(14, 2),
    currency text NOT NULL,
    split_new integer,
    split_old integer,
    to_account_id integer REFERENCES accounts (id),
    ref text,
    memo text
);
CREATE INDEX legacy_transactions_account ON legacy_transactions (account_id);
CREATE INDEX legacy_transactions_security ON legacy_transactions (security_id);
CREATE TABLE corporate_actions (
    id integer PRIMARY KEY,
    household_id integer NOT NULL REFERENCES households (id),
    security_id integer NOT NULL REFERENCES securities (id),
    kind text NOT NULL CHECK (kind IN ('spinoff', 'merger', 'return_of_capital')),
    action_date date NOT NULL,
    new_security_id integer REFERENCES securities (id),
    ratio numeric(20, 8),
    parent_fmv numeric(16, 6),
    new_fmv numeric(16, 6),
    cash_in_lieu_price numeric(16, 6),
    acquirer_security_id integer REFERENCES securities (id),
    shares_per_share numeric(20, 8),
    cash_per_share numeric(16, 6),
    acquirer_fmv numeric(16, 6),
    amount_per_share numeric(16, 6)
);
CREATE TABLE legacy_year_locks (
    household_id integer NOT NULL REFERENCES households (id),
    year integer NOT NULL,
    locked_at timestamptz NOT NULL,
    PRIMARY KEY (household_id, year)
);
CREATE TABLE legacy_locked_figures (
    household_id integer NOT NULL REFERENCES households (id),
    year integer NOT NULL,
    security_id integer NOT NULL REFERENCES securities (id),
    short_term_gain_cents bigint NOT NULL,
    long_term_gain_cents bigint NOT NULL,
    wash_disallowed_cents bigint NOT NULL,
    dividends_cents bigint NOT NULL,
    PRIMARY KEY (household_id, year, security_id)
);
"""

HOUSEHOLDS = [(1, "Raman-Okafor household"), (2, "Lindqvist household"), (3, "Castellano household")]
USERS = [
    (1, 1, "Priya.Raman@Lotbook.Example", "Priya Raman", "owner", True),
    (2, 1, "  daniel.okafor@lotbook.example ", "Daniel Okafor", "member", True),
    (3, 2, "astrid.lindqvist@lotbook.example", "Astrid Lindqvist", "owner", True),
    (4, 2, "JOHAN.LINDQVIST@LOTBOOK.EXAMPLE", "Johan Lindqvist", "member", True),
    (5, 2, "ebba.lindqvist@lotbook.example", "Ebba Lindqvist", "member", False),
    (6, 3, "elena.castellano@lotbook.example", "Elena Castellano", "owner", True),
    (7, 3, " Marco.Castellano@lotbook.example", "Marco Castellano", "owner", True),
    (8, None, "morgan.ellis@ellis-advisory.example", "Morgan Ellis", "advisor", True),
    (9, 1, "asha.okafor@lotbook.example", "Asha Okafor", "viewer", True),
    (10, None, "Hannah@Brooks-Tax.example ", "Hannah Brooks", "preparer", True),
    (11, 3, "lucia.castellano@lotbook.example", "Lucia Castellano", "viewer", True),
    (12, None, "old.preparer@brooks-tax.example", "Lee Brooks", "preparer", False),
]
ASSIGNMENTS = [(8, 1), (8, 2), (10, 2), (10, 3), (12, 1)]
ACCOUNTS = [
    (1, 1, "Priya - Northwind Individual", "northwind", "NW-4821-3377", "FIFO", date(2019, 4, 2)),
    (2, 1, "Daniel - Northwind Individual", "northwind", "NW-5530-0912", "HIFO", date(2018, 9, 14)),
    (3, 1, "Joint - Harbor Direct", "harbor", "HD 7734 0021", "FIFO", date(2021, 2, 1)),
    (4, 1, "Priya - Harbor Direct", "harbor", "HD 7734 0388", "FIFO", date(2022, 6, 20)),
    (5, 1, "Daniel - Northwind Trading", "northwind", "NW-6001-2214", "FIFO", date(2022, 11, 7)),
    (6, 2, "Astrid - Northwind Individual", "northwind", "NW-7310-4455", "HIFO", date(2020, 3, 3)),
    (7, 2, "Lindqvist Joint - Harbor Direct", "harbor", "HD 8120 5567", "FIFO", date(2021, 8, 9)),
    (8, 1, "Priya - Northwind IRA", "northwind", "NW-9104-7710", "FIFO", date(2021, 1, 4)),
    (9, 2, "Astrid - Harbor IRA", "harbor", "HD 8120 9902", "FIFO", date(2020, 5, 6)),
]
IRA_ACCOUNTS = {8, 9}
METHOD_CHANGES = [(1, 4, date(2025, 1, 1), "HIFO"), (2, 5, date(2024, 7, 15), "HIFO"), (3, 5, date(2025, 6, 1), "FIFO"),
                  (4, 6, date(2024, 1, 1), "FIFO"), (5, 6, date(2025, 3, 3), "HIFO")]
# symbol, ISIN, name, currency, start price, drift, vol, dividend per share per quarter (0 = none), split
SEC_SPECS = [
    ("NTMX", "US66988N1037", "Northstar Total Market ETF", "USD", 212.40, 0.08, 0.16, 0.71, None),
    ("GLBX", "US37950E2046", "Glacier Global ex-US ETF", "USD", 54.18, 0.04, 0.15, 0.39, None),
    ("AURS", "US05211X1081", "Aurora Semiconductors", "USD", 418.00, 0.20, 0.45, 0.04, (date(2024, 6, 10), 10, 1)),
    ("KSTL", "US49338L1035", "Keystone Logistics", "USD", 88.25, 0.05, 0.30, 0.0, None),
    ("PNVA", "US72348W1062", "Pinnacle Vaccines", "USD", 64.10, -0.05, 0.40, 0.0, None),
    ("BRKW", "US11373Q1094", "Brookwater Utilities", "USD", 71.35, 0.03, 0.14, 0.68, None),
    ("HLIO", "US42330P2048", "Helio Solar", "USD", 9.82, -0.30, 0.60, 0.0, (date(2025, 2, 3), 1, 8)),
    ("QNTM", "US74765L1008", "Quantum Ledger", "USD", 26.70, 0.10, 0.55, 0.0, None),
    ("ORCH", "US68571J1060", "Orchard Health", "USD", 132.90, 0.06, 0.22, 0.42, None),
    ("TIDL", "US88634T5048", "Tidal Bond ETF", "USD", 48.75, 0.00, 0.06, 0.16, None),
    ("CRVX", "US22789H1077", "Corvex Software", "USD", 251.30, 0.15, 0.35, 0.0, (date(2024, 10, 14), 3, 1)),
    ("MPLR", "CA56501R1064", "Maple Rail", "CAD", 141.20, 0.06, 0.20, 0.79, None),
    ("NRDB", "CA66432B1076", "Northern Rock Bank", "CAD", 72.45, 0.05, 0.18, 0.96, None),
    ("PRAI", "CA73963Y1043", "Prairie Energy", "CAD", 38.60, 0.02, 0.32, 0.25, (date(2024, 6, 11), 2, 1)),
    ("CDLM", "CA15030L1022", "Cedar Lumber", "CAD", 17.85, -0.04, 0.35, 0.09, None),
    ("RHCH", "DE000RHC7341", "Rhein Chemie AG", "EUR", 92.40, 0.02, 0.24, 0.0, None),
    ("DLTW", "DE000DLT2203", "Delta Windkraft AG", "EUR", 23.15, -0.10, 0.45, 0.0, None),
    ("ALPN", "AT0000ALP551", "Alpen Versicherung AG", "EUR", 41.80, 0.05, 0.17, 0.55, None),
    ("LUMR", "FR0000LUM127", "Lumiere Luxe SA", "EUR", 655.00, 0.07, 0.25, 0.0, None),
    ("NRDV", "NO0010NRD447", "Nordvik Shipping ASA", "EUR", 14.62, 0.00, 0.50, 0.21, None),
    # spun-off securities (they trade only after their spin-off)
    ("AURQ", "US05211Q1023", "Aurora Quantum", "USD", 31.40, 0.12, 0.50, 0.0, None),
    ("ORCV", "US68571V1061", "Orchard Veterinary", "USD", 24.85, 0.05, 0.30, 0.0, None),
    ("MPLW", "CA56501X1050", "Maple Logistics", "CAD", 18.60, 0.04, 0.28, 0.0, None),
]
# kind, parent/target/distributing symbol, date, terms (symbols, ratios and per-share amounts in the security's currency)
CORPORATE_ACTIONS = [
    ("spinoff", "AURS", date(2024, 6, 10), {"new": "AURQ", "ratio": F("0.1")}),       # same date as the AURS 10:1 split
    ("spinoff", "ORCH", date(2024, 8, 5), {"new": "ORCV", "ratio": F("0.25")}),
    ("spinoff", "MPLR", date(2025, 3, 10), {"new": "MPLW", "ratio": F("0.2")}),
    ("merger", "QNTM", date(2024, 10, 21), {"acquirer": None, "shares": F(0), "cash": F("31.2")}),
    ("merger", "PNVA", date(2025, 6, 16), {"acquirer": "ORCH", "shares": F("0.35"), "cash": F("4.75")}),
    ("merger", "DLTW", date(2025, 9, 8), {"acquirer": "RHCH", "shares": F("0.18"), "cash": F("2.05")}),
    ("return_of_capital", "BRKW", date(2024, 3, 15), {"amount": F("0.45")}),
    ("return_of_capital", "BRKW", date(2024, 9, 16), {"amount": F("0.45")}),
    ("return_of_capital", "BRKW", date(2025, 3, 17), {"amount": F("0.45")}),
    ("return_of_capital", "TIDL", date(2024, 12, 16), {"amount": F("0.12")}),
    ("return_of_capital", "NRDB", date(2025, 11, 17), {"amount": F("0.3")}),
    ("return_of_capital", "HLIO", date(2024, 4, 15), {"amount": "price"}),           # about one share price
    ("return_of_capital", "HLIO", date(2025, 7, 14), {"amount": "price"}),
]
NEW_SECURITIES = {"AURQ", "ORCV", "MPLW"}
# household -> {account: [symbols]}
HOLDINGS = {
    1: {1: ["NTMX", "AURS", "KSTL", "BRKW", "MPLR", "NRDB", "PRAI", "HLIO", "ORCH", "TIDL", "GLBX"],
        2: ["NTMX", "GLBX", "AURS", "PNVA", "QNTM", "MPLR", "CDLM", "CRVX", "TIDL", "BRKW", "NRDB"],
        8: ["NTMX", "AURS", "BRKW", "ORCH", "KSTL", "MPLR", "HLIO"],
        3: ["RHCH", "ALPN", "LUMR", "NTMX", "AURS", "DLTW", "NRDV", "GLBX"],
        4: ["RHCH", "DLTW", "ALPN", "NRDV", "GLBX", "CRVX", "LUMR"],
        5: ["KSTL", "PNVA", "HLIO", "QNTM", "PRAI", "CDLM", "AURS", "ORCH", "DLTW"]},
    2: {6: ["NTMX", "AURS", "BRKW", "MPLR", "NRDB", "HLIO", "TIDL"], 7: ["RHCH", "DLTW", "NTMX", "ALPN", "AURS"],
        9: ["NTMX", "RHCH", "ALPN", "DLTW", "BRKW"]},
}
DRIP = {(1, "NTMX"), (1, "BRKW"), (2, "NTMX"), (2, "TIDL"), (3, "ALPN"), (1, "NRDB"), (6, "NTMX"), (6, "BRKW"),
        (7, "NTMX"), (2, "GLBX"), (4, "ALPN"), (1, "TIDL"), (2, "BRKW"), (2, "NRDB"), (6, "TIDL"), (6, "NRDB"),
        (3, "GLBX"), (1, "MPLR"), (7, "ALPN"), (4, "NRDV")}
TRANSFERS = [(1, 5, 2, "KSTL", date(2024, 11, 4)), (1, 4, 3, "RHCH", date(2025, 5, 12)),
             (1, 2, 5, "QNTM", date(2023, 8, 21)), (2, 6, 7, "NTMX", date(2025, 9, 15))]
INTENSITY = {1: 1.3, 2: 1.3, 3: 1.2, 4: 1.2, 5: 2.6, 6: 1.5, 7: 1.4, 8: 0.9, 9: 0.8}
MONTHLY = {"TIDL", "BRKW", "NRDB"}


def q(s) -> str:
    return "NULL" if s is None else "'" + str(s).replace("'", "''") + "'"


def copy_text(s) -> str:
    if s is None:
        return "\\N"
    return str(s).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def is_business(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def fx_table(rnd: random.Random) -> list[tuple[str, date, F, str]]:
    rows = []
    for cur, start in (("CAD", 1.3520), ("EUR", 1.0710)):
        r = start
        d = FX_START
        while d <= FX_END:
            r *= math.exp(rnd.gauss(0, 0.0035))
            if is_business(d) and rnd.random() > 0.015:  # a few more days are simply missing
                if cur == "CAD":
                    usd_per = min(max(1 / r, 0.70), 0.77)
                    r = 1 / usd_per
                    if d < CAD_SWITCH:
                        val, quote = r, "units_per_usd"
                    else:
                        val, quote = 1 / r, "usd_per_unit"
                else:
                    r = min(max(r, 1.03), 1.16)
                    val, quote = r, "usd_per_unit"
                digits = 6 if rnd.random() > 0.3 else rnd.choice([2, 3, 4])  # some days carry round rates
                v = F(round(val * 10 ** digits), 10 ** digits)
                rows.append((cur, d, v, quote))
            d += timedelta(days=1)
    return rows


def price_paths(rnd: random.Random) -> dict[str, dict[date, float]]:
    paths = {}
    for sym, _isin, _n, _c, p0, drift, vol, _dps, split in SEC_SPECS:
        p, d, out = p0, date(2022, 12, 1), {}
        while d <= FX_END:
            p *= math.exp(drift / 365 + vol / math.sqrt(365) * rnd.gauss(0, 1))
            p = max(p, 0.5)
            if split and d == split[0]:
                p = p * split[2] / split[1]
            out[d] = p
            d += timedelta(days=1)
        paths[sym] = out
    return paths


def cents(x: F) -> int:
    return E.rhu(x * 100)


class Gen:
    def __init__(self, rnd, rates: E.Rates, paths):
        self.rnd, self.rates, self.paths = rnd, rates, paths
        self.rows: list[dict] = []
        self.next_id = 500000
        self.ref_n = {"northwind": 4800, "harbor": 310}

    def new_id(self) -> int:
        self.next_id += self.rnd.randrange(1, 7)
        return self.next_id

    def ref(self, broker: str, d: date) -> str:
        self.ref_n[broker] += self.rnd.randrange(1, 9)
        if broker == "northwind":
            return f"NW{d:%y}-{self.ref_n[broker]:06d}"
        return f"HD-{d.year}-{self.ref_n[broker]:05d}"

    def tie(self, amount_c: int, cur: str, d: date) -> int:
        """Nudge an amount so that its USD value lands exactly on half a cent, when the day's rate allows it,
        preferring amounts where a rate inverted to 28 digits rounds the other way."""
        if cur == "USD":
            return amount_c
        r = self.rates.rate(cur, d)
        if r.denominator % 2:
            return amount_c
        base = r.denominator // 2
        cands = [base * k for k in range(max(1, (amount_c - 400) // base | 1), (amount_c + 400) // base + 2, 2) if base * k > 0]
        cands = [c for c in cands if abs(c - amount_c) <= 400] or ([base] if base < 10 * amount_c else [])
        wrong = [c for c in cands if dec_usd(c, r) != E.rhu(F(c) * r)]
        pick = wrong or cands
        return min(pick, key=lambda c: abs(c - amount_c)) if pick else amount_c


def main(out: Path) -> None:
    rnd = random.Random(SEED)
    out.mkdir(parents=True, exist_ok=True)
    (out / "001_schema.sql").write_text(SCHEMA)
    fx = fx_table(rnd)
    rates = E.Rates(fx)
    paths = price_paths(rnd)
    spec = {s[0]: s for s in SEC_SPECS}
    lines = ["BEGIN;"]
    for hid, name in HOUSEHOLDS:
        lines.append(f"INSERT INTO households (id, name) VALUES ({hid}, {q(name)});")
    for uid, hid, email, name, role, active in USERS:
        lines.append("INSERT INTO users (id, household_id, email, full_name, role, active) VALUES "
                     f"({uid}, {hid if hid else 'NULL'}, {q(email)}, {q(name)}, {q(role)}, {'true' if active else 'false'});")
    lines.append(f"SELECT setval('users_id_seq', {len(USERS)});")
    for a, h in ASSIGNMENTS:
        lines.append(f"INSERT INTO assignments (user_id, household_id) VALUES ({a}, {h});")
    for aid, hid, name, broker, num, method, opened in ACCOUNTS:
        lines.append("INSERT INTO accounts (id, household_id, name, broker, account_number, lot_method, opened_on, account_type) VALUES "
                     f"({aid}, {hid}, {q(name)}, {q(broker)}, {q(num)}, {q(method)}, '{opened}', "
                     f"'{'ira' if aid in IRA_ACCOUNTS else 'taxable'}');")
    lines.append(f"SELECT setval('accounts_id_seq', {len(ACCOUNTS)});")
    for cid, aid, eff, m in METHOD_CHANGES:
        lines.append("INSERT INTO lot_method_changes (id, account_id, effective_from, lot_method, recorded_at) VALUES "
                     f"({cid}, {aid}, '{eff}', {q(m)}, '{eff - timedelta(days=rnd.randrange(3, 40))} 15:04:00+00');")
    lines.append(f"SELECT setval('lot_method_changes_id_seq', {len(METHOD_CHANGES)});")

    # securities: one row per household and symbol it holds
    sec_id: dict[tuple[int, str], int] = {}
    sid = 0
    sec_rows = []
    for hid in (1, 2):
        held_syms = {s for accts in HOLDINGS[hid].values() for s in accts}
        syms = sorted(held_syms | {ca[3]["new"] for ca in CORPORATE_ACTIONS if ca[0] == "spinoff" and ca[1] in held_syms})
        for s in syms:
            sid += 1
            sec_id[(hid, s)] = sid
            _sym, isin, name, cur, *_ = spec[s]
            sec_rows.append((sid, hid, s, isin, name, cur))
    for sid_, hid, s, isin, name, cur in sec_rows:
        lines.append("INSERT INTO securities (id, household_id, symbol, isin, name, currency) VALUES "
                     f"({sid_}, {hid}, {q(s)}, {q(isin)}, {q(name)}, {q(cur)});")
    lines.append(f"SELECT setval('securities_id_seq', {len(sec_rows)});")
    lines.append("COPY fx_rates (currency, rate_date, rate, quote) FROM stdin;")
    for cur, d, v, quote in fx:
        lines.append(f"{cur}\t{d}\t{float(v):.6f}\t{quote}")
    lines.append("\\.")
    for sid_, hid, s, *_ in sec_rows:
        for pd in PRICE_DAYS:
            lines.append(f"INSERT INTO security_prices (security_id, price_date, close) VALUES ({sid_}, '{pd}', {paths[s][pd]:.4f});")

    # ---- transactions -------------------------------------------------------------------------------
    g = Gen(rnd, rates, paths)
    broker_of = {a[0]: a[3] for a in ACCOUNTS}
    split_rows = []
    hold: dict[tuple[int, str], F] = {}
    events: list[tuple] = []  # (date, order, callable) processed chronologically
    split_days = {s[0]: s[8] for s in SEC_SPECS if s[8]}

    def near_split(sym: str, d: date) -> bool:
        sp = split_days.get(sym)
        return bool(sp) and abs((d - sp[0]).days) <= 32

    def emit(aid, sym, hid, kind, d, qty=None, amt=None, ratio=None, to=None, memo=None, ref=True):
        cur = spec[sym][3]
        row = {"id": g.new_id(), "account_id": aid, "security_id": sec_id[(hid, sym)], "kind": kind, "trade_date": d,
               "quantity": qty, "amount": amt, "currency": cur, "split_new": ratio[0] if ratio else None,
               "split_old": ratio[1] if ratio else None, "to_account_id": to,
               "ref": g.ref(broker_of[aid], d) if ref else None, "memo": memo}
        g.rows.append(row)
        return row

    for hid, accts in HOLDINGS.items():
        for aid, syms in accts.items():
            for sym in syms:
                _s, _isin, _name, cur, _p0, _dr, vol, dps, split = spec[sym]
                intensity = INTENSITY[aid] * (1.5 if sym in ("NTMX", "AURS", "QNTM", "HLIO", "PNVA", "DLTW") else 1.0)
                step = rnd.choice([2, 3, 4, 7, 7, 10])
                d = START + timedelta(days=rnd.randrange(0, 20))
                while d <= END:
                    events.append((d, 1 + rnd.random() / 2, ("buy", hid, aid, sym)))
                    d += timedelta(days=max(2, int(step / intensity) + rnd.randrange(-2, 3)))
                n_sells = int(rnd.randrange(16, 30) * intensity)
                for _ in range(n_sells):
                    sd = START + timedelta(days=rnd.randrange(40, (END - START).days))
                    events.append((sd, 3 + rnd.random() / 2, ("sell", hid, aid, sym)))
                if dps:
                    d = date(2023, rnd.choice([1, 2, 3]), rnd.randrange(10, 28))
                    while d <= END:
                        events.append((d, 1 + rnd.random() / 2, ("div", hid, aid, sym)))
                        d += timedelta(days=(30 if sym in MONTHLY else 91) + rnd.randrange(-3, 4))
    # tie lots: in HIFO periods, a pricey USD lot of 6 or 18 shares is bought and half of it sold days later, so the
    # basis share of the sold half is an exact half cent (R5.4)
    hifo_windows = {2: [(START, END)], 5: [(date(2024, 7, 15), date(2025, 5, 31))], 4: [(date(2025, 1, 1), END)],
                    6: [(START, date(2023, 12, 31)), (date(2025, 3, 3), END)]}
    for aid, windows in hifo_windows.items():
        hid = 1 if aid in HOLDINGS[1] else 2
        for sym in HOLDINGS[hid][aid]:
            if spec[sym][3] != "USD" or sym in split_days:
                continue
            for _ in range(5):
                lo, hi = rnd.choice(windows)
                d = lo + timedelta(days=rnd.randrange(max(1, (hi - lo).days - 20)))
                events.append((d, 1.9, ("tiebuy", hid, aid, sym)))
    for hid, frm, to, sym, d in TRANSFERS:
        events.append((d, 2.0, ("transfer", hid, frm, sym, to)))
    # corporate actions (R13): after the day's splits, before its trades; no sale of an involved security within
    # 32 days of the action (the instruction promises no loss sale within 30 days)
    involved: dict[str, list[date]] = {}
    for i, (ck, csym, cday, terms) in enumerate(CORPORATE_ACTIONS):
        others = [terms.get("new"), terms.get("acquirer")]
        for sym_ in [csym] + [o for o in others if o]:
            involved.setdefault(sym_, []).append(cday)
        for hid in (1, 2):
            if (hid, csym) in sec_id:
                events.append((cday, 0.5, ("ca", hid, None, csym, i)))
        if ck == "spinoff":  # the spun-off security trades afterwards in some accounts holding the parent
            for hid in (1, 2):
                for aid, syms in HOLDINGS[hid].items():
                    if csym in syms and rnd.random() < 0.6:
                        d = cday + timedelta(days=36 + rnd.randrange(10))
                        while d <= END:
                            events.append((d, 1 + rnd.random() / 2, ("buy", hid, aid, terms["new"])))
                            d += timedelta(days=rnd.randrange(9, 26))
                        for _ in range(rnd.randrange(3, 9)):
                            sd = cday + timedelta(days=rnd.randrange(70, max(71, (END - cday).days)))
                            events.append((sd, 3 + rnd.random() / 2, ("sell", hid, aid, terms["new"])))

    def near_action(sym_: str, d: date) -> bool:
        return any(abs((d - x).days) <= 32 for x in involved.get(sym_, ()))
    for sym, sp in split_days.items():
        for hid in (1, 2):
            for aid, syms in HOLDINGS[hid].items():
                events.append((sp[0], 0.0, ("split", hid, aid, sym)))
    heap = [(d, o, i, ev) for i, (d, o, ev) in enumerate(events)]
    heapq.heapify(heap)
    counter = len(heap)

    dead: dict[tuple[int, str], date] = {}  # merged-away securities: nothing happens to them afterwards
    ca_rows: list[dict] = []
    while heap:
        d, _o, _i, ev = heapq.heappop(heap)
        if d > END:
            continue
        kind, hid, aid, sym = ev[:4]
        if (hid, sym) in dead and d >= dead[(hid, sym)]:
            continue
        cur = spec[sym][3]
        price = F(round(g.paths[sym][d] * 10000), 10000)
        key = (aid, sym)
        held = hold.get(key, F(0))
        if kind == "ca":
            ck, csym, cday, terms = CORPORATE_ACTIONS[ev[4]]
            px = lambda s_: F(round(g.paths[s_][d] * 100), 100)  # noqa: E731
            row = {"id": 700 + 3 * len(ca_rows) + rnd.randrange(1, 3), "household_id": hid, "security_id": sec_id[(hid, csym)],
                   "kind": ck, "action_date": d}
            if ck == "spinoff":
                row.update(new_security_id=sec_id[(hid, terms["new"])], ratio=terms["ratio"], parent_fmv=px(csym),
                           new_fmv=px(terms["new"]), cash_in_lieu_price=px(terms["new"]))
            elif ck == "merger":
                acq = terms["acquirer"]
                row.update(acquirer_security_id=sec_id[(hid, acq)] if acq else None, shares_per_share=terms["shares"],
                           cash_per_share=terms["cash"], acquirer_fmv=px(acq) if acq else None)
                for a2 in HOLDINGS[hid]:
                    t_held = hold.get((a2, csym), F(0))
                    if t_held and acq:
                        hold[(a2, acq)] = hold.get((a2, acq), F(0)) + t_held * terms["shares"]
                    hold[(a2, csym)] = F(0)
                dead[(hid, csym)] = d + timedelta(days=1)
            else:
                amt_ = terms["amount"]
                row.update(amount_per_share=F(round(g.paths[csym][d] * 115), 100) if amt_ == "price" else amt_)
            ca_rows.append(row)
            continue
        if kind == "split":
            sp = split_days[sym]
            if held > 0:
                hold[key] = held * sp[1] / sp[2]
                emit(aid, sym, hid, "split", d, ratio=(sp[1], sp[2]))
            continue
        if kind == "tiebuy":
            if not is_business(d) or near_split(sym, d) or near_action(sym, d):
                continue
            tq = rnd.choice([6, 6, 18])
            c = E.rhu(tq * price * F(13, 10) * 100)
            want = 5 if tq == 6 else rnd.choice([11, 17])
            c += (want - c % tq) % tq
            emit(aid, sym, hid, "buy", d, qty=F(tq), amt=F(c, 100), memo=None)
            hold[key] = held + tq
            counter += 1
            heapq.heappush(heap, (d + timedelta(days=rnd.randrange(2, 9)), 3.4, counter, ("tiesell", hid, aid, sym, tq)))
            continue
        if kind == "tiesell":
            tq = F(ev[4], 2)
            if held < tq or near_split(sym, d) or near_action(sym, d):
                continue
            amt = E.rhu(tq * price * 100) - rnd.randrange(0, 50)
            emit(aid, sym, hid, "sell", d, qty=tq, amt=F(amt, 100))
            hold[key] = held - tq
            continue
        if kind == "transfer":
            to = ev[4]
            if held > 0:
                emit(aid, sym, hid, "transfer", d, qty=held, to=to, ref=False, memo="Account consolidation")
                hold[(to, sym)] = hold.get((to, sym), F(0)) + held
                hold[key] = F(0)
            continue
        if not is_business(d) and kind in ("buy", "sell") and rnd.random() < 0.85:
            continue
        if kind == "buy":
            if near_split(sym, d) and rnd.random() < 0.5:
                continue
            budget = rnd.choice([250, 500, 750, 1000, 1500, 2500, 4000]) * (3 if aid == 5 else 1)
            if rnd.random() < 0.3:
                qty = F(round(F(budget) / price * 10000), 10000)
                if qty <= 0:
                    continue
            else:
                qty = F(max(1, int(F(budget) / price)))
            fee = rnd.choice([F(0), F("4.95"), F("9.99")])
            amt = cents(qty * price + fee)
            if rnd.random() < 0.25:
                amt = g.tie(amt, cur, d)
            emit(aid, sym, hid, "buy", d, qty=qty, amt=F(amt, 100))
            hold[key] = held + qty
        elif kind == "sell":
            if held <= 0 or near_split(sym, d) or near_action(sym, d):
                continue
            frac = rnd.choice([F(1, 20), F(1, 10), F(1, 10), F(1, 8), F(1, 5), F(1, 4), F(1, 3), F(1, 2)] * 3 + [F(1)])
            qty = held * frac
            if qty.denominator != 1 and rnd.random() < 0.7:
                qty = F(int(qty)) if int(qty) > 0 else held
            if (qty * 10 ** 8).denominator != 1:
                qty = held
            fee = rnd.choice([F(0), F("4.95"), F("9.99")])
            gross = qty * price - fee
            if gross <= 0:
                continue
            amt = cents(gross)
            if rnd.random() < 0.25:
                amt = g.tie(amt, cur, d)
            emit(aid, sym, hid, "sell", d, qty=qty, amt=F(amt, 100))
            hold[key] = held - qty
            if rnd.random() < 0.45:  # a harvest is often followed by a rebuy; gaps cluster around the 30-day edge
                gap = rnd.choice([1, 3, 8, 15, 22, 29, 30, 30, 31, 31, 32, 40])
                other = [a for a, ss in HOLDINGS[hid].items() if sym in ss] or [aid]
                counter += 1
                heapq.heappush(heap, (d + timedelta(days=gap), 1 + rnd.random() / 2, counter, ("buy", hid, rnd.choice(other), sym)))
        elif kind == "div":
            if held <= 0:
                continue
            dps = F(str(spec[sym][7])) / (3 if sym in MONTHLY else 1)
            amt = cents(held * dps * (F(1, 10) if sym in ("AURS", "CRVX", "PRAI") and d >= date(2024, 6, 1) else 1))
            if amt <= 0:
                continue
            if rnd.random() < 0.3:
                amt = g.tie(amt, cur, d)
            emit(aid, sym, hid, "dividend", d, amt=F(amt, 100))
            if (aid, sym) in DRIP and not near_split(sym, d):
                rq = F(round(F(amt, 100) / price * 10000), 10000)
                if rq > 0:
                    emit(aid, sym, hid, "reinvest", d, qty=rq, amt=F(amt, 100))
                    hold[key] = hold.get(key, F(0)) + rq

    cur_of = {sec_id[(h, s)]: spec[s][3] for (h, s) in sec_id}
    planted = plant_share_ties(g.rows, rates, cur_of, rnd, [ca_txn(r) for r in ca_rows], IRA_ACCOUNTS)

    # quirks the instruction describes: kind spelling, signs, currency case, missing refs
    for r in g.rows:
        roll = rnd.random()
        if roll < 0.04:
            r["kind"] = r["kind"].upper()
        elif roll < 0.07:
            r["kind"] = " " + r["kind"].title() + " "
        if r["kind"].strip().lower() == "sell" and rnd.random() < 0.35:
            r["quantity"] = -r["quantity"]
        if r["kind"].strip().lower() in ("buy", "reinvest") and rnd.random() < 0.3:
            r["amount"] = -r["amount"]
        if rnd.random() < 0.05:
            r["currency"] = r["currency"].lower()
        if r["ref"] and rnd.random() < 0.02:
            r["ref"] = None
        if r["memo"] is None and rnd.random() < 0.03:
            r["memo"] = rnd.choice(["rebalance", "tax-loss harvest", "gift from Nana", "auto-invest", "DRIP"])

    for r in ca_rows:
        cols = ["id", "household_id", "security_id", "kind", "action_date", "new_security_id", "ratio", "parent_fmv", "new_fmv",
                "cash_in_lieu_price", "acquirer_security_id", "shares_per_share", "cash_per_share", "acquirer_fmv", "amount_per_share"]
        vals = []
        for c in cols:
            v = r.get(c)
            vals.append("NULL" if v is None else (f"'{v}'" if c in ("kind", "action_date") else E.qty_str(v) if isinstance(v, F) else str(v)))
        lines.append(f"INSERT INTO corporate_actions ({', '.join(cols)}) VALUES ({', '.join(vals)});")
    lines.append("COPY legacy_transactions (id, account_id, security_id, kind, trade_date, quantity, amount, currency, "
                 "split_new, split_old, to_account_id, ref, memo) FROM stdin;")
    for r in g.rows:
        qv = None if r["quantity"] is None else E.qty_str(r["quantity"])
        av = None if r["amount"] is None else _money(r["amount"])
        lines.append("\t".join([str(r["id"]), str(r["account_id"]), str(r["security_id"]), copy_text(r["kind"]),
                                r["trade_date"].isoformat(), copy_text(qv), copy_text(av), copy_text(r["currency"]),
                                copy_text(r["split_new"]), copy_text(r["split_old"]), copy_text(r["to_account_id"]),
                                copy_text(r["ref"]), copy_text(r["memo"])]))
    lines.append("\\.")

    # ---- the old system's 2023 lock: rules without the wash-sale rule ------------------------------------
    for hid in (1, 2):
        lines.append(f"INSERT INTO legacy_year_locks (household_id, year, locked_at) VALUES ({hid}, 2023, '2024-02-{10 + hid} 18:30:00+00');")
        txns = [_txn(r) for r in g.rows if r["account_id"] in HOLDINGS[hid]] + [ca_txn(r) for r in ca_rows if r["household_id"] == hid]
        cur_of = {sec_id[(hid, s)]: spec[s][3] for (h, s) in sec_id if h == hid}
        init = {a[0]: a[5] for a in ACCOUNTS}
        changes = [(aid, eff, cid, m) for cid, aid, eff, m in METHOD_CHANGES]
        res = E.compute(txns, cur_of, rates, E.method_lookup(init, changes), wash=False, ira=IRA_ACCOUNTS)
        for (y, s), f in sorted(E.year_figures(res).items()):
            if y == 2023 and f.active:
                lines.append("INSERT INTO legacy_locked_figures (household_id, year, security_id, short_term_gain_cents, "
                             f"long_term_gain_cents, wash_disallowed_cents, dividends_cents) VALUES ({hid}, 2023, {s}, "
                             f"{f.short}, {f.long}, 0, {f.dividends});")
    lines.append("COMMIT;")
    (out / "002_records.sql").write_text("\n".join(lines) + "\n")
    print(f"securities={len(sec_rows)} fx={len(fx)} transactions={len(g.rows)} actions={len(ca_rows)} share_ties={planted} "
          f"by_household={ {h: sum(1 for r in g.rows if r['account_id'] in HOLDINGS[h]) for h in (1, 2)} }")


def dec_rhu(x: Decimal) -> int:
    q = int(x)
    return q + (1 if x - q >= Decimal("0.5") else 0)


def dec_usd(cents: int, r: F) -> int:
    getcontext().prec = 28
    return dec_rhu(Decimal(cents) * (Decimal(r.numerator) / Decimal(r.denominator)))


def dec_share(c: int, m: F, q: F) -> int:
    getcontext().prec = 28
    return dec_rhu(Decimal(c) / (Decimal(q.numerator) / Decimal(q.denominator)) * (Decimal(m.numerator) / Decimal(m.denominator)))


def plant_share_ties(rows: list[dict], rates: E.Rates, cur_of: dict, rnd: random.Random, extra=(), ira=frozenset()) -> int:
    """Nudge the amount of some multi-portion USD sales by a few cents so that the share of the proceeds taken by
    the first portion is an exact half cent that per-share-first 28-digit arithmetic rounds the other way."""
    by_id = {r["id"]: r for r in rows}
    txns = [_txn(r) for r in rows]
    init = {a[0]: a[5] for a in ACCOUNTS}
    changes = [(aid, eff, cid, m) for cid, aid, eff, m in METHOD_CHANGES]
    res = E.compute(txns + list(extra), cur_of, rates, E.method_lookup(init, changes), ira=ira)
    first = {}
    for p in res.portions:
        first.setdefault(p.sale.tid, []).append(p)
    n = 0
    for tid, ps in first.items():
        if len(ps) < 2 or cur_of[ps[0].security] != "USD" or ps[0].kind != "sale" or rnd.random() > 0.6:
            continue
        sale = ps[0].sale
        m, q = ps[0].qty, sale.quantity
        P = sale.amount
        for k in range(0, 600):
            hit = None
            for c in (P + k, P - k):
                if c > 0 and (F(c) * m / q).denominator == 2 and dec_share(c, m, q) != E.rhu(F(c) * m / q):
                    hit = c
                    break
            if hit:
                r = by_id[tid]
                r["amount"] = F(hit, 100) * (1 if r["amount"] > 0 else -1)
                n += 1
                break
    return n


def _money(v: F) -> str:
    sign = "-" if v < 0 else ""
    c = abs(F(v)) * 100
    assert c.denominator == 1
    c = int(c)
    return f"{sign}{c // 100}.{c % 100:02d}"


def ca_txn(r: dict) -> E.Txn:
    """A legacy corporate_actions row as an engine event (R13)."""
    k = r["kind"]
    if k == "spinoff":
        params = {"new_security": r["new_security_id"], "ratio": F(r["ratio"]), "parent_fmv": F(r["parent_fmv"]),
                  "new_fmv": F(r["new_fmv"]), "cash_in_lieu_price": F(r["cash_in_lieu_price"])}
    elif k == "merger":
        params = {"acquirer": r.get("acquirer_security_id"), "shares_per_share": F(r["shares_per_share"]),
                  "cash_per_share": F(r["cash_per_share"]), "acquirer_fmv": F(r["acquirer_fmv"]) if r.get("acquirer_fmv") is not None else None}
    else:
        params = {"amount_per_share": F(r["amount_per_share"])}
    return E.Txn(r["id"], r["id"], None, r["security_id"], k, r["action_date"], params=params)


def _txn(r: dict) -> E.Txn:
    kind = r["kind"].strip().lower()
    t = E.Txn(r["id"], r["id"], r["account_id"], r["security_id"], kind, r["trade_date"])
    if kind in ("buy", "reinvest", "sell"):
        t.quantity, t.amount = abs(F(r["quantity"])), int(abs(F(r["amount"])) * 100)
    elif kind == "dividend":
        t.amount = int(abs(F(r["amount"])) * 100)
    elif kind == "split":
        t.ratio = (r["split_new"], r["split_old"])
    elif kind == "transfer":
        t.to_account = r["to_account_id"]
    return t


if __name__ == "__main__":
    main(Path(sys.argv[1]))
