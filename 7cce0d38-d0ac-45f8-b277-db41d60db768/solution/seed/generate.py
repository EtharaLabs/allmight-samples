#!/usr/bin/env python3
"""Deterministic generator for Seatwise's pre-existing data (environment and verifier seed).

Writes 001_schema.sql and 002_records.sql. The same bytes ship in environment/services/db/init/ and
tests/services/db/init/. Re-running with the same SEED reproduces them exactly.

The old platform: 3 organizations, 4 venues (Chicago, Chicago, Denver, Phoenix), 58 events (past and
upcoming, one cancelled), about 8.5k orders and 21k tickets with refunds, resold seats, transfers,
complimentary tickets, admissions (some in the repeated hour of a DST fall-back, recorded with fold=1),
old fee and tax rules, quirky addresses (case, whitespace), and names/gates that need CSV care.

Usage: python3 generate.py <out_dir>
"""

from __future__ import annotations

import random
import string
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SEED = 20261110
UTC = timezone.utc
NOW_CAP = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)   # the old platform's data ends here
# Past shows whose admissions run through the repeated hour of a fall-back night (seconds around the start).
FALLBACK_SHOWS = {4: (-1800, 4 * 3600 - 60), 28: (-1800, 4 * 3600 + 1200), 37: (-7200, 3600)}
# Upcoming events with some orders placed in the repeated hour of the 2026-11-01 fall-back night.
FALLBACK_ORDERS = {12: datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc), 39: datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)}

ORGS = [(1, "Lantern Room Presents"), (2, "Copperline Live"), (3, "Saguaro Sounds")]
VENUES = [  # id, org, name, timezone, address
    (1, 1, "The Lantern Room", "America/Chicago", "1820 W Division St, Chicago, IL"),
    (2, 1, "Lantern Annex", "America/Chicago", "1824 W Division St, Chicago, IL"),
    (3, 2, "Copper Hall", "America/Denver", "2100 Larimer St, Denver, CO"),
    (4, 3, "Saguaro Theater", "America/Phoenix", "410 N 3rd St, Phoenix, AZ"),
]
SECTIONS = [  # venue, code, name, rows, seats per row, accessible
    (1, "BALC", "Balcony", "ABCDEF", 16, []),
    (1, "FLOOR", "Floor", "ABCDEFGHIJKL", 20, ["A1", "A2", "A19", "A20"]),
    (2, "MAIN", "Main room", "ABCDEFGH", 14, ["A1", "A14"]),
    (3, "BOX", "Boxes", "AB", 6, []),
    (3, "MEZZ", "Mezzanine", "ABCDEFGH", 20, ["A1", "A20"]),
    (3, "ORCH", "Orchestra", "ABCDEFGHIJKLMNOPQRST", 24, ["A1", "A2", "A23", "A24", "T1", "T24"]),
    (4, "BALC", "Balcony", "ABCDEFGHIJ", 18, []),
    (4, "ORCH", "Orchestra", "ABCDEFGHIJKLMNOP", 22, ["A1", "A22", "P1", "P22"]),
    (4, "PIT", "Pit", "ABC", 10, []),
]
PRICE_BANDS = {"BALC": (2500, 4200), "FLOOR": (3500, 6200), "MAIN": (2200, 3800), "BOX": (9000, 15000),
               "MEZZ": (4000, 6500), "ORCH": (4500, 8800), "PIT": (6000, 9900)}
FIRST = ["Maria", "James", "Aiyana", "Wei", "Priya", "Luis", "Fatima", "Noah", "Keiko", "Omar", "Elena",
         "Tariq", "Grace", "Mateo", "Hana", "Darius", "Sofia", "Kwame", "Ines", "Rowan", "Yusuf", "Lena",
         "Arjun", "Chloe", "Diego", "Amara", "Felix", "Nadia", "Theo", "Zara", "Bea", "Colm", "Dev", "Esme"]
LAST = ["Lopez", "Nguyen", "Okafor", "Chen", "Patel", "Garcia", "Haddad", "Kim", "Silva", "Novak", "Reyes",
        "Moreau", "Ibrahim", "Kowalski", "Tanaka", "Mensah", "Rossi", "Fischer", "Abara", "Lindqvist",
        "O'Brien", "Dubois", "Varga", "Sato", "Mahlangu", "Costa", "Byrne", "Aziz", "Holm", "Quinn"]
