"""Lotbook domain layer: schema, household state cache, recording transactions, imports, year locks, outbox."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import secrets
import smtplib
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import make_msgid
from fractions import Fraction

import psycopg
from psycopg.rows import dict_row

from . import engine as E
from . import statements as S

ENV = os.environ
DATABASE_URL = ENV.get("DATABASE_URL", "")
FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")
SUMMARY_HEADER = ["symbol", "name", "short_term_gain_cents", "long_term_gain_cents", "wash_disallowed_cents",
                  "dividends_cents", "adj_short_term_cents", "adj_long_term_cents", "adj_dividends_cents"]
REALIZED_HEADER = ["sale_date", "account", "symbol", "quantity", "date_acquired", "term", "proceeds_cents",
                   "basis_cents", "wash_disallowed_cents", "gain_cents"]
UNREALIZED_HEADER = ["account", "symbol", "quantity", "date_acquired", "term", "basis_cents", "market_value_cents",
                     "unrealized_cents"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS lb_credentials (
    user_id integer PRIMARY KEY REFERENCES users (id),
    password_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lb_reset_tokens (
    token_hash text PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL,
    state text NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS lb_sessions (
    sid_hash text PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users (id),
    csrf text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS lb_imports (
    id serial PRIMARY KEY,
    account_id integer NOT NULL REFERENCES accounts (id),
    filename text NOT NULL,
    imported integer NOT NULL,
    duplicates integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lb_transactions (
    id bigint PRIMARY KEY,
    account_id integer NOT NULL REFERENCES accounts (id),
    security_id integer NOT NULL REFERENCES securities (id),
    kind text NOT NULL,
    trade_date date NOT NULL,
    quantity numeric(30, 12),
    amount_cents bigint,
    split_new integer,
    split_old integer,
    to_account_id integer REFERENCES accounts (id),
    ref text,
    description text,
    memo text,
    selection jsonb,
    import_id integer REFERENCES lb_imports (id),
    created_by integer REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lb_transactions_account ON lb_transactions (account_id);
CREATE TABLE IF NOT EXISTS lb_versions (
    household_id integer PRIMARY KEY REFERENCES households (id),
    version bigint NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lb_locks (
    household_id integer NOT NULL REFERENCES households (id),
    year integer NOT NULL,
    locked_by integer REFERENCES users (id),
    locked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (household_id, year)
);
CREATE TABLE IF NOT EXISTS lb_frozen (
    household_id integer NOT NULL REFERENCES households (id),
    year integer NOT NULL,
    summary_csv text NOT NULL,
    realized_csv text NOT NULL,
    figures jsonb NOT NULL,
    landed jsonb NOT NULL,
    totals jsonb NOT NULL,
    PRIMARY KEY (household_id, year)
);
CREATE TABLE IF NOT EXISTS lb_outbox (
    id serial PRIMARY KEY,
    dedupe_key text NOT NULL UNIQUE,
    to_addr text NOT NULL,
    to_name text,
    subject text NOT NULL,
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz
);
CREATE TABLE IF NOT EXISTS lb_corporate_actions (
    id integer PRIMARY KEY,
    household_id integer NOT NULL REFERENCES households (id),
    security_id integer NOT NULL REFERENCES securities (id),
    kind text NOT NULL,
    action_date date NOT NULL,
    new_security_id integer REFERENCES securities (id),
    ratio numeric(30, 12),
    parent_fmv numeric(30, 12),
    new_fmv numeric(30, 12),
    cash_in_lieu_price numeric(30, 12),
    acquirer_security_id integer REFERENCES securities (id),
    shares_per_share numeric(30, 12),
    cash_per_share numeric(30, 12),
    acquirer_fmv numeric(30, 12),
    amount_per_share numeric(30, 12),
    created_by integer REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE lb_outbox ADD COLUMN IF NOT EXISTS to_name text;
INSERT INTO lb_versions (household_id) SELECT id FROM households ON CONFLICT DO NOTHING;
"""


def connect(**kw):
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=4, **kw)


def apply_schema() -> None:
    while True:
        try:
            with connect() as conn:
                conn.execute("SELECT pg_advisory_lock(717171)")
                try:
                    conn.execute(SCHEMA)
                    mx = conn.execute("SELECT coalesce(max(id), 0) AS m FROM legacy_transactions").fetchone()["m"]
                    conn.execute(f"CREATE SEQUENCE IF NOT EXISTS lb_txn_id START WITH {int(mx) + 1000}")
                    ca = conn.execute("SELECT coalesce(max(id), 0) AS m FROM corporate_actions").fetchone()["m"]
                    conn.execute(f"CREATE SEQUENCE IF NOT EXISTS lb_ca_id START WITH {int(ca) + 1000}")
                    conn.commit()
                finally:
                    conn.execute("SELECT pg_advisory_unlock(717171)")
                    conn.commit()
            return
        except psycopg.OperationalError:
            time.sleep(1)


