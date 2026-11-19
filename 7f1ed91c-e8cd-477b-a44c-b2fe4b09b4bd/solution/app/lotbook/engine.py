"""Lotbook tax-lot engine: lots, FIFO/HIFO/specific relief, splits, transfers, wash sales, corporate actions, IRAs (instruction R5-R8, R13).

Everything is exact: quantities and rates are Fractions, money is integer cents. The engine works on one
household at a time and, inside it, on one group of securities at a time: a security together with every
security a corporate action links it to (spin-offs, mergers). Lots, splits, transfers and wash sales never cross
groups.

Public entry points:
    Rates(rows).usd(cents, currency, day)               -> USD cents (R5.1-R5.3)
    compute(txns, securities, rates, method_for, ira)   -> Result (portions, dividends, open lots, errors)
    year_figures(result)                                -> {(year, security): Fig}
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction

CA_KINDS = ("spinoff", "merger", "return_of_capital")
KIND_RANK = {"split": 0, "spinoff": 1, "merger": 1, "return_of_capital": 1, "buy": 2, "reinvest": 2, "transfer": 3,
             "sell": 4, "dividend": 5}
ACQUIRE = ("buy", "reinvest")
WINDOW_DAYS = 30


# ---- rounding and the share rule (R5.2, R5.4) -------------------------------------------------------
def rhu(x: Fraction) -> int:
    """Round a non-negative exact value to an integer, half up."""
    x = Fraction(x)
    if x < 0:
        raise ValueError(f"negative value {x}")
    q, r = divmod(x.numerator, x.denominator)
    return q + (1 if 2 * r >= x.denominator else 0)


def share(amount: int, m: Fraction, q: Fraction) -> int:
    """The part of `amount` cents spread over quantity q that m units take (the rest keeps amount - this)."""
    if m == q:
        return amount
    return rhu(Fraction(amount) * m / q)


def anniversary(d: date) -> date:
    if d.month == 2 and d.day == 29:
        return date(d.year + 1, 2, 28)
    return date(d.year + 1, d.month, d.day)


def term_of(holding_start: date, sold: date) -> str:
    return "long" if sold > anniversary(holding_start) else "short"


def qty_str(q: Fraction) -> str:
    """An exact decimal quantity, without trailing zeros or exponent (R5.5)."""
    q = Fraction(q)
    sign = "-" if q < 0 else ""
    q = abs(q)
    k = 0
    while (q * 10 ** k).denominator != 1:
        k += 1
        if k > 30:
            raise ValueError(f"quantity {q} is not a terminating decimal")
    n = (q * 10 ** k).numerator
    s = str(n).rjust(k + 1, "0")
    whole, frac = (s[:-k], s[-k:]) if k else (s, "")
    frac = frac.rstrip("0")
    return sign + whole + ("." + frac if frac else "")


# ---- exchange rates (R5.1) ----------------------------------------------------------------------------
class Rates:
    def __init__(self, rows):
        """rows: iterable of (currency, rate_date, value: Fraction|str, quote)."""
        by: dict[str, list] = {}
        for cur, d, v, quote in rows:
            v = Fraction(str(v)) if not isinstance(v, Fraction) else v
            r = v if quote == "usd_per_unit" else 1 / v
            by.setdefault(cur.strip().upper(), []).append((d, r))
        self.by = {c: sorted(v) for c, v in by.items()}
        self.dates = {c: [d for d, _ in v] for c, v in self.by.items()}

    def rate(self, cur: str, d: date) -> Fraction | None:
        cur = cur.strip().upper()
        if cur == "USD":
            return Fraction(1)
        ds = self.dates.get(cur)
        if not ds:
            return None
        i = bisect.bisect_right(ds, d) - 1
        return self.by[cur][i][1] if i >= 0 else None

    def usd(self, cents: int, cur: str, d: date) -> int:
        r = self.rate(cur, d)
        if r is None:
            raise ValueError(f"no {cur} rate on or before {d}")
        return rhu(Fraction(cents) * r)


# ---- records -----------------------------------------------------------------------------------------
@dataclass
class Txn:
    tid: int                 # transaction id (legacy id, or the id the application gave it); for a corporate action, its id
    seq: int                 # record order (R6.1)
    account: int | None      # None for a corporate action (it applies to every account, R13)
    security: int            # for a corporate action: the parent, the target, or the distributing security
    kind: str                # buy, reinvest, sell, dividend, split, transfer, spinoff, merger, return_of_capital
    day: date
    quantity: Fraction | None = None   # shares, positive (buy, reinvest, sell)
    amount: int | None = None          # currency cents, non-negative (buy, reinvest, sell, dividend)
    ratio: tuple[int, int] | None = None   # (new, old) for split
    to_account: int | None = None          # transfer destination
    selection: list | None = None          # specific lots: [(acquisition tid, Fraction qty)]
    ref: str | None = None
    note: str = ""
    params: dict | None = None             # corporate action terms (Fractions; security ids)

    def sort_key(self):
        return (self.day, KIND_RANK[self.kind], self.seq)


@dataclass
class Lot:
    acq: int                 # tid of the acquiring buy/reinvestment ("lot id"); lots from a spin-off or merger keep it
    acq_seq: int
    acquired: date
    holding: date            # holding-period start
    qty: Fraction
    basis: int               # USD cents
    account: int
    security: int
    used: bool = False       # has served as replacement shares (R7.2)
    path: tuple = ()
    wash_adjusted: bool = False
    wash_cents: int = 0      # disallowed loss added to this lot's basis when it became replacement shares (R7.3)

    def key(self):
        return (self.acquired, self.acq_seq, self.path)

    def per_share(self) -> Fraction:
        return Fraction(self.basis) / self.qty


@dataclass
class Portion:
    sale: Txn                # the sale, or the corporate action that produced the row
    index: int
    acq: int
    account: int
    security: int
    qty: Fraction
    holding: date
    proceeds: int
    basis: int
    disallowed: int = 0
    kind: str = "sale"       # sale, cash_in_lieu, merger, return_of_capital
    ira: bool = False        # in a retirement account: never reported (R13.9)

    @property
    def raw_gain(self) -> int:
        return self.proceeds - self.basis

    @property
    def gain(self) -> int:
        return self.proceeds - self.basis + self.disallowed

    @property
    def term(self) -> str:
        return term_of(self.holding, self.sale.day)

    @property
    def day(self) -> date:
        return self.sale.day

    @property
    def order(self) -> tuple:
        return (self.sale.day, KIND_RANK[self.sale.kind], self.sale.seq, self.index)


@dataclass
class Result:
    portions: list = field(default_factory=list)
    dividends: list = field(default_factory=list)   # (Txn, usd cents)
    lots: list = field(default_factory=list)        # open Lots after everything
    errors: list = field(default_factory=list)      # (Txn, reason)
    ira: frozenset = frozenset()                    # retirement account ids


@dataclass
class Fig:
    short: int = 0
    long: int = 0
    disallowed: int = 0
    dividends: int = 0
    active: bool = False     # had a sale portion or a dividend in the year

    def get(self, cat: str) -> int:
        return {"short": self.short, "long": self.long, "dividends": self.dividends}[cat]


CATS = ("short", "long", "dividends")


# ---- the engine (R6-R8, R13) --------------------------------------------------------------------------------
def linked(t: Txn) -> int | None:
    p = t.params or {}
    return p.get("new_security") if t.kind == "spinoff" else p.get("acquirer") if t.kind == "merger" else None


def groups(txns: list[Txn]) -> dict[int, int]:
    """security -> group representative (securities linked by spin-offs and mergers share a group)."""
    parent: dict[int, int] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for t in txns:
        find(t.security)
        other = linked(t)
        if other is not None:
            a, b = find(t.security), find(other)
            if a != b:
                parent[max(a, b)] = min(a, b)
    return {s: find(s) for s in list(parent)}


def compute(txns: list[Txn], currency_of: dict[int, str], rates: Rates, method_for, wash: bool = True,
            ira=frozenset()) -> Result:
    res = Result(ira=frozenset(ira))
    rep = groups(txns)
    by_group: dict[int, list[Txn]] = {}
    for t in txns:
        by_group.setdefault(rep[t.security], []).append(t)
    for g, ts in sorted(by_group.items()):
        _group(sorted(ts, key=Txn.sort_key), currency_of, rates, method_for, wash, res)
    return res


def _insert(lst: list[Lot], lot: Lot) -> None:
    lst.insert(bisect.bisect_right(lst, lot.key(), key=Lot.key), lot)


def _split_off(lot: Lot, m: Fraction) -> tuple[Lot, Lot]:
    """Split m shares off `lot` (share rule): the split-off part comes first (R6.4, R7.3)."""
    b = share(lot.basis, m, lot.qty)
    head = Lot(lot.acq, lot.acq_seq, lot.acquired, lot.holding, m, b, lot.account, lot.security, lot.used,
               lot.path + (0,), lot.wash_adjusted, lot.wash_cents)
    rest = Lot(lot.acq, lot.acq_seq, lot.acquired, lot.holding, lot.qty - m, lot.basis - b, lot.account,
               lot.security, lot.used, lot.path + (1,), lot.wash_adjusted, lot.wash_cents)
    return head, rest


def floor_of(q: Fraction) -> int:
    return q.numerator // q.denominator


def _group(order: list[Txn], currency_of: dict[int, str], rates: Rates, method_for, wash: bool, res: Result) -> None:
    lots: dict[tuple[int, int], list[Lot]] = {}     # (account, security) -> open lots in FIFO order
    where: dict[tuple[int, int], int] = {}          # (lot id, security) -> the account holding them (R6.5)
    claims: dict[int, list[tuple[Fraction, int, int]]] = {}
    acqs: dict[int, list[Txn]] = {}
    for t in order:
        if t.kind in ACQUIRE:
            acqs.setdefault(t.security, []).append(t)
    acq_days = {s: [t.day for t in v] for s, v in acqs.items()}
    ira = res.ira

    def make_replacement(lot: Lot, d: int, shift: int) -> None:
        lot.used = True
        if lot.account in ira:  # R13.10: the loss is disallowed for good; nothing is added, the holding period stays
            return
        lot.basis += d
        lot.holding = lot.acquired - timedelta(days=shift)
        lot.wash_adjusted = True
        lot.wash_cents += d

    def accounts_holding(sec: int) -> list[int]:
        return sorted(a for (a, s), v in lots.items() if s == sec and v)

    def carry(lot: Lot, sec: int, qty: Fraction, basis: int) -> Lot:
        return Lot(lot.acq, lot.acq_seq, lot.acquired, lot.holding, qty, basis, lot.account, sec, lot.used, lot.path,
                   lot.wash_adjusted, lot.wash_cents)

    for t in order:
        if t.kind in CA_KINDS:
            _action(t, lots, where, currency_of, rates, res, accounts_holding, carry, ira)
            continue
        acct = lots.setdefault((t.account, t.security), [])
        cur = currency_of[t.security]
        if t.kind == "split":
            n, o = t.ratio
            for lot in acct:
                lot.qty = lot.qty * n / o
        elif t.kind in ACQUIRE:
            basis = rates.usd(t.amount, cur, t.day)
            lot = Lot(t.tid, t.seq, t.day, t.day, Fraction(t.quantity), basis, t.account, t.security)
            parts = []
            for m, d, shift in claims.pop(t.tid, []):
                if m == lot.qty:
                    make_replacement(lot, d, shift)
                    break
                head, lot = _split_off(lot, m)
                make_replacement(head, d, shift)
                parts.append(head)
            else:
                lot = lot if lot.qty > 0 else None
            if lot is not None:
                parts.append(lot)
            for p in parts:
                _insert(acct, p)
            where[(t.tid, t.security)] = t.account
        elif t.kind == "transfer":
            if not acct:
                res.errors.append((t, "the account holds no shares of the security to transfer"))
                continue
            dest = lots.setdefault((t.to_account, t.security), [])
            for lot in acct:
                lot.account = t.to_account
                where[(lot.acq, t.security)] = t.to_account
                _insert(dest, lot)
            lots[(t.account, t.security)] = []
        elif t.kind == "dividend":
            res.dividends.append((t, rates.usd(t.amount, cur, t.day)))
        elif t.kind == "sell":
            picks = _relief(t, acct, method_for)
            if isinstance(picks, str):
                res.errors.append((t, picks))
                continue
            proceeds = rates.usd(t.amount, cur, t.day)
            p_rem, q_rem = proceeds, Fraction(t.quantity)
            sale_portions = []
            in_ira = t.account in ira
            for i, (lot, m) in enumerate(picks):
                b = share(lot.basis, m, lot.qty)
                p = share(p_rem, m, q_rem)
                p_rem, q_rem = p_rem - p, q_rem - m
                sale_portions.append(Portion(t, i, lot.acq, t.account, t.security, m, lot.holding, p, b, ira=in_ira))
                lot.basis -= b
                lot.qty -= m
            lots[(t.account, t.security)] = [l for l in acct if l.qty > 0]
            if wash and not in_ira:
                for por in sale_portions:
                    if por.raw_gain < 0:
                        _wash(por, t, lots, where, claims, acqs.get(t.security, []), acq_days.get(t.security, []),
                              make_replacement)
            res.portions.extend(sale_portions)
        else:
            raise ValueError(f"unknown kind {t.kind}")
    for acct in lots.values():
        res.lots.extend(acct)


def _action(t: Txn, lots, where, currency_of, rates: Rates, res: Result, accounts_holding, carry, ira) -> None:
    """Spin-off, merger or return of capital (R13): every account holding the security, in account id order, and in
    each account its lots in FIFO order. Rows these actions produce are never matched by the wash-sale rule."""
    p = t.params
    sec, D = t.security, t.day
    cur = currency_of[sec]
    idx = 0

    def row(kind, lot, acct, rsec, qty, proceeds, basis):
        nonlocal idx
        res.portions.append(Portion(t, idx, lot.acq, acct, rsec, qty, lot.holding, proceeds, basis, kind=kind, ira=acct in ira))
        idx += 1

    def usd_of(qty: Fraction, price: Fraction) -> int:
        """qty x price in the security's currency, rounded to the cent, then its USD value (R5.3)."""
        return rates.usd(rhu(qty * price * 100), cur, D)

    if t.kind == "spinoff":
        new, r, fp, fn, cil = p["new_security"], p["ratio"], p["parent_fmv"], p["new_fmv"], p["cash_in_lieu_price"]
        a = r * fn / (r * fn + fp)
        for acct in accounts_holding(sec):
            for lot in lots[(acct, sec)]:
                alloc = rhu(lot.basis * a)                  # R13.2
                lot.basis -= alloc
                n = lot.qty * r
                whole = floor_of(n)
                frac = n - whole
                b_frac = share(alloc, frac, n) if frac else 0
                if frac:
                    row("cash_in_lieu", lot, acct, new, frac, usd_of(frac, cil), b_frac)
                if whole:
                    nl = carry(lot, new, Fraction(whole), alloc - b_frac)
                    _insert(lots.setdefault((acct, new), []), nl)
                    where[(lot.acq, new)] = acct
    elif t.kind == "merger":
        acq, s, k, fa = p.get("acquirer"), p["shares_per_share"], p["cash_per_share"], p.get("acquirer_fmv")
        for acct in accounts_holding(sec):
            for lot in lots[(acct, sec)]:
                cash = usd_of(lot.qty, k) if k else 0
                if not s:                                   # cash only: a sale (R13.6)
                    row("merger", lot, acct, sec, lot.qty, cash, lot.basis)
                    continue
                value = usd_of(lot.qty, s * fa)
                realized = cash + value - lot.basis
                recognized = min(realized, cash) if realized > 0 else 0   # R13.5
                if cash:
                    row("merger", lot, acct, sec, lot.qty, cash, cash - recognized)
                nl = carry(lot, acq, lot.qty * s, lot.basis - cash + recognized)
                _insert(lots.setdefault((acct, acq), []), nl)
                where[(lot.acq, acq)] = acct
            lots[(acct, sec)] = []
    else:                                                   # return of capital (R13.7)
        d = p["amount_per_share"]
        for acct in accounts_holding(sec):
            for lot in lots[(acct, sec)]:
                u = usd_of(lot.qty, d)
                if u <= lot.basis:
                    lot.basis -= u
                else:
                    row("return_of_capital", lot, acct, sec, lot.qty, u - lot.basis, 0)
                    lot.basis = 0

