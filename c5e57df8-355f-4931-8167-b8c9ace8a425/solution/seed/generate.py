#!/usr/bin/env python3
"""Deterministic generator for Pickpath's pre-existing data (environment and verifier seed).

Writes 001_schema.sql and 002_records.sql. The same bytes ship in environment/services/db/init/ and
tests/services/db/init/. Re-running reproduces them exactly.

Quirks, all stated in instruction.md R10: user emails with odd case and spaces; lot codes with odd case
and spaces; lots on QA hold and lots in the quarantine bin; inactive SKUs; expired stock; wall-clock
movement and order times with a fold column, clustered on the fall-back nights (2025-11-02, 2026-11-01)
and the spring-forward night (2026-03-08, including times that never happened); several positive
movements of one SKU at the very same instant (FIFO tie by movement id); PO numbers and names that look
like spreadsheet formulas or carry commas, quotes and markup; unit costs on a half-cent grid (FIFO values that
end in exactly half a cent).

Usage: python3 generate.py <out_dir>
"""

from __future__ import annotations

import random
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

SEED = 20261110
TZ = ZoneInfo("America/New_York")
N_SKUS = 5000
SCHEMA = """\
CREATE TABLE sites (
    id integer PRIMARY KEY,
    name text NOT NULL,
    timezone text NOT NULL
);
CREATE TABLE bins (
    id serial PRIMARY KEY,
    code text NOT NULL UNIQUE,
    zone text NOT NULL CHECK (zone IN ('A', 'C', 'F', 'Q')),
    aisle integer,
    bay integer,
    level integer
);
CREATE TABLE skus (
    id serial PRIMARY KEY,
    code text NOT NULL UNIQUE,
    name text NOT NULL,
    case_pack integer NOT NULL CHECK (case_pack > 0),
    unit_weight_g integer NOT NULL CHECK (unit_weight_g > 0),
    length_cm integer NOT NULL,
    width_cm integer NOT NULL,
    height_cm integer NOT NULL,
    storage text NOT NULL CHECK (storage IN ('ambient', 'chilled', 'frozen')),
    taxable boolean NOT NULL,
    reorder_point integer NOT NULL DEFAULT 0,
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE tax_rates (
    state text PRIMARY KEY,
    rate numeric(7, 5) NOT NULL,
    freight_taxable boolean NOT NULL
);
CREATE TABLE price_list (
    id serial PRIMARY KEY,
    tier text NOT NULL CHECK (tier IN ('standard', 'gold', 'platinum')),
    sku_id integer NOT NULL REFERENCES skus (id),
    unit_price_cents integer NOT NULL CHECK (unit_price_cents > 0),
    effective_from date NOT NULL,
    UNIQUE (tier, sku_id, effective_from)
);
CREATE TABLE customers (
    id serial PRIMARY KEY,
    name text NOT NULL,
    tier text NOT NULL CHECK (tier IN ('standard', 'gold', 'platinum')),
    ship_to_state text NOT NULL REFERENCES tax_rates (state),
    approval_limit_cents bigint,
    min_shelf_life_days integer NOT NULL DEFAULT 0,
    allow_split_cases boolean NOT NULL DEFAULT false,
    backorders_allowed boolean NOT NULL DEFAULT true
);
CREATE TABLE users (
    id serial PRIMARY KEY,
    email text NOT NULL,
    full_name text NOT NULL,
    role text NOT NULL CHECK (role IN ('buyer', 'approver', 'picker', 'supervisor', 'finance')),
    customer_id integer REFERENCES customers (id),
    zone text CHECK (zone IN ('A', 'C', 'F')),
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE lots (
    id serial PRIMARY KEY,
    sku_id integer NOT NULL REFERENCES skus (id),
    lot_code text NOT NULL,
    expiry_date date NOT NULL,
    bin_id integer NOT NULL REFERENCES bins (id),
    unit_cost numeric(12, 4) NOT NULL CHECK (unit_cost > 0),
    qa_hold boolean NOT NULL DEFAULT false
);
CREATE INDEX lots_sku ON lots (sku_id);
CREATE TABLE legacy_movements (
    id bigint PRIMARY KEY,
    lot_id integer NOT NULL REFERENCES lots (id),
    kind text NOT NULL CHECK (kind IN ('receipt', 'ship', 'adjust', 'writeoff')),
    units integer NOT NULL,
    local_time timestamp NOT NULL,
    fold smallint NOT NULL DEFAULT 0 CHECK (fold IN (0, 1)),
    ref text
);
CREATE INDEX legacy_movements_lot ON legacy_movements (lot_id);
CREATE TABLE legacy_orders (
    id integer PRIMARY KEY,
    number text NOT NULL UNIQUE,
    customer_id integer NOT NULL REFERENCES customers (id),
    placed_by integer REFERENCES users (id),
    placed_local timestamp NOT NULL,
    fold smallint NOT NULL DEFAULT 0 CHECK (fold IN (0, 1)),
    ship_date date NOT NULL,
    po_number text,
    status text NOT NULL CHECK (status IN ('shipped', 'cancelled')),
    subtotal_cents bigint NOT NULL,
    tax_cents bigint NOT NULL,
    freight_cents bigint NOT NULL,
    freight_tax_cents bigint NOT NULL,
    total_cents bigint NOT NULL
);
CREATE TABLE legacy_order_lines (
    order_id integer NOT NULL REFERENCES legacy_orders (id),
    line_no integer NOT NULL,
    sku_id integer NOT NULL REFERENCES skus (id),
    qty_units integer NOT NULL,
    unit_price_cents integer NOT NULL,
    PRIMARY KEY (order_id, line_no)
);
"""