DOMAINS = ["mail.example", "fastpost.example", "inbox.example", "postbox.example"]
GATES = ["North", "South", "Box Office", "Door 2", "-Side door", "@Balcony stair"]
WORDS = ["Midnight", "Velvet", "Static", "Harbor", "Juniper", "Neon", "Paper", "Copper", "Saguaro", "Lantern",
         "Echo", "Wild", "Golden", "Hollow", "Silver", "Northern", "Desert", "River", "Little", "Electric"]
NOUNS = ["Orchestra", "Quartet", "Revue", "Sessions", "Collective", "Trio", "Choir", "Band", "Players", "Assembly"]

SCHEMA = """\
CREATE TABLE organizations (
    id serial PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE users (
    id serial PRIMARY KEY,
    email text NOT NULL,
    full_name text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE memberships (
    organization_id integer NOT NULL REFERENCES organizations (id),
    user_id integer NOT NULL REFERENCES users (id),
    role text NOT NULL CHECK (role IN ('owner', 'manager', 'box_office', 'scanner')),
    PRIMARY KEY (organization_id, user_id)
);
CREATE TABLE venues (
    id serial PRIMARY KEY,
    organization_id integer NOT NULL REFERENCES organizations (id),
    name text NOT NULL,
    timezone text NOT NULL,
    address text NOT NULL
);
CREATE TABLE sections (
    id serial PRIMARY KEY,
    venue_id integer NOT NULL REFERENCES venues (id),
    code text NOT NULL,
    name text NOT NULL,
    UNIQUE (venue_id, code)
);
CREATE TABLE seats (
    id serial PRIMARY KEY,
    section_id integer NOT NULL REFERENCES sections (id),
    row_label text NOT NULL,
    seat_number integer NOT NULL,
    accessible boolean NOT NULL DEFAULT false,
    UNIQUE (section_id, row_label, seat_number)
);
CREATE TABLE events (
    id serial PRIMARY KEY,
    organization_id integer NOT NULL REFERENCES organizations (id),
    venue_id integer NOT NULL REFERENCES venues (id),
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    starts_local timestamp NOT NULL,
    starts_fold smallint NOT NULL DEFAULT 0 CHECK (starts_fold IN (0, 1)),
    on_sale_local timestamp,
    on_sale_fold smallint NOT NULL DEFAULT 0 CHECK (on_sale_fold IN (0, 1)),
    status text NOT NULL,
    facility_fee_cents integer NOT NULL CHECK (facility_fee_cents >= 0)
);
CREATE TABLE event_prices (
    event_id integer NOT NULL REFERENCES events (id),
    section_id integer NOT NULL REFERENCES sections (id),
    price_cents integer NOT NULL CHECK (price_cents > 0),
    PRIMARY KEY (event_id, section_id)
);
CREATE TABLE legacy_orders (
    id integer PRIMARY KEY,
    event_id integer NOT NULL REFERENCES events (id),
    buyer_email text NOT NULL,
    buyer_name text NOT NULL,
    placed_local timestamp NOT NULL,
    placed_fold smallint NOT NULL DEFAULT 0 CHECK (placed_fold IN (0, 1)),
    total_cents integer NOT NULL,
    old_payment_ref text NOT NULL
);
CREATE INDEX legacy_orders_event ON legacy_orders (event_id);
CREATE TABLE legacy_tickets (
    id integer PRIMARY KEY,
    order_id integer NOT NULL REFERENCES legacy_orders (id),
    seat_id integer NOT NULL REFERENCES seats (id),
    holder_email text NOT NULL,
    holder_name text NOT NULL,
    status text NOT NULL CHECK (status IN ('valid', 'comp', 'refunded')),
    face_cents integer NOT NULL,
    discount_cents integer NOT NULL DEFAULT 0,
    service_fee_cents integer NOT NULL,
    facility_fee_cents integer NOT NULL,
    tax_cents integer NOT NULL,
    total_cents integer NOT NULL,
    refunded_cents integer NOT NULL DEFAULT 0,
    refunded_local timestamp,
    refunded_fold smallint NOT NULL DEFAULT 0 CHECK (refunded_fold IN (0, 1)),
    barcode text NOT NULL UNIQUE,
    checked_in_local timestamp,
    checked_in_fold smallint NOT NULL DEFAULT 0 CHECK (checked_in_fold IN (0, 1)),
    checked_in_gate text
);
CREATE INDEX legacy_tickets_order ON legacy_tickets (order_id);
CREATE INDEX legacy_tickets_seat ON legacy_tickets (seat_id);
CREATE TABLE legacy_transfers (
    id integer PRIMARY KEY,
    ticket_id integer NOT NULL REFERENCES legacy_tickets (id),
    from_email text NOT NULL,
    to_email text NOT NULL,
    transferred_local timestamp NOT NULL,
    transferred_fold smallint NOT NULL DEFAULT 0 CHECK (transferred_fold IN (0, 1))
);
CREATE INDEX legacy_transfers_ticket ON legacy_transfers (ticket_id);
"""


