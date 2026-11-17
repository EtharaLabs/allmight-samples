"""Pickpath reference world: the state the rules act on, kept from the verifier's own actions and the baseline.

The verifier records every action it performs that the application accepted (receipts, payments confirmed by
webhook, waves, picks, counts, shipments, returns, cancellations) and replays it here; the world then predicts
what the application must show (allocations, backorders, refunds, invoices, credits, on-hand, routes, values).
Nothing here reads the application's own tables.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rules as R  # noqa: E402

QA = "QA-01"


@dataclass
class Sku:
    sku_id: int
    code: str
    name: str
    info: R.SkuInfo
    reorder: int
    active: bool


@dataclass
class Lot:
    lot_id: int
    sku: str
    code: str
    expiry: date
    bin: str
    cost: Decimal
    qa_hold: bool = False
    on_hand: int = 0
    version: int = 0
    count_hold: bool = False
    legacy_moves: list = field(default_factory=list)   # (instant, id, kind, units)
    moves: list = field(default_factory=list)          # verifier-era: (seq, kind, units)


@dataclass
class Cust:
    cid: int
    tier: str
    state: str
    limit: int | None
    min_days: int
    split: bool
    backorders: bool
    name: str = ""


@dataclass
class Line:
    no: int
    sku: str
    qty: int
    price: int
    taxable: bool
    amount: int
    tax: int
    backordered: int = 0
    cancelled: int = 0
    shipped: int = 0
    returned: int = 0
    rma_units: int = 0


@dataclass
class Order:
    oid: int
    cust: Cust
    ship: date
    lines: list
    freight: int
    ftax: int
    total: int
    rate: Decimal
    status: str
    created_by: int = 0
    refunds: list = field(default_factory=list)
    shipments: list = field(default_factory=list)
    credits: list = field(default_factory=list)
    shipped_lots: dict = field(default_factory=dict)   # line_no -> set(lot_id)


@dataclass
class Task:
    key: int
    oid: int
    line: int
    lot: int
    units: int
    status: str = "open"
    wave: int | None = None
    app_id: int | None = None


class World:
    def __init__(self, baseline: dict):
        self.b = baseline
        self.tz = baseline["tz"]
        self.rates = {r["state"]: (Decimal(str(r["rate"])), bool(r["freight_taxable"])) for r in baseline["tax_rates"]}
        self.skus: dict[str, Sku] = {}
        self.sku_by_id: dict[int, str] = {}
        for s in baseline["skus"]:
            self._add_sku(s["id"], s["code"], s["name"], s["case_pack"], s["unit_weight_g"], s["length_cm"], s["width_cm"],
                          s["height_cm"], s["storage"], s["taxable"], s["reorder_point"], s["active"])
        self.prices: dict[tuple[str, str], list] = {}
        for p in baseline["price_list"]:
            self.prices.setdefault((p["tier"], self.sku_by_id[p["sku_id"]]), []).append((date.fromisoformat(p["effective_from"]), p["unit_price_cents"]))
        bins = {b["id"]: b["code"] for b in baseline["bins"]}
        self.lots: dict[int, Lot] = {}
        self.lots_of: dict[str, list[int]] = {}
        for l in baseline["lots"]:
            lot = Lot(l["id"], self.sku_by_id[l["sku_id"]], l["lot_code"], date.fromisoformat(l["expiry_date"]), bins[l["bin_id"]],
                      Decimal(str(l["unit_cost"])), bool(l["qa_hold"]))
            self.lots[lot.lot_id] = lot
            self.lots_of.setdefault(lot.sku, []).append(lot.lot_id)
        for m in baseline["legacy_movements"]:
            lot = self.lots[m["lot_id"]]
            at = R.legacy_instant(datetime.fromisoformat(m["local_time"]), m["fold"], self.tz)
            lot.legacy_moves.append((at, m["id"], m["kind"], m["units"]))
            lot.on_hand += m["units"]
            lot.version += 1
        self.customers: dict[int, Cust] = {}
        for c in baseline["customers"]:
            self.customers[c["id"]] = Cust(c["id"], c["tier"], c["ship_to_state"], c["approval_limit_cents"], c["min_shelf_life_days"],
                                           c["allow_split_cases"], c["backorders_allowed"], c["name"])
        self.orders: dict[int, Order] = {}
        self.tasks: list[Task] = []
        self.seq = 0
        self.touched_skus: set[str] = set()

    # -- catalogue ---------------------------------------------------------------------------------------
    def _add_sku(self, sid, code, name, pack, w, l, wd, h, storage, taxable, reorder, active=True):
        self.skus[code] = Sku(sid, code, name, R.SkuInfo(code, pack, w, l, wd, h, storage, bool(taxable)), reorder, bool(active))
        self.sku_by_id[sid] = code

    def add_sku(self, sid, code, name, pack, w, l, wd, h, storage, taxable, reorder):
        self._add_sku(sid, code, name, pack, w, l, wd, h, storage, taxable, reorder, True)

    def add_price(self, tier, code, cents, eff: date):
        self.prices.setdefault((tier, code), []).append((eff, cents))

    def price(self, tier, code, day: date):
        return R.price_on(self.prices.get((tier, code), []), day)

    def receipt(self, lot_id: int, code: str, lot_code: str, expiry: date, bin_code: str, cost: Decimal, units: int) -> None:
        if lot_id not in self.lots:
            self.lots[lot_id] = Lot(lot_id, code, lot_code.strip(), expiry, bin_code, cost)
            self.lots_of.setdefault(code, []).append(lot_id)
        self._move(self.lots[lot_id], "receipt", units)
        self.fill(code)

    def _move(self, lot: Lot, kind: str, units: int) -> None:
        self.seq += 1
        lot.moves.append((self.seq, kind, units))
        lot.on_hand += units
        lot.version += 1
        self.touched_skus.add(lot.sku)

    # -- orders --------------------------------------------------------------------------------------------
    def quote(self, cust: Cust, ship: date, lines: list[tuple[str, int]]) -> dict | str:
        """R4.1 + R5: the expected order JSON money fields, or a reason it is refused."""
        seen = set()
        parts = []
        for code, qty in lines:
            s = self.skus.get(code.strip().upper())
            if s is None or not s.active:
                return "unknown or inactive SKU"
            if s.code in seen:
                return "duplicate SKU"
            seen.add(s.code)
            if not cust.split and qty % s.info.case_pack:
                return "not whole cases"
            p = self.price(cust.tier, s.code, ship)
            if p is None:
                return "no price"
            parts.append((s.info, qty, p))
        rate, ft = self.rates[cust.state]
        return R.order_totals(parts, rate, ft)

    def add_order(self, oid: int, cust: Cust, ship: date, lines: list[tuple[str, int]], created_by: int = 0) -> Order:
        q = self.quote(cust, ship, lines)
        assert isinstance(q, dict), q
        rate, _ = self.rates[cust.state]
        ls = []
        for i, (ql, (code, qty)) in enumerate(zip(q["lines"], lines), start=1):
            s = self.skus[code.strip().upper()]
            ls.append(Line(i, s.code, qty, ql["unit_price_cents"], s.info.taxable, ql["amount_cents"], ql["tax_cents"]))
        needs = cust.limit is not None and q["total_cents"] > cust.limit
        o = Order(oid, cust, ship, ls, q["freight_cents"], q["freight_tax_cents"], q["total_cents"], rate,
                  "awaiting_approval" if needs else "awaiting_payment", created_by)
        self.orders[oid] = o
        return o

    def due(self, o: Order) -> int:
        return R.due_of([{"qty": l.qty, "cancelled": l.cancelled, "unit_price_cents": l.price, "taxable": l.taxable} for l in o.lines],
                        o.freight, o.ftax, o.rate)

    def _refund_diff(self, o: Order, before: int) -> None:
        d = before - self.due(o)
        if d > 0:
            o.refunds.append(d)

    def refresh(self, o: Order) -> None:
        if o.status not in ("in_fulfilment", "complete", "cancelled"):
            return
        if all(l.cancelled == l.qty for l in o.lines):
            o.status = "cancelled"
        elif all(l.shipped + l.cancelled == l.qty for l in o.lines):
            o.status = "complete"
        else:
            o.status = "in_fulfilment"

    # -- allocation ------------------------------------------------------------------------------------------
    def reserved(self, lot_id: int, statuses=("open", "picked")) -> int:
        return sum(t.units for t in self.tasks if t.lot == lot_id and t.status in statuses)

    def allocatable(self, lot: Lot) -> bool:
        return lot.bin != QA and not lot.qa_hold and not lot.count_hold

    def _allocate(self, o: Order, line: Line, n: int) -> int:
        s = self.skus[line.sku]
        views = [R.LotView(l.lot_id, l.expiry, l.on_hand - self.reserved(l.lot_id), self.allocatable(l))
                 for l in (self.lots[i] for i in self.lots_of.get(line.sku, []))]
        got, rem = R.fefo(n, views, o.ship, o.cust.min_days, s.info.case_pack, o.cust.split)
        for lot_id, units in got:
            t = next((t for t in self.tasks if t.oid == o.oid and t.line == line.no and t.lot == lot_id and t.status == "open"
                      and t.wave is None), None)
            if t:
                t.units += units
            else:
                self.tasks.append(Task(len(self.tasks) + 1, o.oid, line.no, lot_id, units))
        if got:
            self.touched_skus.add(line.sku)
        return rem

    def _settle(self, o: Order, line: Line, rem: int) -> None:
        if rem > 0:
            if o.cust.backorders:
                line.backordered += rem
            else:
                line.cancelled += rem

    def pay(self, oid: int) -> None:
        o = self.orders[oid]
        o.status = "in_fulfilment"
        before = self.due(o)
        for line in o.lines:
            self._settle(o, line, self._allocate(o, line, line.qty))
        self._refund_diff(o, before)
        self.refresh(o)

    def fill(self, code: str) -> None:
        for o in sorted(self.orders.values(), key=lambda o: o.oid):
            if o.status != "in_fulfilment":
                continue
            for line in o.lines:
                if line.sku == code and line.backordered > 0:
                    line.backordered = self._allocate(o, line, line.backordered)

    def cancel_backorder(self, oid: int, line_no: int, n: int) -> None:
        o = self.orders[oid]
        line = o.lines[line_no - 1]
        before = self.due(o)
        line.backordered -= n
        line.cancelled += n
        self._refund_diff(o, before)
        self.refresh(o)

    def approve(self, oid: int) -> None:
        self.orders[oid].status = "awaiting_payment"

    def cancel(self, oid: int) -> None:
        self.orders[oid].status = "cancelled"

    # -- waves, picks, counts --------------------------------------------------------------------------------
    def wave(self, wid: int, zone: str, ship: date, k: int) -> tuple[list[Task], int, list[str]]:
        cands = [t for t in self.tasks if t.status == "open" and t.wave is None and t.units > 0
                 and self.lots[t.lot].bin[0] == zone and self.orders[t.oid].ship == ship]
        cands.sort(key=lambda t: (t.oid, t.line, self.lots[t.lot].expiry, t.lot))
        chosen = cands[:k]
        for t in chosen:
            t.wave = wid
        dist, seq = R.route([self.lots[t.lot].bin for t in chosen]) if chosen else (0, [])
        return chosen, dist, seq

    def route_of(self, wid: int) -> tuple[list[Task], int, list[str]]:
        """A wave already made: its tasks, its route distance and its stops in route order (R7.3)."""
        tasks = [t for t in self.tasks if t.wave == wid]
        dist, seq = R.route([self.lots[t.lot].bin for t in tasks]) if tasks else (0, [])
        return tasks, dist, seq

    def task_by_app(self, app_id: int) -> Task:
        return next(t for t in self.tasks if t.app_id == app_id)

    def confirm(self, t: Task, units: int) -> None:
        o = self.orders[t.oid]
        if units == t.units:
            t.status = "picked"
            return
        short = t.units - units
        before = self.due(o)
        t.units, t.status = units, "picked"
        self.lots[t.lot].count_hold = True
        line = o.lines[t.line - 1]
        self._settle(o, line, self._allocate(o, line, short))
        self._refund_diff(o, before)
        self.refresh(o)

    def count(self, lot_id: int, counted: int, realloc=None) -> list[tuple[int, int, int]]:
        """R7.7. Units are released from the lot's unpicked tasks in the stated order (highest order id, then highest
        line_no, first). The text does not fix the order in which the released units are then allocated anew, so
        `realloc` may reorder the released (order id, line_no, units) entries; None keeps the release order.
        Returns the released entries in release order."""
        lot = self.lots[lot_id]
        new = counted + self.reserved(lot_id, ("picked",))
        self._move(lot, "count", new - lot.on_hand)
        lot.count_hold = False
        excess = self.reserved(lot_id, ("open",)) - counted
        released = []
        if excess > 0:
            for t in sorted([t for t in self.tasks if t.lot == lot_id and t.status == "open"],
                            key=lambda t: (t.oid, t.line, t.key), reverse=True):
                if excess <= 0:
                    break
                take = min(excess, t.units)
                t.units -= take
                excess -= take
                released.append((t.oid, t.line, take))
            self.tasks = [t for t in self.tasks if not (t.status == "open" and t.units == 0)]
        befores = {}
        for oid, ln, n in (released if realloc is None else realloc(list(released))):
            o = self.orders[oid]
            befores.setdefault(oid, self.due(o))
            line = o.lines[ln - 1]
            self._settle(o, line, self._allocate(o, line, n))
        for oid, before in befores.items():
            self._refund_diff(self.orders[oid], before)
            self.refresh(self.orders[oid])
        self.fill(lot.sku)
        return released

    # -- trying the readings the text allows -----------------------------------------------------------------
    def _snapshot(self, code: str):
        """Everything a count of a lot of SKU `code` can change, kept by value so objects keep their identity."""
        return (self.seq, set(self.touched_skus), list(self.tasks), [(t, t.units, t.status, t.wave) for t in self.tasks],
                [(l, l.on_hand, l.version, l.count_hold, list(l.moves)) for l in (self.lots[i] for i in self.lots_of.get(code, []))],
                [(o, o.status, list(o.refunds), [(ln, ln.backordered, ln.cancelled) for ln in o.lines]) for o in self.orders.values()])

    def _restore(self, snap) -> None:
        self.seq, touched, tasks, task_vals, lot_vals, order_vals = snap
        self.touched_skus = set(touched)
        self.tasks = list(tasks)
        for t, units, status, wave in task_vals:
            t.units, t.status, t.wave = units, status, wave
        for l, on_hand, version, hold, moves in lot_vals:
            l.on_hand, l.version, l.count_hold, l.moves = on_hand, version, hold, list(moves)
        for o, status, refunds, lines in order_vals:
            o.status, o.refunds = status, list(refunds)
            for ln, bo, cancelled in lines:
                ln.backordered, ln.cancelled = bo, cancelled

    def count_matching(self, lot_id: int, counted: int, accept) -> tuple[bool, list[tuple[int, int, int]]]:
        """Apply a count (R7.7) under every order of re-allocating its released units that the text allows, and keep
        the first world for which `accept()` is true: the release order first, then ascending order id and line_no
        (the order R6.4 fills backorders in), then every other ordering of the released entries (up to five
        entries). When none is accepted, the release-order world is kept. Returns (accepted, released entries)."""
        snap = self._snapshot(self.lots[lot_id].sku)
        released = self.count(lot_id, counted)
        if accept():
            return True, released
        orders = [sorted(released, key=lambda e: (e[0], e[1]))]
        if 2 < len(released) <= 5:
            import itertools
            orders += [list(p) for p in itertools.permutations(released)]
        tried = [list(released)]
        for order in orders:
            if order in tried:
                continue
            tried.append(order)
            self._restore(snap)
            self.count(lot_id, counted, realloc=lambda _r, order=order: order)
            if accept():
                return True, released
        self._restore(snap)
        self.count(lot_id, counted)
        return False, released

    # -- shipping and returns ---------------------------------------------------------------------------------
    def ship(self, oid: int) -> dict:
        o = self.orders[oid]
        picked = [t for t in self.tasks if t.oid == oid and t.status == "picked" and t.units > 0]
        first = not o.shipments
        per_line: dict[int, int] = {}
        per_lot: dict[int, int] = {}
        for t in picked:
            per_line[t.line] = per_line.get(t.line, 0) + t.units
            per_lot[t.lot] = per_lot.get(t.lot, 0) + t.units
            o.shipped_lots.setdefault(t.line, set()).add(t.lot)
        lines, total = [], 0
        for line in o.lines:
            u = per_line.get(line.no, 0)
            if not u:
                continue
            amt = u * line.price
            tax = R.shipment_line_tax(line.shipped, line.shipped + u, line.price, line.taxable, o.rate)
            lines.append({"line_no": line.no, "units": u, "amount_cents": amt, "tax_cents": tax})
            total += amt + tax
            line.shipped += u
        fr, ftx = (o.freight, o.ftax) if first else (0, 0)
        total += fr + ftx
        for lot_id, u in sorted(per_lot.items()):
            self._move(self.lots[lot_id], "ship", -u)
        for t in self.tasks:
            if t.oid == oid and t.status == "picked":
                t.status = "shipped"
        inv = {"lines": lines, "freight": fr, "freight_tax": ftx, "total": total}
        o.shipments.append(inv)
        self.refresh(o)
        return inv

    def rma_credit(self, oid: int, line_no: int, units: int) -> int:
        o = self.orders[oid]
        line = o.lines[line_no - 1]
        return R.credit_of(line.shipped - line.returned, units, line.price, line.taxable, o.rate)

    def rma_receive(self, oid: int, line_no: int, units: int) -> int:
        o = self.orders[oid]
        c = self.rma_credit(oid, line_no, units)
        o.lines[line_no - 1].returned += units
        o.credits.append(c)
        return c

    def rma_lot(self, oid: int, line_no: int) -> Lot:
        lots = [self.lots[i] for i in self.orders[oid].shipped_lots.get(line_no, set())]
        return sorted(lots, key=lambda l: (l.expiry, l.lot_id))[0]

    def rma_inspect(self, oid: int, line_no: int, units: int, outcome: str, today: date) -> str:
        lot = self.rma_lot(oid, line_no)
        if outcome == "restock" and lot.expiry >= today + timedelta(days=7):
            self._move(lot, "restock", units)
            self.fill(lot.sku)
            return "restocked"
        return "written_off"

    # -- what the application must show ------------------------------------------------------------------------
    def order_view(self, oid: int) -> dict:
        o = self.orders[oid]
        lines = []
        for line in o.lines:
            alloc: dict[int, int] = {}
            for t in self.tasks:
                if t.oid == oid and t.line == line.no and t.status in ("open", "picked") and t.units:
                    alloc[t.lot] = alloc.get(t.lot, 0) + t.units
            lines.append({"line_no": line.no, "sku": line.sku, "qty": line.qty, "unit_price_cents": line.price,
                          "amount_cents": line.amount, "tax_cents": line.tax, "reserved": sum(alloc.values()),
                          "shipped": line.shipped, "backordered": line.backordered, "cancelled": line.cancelled,
                          "returned": line.returned,
                          "allocations": [{"lot_id": k, "units": v} for k, v in sorted(alloc.items())]})
        return {"status": o.status, "lines": lines, "refunds": list(o.refunds),
                "invoice_totals": [s["total"] for s in o.shipments], "credits": list(o.credits)}

    def lot_status(self, lot: Lot, today: date) -> str:
        if lot.bin == QA:
            return "quarantine"
        if lot.qa_hold or lot.count_hold:
            return "hold"
        if lot.expiry < today:
            return "expired"
        return "available"

    def on_hand_row(self, lot: Lot, today: date) -> list[str]:
        res = self.reserved(lot.lot_id)
        st = self.lot_status(lot, today)
        return [str(lot.lot_id), lot.sku, lot.code, lot.bin, lot.expiry.isoformat(), str(lot.on_hand), str(res),
                str(lot.on_hand - res if st == "available" else 0), st]

    def sku_available(self, code: str, today: date) -> int:
        return sum(l.on_hand - self.reserved(l.lot_id) for l in (self.lots[i] for i in self.lots_of.get(code, []))
                   if self.allocatable(l) and l.expiry >= today)

    # -- FIFO value (R9.5) -------------------------------------------------------------------------------------
    FAR = datetime(9999, 1, 1, tzinfo=R.timezone.utc)

    def value(self, code: str, as_of: datetime | None = None) -> tuple[int, int]:
        """(on hand, FIFO cents) at `as_of`; None means now (every verifier-era movement included, in the order made)."""
        mv = []
        for i in self.lots_of.get(code, []):
            lot = self.lots[i]
            for at, mid, kind, u in lot.legacy_moves:
                mv.append((at, (0, mid), u, lot.cost))
            if as_of is None:
                for seq, kind, u in lot.moves:
                    mv.append((self.FAR, (1, seq), u, lot.cost))
        return R.fifo_value(mv, as_of or self.FAR)

    def ledger_rows(self, code: str) -> list[tuple]:
        """Legacy ledger rows of a SKU: (lot_id, kind, units, at UTC)."""
        out = []
        for i in self.lots_of.get(code, []):
            for at, mid, kind, u in self.lots[i].legacy_moves:
                out.append((str(i), kind, str(u), R.utc_z(at)))
        return out