STATES = [("NY", "0.08875", True), ("NJ", "0.06625", True), ("PA", "0.06000", False), ("CT", "0.06350", True),
          ("MA", "0.06250", False), ("VT", "0.06000", True), ("NH", "0.00000", False), ("DE", "0.00000", False),
          ("MD", "0.06000", False), ("VA", "0.05300", False), ("IL", "0.10250", False), ("CA", "0.07250", False)]
CATS = {  # prefix: (storage, taxable, words, shelf-life days range, case packs)
    "OIL": ("ambient", False, ["Arbequina Olive Oil", "Picual Olive Oil", "Koroneiki Oil", "Walnut Oil", "Pumpkin Seed Oil"], (300, 700), [6, 12]),
    "VIN": ("ambient", False, ["Sherry Vinegar", "Balsamic Vinegar", "Cider Vinegar", "Moscatel Vinegar"], (400, 900), [6, 12]),
    "TIN": ("ambient", False, ["Sardines in Oil", "Mackerel Fillets", "Octopus in Garlic", "Cockles", "Bonito Ventresca"], (500, 1000), [12, 24, 48]),
    "PAS": ("ambient", False, ["Bronze-cut Rigatoni", "Pici", "Orecchiette", "Fregola", "Tajarin"], (250, 600), [10, 12, 20]),
    "CHO": ("ambient", True, ["Dark Chocolate 72%", "Gianduja Bar", "Cocoa Nibs", "Milk Chocolate Buttons"], (200, 500), [10, 24, 36]),
    "SPI": ("ambient", False, ["Smoked Paprika", "Saffron Threads", "Sumac", "Urfa Chili", "Fennel Pollen"], (300, 800), [6, 12, 24]),
    "HON": ("ambient", False, ["Chestnut Honey", "Thyme Honey", "Buckwheat Honey"], (500, 1000), [6, 12]),
    "BIS": ("ambient", False, ["Oat Biscuits", "Cantucci", "Seeded Crackers", "Grissini"], (150, 360), [8, 12, 24]),
    "GFT": ("ambient", True, ["Gift Box", "Hamper Basket", "Tasting Crate"], (600, 1000), [1, 4]),
    "KIT": ("ambient", True, ["Olive Wood Board", "Cheese Knife Set", "Oil Cruet"], (900, 1200), [1, 4, 6]),
    "CHE": ("chilled", False, ["Comte 18 Month", "Manchego Curado", "Stilton", "Taleggio", "Gruyere Reserve"], (40, 160), [1, 4, 6, 12]),
    "CUR": ("chilled", False, ["Prosciutto di Parma", "Bresaola", "Finocchiona", "Lomo Iberico", "Speck"], (40, 120), [4, 6, 10]),
    "BUT": ("chilled", False, ["Cultured Butter", "Salted Butter", "Brown Butter"], (30, 90), [12, 24]),
    "BUR": ("chilled", False, ["Burrata", "Stracciatella", "Fior di Latte"], (10, 30), [6, 12]),
    "PST": ("frozen", False, ["Croissant Dough", "Puff Pastry Sheets", "Pain au Chocolat"], (150, 360), [6, 10, 24]),
    "GEL": ("frozen", True, ["Pistachio Gelato", "Stracciatella Gelato", "Lemon Sorbet"], (180, 400), [4, 6]),
    "DUM": ("frozen", False, ["Pork Dumplings", "Vegetable Gyoza", "Xiao Long Bao"], (180, 360), [10, 20, 40]),
}
QUIRKY_NAMES = ['Fish & Chips <Classic> Crisps', 'Grandma\'s "Best" Jam, Strawberry', '=HYPERLINK("x") Spread',
                '-18 Degree Gelato Tub', '+Plus Mixed Nuts', '@Home Pantry Box', 'Caffe, Latte & More',
                '<b>Bold</b> Roast Beans', 'Tab\tName? no', 'Line One Two']
