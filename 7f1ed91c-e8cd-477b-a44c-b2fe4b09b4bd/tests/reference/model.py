"""Verifier-side model of Lotbook: what every report, lot list and email must say.

The model mirrors instruction.md (R5-R9) from the verifier's own records only: the pre-existing data read
from the database before the submission ran (tests/snapshot.py), plus every account, security, transaction,
method change and lock the verifier itself made through the application and saw accepted. It never reads
the submission's tables.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # importable standalone (Reward Kit)

from reference import engine as E  # noqa: E402
from reference.engine import Fig, Txn  # noqa: E402

REALIZED_HEADER = ["sale_date", "account", "symbol", "quantity", "date_acquired", "term", "proceeds_cents",
                   "basis_cents", "wash_disallowed_cents", "gain_cents"]
SUMMARY_HEADER = ["symbol", "name", "short_term_gain_cents", "long_term_gain_cents", "wash_disallowed_cents",
                  "dividends_cents", "adj_short_term_cents", "adj_long_term_cents", "adj_dividends_cents"]
UNREALIZED_HEADER = ["account", "symbol", "quantity", "date_acquired", "term", "basis_cents", "market_value_cents",
                     "unrealized_cents"]
LOTS_FIELDS = ("lot_id", "symbol", "quantity", "acquired", "holding_start", "basis_cents")
FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def neutral(s: str) -> str:
    return "'" + s if s and s[0] in FORMULA_LEAD else s


def csv_text(header: list[str], rows: list[list[str]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


@dataclass
class Account:
    aid: int
    hid: int
    name: str
    broker: str
    number: str
    method: str
    kind: str = "taxable"   # taxable or ira (R13.9)


@dataclass
class Sec:
    sid: int
    hid: int
    symbol: str
    isin: str
    name: str
    currency: str


@dataclass
class Household:
    hid: int
    name: str
    accounts: dict = field(default_factory=dict)
    securities: dict = field(default_factory=dict)
    txns: list = field(default_factory=list)
    actions: list = field(default_factory=list)   # corporate actions (R13) as engine events
    changes: list = field(default_factory=list)   # (aid, eff, seq, method)
    lock_through: int | None = None
    legacy_lock: int | None = None
    frozen: dict = field(default_factory=dict)    # year -> {"figs": {sid: Fig}, "summary": rows, "realized": rows}
    landed: dict = field(default_factory=dict)    # (landing year, origin year, sid, cat) -> cents
    _cache: dict = field(default_factory=dict)   # group -> (fingerprint, Result)
    version: int = 0

    @property
    def ira(self) -> frozenset:
        return frozenset(a.aid for a in self.accounts.values() if a.kind == "ira")

    # -- the engine over everything recorded (per group of linked securities, cached) ----------------
    def result(self, rates: E.Rates, sids: set | None = None) -> E.Result:
        events = self.txns + self.actions
        rep = E.groups(events)
        wanted = None if sids is None else {rep.get(s, s) for s in sids}
        by: dict[int, list] = {}
        for t in events:
            g = rep[t.security]
            if wanted is None or g in wanted:
                by.setdefault(g, []).append(t)
        init = {a.aid: a.method for a in self.accounts.values()}
        method_for = E.method_lookup(init, self.changes)
        ira = self.ira
        out = E.Result(ira=ira)
        for g in sorted(by):
            ts = by[g]
            accts = sorted({t.account for t in ts if t.account} | {t.to_account for t in ts if t.to_account})
            fp = (len(ts), max(t.seq for t in ts), sum(t.tid for t in ts), tuple(c for c in self.changes if c[0] in accts),
                  tuple((a, init.get(a)) for a in accts), tuple(sorted(a for a in ira if a in accts)))
            hit = self._cache.get(g)
            if not hit or hit[0] != fp:
                members = {t.security for t in ts} | {E.linked(t) for t in ts if E.linked(t)}
                hit = (fp, E.compute(ts, {s: self.securities[s].currency for s in members}, rates, method_for, ira=ira))
                self._cache[g] = hit
            r = hit[1]
            out.portions.extend(r.portions)
            out.dividends.extend(r.dividends)
            out.lots.extend(r.lots)
            out.errors.extend(r.errors)
        return out

    def touch(self) -> None:
        self.version += 1

    def figs(self, rates, sids: set | None = None) -> dict:
        return E.year_figures(self.result(rates, sids))

    # -- locks and adjustments (R8.2-R8.4) --------------------------------------------------------------
    def frozen_fig(self, year: int, sid: int) -> Fig:
        fr = self.frozen.get(year)
        if fr is None:
            return Fig()
        return fr["figs"].get(sid, Fig())

    def adjustments(self, rates, only: set | None = None) -> dict[tuple[int, int, str], int]:
        """(origin year, sid, cat) -> cents landing in the first unlocked year (lock_through + 1)."""
        out: dict = {}
        L = self.lock_through
        if L is None:
            return out
        figs = self.figs(rates, only)
        origins = {y for (y, _s) in figs if y <= L} | {y for y in self.frozen if y <= L}
        for Y in sorted(origins):
            sids = {s for (y, s) in figs if y == Y} | set(self.frozen.get(Y, {}).get("figs", {}))
            for sid in sorted(sids):
                if only is not None and sid not in only:
                    continue
                c = figs.get((Y, sid), Fig())
                f = self.frozen_fig(Y, sid)
                for cat in E.CATS:
                    a = sum(v for (z, y, s, k), v in self.landed.items() if y == Y and s == sid and k == cat and Y < z <= L)
                    delta = c.get(cat) - f.get(cat) - a
                    if delta:
                        out[(Y, sid, cat)] = delta
        return out

    def is_locked(self, year: int) -> bool:
        return self.lock_through is not None and year <= self.lock_through

    def summary_rows(self, year: int, rates, sids: set | None = None) -> list[list[str]]:
        if self.is_locked(year):
            fr = self.frozen.get(year)
            rows = [list(r) for r in fr["summary"]] if fr and fr.get("summary") is not None else []
            syms = None if sids is None else {self.securities[s].symbol for s in sids}
            return [r for r in rows if syms is None or r[0] in syms]
        figs = self.figs(rates, sids)
        adj: dict[int, dict[str, int]] = {}
        if self.lock_through is not None and year == self.lock_through + 1:
            for (_y, sid, cat), v in self.adjustments(rates, sids).items():
                adj.setdefault(sid, {}).setdefault(cat, 0)
                adj[sid][cat] += v
        rows = []
        for sid, sec in self.securities.items():
            if sids is not None and sid not in sids:
                continue
            f = figs.get((year, sid))
            a = adj.get(sid, {})
            if not ((f and f.active) or any(a.values())):
                continue
            f = f or Fig()
            rows.append([sec.symbol, sec.name, str(f.short), str(f.long), str(f.disallowed), str(f.dividends),
                         str(a.get("short", 0)), str(a.get("long", 0)), str(a.get("dividends", 0))])
        rows.sort(key=lambda r: r[0])
        return rows

    def realized_rows(self, year: int, rates, raw: bool = False, sids: set | None = None) -> list[list[str]]:
        if self.is_locked(year) and not raw:
            fr = self.frozen.get(year)
            if fr and fr.get("realized") is not None:
                syms = None if sids is None else {self.securities[s].symbol for s in sids}
                return [list(r) for r in fr["realized"] if syms is None or r[2] in syms]
            if fr is None:  # locked while it had no data (R8.3 freezes every earlier year at the first lock): stays empty
                return []
            # the old tool's lock (realized None): realized.csv shows what the rules give (R9.2)
        res = self.result(rates, sids)
        ps = sorted((p for p in res.portions if p.day.year == year and not p.ira and (sids is None or p.security in sids)),
                    key=lambda p: p.order)
        return [self.realized_row(p) for p in ps]

    def realized_row(self, p: E.Portion) -> list[str]:
        acct = self.accounts[p.account]
        sec = self.securities[p.security]
        return [p.day.isoformat(), acct.name, sec.symbol, E.qty_str(p.qty), p.holding.isoformat(), p.term,
                str(p.proceeds), str(p.basis), str(p.disallowed), str(p.gain)]

    def lock(self, year: int, rates) -> None:
        figs = self.figs(rates)
        adj = self.adjustments(rates)
        years = [year] if self.lock_through is not None else sorted({y for (y, _s) in figs if y < year} | {year})
        for Y in years:
            summary = self.summary_rows(Y, rates)
            realized = self.realized_rows(Y, rates, raw=True)
            self.frozen[Y] = {"figs": {s: f for (y, s), f in figs.items() if y == Y}, "summary": summary,
                              "realized": realized}
        for (origin, sid, cat), v in adj.items():
            self.landed[(year, origin, sid, cat)] = v
        self.lock_through = year

    def totals(self, rows: list[list[str]]) -> dict[str, int]:
        t = {"short": 0, "long": 0, "disallowed": 0, "dividends": 0, "adjustments": 0}
        for r in rows:
            t["short"] += int(r[2])
            t["long"] += int(r[3])
            t["disallowed"] += int(r[4])
            t["dividends"] += int(r[5])
            t["adjustments"] += int(r[6]) + int(r[7]) + int(r[8])
        return t

    # -- open lots and unrealized gains ---------------------------------------------------------------
    def open_lots(self, rates, aid: int | None = None, sids: set | None = None) -> list[E.Lot]:
        res = self.result(rates, sids)
        out = [l for l in res.lots if (aid is None or l.account == aid) and (sids is None or l.security in sids)]
        return sorted(out, key=lambda l: (self.securities[l.security].symbol, l.account, l.key()))

    def lot_tuple(self, l: E.Lot, app_tid_of=None) -> tuple:
        lid = app_tid_of(l.acq) if app_tid_of else l.acq
        return (str(lid), self.securities[l.security].symbol, E.qty_str(l.qty), l.acquired.isoformat(),
                l.holding.isoformat(), str(l.basis))

    def unrealized_rows(self, rates, prices: dict[int, tuple[date, Fraction]], sids: set | None = None) -> list[list[str]]:
        rows = []
        for l in self.open_lots(rates, sids=sids):
            if l.security not in prices:
                continue
            pday, close = prices[l.security]
            sec = self.securities[l.security]
            r = rates.rate(sec.currency, pday)
            mv = E.rhu(l.qty * close * r * 100)
            rows.append([self.accounts[l.account].name, sec.symbol, E.qty_str(l.qty), l.holding.isoformat(),
                         E.term_of(l.holding, pday), str(l.basis), str(mv), str(mv - l.basis)])
        return rows


def render(rows: list[list[str]], text_cols: tuple[int, ...]) -> list[list[str]]:
    """Expected CSV cell values after the spreadsheet-safety rule (R9.5)."""
    return [[neutral(c) if i in text_cols else c for i, c in enumerate(r)] for r in rows]


REALIZED_TEXT = (1, 2)
SUMMARY_TEXT = (0, 1)
UNREALIZED_TEXT = (0, 1)


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    c = abs(cents)
    return f"{sign}${c // 100:,}.{c % 100:02d}"


# ---- legacy rows -> transactions ---------------------------------------------------------------------------
def legacy_txn(r: dict) -> Txn:
    kind = r["kind"].strip().lower()
    q = Fraction(str(r["quantity"])) if r.get("quantity") is not None else None
    amt = Fraction(str(r["amount"])) if r.get("amount") is not None else None
    t = Txn(tid=int(r["id"]), seq=int(r["id"]), account=int(r["account_id"]), security=int(r["security_id"]),
            kind=kind, day=date.fromisoformat(r["trade_date"]), ref=r.get("ref"))
    if kind in ("buy", "reinvest", "sell"):
        t.quantity = abs(q)
        t.amount = int(abs(amt) * 100)
    elif kind == "dividend":
        t.amount = int(abs(amt) * 100)
    elif kind == "split":
        t.ratio = (int(r["split_new"]), int(r["split_old"]))
    elif kind == "transfer":
        t.to_account = int(r["to_account_id"])
    return t


def action_txn(r: dict) -> Txn:
    """A corporate action (legacy row, or one the verifier recorded) as an engine event (R13)."""
    num = lambda v: None if v is None or v == "" else Fraction(str(v))  # noqa: E731
    k = r["kind"]
    if k == "spinoff":
        params = {"new_security": int(r["new_security_id"]), "ratio": num(r.get("ratio")), "parent_fmv": num(r.get("parent_fmv")),
                  "new_fmv": num(r.get("new_fmv")), "cash_in_lieu_price": num(r.get("cash_in_lieu_price"))}
    elif k == "merger":
        acq = r.get("acquirer_security_id")
        params = {"acquirer": int(acq) if acq not in (None, "") else None, "shares_per_share": num(r.get("shares_per_share")) or Fraction(0),
                  "cash_per_share": num(r.get("cash_per_share")) or Fraction(0), "acquirer_fmv": num(r.get("acquirer_fmv"))}
    else:
        params = {"amount_per_share": num(r.get("amount_per_share"))}
    i = int(r["id"])
    return Txn(i, i, None, int(r["security_id"]), k, date.fromisoformat(str(r["action_date"])), params=params)


def same_content(a: Txn, b: Txn) -> bool:
    return (a.kind, a.day, a.security, a.quantity, a.amount, a.ratio) == (b.kind, b.day, b.security, b.quantity, b.amount, b.ratio)