def q(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def local_fold(instant: datetime, tz: str) -> tuple[str, int]:
    """UTC instant -> (naive local wall-clock text, fold) at the venue."""
    loc = instant.astimezone(ZoneInfo(tz))
    naive = loc.replace(tzinfo=None)
    fold = 0
    first = naive.replace(fold=0).replace(tzinfo=ZoneInfo(tz)).astimezone(UTC)
    if first != instant.astimezone(UTC):
        fold = 1
    return naive.strftime("%Y-%m-%d %H:%M:%S"), fold


def to_instant(naive: datetime, fold: int, tz: str) -> datetime:
    return naive.replace(fold=fold, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def half_even(num: int, den: int) -> int:
    qt, r = divmod(num, den)
    if 2 * r > den or (2 * r == den and qt % 2 == 1):
        qt += 1
    return qt


class Gen:
    def __init__(self):
        self.rnd = random.Random(SEED)
        self.used_codes: set[str] = set()
        self.people: list[tuple[str, str]] = []
        rnd = self.rnd
        seen = set()
        while len(self.people) < 6500:
            fn, ln = rnd.choice(FIRST), rnd.choice(LAST)
            n = rnd.randrange(1, 9999)
            email = f"{fn}.{ln}.{n}@{rnd.choice(DOMAINS)}".lower().replace("'", "")
            if email in seen:
                continue
            seen.add(email)
            name = f"{fn} {ln}"
            roll = rnd.random()
            if roll < 0.004:
                name = f"{ln}, {fn} \"{fn[:2]}\""
            elif roll < 0.007:
                name = f"={fn} {ln}"
            elif roll < 0.009:
                name = f"+{fn} {ln}"
            self.people.append((email, name))

    def quirk(self, email: str) -> str:
        r = self.rnd.random()
        if r < 0.05:
            local, dom = email.split("@")
            return local.title() + "@" + dom.upper()
        if r < 0.08:
            return f"  {email} "
        if r < 0.10:
            return email.upper()
        if r < 0.11:
            return f"\t{email}"
        return email

    def barcode(self, prefix: str) -> str:
        while True:
            c = prefix + "-" + "".join(self.rnd.choice(string.ascii_uppercase + string.digits) for _ in range(10))
            if c not in self.used_codes:
                self.used_codes.add(c)
                return c


def main(out: Path) -> None:
    g = Gen()
    rnd = g.rnd
    out.mkdir(parents=True, exist_ok=True)
    (out / "001_schema.sql").write_text(SCHEMA)
    L: list[str] = ["BEGIN;"]
    for oid, name in ORGS:
        L.append(f"INSERT INTO organizations (id, name, created_at) VALUES ({oid}, {q(name)}, '2023-0{oid + 1}-15T17:00:00Z');")

    # ---- members ---------------------------------------------------------------------------------
    staff = [
        # org, role, email (stored), name
        (1, "owner", "nadia.brooks@lanternroom.example", "Nadia Brooks"),
        (1, "manager", "Theo.Marsh@LanternRoom.example", "Theo Marsh"),
        (1, "manager", "ines.carvalho@lanternroom.example", "Ines Carvalho"),
        (1, "box_office", "  dev.raman@lanternroom.example ", "Dev Raman"),
        (1, "box_office", "kat.oyelaran@lanternroom.example", "Kat Oyelaran"),
        (1, "scanner", "sam.ortiz@lanternroom.example", "Sam Ortiz"),
        (1, "scanner", "ROWAN.PIKE@LANTERNROOM.EXAMPLE", "Rowan Pike"),
        (1, "scanner", "jo.feld@gatecrew.example", "Jo Feld"),
        (2, "owner", "marcus.hale@copperline.example", "Marcus Hale"),
        (2, "manager", "priya.anand@copperline.example", "Priya Anand"),
        (2, "manager", "Leo.Brandt@Copperline.example", "Leo Brandt"),
        (2, "box_office", "amara.osei@copperline.example", "Amara Osei"),
        (2, "box_office", " wren.tully@copperline.example", "Wren Tully"),
        (2, "scanner", "eli.novak@copperline.example", "Eli Novak"),
        (2, "scanner", "mo.farah@copperline.example", "Mo Farah"),
        (3, "owner", "lucia.ferreira@saguarosounds.example", "Lucia Ferreira"),
        (3, "manager", "omar.saleh@saguarosounds.example", "Omar Saleh"),
        (3, "manager", "Grace.Yoon@SaguaroSounds.example ", "Grace Yoon"),
        (3, "box_office", "hector.ruiz@saguarosounds.example", "Hector Ruiz"),
        (3, "box_office", "bea.lind@saguarosounds.example", "Bea Lind"),
        (3, "scanner", "tomas.reyes@saguarosounds.example", "Tomas Reyes"),
        (3, "scanner", "ana.kova@saguarosounds.example", "Ana Kova"),
    ]
    uid_by_email: dict[str, int] = {}
    for org, role, email, name in staff:
        norm = email.strip().lower()
        if norm not in uid_by_email:
            uid = len(uid_by_email) + 1
            uid_by_email[norm] = uid
            L.append(f"INSERT INTO users (id, email, full_name, created_at) VALUES ({uid}, {q(email)}, {q(name)}, "
                     f"'2024-01-{uid:02d}T12:00:00Z');")
        L.append(f"INSERT INTO memberships VALUES ({org}, {uid_by_email[norm]}, {q(role)});")
    # Jo Feld also scans for Copperline (one account, two organizations, two roles).
    L.append(f"INSERT INTO memberships VALUES (2, {uid_by_email['jo.feld@gatecrew.example']}, 'scanner');")

    # ---- venues, sections, seats ------------------------------------------------------------------
    for vid, org, name, tz, addr in VENUES:
        L.append(f"INSERT INTO venues (id, organization_id, name, timezone, address) VALUES ({vid}, {org}, {q(name)}, {q(tz)}, {q(addr)});")
    seats_by_section: dict[int, list[tuple[int, str, int, bool]]] = {}
    section_ids: dict[tuple[int, str], int] = {}
    seat_rows = []
    sid_seq, seat_seq = 0, 0
    for vid, code, name, rows, per_row, acc in SECTIONS:
        sid_seq += 1
        section_ids[(vid, code)] = sid_seq
        L.append(f"INSERT INTO sections (id, venue_id, code, name) VALUES ({sid_seq}, {vid}, {q(code)}, {q(name)});")
        lst = []
        for r in rows:
            for n in range(1, per_row + 1):
                seat_seq += 1
                a = f"{r}{n}" in acc
                lst.append((seat_seq, r, n, a))
                seat_rows.append(f"({seat_seq}, {sid_seq}, {q(r)}, {n}, {q(a)})")
        seats_by_section[sid_seq] = lst
    for i in range(0, len(seat_rows), 500):
        L.append("INSERT INTO seats (id, section_id, row_label, seat_number, accessible) VALUES " + ",".join(seat_rows[i:i + 500]) + ";")

    # ---- events ----------------------------------------------------------------------------------
    plan = []  # (venue, local start naive, fold, past?)

    def add(vid, y, m, d, hh, mm=0, fold=0):
        plan.append((vid, datetime(y, m, d, hh, mm), fold))
    # Upcoming events lie in 2029, more than two years after the data was taken (2026-11-09), so the data stays
    # valid for a grader running long after delivery; the two repeated-hour shows sit on the 2029-11-04 fall-back.
    # Lantern Room: 10 past + 4 upcoming (a late show across the 2025-11-02 fall-back; a 01:15 show in the repeated hour)
    for (m, d) in [(9, 12), (10, 3), (10, 24), (11, 1), (12, 5), (1, 16), (2, 20), (4, 10), (6, 19), (9, 25)]:
        y = 2025 if m >= 9 else 2026
        add(1, y, m, d, 23 if (m, d) == (11, 1) else 20)
    for (m, d, hh, mm, f) in [(2, 12, 20, 0, 0), (6, 12, 20, 0, 0), (11, 4, 1, 15, 0), (12, 31, 21, 30, 0)]:
        add(1, 2029, m, d, hh, mm, f)
    # Lantern Annex: 7 past + 3 upcoming
    for (y, m, d) in [(2025, 9, 20), (2025, 11, 15), (2026, 1, 24), (2026, 3, 7), (2026, 5, 2), (2026, 7, 18), (2026, 10, 10)]:
        add(2, y, m, d, 19, 30)
    for (m, d) in [(3, 13), (5, 22), (9, 18)]:
        add(2, 2029, m, d, 19, 30)
    # Copper Hall: 13 past + 5 upcoming (a 01:30 show in the second, standard-time 01:30 of 2026-11-01 and 2029-11-04)
    for (y, m, d, hh, mm, f) in [(2025, 9, 6, 20, 0, 0), (2025, 9, 27, 20, 0, 0), (2025, 10, 18, 20, 0, 0),
                                 (2025, 11, 1, 22, 30, 0), (2025, 12, 13, 20, 0, 0), (2026, 1, 31, 20, 0, 0),
                                 (2026, 3, 14, 20, 0, 0), (2026, 4, 25, 20, 0, 0), (2026, 6, 6, 20, 0, 0),
                                 (2026, 7, 25, 20, 0, 0), (2026, 9, 12, 20, 0, 0), (2026, 10, 17, 20, 0, 0),
                                 (2026, 11, 1, 1, 30, 1)]:
        add(3, y, m, d, hh, mm, f)
    for (m, d, hh, mm, f) in [(1, 23, 20, 0, 0), (4, 17, 20, 0, 0), (7, 3, 20, 0, 0), (11, 4, 1, 30, 1), (12, 18, 20, 0, 0)]:
        add(3, 2029, m, d, hh, mm, f)
    # Saguaro Theater (no DST): 12 past + 4 upcoming
    for (y, m, d) in [(2025, 9, 13), (2025, 10, 11), (2025, 11, 2), (2025, 12, 20), (2026, 1, 10), (2026, 2, 14),
                      (2026, 3, 21), (2026, 4, 18), (2026, 5, 30), (2026, 8, 22), (2026, 9, 19), (2026, 10, 24)]:
        add(4, y, m, d, 19, 0)
    for (m, d) in [(2, 27), (3, 14), (10, 30), (11, 7)]:
        add(4, 2029, m, d, 19, 0)

    venue_by_id = {v[0]: v for v in VENUES}
    sections_of = {}
    for (vid, code), sid in section_ids.items():
        sections_of.setdefault(vid, []).append((code, sid))
    order_id, ticket_id, transfer_id = 0, 0, 0
    order_rows, ticket_rows, transfer_rows = [], [], []
    cancelled_event = 8  # a past Lantern Room event cancelled by the old platform; every ticket refunded
    over_limit_buyers: list[str] = []
    for eid, (vid, start_naive, fold) in enumerate(plan, start=1):
        _, org, vname, tz, _ = venue_by_id[vid]
        start = to_instant(start_naive, fold, tz)
        past = start < datetime(2026, 11, 10, tzinfo=UTC)
        title = f"{rnd.choice(WORDS)} {rnd.choice(NOUNS)}"
        if eid % 11 == 3:
            title = f"{title} & Friends, \"Live\""
        fee = rnd.choice([0, 150, 200, 250, 275, 300])
        status = "cancelled" if eid == cancelled_event else "published"
        on_sale = start - timedelta(days=rnd.randrange(45, 120))
        if not past:
            on_sale = min(on_sale, datetime(2026, 9, 1, 15, 0, tzinfo=UTC))
        osl, osf = local_fold(on_sale, tz)
        sl, sf = local_fold(start, tz)
        assert (sl, sf) == (start_naive.strftime("%Y-%m-%d %H:%M:%S"), fold)
        L.append(f"INSERT INTO events (id, organization_id, venue_id, name, description, starts_local, starts_fold, "
                 f"on_sale_local, on_sale_fold, status, facility_fee_cents) VALUES ({eid}, {org}, {vid}, {q(title)}, "
                 f"{q('An evening at ' + vname + '.')}, {q(sl)}, {sf}, {q(osl)}, {osf}, {q(status)}, {fee});")
        prices = {}
        for code, sid in sorted(sections_of[vid]):
            lo, hi = PRICE_BANDS[code]
            p = rnd.randrange(lo, hi, 10)
            if rnd.random() < 0.3:
                p = p - p % 40 + 20  # current price on a 7.5 % half-cent boundary
            prices[sid] = p
            L.append(f"INSERT INTO event_prices VALUES ({eid}, {sid}, {p});")
        # ---- orders and tickets ----
        sell = rnd.uniform(0.72, 0.97) if past else rnd.uniform(0.25, 0.6)
        all_seats = [(sid, s) for code, sid in sections_of[vid] for s in seats_by_section[sid]]
        rnd.shuffle(all_seats)
        target = int(len(all_seats) * sell)
        free = all_seats[:]
        sold_tickets = []
        old_rules = start < datetime(2026, 3, 1, tzinfo=UTC)
        pending_resale = []
        n_orders_event = 0
        while target > 0 and free:
            qty = min(target, rnd.choice([1, 2, 2, 2, 3, 4, 4, 5, 6]))
            pick = [free.pop() for _ in range(min(qty, len(free)))]
            if pending_resale and rnd.random() < 0.5:
                free.insert(0, pick[0])
                pick = [pending_resale.pop()] + pick[1:]
            target -= len(pick)
            order_id += 1
            n_orders_event += 1
            email, name = rnd.choice(g.people)
            if not past and eid in (12, 40, 56) and n_orders_event <= 3:
                # buyers who already have more than the new 6-ticket limit (old platform allowed 8)
                email, name = g.people[100 + eid + n_orders_event % 2]
                over_limit_buyers.append(email)
            horizon = min(start, NOW_CAP)
            placed = on_sale + timedelta(seconds=rnd.randrange(0, max(60, int((horizon - on_sale).total_seconds() * 0.9))))
            if eid in FALLBACK_ORDERS and n_orders_event % 9 == 0:
                # some buyers ordered during the repeated 01:00-02:00 hour of the 2026-11-01 fall-back night
                placed = FALLBACK_ORDERS[eid] + timedelta(seconds=rnd.randrange(0, 7200))
            pl, pf = local_fold(placed, tz)
            comp = rnd.random() < 0.02
            if comp:
                email, name = rnd.choice([s[2:4] for s in staff if s[0] == org])
                email = email.strip().lower()
            order_total = 0
            pct_off = 0 if comp or rnd.random() > 0.08 else rnd.choice([10, 15, 25])
            trows = []
            for sid, (seat_id, r, n, acc) in pick:
                ticket_id += 1
                face = prices[sid] if rnd.random() < 0.6 else prices[sid] - rnd.randrange(0, 1500, 50)
                face = max(face, 1000)
                if comp:
                    disc = svc = fac = tax = 0
                    face = 0
                else:
                    disc = face * pct_off // 100
                    svc = half_even(face * 10, 100) if old_rules else half_even(face * 9, 100)
                    fac = fee
                    tax = half_even((face - disc + svc) * 925, 10000)
                total = face - disc + svc + fac + tax
                order_total += total
                hold_email, hold_name = g.quirk(email), name
                st = "comp" if comp else "valid"
                refunded = 0
                rl, rf = None, 0
                checkin = None
                if eid == cancelled_event and not comp:
                    st, refunded = "refunded", total
                    rl, rf = local_fold(start - timedelta(days=3, hours=rnd.randrange(0, 20)), tz)
                elif not comp and rnd.random() < 0.05:
                    st = "refunded"
                    refunded = total if rnd.random() < 0.6 else (total + 1) // 2
                    when = placed + (horizon - placed) * rnd.uniform(0.05, 0.6)
                    rl, rf = local_fold(when, tz)
                    if rnd.random() < 0.6:
                        pending_resale.append((sid, (seat_id, r, n, acc)))
                trows.append([ticket_id, order_id, seat_id, hold_email, hold_name, st, face, disc, svc, fac, tax, total,
                              refunded, rl, rf, None, None, 0, None, placed])
            for tr in trows:
                # transfers
                if tr[5] != "refunded" and rnd.random() < 0.035:
                    hops = 1 if rnd.random() < 0.8 else 2
                    cur = tr[3]
                    t_at = tr[19]
                    for _ in range(hops):
                        to_email, to_name = rnd.choice(g.people)
                        t_at = t_at + (min(start, NOW_CAP) - t_at) * rnd.uniform(0.1, 0.5)
                        transfer_id += 1
                        tl, tf = local_fold(t_at, tz)
                        transfer_rows.append(f"({transfer_id}, {tr[0]}, {q(cur)}, {q(g.quirk(to_email))}, {q(tl)}, {tf})")
                        cur = g.quirk(to_email)
                        tr[3], tr[4] = cur, to_name
                # admissions for past events
                if past and tr[5] in ("valid", "comp") and eid != cancelled_event and rnd.random() < 0.88:
                    if eid in FALLBACK_SHOWS:
                        adm = start + timedelta(seconds=rnd.randrange(*FALLBACK_SHOWS[eid]))
                    else:
                        adm = start + timedelta(seconds=rnd.randrange(-7200, 3600))
                    al, af = local_fold(adm, tz)
                    tr[15], tr[17] = al, af
                    tr[18] = rnd.choice(GATES[:4]) if rnd.random() < 0.97 else rnd.choice(GATES[4:])
                tr[16] = g.barcode(["LR", "LA", "CH", "ST"][vid - 1])
                ticket_rows.append(tr)
            ref = "oldpay_" + "".join(rnd.choice("0123456789abcdef") for _ in range(16))
            order_rows.append(f"({order_id}, {eid}, {q(g.quirk(email))}, {q(name)}, {q(pl)}, {pf}, {order_total}, {q(ref)})")
    for i in range(0, len(order_rows), 500):
        L.append("INSERT INTO legacy_orders (id, event_id, buyer_email, buyer_name, placed_local, placed_fold, total_cents, "
                 "old_payment_ref) VALUES " + ",".join(order_rows[i:i + 500]) + ";")
    vals = []
    for tr in ticket_rows:
        (tid, oid, seat_id, he, hn, st, face, disc, svc, fac, tax, total, refunded, rl, rf, cil, barcode, cif, gate, _p) = tr
        vals.append(f"({tid}, {oid}, {seat_id}, {q(he)}, {q(hn)}, {q(st)}, {face}, {disc}, {svc}, {fac}, {tax}, {total}, "
                    f"{refunded}, {q(rl)}, {rf}, {q(barcode)}, {q(cil)}, {cif}, {q(gate)})")
    for i in range(0, len(vals), 500):
        L.append("INSERT INTO legacy_tickets (id, order_id, seat_id, holder_email, holder_name, status, face_cents, discount_cents, "
                 "service_fee_cents, facility_fee_cents, tax_cents, total_cents, refunded_cents, refunded_local, refunded_fold, "
                 "barcode, checked_in_local, checked_in_fold, checked_in_gate) VALUES " + ",".join(vals[i:i + 500]) + ";")
    for i in range(0, len(transfer_rows), 500):
        L.append("INSERT INTO legacy_transfers (id, ticket_id, from_email, to_email, transferred_local, transferred_fold) VALUES "
                 + ",".join(transfer_rows[i:i + 500]) + ";")
    for t in ("organizations", "users", "venues", "sections", "seats", "events"):
        L.append(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), (SELECT max(id) FROM {t}));")
    L.append("COMMIT;")
    (out / "002_records.sql").write_text("\n".join(L) + "\n")
    print(f"seed: {len(plan)} events, {order_id} orders, {ticket_id} tickets, {transfer_id} transfers", file=sys.stderr)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
