"""Verifier-side model of Seatwise: what every dashboard figure, export row, limit and scan result must be.

It holds the old platform's records read from the database before the submission ran (baseline
snapshot) plus everything the verifier itself did and saw confirmed: events it created, orders it
held and paid, refunds it saw complete, transfers, admissions. It never reads the submission's tables.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # importable standalone (Reward Kit)

from reference import money as M  # noqa: E402

UTC = timezone.utc
CSV_HEADER = ["ticket_id", "order_id", "section", "row", "seat", "holder_name", "holder_email", "buyer_name",
              "buyer_email", "status", "total_cents", "refunded_cents", "purchased_at", "checked_in_at", "gate"]
DANGER = ("=", "+", "-", "@", "\t", "\r")


def norm(e: str | None) -> str:
    return (e or "").strip().lower()


def inst(d: datetime | None) -> str:
    return "" if d is None else d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_inst(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def local_to_instant(naive: datetime, fold: int, tz: str) -> datetime:
    return naive.replace(fold=fold, tzinfo=ZoneInfo(tz)).astimezone(UTC)


def wall_to_instant(text: str, tz: str) -> datetime | None:
    """Organizer-typed 'YYYY-MM-DDTHH:MM' (R5.2): ambiguous -> first occurrence; nonexistent -> None."""
    naive = datetime.strptime(text, "%Y-%m-%dT%H:%M")
    z = ZoneInfo(tz)
    at = naive.replace(fold=0, tzinfo=z).astimezone(UTC)
    back = at.astimezone(z).replace(tzinfo=None)
    return at if back == naive else None


def instant_to_wall(d: datetime, tz: str) -> str:
    return d.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")


def neutral(v: str) -> str:
    return ("'" + v) if v and v[0] in DANGER else v


def seat_parts(label: str) -> tuple[str, str, int]:
    sec, row, num = label.rsplit("-", 2)
    return sec, row, int(num)


@dataclass
class Section:
    code: str
    name: str
    seats: list[str]                  # labels in seat order
    accessible: set[str] = field(default_factory=set)
    price: int = 0


@dataclass
class Event:
    id: int
    org: int
    venue: int
    tz: str
    start: datetime
    fee: int
    sections: dict[str, Section]
    name: str = ""
    legacy: bool = False
    status: str = "published"

    def section_of(self, label: str) -> str:
        return seat_parts(label)[0]

    def capacity(self) -> int:
        return sum(len(s.seats) for s in self.sections.values())


@dataclass
class Ticket:
    id: int
    event: int
    order: int
    seat: str
    holder_email: str
    holder_name: str
    buyer_email: str
    buyer_name: str
    status: str                       # valid | refund_pending | refunded
    total: int
    refunded: int = 0                 # completed refunds
    pending: int = 0                  # requested, not yet completed
    purchased_at: datetime | None = None
    purchased_window: tuple | None = None   # (lo, hi) for tickets sold by the application
    admitted_at: datetime | None = None
    gate: str | None = None
    legacy: bool = False
    tokens: list[str] = field(default_factory=list)   # every token seen; the last one is current

    @property
    def section(self) -> str:
        return seat_parts(self.seat)[0]


@dataclass
class Order:
    id: int
    event: int
    buyer_email: str
    buyer_name: str
    seats: list[str]
    quote: M.Quote | None = None
    status: str = "pending"           # pending | expired | released | paid | unavailable
    hold_expires: datetime | None = None
    session: str | None = None
    paid_window: tuple | None = None
    late_refunded: int = 0            # completed late-payment refund (R8.4)
    late_paid: int = 0                # payment received for an unavailable order

    def line_total(self, label: str) -> int:
        for l in self.quote.lines:
            if l.key == label:
                return l.total
        raise KeyError(label)


@dataclass
class Offer:
    event: int
    seats: list[str]
    expires: datetime
    status: str = "open"


class Model:
    def __init__(self, baseline: dict):
        self.b = baseline
        self.events: dict[int, Event] = {}
        self.tickets: dict[int, Ticket] = {}
        self.orders: dict[int, Order] = {}
        self.offers: list[Offer] = []
        self.legacy_orders: dict[int, dict] = {}
        self.seat_label: dict[int, str] = {}
        self.venue_tz: dict[int, str] = {}
        self.scan_ids: dict[int, dict] = {}      # event -> scan_id -> fields
        self._req = 0
        self._load_legacy()

    # ---- legacy ---------------------------------------------------------------------------------
    def _load_legacy(self) -> None:
        b = self.b
        venues = {v["id"]: v for v in b["venues"]}
        self.venue_tz = {v["id"]: v["timezone"] for v in b["venues"]}
        secs = {s["id"]: s for s in b["sections"]}
        by_venue: dict[int, dict[str, Section]] = {}
        for s in b["sections"]:
            by_venue.setdefault(s["venue_id"], {})[s["code"]] = Section(s["code"], s["name"], [])
        for st in b["seats"]:
            sec = secs[st["section_id"]]
            label = f"{sec['code']}-{st['row_label']}-{st['seat_number']}"
            self.seat_label[st["id"]] = label
            S = by_venue[sec["venue_id"]][sec["code"]]
            S.seats.append(label)
            if st["accessible"]:
                S.accessible.add(label)
        for secmap in by_venue.values():
            for S in secmap.values():
                S.seats = M.sort_seats(S.seats)
        prices = {}
        for p in b["event_prices"]:
            prices.setdefault(p["event_id"], {})[secs[p["section_id"]]["code"]] = p["price_cents"]
        for e in b["events"]:
            tz = venues[e["venue_id"]]["timezone"]
            start = local_to_instant(datetime.fromisoformat(e["starts_local"]), e["starts_fold"], tz)
            sections = {}
            for code, S in by_venue[e["venue_id"]].items():
                sections[code] = Section(S.code, S.name, list(S.seats), set(S.accessible), prices.get(e["id"], {}).get(code, 0))
            self.events[e["id"]] = Event(e["id"], e["organization_id"], e["venue_id"], tz, start, e["facility_fee_cents"],
                                         sections, e["name"], True, e["status"])
        for o in b["legacy_orders"]:
            self.legacy_orders[o["id"]] = o
        for t in b["legacy_tickets"]:
            o = self.legacy_orders[t["order_id"]]
            ev = self.events[o["event_id"]]
            adm = None
            if t["checked_in_local"]:
                adm = local_to_instant(datetime.fromisoformat(t["checked_in_local"]), t["checked_in_fold"], ev.tz)
            self.tickets[t["id"]] = Ticket(
                t["id"], ev.id, o["id"], self.seat_label[t["seat_id"]], norm(t["holder_email"]), t["holder_name"],
                norm(o["buyer_email"]), o["buyer_name"], "refunded" if t["status"] == "refunded" else "valid",
                t["total_cents"], t["refunded_cents"],
                purchased_at=local_to_instant(datetime.fromisoformat(o["placed_local"]), o["placed_fold"], ev.tz),
                admitted_at=adm, gate=t["checked_in_gate"], legacy=True, tokens=[t["barcode"]])

    def legacy_events(self, org: int) -> list[Event]:
        return [e for e in sorted(self.events.values(), key=lambda e: e.id) if e.legacy and e.org == org]

    # ---- verifier activity ----------------------------------------------------------------------
    def add_event(self, ev: Event) -> None:
        self.events[ev.id] = ev

    def quote_for(self, event_id: int, seats: list[str], codes: list[M.Code] = ()) -> M.Quote:
        ev = self.events[event_id]
        ordered = M.sort_seats(seats)
        return M.quote([(s, ev.sections[seat_parts(s)[0]].price) for s in ordered], ev.fee, list(codes))

    def add_order(self, o: Order) -> None:
        self.orders[o.id] = o

    def issue(self, order_id: int, ticket_ids: dict[str, int], window: tuple) -> None:
        o = self.orders[order_id]
        o.status = "paid"
        o.paid_window = window
        for label in M.sort_seats(o.seats):
            tid = ticket_ids[label]
            self.tickets[tid] = Ticket(tid, o.event, o.id, label, o.buyer_email, o.buyer_name, o.buyer_email,
                                       o.buyer_name, "valid", o.line_total(label), purchased_window=window)

    def tickets_of(self, event_id: int) -> list[Ticket]:
        return [t for t in self.tickets.values() if t.event == event_id]

    def refund_requested(self, tid: int, amount: int) -> None:
        t = self.tickets[tid]
        t.status = "refund_pending"
        t.pending += amount

    def refund_completed(self, tid: int, amount: int) -> None:
        t = self.tickets[tid]
        t.status = "refunded"
        t.pending = max(0, t.pending - amount)
        t.refunded += amount

    def transfer(self, tid: int, email: str, name: str, new_token: str | None) -> None:
        t = self.tickets[tid]
        t.holder_email, t.holder_name = norm(email), name
        if new_token:
            t.tokens.append(new_token)

    # ---- per-buyer limit (R6.3) ---------------------------------------------------------------------
    def buyer_count(self, event_id: int, email: str, at: datetime) -> int:
        e = norm(email)
        n = sum(1 for t in self.tickets.values() if t.event == event_id and t.buyer_email == e and t.status != "refunded")
        for o in self.orders.values():
            if o.event == event_id and o.buyer_email == e and o.status == "pending" and o.hold_expires and o.hold_expires > at:
                n += len(o.seats)
        return n

    # ---- dashboard (R15) -------------------------------------------------------------------------
    def dashboard(self, event_id: int, at: datetime) -> dict:
        ev = self.events[event_id]
        keys = ["capacity", "sold", "held", "available", "admitted", "gross", "refunded", "net"]
        per = {code: dict.fromkeys(keys, 0) for code in ev.sections}
        for code, S in ev.sections.items():
            per[code]["capacity"] = len(S.seats)
        for t in self.tickets_of(event_id):
            f = per[t.section]
            if t.status in ("valid", "refund_pending"):
                f["sold"] += 1
            if t.admitted_at is not None:
                f["admitted"] += 1
            f["gross"] += t.total
            f["refunded"] += t.refunded
        for o in self.orders.values():
            if o.event != event_id:
                continue
            if o.status == "pending" and o.hold_expires and o.hold_expires > at:
                for s in o.seats:
                    per[seat_parts(s)[0]]["held"] += 1
            if o.status == "unavailable":
                for l in o.quote.lines:
                    per[seat_parts(l.key)[0]]["gross"] += l.total if o.late_paid else 0
                    per[seat_parts(l.key)[0]]["refunded"] += l.total if o.late_refunded else 0
        for off in self.offers:
            if off.event == event_id and off.status == "open" and off.expires > at:
                for s in off.seats:
                    per[seat_parts(s)[0]]["held"] += 1
        for f in per.values():
            f["available"] = f["capacity"] - f["sold"] - f["held"]
            f["net"] = f["gross"] - f["refunded"]
        total = {k: sum(f[k] for f in per.values()) for k in keys}
        return {"total": total, "sections": per}

    # ---- export (R16) ------------------------------------------------------------------------------
    def export_rows(self, event_id: int) -> list[dict]:
        rows = []
        for t in self.tickets_of(event_id):
            sec, row, num = seat_parts(t.seat)
            rows.append({
                "ticket_id": str(t.id), "order_id": str(t.order), "section": sec, "row": row, "seat": str(num),
                "holder_name": neutral(t.holder_name), "holder_email": neutral(norm(t.holder_email)),
                "buyer_name": neutral(t.buyer_name), "buyer_email": neutral(norm(t.buyer_email)),
                "status": t.status, "total_cents": str(t.total), "refunded_cents": str(t.refunded),
                "purchased_at": inst(t.purchased_at) if t.purchased_at else None,
                "checked_in_at": inst(t.admitted_at), "gate": neutral(t.gate or ""),
                "_window": t.purchased_window, "_key": (sec, row, num, t.id),
            })
        rows.sort(key=lambda r: r["_key"])
        return rows

    # ---- scans (R13.3, R13.4) ------------------------------------------------------------------------
    def token_lookup(self, token: str) -> tuple[int | None, bool]:
        """(ticket id, is current token) for a token the verifier has seen issued; (None, False) otherwise."""
        for t in self.tickets.values():
            if token in t.tokens:
                return t.id, token == t.tokens[-1]
        return None, False

    def scan(self, device_event: int, rows: list[dict], online_at: datetime | None = None) -> list[dict]:
        """Expected results for one request. rows: {scan_id, token, gate, scanned_at (datetime)}; online when online_at."""
        ev = self.events[device_event]
        seen = self.scan_ids.setdefault(device_event, {})
        results: list[dict] = []
        accepted: list[tuple[int, int]] = []   # (row index, ticket id)
        self._req += 1
        req = self._req
        for i, r in enumerate(rows):
            at = online_at if online_at is not None else r["scanned_at"]
            sid = r.get("scan_id")
            fields = (r["token"], r["gate"], inst(at))
            if sid is not None:
                if sid in seen:
                    if seen[sid] == fields:
                        results.append({"result": "duplicate"})
                    else:
                        results.append({"result": "refused", "reason": "scan_id_reused"})
                    continue
                seen[sid] = fields
            tid, current = self.token_lookup(r["token"])
            reason = None
            t = self.tickets.get(tid) if tid else None
            if t is None:
                reason = "invalid"
            elif t.event != device_event:
                reason = "wrong_event"
            elif ev.status == "cancelled":
                reason = "cancelled"
            elif t.status in ("refund_pending", "refunded"):
                reason = "refunded"
            elif not current:
                reason = "transferred"
            elif not (ev.start - timedelta(hours=3) <= at < ev.start + timedelta(hours=6)):
                reason = "outside_window"
            if reason:
                results.append({"result": "refused", "reason": reason, "ticket_id": None if reason == "invalid" else tid})
                continue
            if t.admitted_at is None or at < t.admitted_at:
                t.admitted_at, t.gate = at, r["gate"]
                t._adm_row = (req, i)
            accepted.append((i, tid))
            results.append({"result": "accepted", "ticket_id": tid})
        for i, tid in accepted:
            t = self.tickets[tid]
            mine = getattr(t, "_adm_row", None) == (req, i)
            results[i] = {"result": "admitted" if mine else "already_admitted", "ticket_id": tid,
                          "admitted_at": inst(t.admitted_at), "gate": t.gate}
        return results