def _relief(t: Txn, acct: list[Lot], method_for):
    q = Fraction(t.quantity)
    held = sum((l.qty for l in acct), Fraction(0))
    if t.selection:
        want: dict[int, Fraction] = {}
        for acq, qq in t.selection:
            want[acq] = want.get(acq, Fraction(0)) + Fraction(qq)
        if sum(want.values()) != q:
            return "the chosen lot quantities do not add up to the quantity sold"
        for acq, qq in want.items():
            have = sum((l.qty for l in acct if l.acq == acq), Fraction(0))
            if qq <= 0 or have < qq:
                return f"lot {acq} does not hold {qty_str(qq)} shares in this account at that point"
        picks = []
        for lot in acct:  # portions in FIFO order of the chosen lots
            need = want.get(lot.acq, Fraction(0))
            if need > 0:
                m = min(need, lot.qty)
                picks.append((lot, m))
                want[lot.acq] = need - m
        return picks
    if held < q:
        return f"sells {qty_str(q)} shares but the account holds {qty_str(held)} at that point"
    method = method_for(t.account, t.day)
    ordered = acct if method == "FIFO" else sorted(acct, key=lambda l: (-l.per_share(), l.key()))
    picks, rem = [], q
    for lot in ordered:
        if rem == 0:
            break
        m = min(rem, lot.qty)
        picks.append((lot, m))
        rem -= m
    return picks