def norm_email(s: str) -> str:
    return (s or "").strip().lower()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return "scrypt$" + salt.hex() + "$" + dk.hex()


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
        return secrets.compare_digest(got.hex(), dk)
    except Exception:
        return False


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def neutral(s: str) -> str:
    return "'" + s if s and s[0] in FORMULA_LEAD else s


def csv_text(header: list[str], rows: list[list[str]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}${c // 100:,}.{c % 100:02d}"


# ---- access ------------------------------------------------------------------------------------------------
HOUSEHOLD_ROLES = ("owner", "member", "viewer")


def visible_households(conn, user: dict) -> list[int]:
    if user["role"] in HOUSEHOLD_ROLES:
        return [user["household_id"]] if user["household_id"] else []
    return [r["household_id"] for r in conn.execute(
        "SELECT household_id FROM assignments WHERE user_id = %s ORDER BY household_id", (user["id"],))]


def can_see(conn, user: dict, hid: int) -> bool:
    return hid in visible_households(conn, user)


def can_change(user: dict, hid: int) -> bool:
    """Accounts, securities, imports, entries, transfers, method changes (R1.2)."""
    return user["role"] in ("owner", "member") and user["household_id"] == hid


def can_lock(conn, user: dict, hid: int) -> bool:
    if user["role"] == "owner":
        return user["household_id"] == hid
    return user["role"] == "preparer" and can_see(conn, user, hid)


def can_rename(user: dict) -> bool:
    return user["role"] in HOUSEHOLD_ROLES


# ---- exchange rates (never change) -----------------------------------------------------------------------
_RATES: E.Rates | None = None
_RATES_LOCK = threading.Lock()


def rates(conn) -> E.Rates:
    global _RATES
    with _RATES_LOCK:
        if _RATES is None:
            rows = conn.execute("SELECT currency, rate_date, rate::text AS rate, quote FROM fx_rates").fetchall()
            _RATES = E.Rates([(r["currency"], r["rate_date"], Fraction(r["rate"]), r["quote"]) for r in rows])
        return _RATES


# ---- household state ---------------------------------------------------------------------------------------
@dataclass
class HState:
    hid: int
    version: int = -1
    accounts: dict = field(default_factory=dict)
    securities: dict = field(default_factory=dict)
    legacy: list = field(default_factory=list)
    app: list = field(default_factory=list)
    app_max_id: int = 0
    refs: dict = field(default_factory=dict)          # (account, ref) -> Txn
    changes: list = field(default_factory=list)
    lock_through: int | None = None
    legacy_lock: int | None = None
    legacy_years: set = field(default_factory=set)
    frozen: dict = field(default_factory=dict)        # year -> row
    legacy_figs: dict = field(default_factory=dict)   # (year, sid) -> Fig
    sec_results: dict = field(default_factory=dict)   # group -> (fingerprint, Result)
    by_sec: dict = field(default_factory=dict)        # sid -> [Txn]
    actions: list = field(default_factory=list)       # corporate actions (R13) as engine events
    group_of: dict = field(default_factory=dict)      # sid -> group representative
    ira: frozenset = frozenset()                      # IRA account ids (R13.9)

    def txns(self) -> list:
        return self.legacy + self.app

    def method_for(self):
        init = {a: r["lot_method"] for a, r in self.accounts.items()}
        return E.method_lookup(init, self.changes)


_STATES: dict[int, HState] = {}
_STATE_LOCKS: dict[int, threading.Lock] = {}
_GLOBAL = threading.Lock()


def _hlock(hid: int) -> threading.RLock:
    """Held while a household's cached state is refreshed or computed on (re-entrant)."""
    with _GLOBAL:
        return _STATE_LOCKS.setdefault(hid, threading.RLock())


class household:
    """with household(conn, hid) as st: the refreshed state, computed on under the household's lock."""

    def __init__(self, conn, hid: int):
        self.conn, self.hid = conn, hid

    def __enter__(self) -> "HState":
        self.lock = _hlock(self.hid)
        self.lock.acquire()
        return state(self.conn, self.hid)

    def __exit__(self, *exc) -> None:
        self.lock.release()


def _txn_from_app(r: dict) -> E.Txn:
    t = E.Txn(int(r["id"]), int(r["id"]), r["account_id"], r["security_id"], r["kind"], r["trade_date"], ref=r["ref"])
    if r["quantity"] is not None:
        t.quantity = Fraction(str(r["quantity"]))
    t.amount = r["amount_cents"]
    if r["split_new"]:
        t.ratio = (r["split_new"], r["split_old"])
    t.to_account = r["to_account_id"]
    if r["selection"]:
        t.selection = [(int(a), Fraction(q)) for a, q in r["selection"]]
    return t


def _txn_from_legacy(r: dict) -> E.Txn:
    kind = r["kind"].strip().lower()
    t = E.Txn(int(r["id"]), int(r["id"]), r["account_id"], r["security_id"], kind, r["trade_date"], ref=r["ref"])
    if kind in ("buy", "reinvest", "sell"):
        t.quantity = abs(Fraction(str(r["quantity"])))
        t.amount = int(abs(Fraction(str(r["amount"]))) * 100)
    elif kind == "dividend":
        t.amount = int(abs(Fraction(str(r["amount"]))) * 100)
    elif kind == "split":
        t.ratio = (int(r["split_new"]), int(r["split_old"]))
    elif kind == "transfer":
        t.to_account = r["to_account_id"]
    return t


def _num(v):
    return None if v is None else Fraction(str(v))


def action_txn(r: dict) -> E.Txn:
    """A corporate_actions / lb_corporate_actions row as an engine event (R13)."""
    k = r["kind"]
    if k == "spinoff":
        params = {"new_security": r["new_security_id"], "ratio": _num(r["ratio"]), "parent_fmv": _num(r["parent_fmv"]),
                  "new_fmv": _num(r["new_fmv"]), "cash_in_lieu_price": _num(r["cash_in_lieu_price"])}
    elif k == "merger":
        params = {"acquirer": r["acquirer_security_id"], "shares_per_share": _num(r["shares_per_share"]),
                  "cash_per_share": _num(r["cash_per_share"]), "acquirer_fmv": _num(r["acquirer_fmv"])}
    else:
        params = {"amount_per_share": _num(r["amount_per_share"])}
    return E.Txn(int(r["id"]), int(r["id"]), None, r["security_id"], k, r["action_date"], params=params)


ACTION_SQL = ("SELECT id, security_id, kind, action_date, new_security_id, ratio, parent_fmv, new_fmv, cash_in_lieu_price, "
              "acquirer_security_id, shares_per_share, cash_per_share, acquirer_fmv, amount_per_share FROM {t} "
              "WHERE household_id = %s ORDER BY id")


def state(conn, hid: int) -> HState:
    """The household's records, refreshed when its version moved. Callers hold no DB lock."""
    with _hlock(hid):
        st = _STATES.get(hid)
        v = conn.execute("SELECT version FROM lb_versions WHERE household_id = %s", (hid,)).fetchone()
        v = v["version"] if v else 0
        if st is not None and st.version == v:
            return st
        if st is None:
            st = HState(hid)
            _STATES[hid] = st
            for r in conn.execute(
                    "SELECT t.* FROM legacy_transactions t JOIN accounts a ON a.id = t.account_id WHERE a.household_id = %s ORDER BY t.id",
                    (hid,)):
                t = _txn_from_legacy(r)
                st.legacy.append(t)
                if t.ref:
                    st.refs[(t.account, t.ref.strip())] = t
            for (y, s, sh, lo, dis, dv) in [(r["year"], r["security_id"], r["short_term_gain_cents"], r["long_term_gain_cents"],
                                             r["wash_disallowed_cents"], r["dividends_cents"]) for r in conn.execute(
                    "SELECT * FROM legacy_locked_figures WHERE household_id = %s", (hid,))]:
                st.legacy_figs[(y, s)] = E.Fig(sh, lo, dis, dv, True)
            st.legacy_years = {r["year"] for r in conn.execute("SELECT year FROM legacy_year_locks WHERE household_id = %s", (hid,))}
            st.legacy_lock = max(st.legacy_years) if st.legacy_years else None
            for t in st.legacy:
                st.by_sec.setdefault(t.security, []).append(t)
        st.accounts = {r["id"]: r for r in conn.execute("SELECT * FROM accounts WHERE household_id = %s ORDER BY id", (hid,))}
        st.securities = {r["id"]: r for r in conn.execute("SELECT * FROM securities WHERE household_id = %s ORDER BY id", (hid,))}
        for r in conn.execute("SELECT t.* FROM lb_transactions t JOIN accounts a ON a.id = t.account_id "
                              "WHERE a.household_id = %s AND t.id > %s ORDER BY t.id", (hid, st.app_max_id)):
            t = _txn_from_app(r)
            st.app.append(t)
            st.app_max_id = max(st.app_max_id, t.tid)
            st.by_sec.setdefault(t.security, []).append(t)
            if t.ref:
                st.refs[(t.account, t.ref)] = t
        st.changes = [(r["account_id"], r["effective_from"], r["id"], r["lot_method"]) for r in conn.execute(
            "SELECT c.* FROM lot_method_changes c JOIN accounts a ON a.id = c.account_id WHERE a.household_id = %s ORDER BY c.id",
            (hid,))]
        st.frozen = {r["year"]: r for r in conn.execute("SELECT * FROM lb_frozen WHERE household_id = %s", (hid,))}
        st.ira = frozenset(a for a, r in st.accounts.items() if r.get("account_type") == "ira")
        st.actions = [action_txn(r) for t in ("corporate_actions", "lb_corporate_actions")
                      for r in conn.execute(ACTION_SQL.format(t=t), (hid,))]
        st.group_of = E.groups(st.txns() + st.actions)
        for sid in st.securities:
            st.group_of.setdefault(sid, sid)
        app_lock = conn.execute("SELECT max(year) AS y FROM lb_locks WHERE household_id = %s", (hid,)).fetchone()["y"]
        st.lock_through = max([y for y in (st.legacy_lock, app_lock) if y is not None], default=None)
        st.version = v
        return st


def _fingerprint(st: HState, txns: list) -> tuple:
    accts = sorted({t.account for t in txns if t.account} | {t.to_account for t in txns if t.to_account})
    ch = tuple(c for c in st.changes if c[0] in accts)
    return (len(txns), max((t.tid for t in txns), default=0), sum(t.tid for t in txns), ch, tuple(accts),
            tuple(sorted(a for a in st.ira if a in accts)))


def group_events(st: HState, members: set) -> list:
    out = [t for sid in sorted(members) for t in st.by_sec.get(sid, [])]
    return out + [a for a in st.actions if a.security in members]


def group_result(conn, st: HState, g: int, extra: list | None = None) -> E.Result:
    """The engine over one group of linked securities (R13), cached per group."""
    rt = rates(conn)
    members = {s for s, r in st.group_of.items() if r == g}
    txns = group_events(st, members)
    if extra:
        txns += extra
        members |= {t.security for t in extra} | {E.linked(t) for t in extra if E.linked(t)}
    else:
        fp = _fingerprint(st, txns)
        hit = st.sec_results.get(g)
        if hit and hit[0] == fp:
            return hit[1]
    cur = {sid: (st.securities[sid]["currency"] if sid in st.securities else "USD") for sid in members}
    res = E.compute(txns, cur, rt, st.method_for(), ira=st.ira)
    if not extra:
        st.sec_results[g] = (_fingerprint(st, txns), res)
    return res


def household_result(conn, st: HState) -> E.Result:
    out = E.Result(ira=st.ira)
    for g in sorted(set(st.group_of.values())):
        r = group_result(conn, st, g)
        out.portions.extend(r.portions)
        out.dividends.extend(r.dividends)
        out.lots.extend(r.lots)
        out.errors.extend(r.errors)
    return out


def bump(conn, hid: int) -> None:
    conn.execute("UPDATE lb_versions SET version = version + 1 WHERE household_id = %s", (hid,))


def lock_household(conn, hid: int) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(42, %s)", (hid,))


# ---- recording (R3.4, R3.5, R4) -----------------------------------------------------------------------------
def check_new(conn, st: HState, new: list[E.Txn]) -> list[tuple[E.Txn, str]]:
    """Errors (R6.6) that adding `new` would cause anywhere in the affected groups of securities."""
    rt = rates(conn)
    touched = {t.security for t in new} | {E.linked(t) for t in new if E.linked(t)}
    groups_hit = {st.group_of.get(s, s) for s in touched}
    members = {s for s, r in st.group_of.items() if r in groups_hit} | touched
    txns = group_events(st, members) + list(new)
    cur = {sid: (st.securities[sid]["currency"] if sid in st.securities else "USD") for sid in members}
    return E.compute(txns, cur, rt, st.method_for(), ira=st.ira).errors


def next_ids(conn, n: int) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT nextval('lb_txn_id') AS id FROM generate_series(1, %s)", (n,))]


def insert_txn(conn, t: E.Txn, user_id: int, description: str | None = None, memo: str | None = None,
               import_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO lb_transactions (id, account_id, security_id, kind, trade_date, quantity, amount_cents, split_new, split_old, "
        "to_account_id, ref, description, memo, selection, import_id, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (t.tid, t.account, t.security, t.kind, t.day, None if t.quantity is None else str(_dec(t.quantity)), t.amount,
         t.ratio[0] if t.ratio else None, t.ratio[1] if t.ratio else None, t.to_account, t.ref, description, memo,
         json.dumps([[a, str(q)] for a, q in t.selection]) if t.selection else None, import_id, user_id))


def _dec(q: Fraction) -> str:
    return E.qty_str(q)


def record(conn, hid: int, user_id: int, specs: list[tuple[E.Txn, str | None, str | None]]) -> tuple[bool, str]:
    """Record hand-entered transactions after the short check. specs: (txn without ids, description, memo)."""
    with conn.transaction(), _hlock(hid):
        lock_household(conn, hid)
        st = state(conn, hid)
        rt = rates(conn)
        for t, _d, _m in specs:
            cur = st.securities[t.security]["currency"]
            if t.kind != "split" and t.kind != "transfer" and rt.rate(cur, t.day) is None:
                return False, f"there is no {cur} exchange rate on or before {t.day}"
        ids = next_ids(conn, len(specs))
        for (t, _d, _m), i in zip(specs, ids):
            t.tid = t.seq = i
        errs = check_new(conn, st, [t for t, _d, _m in specs])
        if errs:
            return False, "; ".join(f"{e[0].kind} on {e[0].day}: {e[1]}" for e in errs[:3])
        for t, d, m in specs:
            insert_txn(conn, t, user_id, d, m)
        bump(conn, hid)
    return True, ""


ACTION_COLS = ("new_security_id", "ratio", "parent_fmv", "new_fmv", "cash_in_lieu_price", "acquirer_security_id",
               "shares_per_share", "cash_per_share", "acquirer_fmv", "amount_per_share")


def record_action(conn, hid: int, user_id: int, kind: str, security_id: int, day: date, terms: dict) -> tuple[bool, str]:
    """R13.11: an owner or member records a corporate action; terms are already parsed and validated as numbers."""
    with conn.transaction(), _hlock(hid):
        lock_household(conn, hid)
        st = state(conn, hid)
        secs = st.securities
        if security_id not in secs:
            return False, "the security does not belong to this household"
        cur = secs[security_id]["currency"]
        for col in ("new_security_id", "acquirer_security_id"):
            other = terms.get(col)
            if other is None:
                continue
            if other not in secs:
                return False, "a named security does not belong to this household"
            if other == security_id:
                return False, "the action names the same security twice"
            if secs[other]["currency"] != cur:
                return False, "the securities of an action must have the same currency"
        if cur != "USD" and rates(conn).rate(cur, day) is None:
            return False, f"there is no {cur} exchange rate on or before {day}"
        aid = conn.execute("SELECT nextval('lb_ca_id') AS id").fetchone()["id"]
        row = {"id": aid, "security_id": security_id, "kind": kind, "action_date": day, **{c: terms.get(c) for c in ACTION_COLS}}
        t = action_txn(row)
        errs = check_new(conn, st, [t])
        if errs:
            return False, "; ".join(f"{e[0].kind} on {e[0].day}: {e[1]}" for e in errs[:3])
        conn.execute("INSERT INTO lb_corporate_actions (id, household_id, security_id, kind, action_date, " + ", ".join(ACTION_COLS)
                     + ", created_by) VALUES (" + ", ".join(["%s"] * (6 + len(ACTION_COLS))) + ")",
                     [aid, hid, security_id, kind, day] + [E.qty_str(terms[c]) if isinstance(terms.get(c), Fraction) else terms.get(c)
                                                            for c in ACTION_COLS] + [user_id])
        bump(conn, hid)
    return True, ""


@dataclass
class ImportResult:
    status: str
    imported: int = 0
    duplicates: int = 0
    errors: list = field(default_factory=list)


def import_file(conn, account: dict, user_id: int, filename: str, data: bytes) -> ImportResult:
    hid = account["household_id"]
    with conn.transaction(), _hlock(hid):
        lock_household(conn, hid)
        st = state(conn, hid)
        rt = rates(conn)
        by_symbol = {s["symbol"].strip().upper(): (sid, s["currency"]) for sid, s in st.securities.items()}
        by_isin = {s["isin"].strip().upper(): (sid, s["currency"]) for sid, s in st.securities.items()}

        def find(broker, key):
            return (by_symbol if broker == "northwind" else by_isin).get(key.strip().upper())

        rows, errors = S.parse(account["broker"], data, account["account_number"], find)
        if errors and errors[0][0] == 0 and not rows:
            return ImportResult("rejected", errors=errors)
        new, dups, seen = [], 0, {}
        for row in rows:
            cand = E.Txn(0, 0, account["id"], row.security_id, row.kind, row.day, row.quantity, row.amount, row.ratio,
                         ref=row.ref)
            cur = st.securities[row.security_id]["currency"]
            if row.kind != "split" and rt.rate(cur, row.day) is None:
                errors.append((row.line, f"there is no {cur} exchange rate on or before {row.day}"))
                continue
            prev = st.refs.get((account["id"], row.ref)) or seen.get(row.ref)
            if prev is not None:
                if _same(prev, cand):
                    dups += 1
                    continue
                errors.append((row.line, f"reference {row.ref} was already recorded with different content"))
                continue
            seen[row.ref] = cand
            new.append((row, cand))
        if errors:
            return ImportResult("rejected", errors=sorted(errors))
        if new:
            ids = next_ids(conn, len(new))
            for (row, t), i in zip(new, ids):
                t.tid = t.seq = i
            errs = check_new(conn, st, [t for _r, t in new])
            if errs:
                line_of = {t.tid: r.line for r, t in new}
                return ImportResult("rejected", errors=sorted(
                    (line_of.get(e[0].tid, 0), f"{e[0].kind} on {e[0].day}" + (f" (reference {e[0].ref})" if e[0].ref else "")
                     + f": {e[1]}") for e in errs))
        imp = conn.execute("INSERT INTO lb_imports (account_id, filename, imported, duplicates) VALUES (%s,%s,%s,%s) RETURNING id",
                           (account["id"], filename[:300], len(new), dups)).fetchone()["id"]
        for row, t in new:
            insert_txn(conn, t, user_id, description=row.description[:500], import_id=imp)
        if new:
            bump(conn, hid)
    return ImportResult("imported", len(new), dups)


def _same(a: E.Txn, b: E.Txn) -> bool:
    return (a.kind, a.day, a.security, a.quantity, a.amount, a.ratio) == (b.kind, b.day, b.security, b.quantity, b.amount, b.ratio)


# ---- reports (R8, R9) --------------------------------------------------------------------------------------
def figures(conn, st: HState) -> dict:
    return E.year_figures(household_result(conn, st))


def frozen_figs(st: HState, year: int) -> dict[int, E.Fig]:
    if st.legacy_lock is not None and year <= st.legacy_lock and year not in st.frozen:
        return {s: f for (y, s), f in st.legacy_figs.items() if y == year}
    fr = st.frozen.get(year)
    if not fr:
        return {}
    return {int(s): E.Fig(v[0], v[1], v[2], v[3], True) for s, v in fr["figures"].items()}


def landed(st: HState) -> dict:
    out = {}
    for y, fr in st.frozen.items():
        for k, v in fr["landed"].items():
            origin, sid, cat = k.split(":")
            out[(y, int(origin), int(sid), cat)] = v
    return out


def adjustments(conn, st: HState, figs: dict | None = None) -> dict:
    L = st.lock_through
    if L is None:
        return {}
    figs = figs if figs is not None else figures(conn, st)
    ld = landed(st)
    out = {}
    origins = {y for (y, _s) in figs if y <= L} | {y for (y, _s) in st.legacy_figs if y <= L} | {y for y in st.frozen if y <= L}
    for Y in sorted(origins):
        fz = frozen_figs(st, Y)
        sids = {s for (y, s) in figs if y == Y} | set(fz)
        for sid in sids:
            c = figs.get((Y, sid), E.Fig())
            f = fz.get(sid, E.Fig())
            for cat in E.CATS:
                a = sum(v for (z, y, s, k), v in ld.items() if y == Y and s == sid and k == cat and Y < z <= L)
                d = c.get(cat) - f.get(cat) - a
                if d:
                    out[(Y, sid, cat)] = d
    return out


def is_locked(st: HState, year: int) -> bool:
    return st.lock_through is not None and year <= st.lock_through


def summary_rows(conn, st: HState, year: int, figs: dict | None = None) -> list[list[str]]:
    figs = figs if figs is not None else figures(conn, st)
    adj: dict = {}
    if st.lock_through is not None and year == st.lock_through + 1:
        for (_y, sid, cat), v in adjustments(conn, st, figs).items():
            adj.setdefault(sid, {}).setdefault(cat, 0)
            adj[sid][cat] += v
    rows = []
    for sid, sec in st.securities.items():
        f = figs.get((year, sid))
        a = adj.get(sid, {})
        if not ((f and f.active) or any(a.values())):
            continue
        f = f or E.Fig()
        rows.append([sec["symbol"], sec["name"], str(f.short), str(f.long), str(f.disallowed), str(f.dividends),
                     str(a.get("short", 0)), str(a.get("long", 0)), str(a.get("dividends", 0))])
    rows.sort(key=lambda r: r[0])
    return rows


def legacy_summary_rows(st: HState, year: int) -> list[list[str]]:
    rows = []
    for (y, sid), f in st.legacy_figs.items():
        if y == year and sid in st.securities:
            s = st.securities[sid]
            rows.append([s["symbol"], s["name"], str(f.short), str(f.long), str(f.disallowed), str(f.dividends), "0", "0", "0"])
    return sorted(rows, key=lambda r: r[0])


def realized_rows(conn, st: HState, year: int) -> list[list[str]]:
    res = household_result(conn, st)
    ps = sorted((p for p in res.portions if p.day.year == year and not p.ira), key=lambda p: p.order)
    out = []
    for p in ps:
        out.append([p.day.isoformat(), st.accounts[p.account]["name"], st.securities[p.security]["symbol"], E.qty_str(p.qty),
                    p.holding.isoformat(), p.term, str(p.proceeds), str(p.basis), str(p.disallowed), str(p.gain)])
    return out


def summary_csv(rows) -> str:
    return csv_text(SUMMARY_HEADER, [[neutral(r[0]), neutral(r[1])] + r[2:] for r in rows])


def realized_csv(rows) -> str:
    return csv_text(REALIZED_HEADER, [[r[0], neutral(r[1]), neutral(r[2])] + r[3:] for r in rows])


def totals_of(rows) -> dict:
    t = {"short": 0, "long": 0, "disallowed": 0, "dividends": 0, "adjustments": 0}
    for r in rows:
        t["short"] += int(r[2])
        t["long"] += int(r[3])
        t["disallowed"] += int(r[4])
        t["dividends"] += int(r[5])
        t["adjustments"] += int(r[6]) + int(r[7]) + int(r[8])
    return t


def year_report(conn, st: HState, year: int) -> dict:
    """{'locked', 'summary_csv', 'realized_csv', 'totals', 'rows', 'adjust_by_origin'} for the year page and exports."""
    if is_locked(st, year):
        fr = st.frozen.get(year)
        if fr:
            rows = list(csv.reader(io.StringIO(fr["summary_csv"], newline="")))[1:]
            return {"locked": True, "summary_csv": fr["summary_csv"], "realized_csv": fr["realized_csv"],
                    "totals": fr["totals"], "rows": rows, "legacy": False}
        if year in st.legacy_years:  # the old tool's lock: its figures, and what the rules give for realized.csv (R9.2)
            rows = legacy_summary_rows(st, year)
            return {"locked": True, "summary_csv": summary_csv(rows), "realized_csv": realized_csv(realized_rows(conn, st, year)),
                    "totals": totals_of(rows), "rows": rows, "legacy": True}
        # a locked year that had nothing in it when it was frozen (R8.3: the first lock freezes every earlier year)
        return {"locked": True, "summary_csv": summary_csv([]), "realized_csv": realized_csv([]), "totals": totals_of([]),
                "rows": [], "legacy": False}
    figs = figures(conn, st)
    rows = summary_rows(conn, st, year, figs)
    return {"locked": False, "summary_csv": summary_csv(rows), "realized_csv": realized_csv(realized_rows(conn, st, year)),
            "totals": totals_of(rows), "rows": rows, "legacy": False}


def unrealized_csv(conn, st: HState) -> str:
    rt = rates(conn)
    prices = {}
    for r in conn.execute("SELECT DISTINCT ON (p.security_id) p.security_id, p.price_date, p.close::text AS close "
                          "FROM security_prices p JOIN securities s ON s.id = p.security_id WHERE s.household_id = %s "
                          "ORDER BY p.security_id, p.price_date DESC", (st.hid,)):
        prices[r["security_id"]] = (r["price_date"], Fraction(r["close"]))
    res = household_result(conn, st)
    lots = sorted((l for l in res.lots if l.security in prices),
                  key=lambda l: (st.securities[l.security]["symbol"], l.account, l.key()))
    rows = []
    for l in lots:
        pday, close = prices[l.security]
        sec = st.securities[l.security]
        mv = E.rhu(l.qty * close * rt.rate(sec["currency"], pday) * 100)
        rows.append([neutral(st.accounts[l.account]["name"]), neutral(sec["symbol"]), E.qty_str(l.qty), l.holding.isoformat(),
                     E.term_of(l.holding, pday), str(l.basis), str(mv), str(mv - l.basis)])
    return csv_text(UNREALIZED_HEADER, rows)


def lock_year(conn, hid: int, year: int, user: dict) -> tuple[int, str]:
    today = now_utc().date()
    with conn.transaction(), _hlock(hid):
        lock_household(conn, hid)
        st = state(conn, hid)
        L = st.lock_through
        if not date(year, 12, 31) < today:
            return 409, f"{year} has not ended yet, so it cannot be locked."
        if L is not None and year != L + 1:
            if year <= L:
                return 409, f"{year} is already locked."
            return 409, f"Lock {L + 1} first: years are locked in order."
        figs = figures(conn, st)
        adj = adjustments(conn, st, figs)
        years = [year] if L is not None else sorted({y for (y, _s) in figs if y < year} | {year})
        for Y in years:
            rows = summary_rows(conn, st, Y, figs)
            fz = {str(s): [f.short, f.long, f.disallowed, f.dividends] for (y, s), f in figs.items() if y == Y}
            ld = {f"{o}:{s}:{c}": v for (o, s, c), v in adj.items()} if Y == year else {}
            conn.execute(
                "INSERT INTO lb_frozen (household_id, year, summary_csv, realized_csv, figures, landed, totals) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (hid, Y, summary_csv(rows), realized_csv(realized_rows(conn, st, Y)), json.dumps(fz), json.dumps(ld),
                 json.dumps(totals_of(rows))))
            conn.execute("INSERT INTO lb_locks (household_id, year, locked_by) VALUES (%s,%s,%s)", (hid, Y, user["id"]))
        rows = summary_rows(conn, st, year, figs)
        t = totals_of(rows)
        hname = conn.execute("SELECT name FROM households WHERE id = %s", (hid,)).fetchone()["name"]
        for m in conn.execute("SELECT id, email, full_name FROM users WHERE household_id = %s AND active "
                              "AND role IN ('owner', 'member', 'viewer') ORDER BY id", (hid,)).fetchall():
            body = (f"Hello {m['full_name']},\n\n{user['full_name']} locked tax year {year} for the {hname} in Lotbook.\n"
                    f"These are the year's final figures:\n\n"
                    f"Short-term gains: {dollars(t['short'])}\n"
                    f"Long-term gains: {dollars(t['long'])}\n"
                    f"Wash-sale loss disallowed: {dollars(t['disallowed'])}\n"
                    f"Dividends: {dollars(t['dividends'])}\n"
                    f"Adjustments for earlier years: {dollars(t['adjustments'])}\n\n"
                    f"The year's reports are now frozen. Anything recorded later for {year} or earlier appears as an "
                    f"adjustment in {year + 1}.\n\nLotbook\n")
            enqueue_mail(conn, f"lock:{hid}:{year}:{m['id']}", norm_email(m["email"]),
                         f"Year-end summary {year} for the {hname}, locked by {header_text(user['full_name'])}", body,
                         to_name=header_text(m["full_name"]))
        bump(conn, hid)
    return 200, ""


# ---- outbox ------------------------------------------------------------------------------------------------
def header_text(s: str) -> str:
    """R11.5: typed text bound for a header loses its line breaks."""
    return (s or "").replace("\r", " ").replace("\n", " ")


def enqueue_mail(conn, key: str, to: str, subject: str, body: str, to_name: str | None = None) -> None:
    conn.execute("INSERT INTO lb_outbox (dedupe_key, to_addr, to_name, subject, body) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                 (key, to, to_name, header_text(subject), body))


def send_now(to: str, subject: str, body: str) -> None:
    """Send one message straight to SMTP, in the background, without storing it (reset links carry a secret, R11.6)."""
    def run():
        for attempt in range(5):
            try:
                msg = EmailMessage()
                msg["From"] = ENV.get("MAIL_FROM", "statements@lotbook.example")
                msg["To"] = to
                msg["Subject"] = header_text(subject)
                msg["Message-ID"] = make_msgid(domain="lotbook.example")
                msg.set_content(body)
                with smtplib.SMTP(ENV.get("SMTP_HOST", "127.0.0.1"), int(ENV.get("SMTP_PORT", "25")), timeout=20) as s:
                    s.send_message(msg, to_addrs=[to])
                return
            except Exception:
                time.sleep(1 + attempt)
    threading.Thread(target=run, daemon=True).start()


def outbox_loop() -> None:
    """Sends queued mail one message per transaction, so a crash re-sends at most the message in flight."""
    while True:
        try:
            with connect() as conn:
                while True:
                    with conn.transaction():
                        rows = conn.execute("SELECT * FROM lb_outbox WHERE sent_at IS NULL ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED").fetchall()
                        for r in rows:
                            msg = EmailMessage()
                            msg["From"] = ENV.get("MAIL_FROM", "statements@lotbook.example")
                            msg["To"] = Address(display_name=header_text(r["to_name"] or ""), addr_spec=r["to_addr"])
                            msg["Subject"] = header_text(r["subject"])
                            msg["Message-ID"] = make_msgid(domain="lotbook.example")
                            msg.set_content(r["body"])
                            with smtplib.SMTP(ENV.get("SMTP_HOST", "127.0.0.1"), int(ENV.get("SMTP_PORT", "25")), timeout=20) as s:
                                s.send_message(msg, to_addrs=[r["to_addr"]])
                            conn.execute("UPDATE lb_outbox SET sent_at = now() WHERE id = %s", (r["id"],))
                    if not rows:
                        time.sleep(1)
        except Exception:
            time.sleep(2)


def start_outbox() -> None:
    threading.Thread(target=outbox_loop, daemon=True).start()