FIRST = ["Maria", "James", "Aiyana", "Wei", "Priya", "Luis", "Fatima", "Noah", "Keiko", "Omar", "Elena", "Tariq",
         "Grace", "Mateo", "Hana", "Darius", "Sofia", "Kwame", "Ines", "Rowan", "Yusuf", "Lena", "Arjun", "Chloe",
         "Diego", "Amara", "Felix", "Nadia", "Theo", "Zara"]
LAST = ["Lopez", "Nguyen", "Okafor", "Chen", "Patel", "Garcia", "Haddad", "Kim", "Silva", "Novak", "Reyes", "Moreau",
        "Ibrahim", "Kowalski", "Tanaka", "Mensah", "Rossi", "Fischer", "Abara", "Lindqvist", "O'Brien", "Dubois",
        "Varga", "Sato", "Mahlangu", "Costa", "Byrne", "Aziz", "Holm", "Quinn"]
BIZ_A = ["Bluebird", "Copper Pot", "Fig & Olive", "Harbor", "Juniper", "Little Italy", "Marigold", "North Fork", "Oak",
         "Pearl Street", "Quince", "Rye & Rind", "Salt Box", "Tidewater", "Union", "Vesper", "Wild Thyme", "Yellow Door"]
BIZ_B = ["Deli", "Kitchen", "Market", "Bistro", "Provisions", "Grocer", "Trattoria", "Cafe", "Cheese Shop", "Larder"]
QUIRKY_BIZ = ['=Delights Catering', '+Plus Market', '@Home Deli', 'Smith, Jones & Co', 'The "Good" Grocer',
              '<i>Italic</i> Eats', '-Minus Bakery']


def q(s) -> str:
    if s is None:
        return "NULL"
    return "'" + str(s).replace("'", "''") + "'"


def cp(v) -> str:
    """One COPY text field."""
    if v is None:
        return "\\N"
    if isinstance(v, bool):
        return "t" if v else "f"
    return str(v).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def copy_block(table: str, cols: list[str], rows: list[tuple]) -> list[str]:
    out = [f"COPY {table} ({', '.join(cols)}) FROM stdin;"]
    out += ["\t".join(cp(v) for v in r) for r in rows]
    out.append("\\.")
    return out


def to_local(instant: datetime) -> tuple[datetime, int]:
    """UTC instant -> (naive wall clock, fold) the way the old system stored it."""
    loc = instant.astimezone(TZ)
    return loc.replace(tzinfo=None, fold=0), loc.fold