def _wash(por: Portion, sale: Txn, lots, where, claims, acqs, acq_days, make_replacement) -> None:
    D = sale.day
    loss = por.basis - por.proceeds
    l_rem, q_rem = loss, por.qty
    shift = (D - por.holding).days
    lo = bisect.bisect_left(acq_days, D - timedelta(days=WINDOW_DAYS))
    hi = bisect.bisect_right(acq_days, D + timedelta(days=WINDOW_DAYS))
    for c in acqs[lo:hi]:  # earliest acquisition first (date, then record order)
        if q_rem == 0:
            break
        if c.tid == por.acq:
            continue
        if c.day <= D:  # already processed: its still-open, unused shares (wherever they are held now)
            acct = lots.get((where.get((c.tid, c.security)), c.security), [])
            i = bisect.bisect_left(acct, (c.day, c.seq), key=lambda l: (l.acquired, l.acq_seq))
            while i < len(acct) and q_rem > 0 and acct[i].acq == c.tid:
                lot = acct[i]
                if not lot.used:
                    m = min(q_rem, lot.qty)
                    d = share(l_rem, m, q_rem)
                    l_rem, q_rem = l_rem - d, q_rem - m
                    if m == lot.qty:
                        make_replacement(lot, d, shift)
                    else:
                        head, rest = _split_off(lot, m)
                        make_replacement(head, d, shift)
                        acct[i:i + 1] = [head, rest]
                        i += 1
                i += 1
        else:  # a later acquisition: claim its shares now; the claim is applied when it is processed
            taken = sum((m for m, _, _ in claims.get(c.tid, [])), Fraction(0))
            avail = Fraction(c.quantity) - taken
            if avail <= 0:
                continue
            m = min(q_rem, avail)
            d = share(l_rem, m, q_rem)
            l_rem, q_rem = l_rem - d, q_rem - m
            claims.setdefault(c.tid, []).append((m, d, shift))
    por.disallowed = loss - l_rem


def year_figures(res: Result) -> dict[tuple[int, int], Fig]:
    """Per tax year and security (R8.1); retirement accounts report nothing (R13.9)."""
    out: dict[tuple[int, int], Fig] = {}
    for p in res.portions:
        if p.ira:
            continue
        f = out.setdefault((p.day.year, p.security), Fig())
        f.active = True
        if p.term == "short":
            f.short += p.gain
        else:
            f.long += p.gain
        f.disallowed += p.disallowed
    for t, usd in res.dividends:
        if t.account in res.ira:
            continue
        f = out.setdefault((t.day.year, t.security), Fig())
        f.active = True
        f.dividends += usd
    return out


def method_lookup(initial: dict[int, str], changes: list[tuple[int, date, int, str]]):
    """changes: (account, effective_from, record order, method). The latest effective change on or before the day wins."""
    by: dict[int, list] = {}
    for a, eff, seq, m in changes:
        by.setdefault(a, []).append((eff, seq, m))
    for v in by.values():
        v.sort()

    def method_for(account: int, day: date) -> str:
        best = initial.get(account, "FIFO")
        for eff, _seq, m in by.get(account, []):
            if eff <= day:
                best = m
        return best
    return method_for