def main(out: Path) -> None:
    rnd = random.Random(SEED)
    out.mkdir(parents=True, exist_ok=True)
    (out / "001_schema.sql").write_text(SCHEMA)
    L: list[str] = ["BEGIN;", "INSERT INTO sites VALUES (1, 'Secaucus DC', 'America/New_York');"]
    for st, rate, ft in STATES:
        L.append(f"INSERT INTO tax_rates VALUES ({q(st)}, {rate}, {'true' if ft else 'false'});")

    # ---- bins ------------------------------------------------------------------------------------
    bins = []
    zone_bins: dict[str, list[int]] = {"A": [], "C": [], "F": []}
    for aisle in range(1, 17):
        zone = "A" if aisle <= 10 else ("C" if aisle <= 14 else "F")
        for bay in range(1, 21):
            for level in range(1, 5):
                bid = len(bins) + 1
                bins.append((bid, f"{zone}{aisle:02d}-{bay:02d}-{level}", zone, aisle, bay, level))
                zone_bins[zone].append(bid)
    qa_bin = len(bins) + 1
    bins.append((qa_bin, "QA-01", "Q", None, None, None))
    L += copy_block("bins", ["id", "code", "zone", "aisle", "bay", "level"], bins)
    L.append(f"SELECT setval('bins_id_seq', {len(bins)});")

    # ---- skus ------------------------------------------------------------------------------------
    skus = []
    cat_names = list(CATS)
    per_cat: dict[str, int] = {}
    for i in range(N_SKUS):
        cat = cat_names[i % len(cat_names)] if i < 3 * len(cat_names) else rnd.choice(cat_names)
        storage, taxable, words, _, packs = CATS[cat]
        per_cat[cat] = per_cat.get(cat, 0) + 1
        code = f"{cat}-{per_cat[cat]:05d}"
        name = f"{rnd.choice(words)} {rnd.choice(['', 'Reserve ', 'Organic ', 'Classic '])}{rnd.choice(['250g', '500g', '750ml', '1kg', '2kg', '125g', '6-pack', 'Large'])}".replace("  ", " ")
        if rnd.random() < 0.004:
            name = rnd.choice(QUIRKY_NAMES).replace("\t", " ")
        pack = rnd.choice(packs)
        w = rnd.randint(60, 1400)
        dims = (rnd.randint(15, 60), rnd.randint(10, 45), rnd.randint(6, 40))
        reorder = 0 if rnd.random() < 0.5 else rnd.choice([12, 24, 48, 96, 200, 400])
        active = rnd.random() > 0.03
        skus.append((i + 1, code, name, pack, w, dims[0], dims[1], dims[2], storage, taxable, reorder, active))
    L += copy_block("skus", ["id", "code", "name", "case_pack", "unit_weight_g", "length_cm", "width_cm", "height_cm",
                             "storage", "taxable", "reorder_point", "active"], skus)
    L.append(f"SELECT setval('skus_id_seq', {len(skus)});")

    # ---- prices ----------------------------------------------------------------------------------
    prices = []
    for s in skus:
        base = rnd.randint(180, 9000)
        no_plat = rnd.random() < 0.03
        changes = [date(2026, 1, 1)]
        if rnd.random() < 0.45:
            changes.append(date(2026, 1, 1) + timedelta(days=rnd.randint(120, 540)))
        if rnd.random() < 0.10:
            changes.append(date(2027, 6, 1) + timedelta(days=rnd.randint(0, 200)))
        for k, eff in enumerate(sorted(set(changes))):
            p = int(base * (1 + 0.06 * k))
            for tier, f in (("standard", 1.0), ("gold", 0.94), ("platinum", 0.89)):
                if tier == "platinum" and no_plat:
                    continue
                prices.append((len(prices) + 1, tier, s[0], max(1, int(p * f)), eff.isoformat()))
    L += copy_block("price_list", ["id", "tier", "sku_id", "unit_price_cents", "effective_from"], prices)
    L.append(f"SELECT setval('price_list_id_seq', {len(prices)});")

    # ---- customers and users ---------------------------------------------------------------------
    customers = []
    for i in range(160):
        nm = f"{rnd.choice(BIZ_A)} {rnd.choice(BIZ_B)}"
        if i % 23 == 5:
            nm = QUIRKY_BIZ[(i // 23) % len(QUIRKY_BIZ)]
        tier = rnd.choice(["standard", "standard", "gold", "platinum"])
        st = rnd.choice(STATES)[0]
        limit = None if rnd.random() < 0.4 else rnd.choice([25000, 50000, 100000, 250000, 500000])
        customers.append((i + 1, nm, tier, st, limit, rnd.choice([0, 3, 5, 7, 10, 14, 21, 30]),
                          rnd.random() < 0.35, rnd.random() < 0.7))
    L += copy_block("customers", ["id", "name", "tier", "ship_to_state", "approval_limit_cents", "min_shelf_life_days",
                                  "allow_split_cases", "backorders_allowed"], customers)
    L.append(f"SELECT setval('customers_id_seq', {len(customers)});")

    users = []
    used: set[str] = set()

    def user(role, name, email, cust=None, zone=None, active=True, quirk=True):
        uid = len(users) + 1
        stored = email
        if quirk:
            r = rnd.random()
            if r < 0.07:
                stored = email.split("@")[0].title() + "@" + email.split("@")[1].title()
            elif r < 0.11:
                stored = f"  {email} "
            elif r < 0.13:
                stored = email.upper()
        used.add(email)
        users.append((uid, stored, name, role, cust, zone, active))
        return uid

    user("finance", "Priya Raman", "priya.raman@larderline.example", quirk=False)
    user("finance", "Tom Becker", "tom.becker@larderline.example", quirk=False)
    user("finance", "Iris Vann", "iris.vann@larderline.example", quirk=False, active=False)
    user("supervisor", "Grace Okafor", "grace.okafor@larderline.example", quirk=False)
    user("supervisor", "Luis Ortega", "luis.ortega@larderline.example", quirk=False)
    user("supervisor", "Hana Sato", "hana.sato@larderline.example")
    for z in "ACF":
        for k in range(6):
            fn, ln = rnd.choice(FIRST), rnd.choice(LAST)
            user("picker", f"{fn} {ln}", f"{fn}.{ln}.p{z}{k}@larderline.example".lower().replace("'", ""), zone=z,
                 active=rnd.random() > 0.1)
    for c in customers:
        dom = "".join(ch for ch in c[1].lower() if ch.isalnum())[:14] or "shop"
        for k in range(rnd.randint(1, 4)):
            fn, ln = rnd.choice(FIRST), rnd.choice(LAST)
            user("buyer", f"{fn} {ln}", f"{fn}.{ln}.{c[0]}{k}@{dom}.example".lower().replace("'", ""), cust=c[0],
                 active=rnd.random() > 0.05)
        for k in range(rnd.randint(0, 2)):
            fn, ln = rnd.choice(FIRST), rnd.choice(LAST)
            user("approver", f"{fn} {ln}", f"{fn}.{ln}.{c[0]}a{k}@{dom}.example".lower().replace("'", ""), cust=c[0])
    L += copy_block("users", ["id", "email", "full_name", "role", "customer_id", "zone", "active"], users)
    L.append(f"SELECT setval('users_id_seq', {len(users)});")

    # ---- lots and movements ----------------------------------------------------------------------
    start = datetime(2025, 11, 1, 12, tzinfo=timezone.utc)
    end = datetime(2026, 11, 9, 23, tzinfo=timezone.utc)
    span = int((end - start).total_seconds())
    fold_nights = [datetime(2025, 11, 2, 5, 0, tzinfo=timezone.utc), datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc)]
    gap_night = datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)  # 02:00 EST would be 07:00Z

    def rand_instant() -> datetime:
        r = rnd.random()
        if r < 0.05:  # inside a repeated hour (01:00-02:00 twice: 05:00Z-07:00Z)
            return rnd.choice(fold_nights) + timedelta(seconds=rnd.randint(0, 7199))
        return start + timedelta(seconds=rnd.randint(0, span))

    lots, mov = [], []
    gap_rows = []  # movements stored at wall-clock times that never happened
    zone_of = {"ambient": "A", "chilled": "C", "frozen": "F"}
    for s in skus:
        sid, code, storage = s[0], s[1], s[8]
        cat = code.split("-")[0]
        lo, hi = CATS[cat][3]
        nlots = rnd.choice([2, 3, 4, 5, 6, 6, 7, 8, 9, 10])
        pair_fold = rnd.random() < 0.04  # two receipts in one repeated hour, wall clock out of order
        same_instant = rnd.random() < 0.01
        for k in range(nlots):
            lid = len(lots) + 1
            t0 = rand_instant()
            if pair_fold and k < 2:
                night = rnd.choice(fold_nights)
                t0 = night + timedelta(minutes=40) if k == 0 else night + timedelta(minutes=70)  # 01:40 fold0, 01:10 fold1
            if same_instant and k < 2:
                t0 = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)
            exp = (t0.astimezone(TZ).date() + timedelta(days=rnd.randint(lo, hi)))
            if rnd.random() < 0.06:
                exp = date(2026, 11, 10) + timedelta(days=rnd.randint(-40, 40))
            lc = f"L{t0.astimezone(TZ).strftime('%y%j')}-{rnd.randint(1, 99):02d}{chr(65 + k)}"
            r = rnd.random()
            if r < 0.04:
                lc = lc.lower()
            elif r < 0.06:
                lc = f" {lc}  "
            bin_id = qa_bin if rnd.random() < 0.015 else rnd.choice(zone_bins[zone_of[storage]])
            cost = Decimal(rnd.randint(9000, 900000)) / Decimal(10000)
            if rnd.random() < 0.12:  # costs on a half-cent grid: odd unit counts value at exactly half a cent
                cost = Decimal(2 * rnd.randint(9, 900) + 1) / Decimal(200)
            hold = rnd.random() < 0.02
            lots.append((lid, sid, lc, exp.isoformat(), bin_id, f"{cost:.4f}", hold))
            qty = rnd.choice([12, 24, 36, 48, 60, 96, 120, 144, 200, 240, 360, 480]) * (1 if rnd.random() < 0.8 else 3)
            events = [(t0, "receipt", qty)]
            if rnd.random() < 0.08:
                events.append((t0 + timedelta(days=rnd.randint(1, 20), minutes=rnd.randint(0, 600)), "receipt",
                               rnd.choice([12, 24, 48])))
            on = qty
            t = t0
            for _ in range(rnd.choice([0, 1, 1, 2, 2, 3, 4])):
                t = t + timedelta(days=rnd.randint(1, 40), minutes=rnd.randint(0, 900))
                if t > end:
                    break
                kind = rnd.choices(["ship", "adjust", "writeoff"], [85, 10, 5])[0]
                if kind == "ship":
                    u = -rnd.randint(1, max(1, on))
                elif kind == "adjust":
                    u = rnd.randint(-min(on, 6), 6)
                else:
                    u = -rnd.randint(1, max(1, min(on, 12)))
                if on + u < 0 or u == 0:
                    continue
                events.append((t, kind, u))
                on += u
            events.sort(key=lambda e: e[0])
            run = 0
            for at, kind, u in events:
                run += u
                assert run >= 0
            for at, kind, u in events:
                if at > end:
                    continue
                lt, fold = to_local(at)
                if rnd.random() < 0.01 and kind != "receipt":
                    fold = 1 if lt.hour not in (1,) else fold  # fold=1 where it changes nothing (quirk)
                mov.append([None, lid, kind, u, lt, fold, None])
        if rnd.random() < 0.01 and lots:  # receipts written at a wall-clock time that never happened
            lid = lots[-1][0]
            gap_rows.append(lid)
    # gap-hour receipts (02:00-03:00 on 2026-03-08 never happened): extra small receipts on existing lots
    for lid in gap_rows:
        minute = rnd.randint(0, 59)
        fold = rnd.randint(0, 1)
        mov.append([None, lid, "adjust", rnd.randint(1, 6), datetime(2026, 3, 8, 2, minute), fold, "gap"])
    # movement ids: recorded order is not time order (ids follow lots, then the gap fixes)
    for i, m in enumerate(mov):
        m[0] = 10_000_000 + i + 1
        if m[6] is None:
            m[6] = rnd.choice(["", "SO-" + str(rnd.randint(100000, 199999)), "CC", "DMG"]) if m[2] != "receipt" else "PO-" + str(rnd.randint(4000, 9999))
    L += copy_block("lots", ["id", "sku_id", "lot_code", "expiry_date", "bin_id", "unit_cost", "qa_hold"], lots)
    L.append(f"SELECT setval('lots_id_seq', {len(lots)});")
    L += copy_block("legacy_movements", ["id", "lot_id", "kind", "units", "local_time", "fold", "ref"],
                    [(m[0], m[1], m[2], m[3], m[4].strftime("%Y-%m-%d %H:%M:%S"), m[5], m[6]) for m in mov])

    # ---- legacy orders ---------------------------------------------------------------------------
    buyers_by_cust: dict[int, list[int]] = {}
    for u in users:
        if u[3] in ("buyer", "approver"):
            buyers_by_cust.setdefault(u[4], []).append(u[0])
    orders, olines = [], []
    pos = ["", "", "", "PO 1182", "=SUM(A1:A9)", "+1-555-0100", "-standing", "@weekly", "Smith, Jones", 'say "rush"',
           "Q4/Holiday", "<b>urgent</b>"]
    for i in range(6000):
        oid = 70001 + i
        cust = rnd.choice(customers)
        r = rnd.random()
        if r < 0.03:
            at = rnd.choice(fold_nights) + timedelta(seconds=rnd.randint(0, 7199))
        elif r < 0.04:
            at = gap_night + timedelta(minutes=rnd.randint(-30, 90))
        else:
            at = start + timedelta(seconds=rnd.randint(0, span))
        lt, fold = to_local(at)
        ship = lt.date() + timedelta(days=rnd.randint(1, 14))
        n = rnd.randint(1, 5)
        sub = 0
        for ln in range(1, n + 1):
            s = rnd.choice(skus)
            qty = s[3] * rnd.randint(1, 6)
            price = rnd.randint(200, 9000)
            olines.append((oid, ln, s[0], qty, price))
            sub += qty * price
        tax = int(sub * 0.02)
        fr = 0 if sub >= 250000 else 1200 + 90 * rnd.randint(4, 80)
        ftax = int(fr * 0.06)
        po = rnd.choice(pos)
        status = "cancelled" if rnd.random() < 0.1 else "shipped"
        orders.append((oid, f"SO-{oid}", cust[0], rnd.choice(buyers_by_cust.get(cust[0], [None])), lt.strftime("%Y-%m-%d %H:%M:%S"),
                       fold, ship.isoformat(), po, status, sub, tax, fr, ftax, sub + tax + fr + ftax))
    L += copy_block("legacy_orders", ["id", "number", "customer_id", "placed_by", "placed_local", "fold", "ship_date",
                                      "po_number", "status", "subtotal_cents", "tax_cents", "freight_cents",
                                      "freight_tax_cents", "total_cents"], orders)
    L += copy_block("legacy_order_lines", ["order_id", "line_no", "sku_id", "qty_units", "unit_price_cents"], olines)
    L.append("COMMIT;")
    (out / "002_records.sql").write_text("\n".join(L) + "\n")
    print(f"seed: {len(skus)} skus, {len(lots)} lots, {len(mov)} movements, {len(prices)} prices, "
          f"{len(customers)} customers, {len(users)} users, {len(orders)} legacy orders", file=sys.stderr)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
