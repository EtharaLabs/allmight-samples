#!/usr/bin/env python3
# FORGE-CANARY-BEGIN
# FORGE-CANARY-SLOT-1: 8e49de0fadec9ce1a152a788f3b15d1d4116c89baccb6ece97820c3a61139e57
# FORGE-CANARY-SLOT-2: c9e1cbabd555209e15383bf616c9a8caa1f2ab1ca16b0f0f1d8811b07d917909
# FORGE-CANARY-SLOT-3: 435bcab1d6aab734bf215774f2dfb9eea9072e666ae80aa4d066fa06e7edfb48
# FORGE-CANARY-SLOT-4: a27bdf669e9ce53c83a949838ce9cafec5c988090fb292d50dc0f93183024abd
# FORGE-CANARY-END
"""Derive every generated bundle artifact from solution/grounding.yaml alone.

FORGE Phase build item 10 requires one private source from which the golden
trajectory, the checker fixtures, the executable oracle, the generated ground
truth and the compiled criteria are all derived, so none can disagree with
another or with the fixtures. This script is that derivation. It reads
``solution/grounding.yaml`` and writes:

- ``environment/inputs/statement.csv``, ``receipts/*.txt``, ``policy.json``
- ``solution/solve.sh``       the executable oracle
- ``solution/TRUTH.md``       the human-readable golden trajectory
- ``tests/test_output.py``    the compiled deterministic criteria
- ``tests/fixtures.json``     the checker fixtures

It invokes no model, no network, no clock, no locale and no random source, so
two runs under different host identities produce byte-identical output.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from itertools import combinations
from pathlib import Path

import yaml

BANNER = "GENERATED SECTION. DO NOT HAND-EDIT."
CATEGORIES = ("travel", "meals", "software", "hardware", "other")
CANARY_SLOTS = 4
CANARY_PLACEHOLDER = "0" * 64


def canary_block(marker: str) -> list[str]:
    """The item 11a block in the bound token_format, at placeholder values.

    Item 2 orders the emission: placeholders first, then the hash over them,
    then the derived tokens. Hashing therefore precedes planting and the
    derivation stays acyclic, because the item 2 normalization rewrites exactly
    this slot form before hashing.
    """
    stem = "FORGE-CANARY"
    lines = [f"{marker} {stem}-BEGIN"]
    lines += [
        f"{marker} {stem}-SLOT-{slot}: {CANARY_PLACEHOLDER}"
        for slot in range(1, CANARY_SLOTS + 1)
    ]
    lines.append(f"{marker} {stem}-END")
    return lines


def load(bundle: Path) -> dict:
    with (bundle / "solution" / "grounding.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def cents(total: str) -> int:
    whole, _, fraction = total.partition(".")
    return int(whole) * 100 + int(fraction)


def eligible(ground: dict) -> dict[tuple[str, str], tuple[int, int]]:
    """Every (receipt_id, txn_id) pair the policy admits, with its two deltas."""
    policy = ground["policy"]
    window, tolerance = policy["match_window_days"], policy["amount_tolerance_cents"]
    out: dict[tuple[str, str], tuple[int, int]] = {}
    for receipt in ground["receipts"]:
        total = cents(receipt["total"])
        rdate = date.fromisoformat(receipt["date"])
        for row in ground["statement"]:
            magnitude = abs(row["amount_cents"])
            amount_delta = total - magnitude
            date_delta = (rdate - date.fromisoformat(row["posted_date"])).days
            if abs(amount_delta) <= tolerance and abs(date_delta) <= window:
                out[(receipt["receipt_id"], row["txn_id"])] = (amount_delta, date_delta)
    return out


def matching(edges: dict[tuple[str, str], tuple[int, int]]) -> list[tuple[str, str]]:
    """A maximum-cardinality matching, tie-broken as instruction.md states.

    The corpus is small enough to decide maximum cardinality by exhaustive
    search over edge subsets, which removes any doubt that the result is
    maximum rather than merely greedy. Ties break on summed absolute amount
    delta, then summed absolute date delta, then the sorted receipt-id list,
    so exactly one matching is selected.
    """
    keys = sorted(edges)
    for size in range(min(len(keys), 64), 0, -1):
        feasible = [
            combo
            for combo in combinations(keys, size)
            if len({pair[0] for pair in combo}) == size
            and len({pair[1] for pair in combo}) == size
        ]
        if not feasible:
            continue
        return sorted(
            min(
                feasible,
                key=lambda combo: (
                    sum(abs(edges[pair][0]) for pair in combo),
                    sum(abs(edges[pair][1]) for pair in combo),
                    sorted(pair[0] for pair in combo),
                ),
            )
        )
    return []


def answer(ground: dict) -> dict:
    """The one graded outcome: the reconciliation every deliverable must agree with."""
    edges = eligible(ground)
    pairs = matching(edges)
    limits = ground["policy"]["category_limits_cents"]
    by_id = {item["receipt_id"]: item for item in ground["receipts"]}

    matched = []
    for receipt_id, txn_id in sorted(pairs):
        receipt = by_id[receipt_id]
        amount_delta, date_delta = edges[(receipt_id, txn_id)]
        total = cents(receipt["total"])
        category = receipt["category"]
        matched.append(
            {
                "receipt_id": receipt_id,
                "txn_id": txn_id,
                "amount_delta_cents": amount_delta,
                "date_delta_days": date_delta,
                "category": category,
                "violates_policy": category in limits and total > limits[category],
            }
        )

    posted = {pair[1] for pair in pairs}
    receipted = {pair[0] for pair in pairs}
    unreceipted = sorted(row["txn_id"] for row in ground["statement"] if row["txn_id"] not in posted)
    unposted = sorted(r["receipt_id"] for r in ground["receipts"] if r["receipt_id"] not in receipted)

    return {
        "matched": matched,
        "unreceipted": unreceipted,
        "unposted": unposted,
        "totals": {
            "matched_count": len(matched),
            "unreceipted_count": len(unreceipted),
            "unposted_count": len(unposted),
            "violation_count": sum(1 for item in matched if item["violates_policy"]),
        },
    }


def write_inputs(bundle: Path, ground: dict) -> list[Path]:
    root = bundle / "environment" / "inputs"
    (root / "receipts").mkdir(parents=True, exist_ok=True)
    written = []

    lines = ["txn_id,posted_date,description,amount_cents,currency"]
    lines += [
        f"{row['txn_id']},{row['posted_date']},{row['description']},"
        f"{row['amount_cents']},{row['currency']}"
        for row in ground["statement"]
    ]
    statement = root / "statement.csv"
    statement.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written.append(statement)

    policy = root / "policy.json"
    policy.write_text(json.dumps(ground["policy"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written.append(policy)

    for receipt in ground["receipts"]:
        body = [
            f"RECEIPT-ID: {receipt['receipt_id']}",
            f"DATE: {receipt['date']}",
            f"VENDOR: {receipt['vendor']}",
            f"TOTAL: {receipt['total']}",
            f"CATEGORY: {receipt['category']}",
        ]
        body += list(receipt.get("noise") or [])
        path = root / "receipts" / f"{receipt['receipt_id'].lower()}.txt"
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        written.append(path)

    return written


ORACLE_BODY = '''\
"""The reference solution, solving the task from the shipped inputs alone.

It embeds no expected value: it reads policy.json, the statement and every
receipt, builds the eligibility graph, chooses a maximum-cardinality matching
under the stated tie-break, and writes the four deliverables. So a passing
oracle proves the task is solvable from the environment rather than proving a
constant was printed.
"""

from __future__ import annotations

import csv
import inspect
import json
import os
import subprocess
from datetime import date
from itertools import combinations
from pathlib import Path

import uvicorn

APP = Path("/app")
INPUTS = APP / "inputs"
OUT = APP / "out"
DSN = "postgresql://ledgerline:ledgerline@localhost:5432/ledgerline"

DDL = """
drop table if exists reconciliation;
create table reconciliation (
  receipt_id         text    not null,
  txn_id             text    not null,
  amount_delta_cents integer not null,
  date_delta_days    integer not null,
  category           text    not null,
  violates_policy    boolean not null
);
"""


def cents(total: str) -> int:
    whole, _, fraction = total.strip().partition(".")
    return int(whole) * 100 + int(fraction)


def read_statement() -> list[dict]:
    with (INPUTS / "statement.csv").open(encoding="utf-8", newline="") as handle:
        return [
            {
                "txn_id": row["txn_id"],
                "posted_date": date.fromisoformat(row["posted_date"]),
                "amount_cents": int(row["amount_cents"]),
            }
            for row in csv.DictReader(handle)
        ]


def read_receipts() -> list[dict]:
    """Each field line splits on its FIRST colon only, and any line that is not
    a known field is ignored, because a vendor name may contain a colon and a
    receipt may carry trailing prose."""
    fields = {"RECEIPT-ID", "DATE", "VENDOR", "TOTAL", "CATEGORY"}
    out = []
    for path in sorted(INPUTS.glob("receipts/*.txt")):
        found = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition(":")
            if sep and name.strip() in fields:
                found[name.strip()] = value.strip()
        out.append(
            {
                "receipt_id": found["RECEIPT-ID"],
                "date": date.fromisoformat(found["DATE"]),
                "total_cents": cents(found["TOTAL"]),
                "category": found["CATEGORY"],
            }
        )
    return out


def answer_from_inputs() -> dict:
    """Recompute the reconciliation from the shipped inputs alone.

    Both the deliverable writer and the served application call this, so the
    screens and the JSON are two renderings of one computation rather than one
    copied from the other.
    """
    policy = json.loads((INPUTS / "policy.json").read_text(encoding="utf-8"))
    window = policy["match_window_days"]
    tolerance = policy["amount_tolerance_cents"]
    limits = policy["category_limits_cents"]

    rows = read_statement()
    receipts = read_receipts()

    edges: dict[tuple[str, str], tuple[int, int]] = {}
    for receipt in receipts:
        for row in rows:
            amount_delta = receipt["total_cents"] - abs(row["amount_cents"])
            date_delta = (receipt["date"] - row["posted_date"]).days
            if abs(amount_delta) <= tolerance and abs(date_delta) <= window:
                edges[(receipt["receipt_id"], row["txn_id"])] = (amount_delta, date_delta)

    keys = sorted(edges)
    pairs: list[tuple[str, str]] = []
    for size in range(len(keys), 0, -1):
        feasible = [
            combo
            for combo in combinations(keys, size)
            if len({p[0] for p in combo}) == size and len({p[1] for p in combo}) == size
        ]
        if feasible:
            pairs = sorted(
                min(
                    feasible,
                    key=lambda combo: (
                        sum(abs(edges[p][0]) for p in combo),
                        sum(abs(edges[p][1]) for p in combo),
                        sorted(p[0] for p in combo),
                    ),
                )
            )
            break

    by_id = {item["receipt_id"]: item for item in receipts}
    matched = []
    for receipt_id, txn_id in pairs:
        receipt = by_id[receipt_id]
        amount_delta, date_delta = edges[(receipt_id, txn_id)]
        category = receipt["category"]
        matched.append(
            {
                "receipt_id": receipt_id,
                "txn_id": txn_id,
                "amount_delta_cents": amount_delta,
                "date_delta_days": date_delta,
                "category": category,
                "violates_policy": category in limits
                and receipt["total_cents"] > limits[category],
            }
        )

    posted = {p[1] for p in pairs}
    receipted = {p[0] for p in pairs}
    unreceipted = sorted(r["txn_id"] for r in rows if r["txn_id"] not in posted)
    unposted = sorted(r["receipt_id"] for r in receipts if r["receipt_id"] not in receipted)
    violations = sum(1 for item in matched if item["violates_policy"])

    return {
        "matched": matched,
        "unreceipted": unreceipted,
        "unposted": unposted,
        "totals": {
            "matched_count": len(matched),
            "unreceipted_count": len(unreceipted),
            "unposted_count": len(unposted),
            "violation_count": violations,
        },
        "edges": {f"{r}|{t}": list(d) for (r, t), d in edges.items()},
    }


def contested_account(result: dict) -> str:
    """The written account of the three rows a reviewer is most likely to dispute.

    Derived from the same recomputation the JSON reports, so the prose cannot
    disagree with the matching it describes. Each sentence states a relation
    this run computed rather than a rule the statement states in general.
    """
    edges = {
        (key.split("|")[0], key.split("|")[1]): tuple(delta)
        for key, delta in result["edges"].items()
    }
    partner = {item["txn_id"]: item for item in result["matched"]}
    eligible: dict[str, list[str]] = {}
    for receipt_id, txn_id in edges:
        eligible.setdefault(txn_id, []).append(receipt_id)
    for txn_id in eligible:
        eligible[txn_id].sort()

    contended = sorted(t for t, rs in eligible.items() if len(rs) > 1)
    unmatched = list(result["unreceipted"])

    # A row is pulled off an edge when its partner had a nearer row it did not
    # take, which is what maximum cardinality costs and the trap this corpus
    # sets: a solver matching greedily by closeness never pays it.
    pulled = []
    for txn_id, item in sorted(partner.items()):
        if txn_id in contended:
            continue
        receipt_id = item["receipt_id"]
        mine = abs(item["amount_delta_cents"])
        nearer = [
            t
            for (r, t) in edges
            if r == receipt_id and t != txn_id and abs(edges[(r, t)][0]) < mine
        ]
        if nearer:
            pulled.append((txn_id, receipt_id, sorted(nearer)))

    chosen = contended[:1] + [p[0] for p in pulled][:1] + unmatched[:1]

    lines: list[str] = []
    for txn_id in chosen:
        lines += [f"## {txn_id}", ""]
        if txn_id in contended:
            item = partner[txn_id]
            took = item["receipt_id"]
            others = [r for r in eligible[txn_id] if r != took]
            rival = others[0]
            rival_amount, rival_date = edges[(rival, txn_id)]
            rival_rows = sorted(t for (r, t) in edges if r == rival and t != txn_id)
            lines += [
                f"This row was matched to {took}, at an amount delta of "
                f"{item['amount_delta_cents']} cents and a date delta of "
                f"{item['date_delta_days']} days. It was also eligible for "
                f"{rival}, whose edge to this row sits closer at "
                f"{rival_amount} cents and {rival_date} days.",
                "",
                f"The rules did not take that closer edge. The matching "
                f"maximises how many pairs exist before it minimises any "
                f"delta, and {rival} is the only receipt eligible for "
                f"{', '.join(rival_rows)}, so assigning {rival} to this row "
                f"would have stranded {', '.join(rival_rows)} and produced "
                f"{result['totals']['matched_count'] - 1} pairs instead of "
                f"{result['totals']['matched_count']}.",
                "",
            ]
        elif txn_id in dict((p[0], p) for p in pulled):
            _, receipt_id, nearer = dict((p[0], p) for p in pulled)[txn_id]
            item = partner[txn_id]
            lines += [
                f"This row was matched to {receipt_id}, at an amount delta of "
                f"{item['amount_delta_cents']} cents and a date delta of "
                f"{item['date_delta_days']} days. {receipt_id} is the only "
                f"receipt eligible for this row, so this row had no "
                f"alternative of its own.",
                "",
                f"What a reviewer would dispute is the other side of the pair. "
                f"{receipt_id} was pulled off {', '.join(nearer)}, a row it "
                f"fits more closely, to reach this one, because leaving this "
                f"row unmatched would have cost a pair. That is why this pair "
                f"carries a wider amount delta than the edge {receipt_id} gave "
                f"up.",
                "",
            ]
        else:
            lines += [
                "This row was matched to no receipt. No receipt is eligible "
                "for it at all: every receipt falls outside the amount "
                "tolerance against its amount, or outside the date window "
                "against its posted date, or both.",
                "",
                "It is therefore unreceipted rather than contested between "
                "candidates, and it appears in the unreceipted array of "
                "reconciliation.json rather than in matched. A reviewer who "
                "expected a near miss here would find the graph has no edge "
                "to argue over.",
                "",
            ]
    return "\\n".join(lines).rstrip("\\n") + "\\n"


def policy_notes(result: dict) -> str:
    policy = json.loads((INPUTS / "policy.json").read_text(encoding="utf-8"))
    limits = policy["category_limits_cents"]
    receipts = {r["receipt_id"]: r for r in read_receipts()}

    violating = sorted(
        (i for i in result["matched"] if i["violates_policy"]),
        key=lambda i: i["receipt_id"],
    )
    clean = sorted(
        (i for i in result["matched"] if not i["violates_policy"]),
        key=lambda i: i["receipt_id"],
    )

    lines: list[str] = []
    for item in violating + clean[:1]:
        receipt_id = item["receipt_id"]
        total = receipts[receipt_id]["total_cents"]
        category = item["category"]
        cap = limits.get(category)
        lines += [f"## {receipt_id}", ""]
        if cap is None:
            lines += [
                f"This receipt is categorised {category} and its total is "
                f"{total} cents. The category_limits_cents table of "
                f"policy.json sets no limit for {category} at all, so no "
                f"amount in this category can exceed one.",
                "",
                f"The pair therefore does not violate policy, and it is the "
                f"absence of a limit rather than a comparison against one that "
                f"settles it.",
                "",
            ]
        elif total > cap:
            lines += [
                f"This receipt is categorised {category} and its total is "
                f"{total} cents. The category_limits_cents table of "
                f"policy.json sets the {category} limit at {cap} cents.",
                "",
                f"{total} strictly exceeds {cap}, by {total - cap} cents, so "
                f"the pair violates policy under the rule that a pair violates "
                f"when its category carries a limit and its total strictly "
                f"exceeds that limit.",
                "",
            ]
        else:
            lines += [
                f"This receipt is categorised {category} and its total is "
                f"{total} cents. The category_limits_cents table of "
                f"policy.json sets the {category} limit at {cap} cents.",
                "",
                f"{total} does not exceed {cap}, falling short of it by "
                f"{cap - total} cents, so the pair does not violate policy. "
                f"The limit applies to this category and the total stays "
                f"within it.",
                "",
            ]
    return "\\n".join(lines).rstrip("\\n") + "\\n"


COLON_RULE = (
    "a VENDOR value may itself contain a colon, and only the first colon on a "
    "line separates the field name from its value"
)
NONFIELD_RULE = (
    "a receipt may carry additional lines that are not field lines, and they "
    "are ignored"
)


def parse_notes() -> str:
    fields = {"RECEIPT-ID", "DATE", "VENDOR", "TOTAL", "CATEGORY"}
    hazards = []
    for path in sorted(INPUTS.glob("receipts/*.txt")):
        found, extra, vendor_colon = {}, [], False
        for line in path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition(":")
            if sep and name.strip() in fields:
                found[name.strip()] = value.strip()
                if name.strip() == "VENDOR" and ":" in value:
                    vendor_colon = True
            elif line.strip():
                extra.append(line.strip())
        if vendor_colon or extra:
            hazards.append((found["RECEIPT-ID"], found["VENDOR"], vendor_colon, extra))

    lines: list[str] = []
    for receipt_id, vendor, vendor_colon, extra in sorted(hazards):
        lines += [f"## {receipt_id}", ""]
        if vendor_colon:
            truncated = vendor.split(":")[0].strip()
            dropped = ":".join(vendor.split(":")[1:]).strip()
            lines += [
                f"This receipt exercises the rule that {COLON_RULE}. Its VENDOR "
                f"line reads {vendor!r}, which carries a colon inside the value "
                f"rather than only between the field name and the value.",
                "",
                f"A reader who split that line on every colon would have taken "
                f"the vendor to be {truncated!r} and dropped {dropped!r} "
                f"entirely, because the second colon would have been read as "
                f"another separator. The correct reading splits on the first "
                f"colon only, so the vendor is {vendor!r}.",
                "",
            ]
        else:
            quoted = ", ".join(repr(line) for line in extra)
            plural = "lines" if len(extra) != 1 else "line"
            lines += [
                f"This receipt exercises the rule that {NONFIELD_RULE}. Besides "
                f"its five field lines it carries {len(extra)} further "
                f"{plural}: {quoted}.",
                "",
                f"A reader who treated every line as a field line would have "
                f"taken the text before each line's first colon as a field name "
                f"and admitted spurious fields from those {plural}. The correct "
                f"reading keeps only the five known field names, so the vendor "
                f"is {vendor!r} and no field comes from the trailing text.",
                "",
            ]
    return "\\n".join(lines).rstrip("\\n") + "\\n"


def convention_notes() -> str:
    policy = json.loads((INPUTS / "policy.json").read_text(encoding="utf-8"))
    window = policy["match_window_days"]
    tolerance = policy["amount_tolerance_cents"]
    rows = read_statement()
    receipts = read_receipts()

    def graph(signed: bool) -> dict[tuple[str, str], tuple[int, int]]:
        out: dict[tuple[str, str], tuple[int, int]] = {}
        for receipt in receipts:
            for row in rows:
                target = row["amount_cents"] if signed else abs(row["amount_cents"])
                amount_delta = receipt["total_cents"] - target
                date_delta = (receipt["date"] - row["posted_date"]).days
                if abs(amount_delta) <= tolerance and abs(date_delta) <= window:
                    out[(receipt["receipt_id"], row["txn_id"])] = (amount_delta, date_delta)
        return out

    def maximum(edges: dict[tuple[str, str], tuple[int, int]]) -> list[tuple[str, str]]:
        keys = sorted(edges)
        for size in range(len(keys), 0, -1):
            feasible = [
                combo
                for combo in combinations(keys, size)
                if len({p[0] for p in combo}) == size and len({p[1] for p in combo}) == size
            ]
            if feasible:
                return sorted(
                    min(
                        feasible,
                        key=lambda combo: (
                            sum(abs(edges[p][0]) for p in combo),
                            sum(abs(edges[p][1]) for p in combo),
                            sorted(p[0] for p in combo),
                        ),
                    )
                )
        return []

    absolute, signed = graph(False), graph(True)
    m_absolute, m_signed = maximum(absolute), maximum(signed)

    by_txn_absolute: dict[str, set[str]] = {}
    by_txn_signed: dict[str, set[str]] = {}
    for receipt_id, txn_id in absolute:
        by_txn_absolute.setdefault(txn_id, set()).add(receipt_id)
    for receipt_id, txn_id in signed:
        by_txn_signed.setdefault(txn_id, set()).add(receipt_id)

    diverging, coinciding = [], []
    for row in rows:
        txn_id = row["txn_id"]
        pair = (by_txn_absolute.get(txn_id, set()), by_txn_signed.get(txn_id, set()))
        (diverging if pair[0] != pair[1] else coinciding).append(txn_id)
    diverging.sort()
    coinciding.sort()

    survivors = ", ".join(f"{r} to {t}" for r, t in m_signed) or "none"
    by_id = {r["receipt_id"]: r for r in receipts}
    by_row = {r["txn_id"]: r for r in rows}

    def arithmetic(txn_id: str) -> str:
        row = by_row[txn_id]
        candidates = sorted(by_txn_absolute.get(txn_id, set()) | by_txn_signed.get(txn_id, set()))
        if not candidates:
            return (
                f"{txn_id} carries an amount_cents of {row['amount_cents']} and no "
                f"receipt is within {tolerance} cents of it under either rule, so "
                f"both rules leave it with an empty eligible set and it coincides "
                f"for want of any candidate at all."
            )
        receipt_id = candidates[0]
        total = by_id[receipt_id]["total_cents"]
        amount = row["amount_cents"]
        return (
            f"{txn_id} carries an amount_cents of {amount} and {receipt_id} a total "
            f"of {total} cents. The absolute-value rule computes "
            f"{total} - {abs(amount)} = {total - abs(amount)}, whose magnitude is "
            f"{'within' if abs(total - abs(amount)) <= tolerance else 'outside'} the "
            f"tolerance of {tolerance}. The signed comparison computes "
            f"{total} - ({amount}) = {total - amount}, whose magnitude is "
            f"{'within' if abs(total - amount) <= tolerance else 'outside'} it."
        )

    lines = [
        "## collapse",
        "",
        f"Over these inputs the absolute-value rule admits {len(absolute)} eligible "
        f"pairs and the signed comparison admits {len(signed)}.",
        "",
        f"The maximum matching contains {len(m_absolute)} pairs under the "
        f"absolute-value rule and {len(m_signed)} under the signed comparison, so "
        f"dropping the absolute value would cost "
        f"{len(m_absolute) - len(m_signed)} matched pairs.",
        "",
        f"The pairs that survive the signed comparison are: {survivors}. Every "
        f"other pair the absolute-value rule admits is rejected, because a debit "
        f"row carries a negative amount_cents while a receipt total is unsigned, "
        f"so the signed difference is the sum of the two magnitudes rather than "
        f"their difference and exceeds the tolerance.",
        "",
        "## divergence",
        "",
        f"The rows whose eligible set differs between the two rules are: "
        f"{', '.join(diverging) or 'none'}.",
        "",
        f"The rows whose eligible set is the same under both rules are: "
        f"{', '.join(coinciding) or 'none'}.",
        "",
        arithmetic(diverging[0]) if diverging else "No row diverges.",
        "",
        arithmetic(coinciding[0]) if coinciding else "No row coincides.",
        "",
    ]
    return "\\n".join(lines).rstrip("\\n") + "\\n"


def main() -> int:
    result = answer_from_inputs()
    matched = result["matched"]
    unreceipted = result["unreceipted"]
    unposted = result["unposted"]
    violations = result["totals"]["violation_count"]

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "reconciliation.json").write_text(
        json.dumps(
            {key: result[key] for key in ("matched", "unreceipted", "unposted", "totals")},
            indent=2,
        )
        + "\\n",
        encoding="utf-8",
    )
    (OUT / "summary.txt").write_text(
        f"matched: {len(matched)}\\n"
        f"unreceipted: {len(unreceipted)}\\n"
        f"unposted: {len(unposted)}\\n"
        f"violations: {violations}\\n",
        encoding="utf-8",
    )
    (OUT / "contested.md").write_text(contested_account(result), encoding="utf-8")
    (OUT / "policy-notes.md").write_text(policy_notes(result), encoding="utf-8")
    (OUT / "parse-notes.md").write_text(parse_notes(), encoding="utf-8")
    (OUT / "convention-notes.md").write_text(convention_notes(), encoding="utf-8")

    copy = "".join(
        "\\t".join(
            (
                item["receipt_id"],
                item["txn_id"],
                str(item["amount_delta_cents"]),
                str(item["date_delta_days"]),
                item["category"],
                "t" if item["violates_policy"] else "f",
            )
        )
        + "\\n"
        for item in matched
    )
    script = DDL + "copy reconciliation from stdin;\\n" + copy + "\\\\.\\n"
    subprocess.run(
        ["psql", DSN, "--no-psqlrc", "--quiet", "--set", "ON_ERROR_STOP=1", "--file", "-"],
        input=script,
        text=True,
        check=True,
        env={**os.environ, "PGCLIENTENCODING": "UTF8"},
    )

    write_app()
    return 0


def esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# The stylesheet is served by the application itself at a relative path,
# because the screens-are-self-contained red line refuses a request to any
# other origin. table-layout fixed is what makes every cell of a column share
# a left edge: an auto layout sizes columns per row and lays out ragged, which
# the one-pixel alignment obligation measures.
STYLE = """\\
body { font-family: "Liberation Sans", sans-serif; font-size: 16px; margin: 24px; }
table { table-layout: fixed; border-collapse: collapse; width: 1040px; }
th, td { width: 173px; padding: 4px 8px; text-align: left; overflow: hidden; }
.state { font-weight: 400; }
.violation .state { font-weight: 700; color: #8a1c1c; }
.totals li { font-weight: 400; }
"""


def render_site(
    matched: list[dict], unreceipted: list[str], unposted: list[str], violations: int
) -> dict[str, tuple[str, str]]:
    """Every document the application serves, keyed by its request path.

    Each value is its media type and its body. The server returns these bytes
    and serve_forever returns these same bytes, so the screens the grader
    dials and the reconciliation this computes cannot disagree.
    """
    rows = []
    for item in matched:
        state = "violation" if item["violates_policy"] else "compliant"
        klass = ' class="violation"' if item["violates_policy"] else ""
        rows.append(
            f'      <tr{klass} data-receipt-id="{esc(item["receipt_id"])}">'
            f'<td><a href="/pair/{esc(item["receipt_id"])}">{esc(item["receipt_id"])}</a></td>'
            f'<td>{esc(item["txn_id"])}</td>'
            f'<td>{item["amount_delta_cents"]}</td>'
            f'<td>{item["date_delta_days"]}</td>'
            f'<td>{esc(item["category"])}</td>'
            f'<td class="state">{state}</td></tr>'
        )

    review = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        "<title>Ledgerline reconciliation review</title>",
        '<link rel="stylesheet" href="/app.css"></head><body>',
        "  <h1>Reconciliation review</h1>",
        '  <table id="matched-table"><thead><tr>',
        "    <th>receipt</th><th>txn</th><th>amount delta</th>",
        "    <th>date delta</th><th>category</th><th>policy</th>",
        "  </tr></thead><tbody>",
        *rows,
        "  </tbody></table>",
        '  <ul id="unreceipted-list">',
        *[f"    <li>{esc(txn)}</li>" for txn in unreceipted],
        "  </ul>",
        '  <ul id="unposted-list">',
        *[f"    <li>{esc(rid)}</li>" for rid in unposted],
        "  </ul>",
        '  <ul class="totals">',
        f'    <li id="totals-matched">{len(matched)}</li>',
        f'    <li id="totals-unreceipted">{len(unreceipted)}</li>',
        f'    <li id="totals-unposted">{len(unposted)}</li>',
        f'    <li id="totals-violations">{violations}</li>',
        "  </ul>",
        "</body></html>",
        "",
    ]
    documents = {
        "/": ("text/html; charset=utf-8", "\\n".join(review)),
        "/app.css": ("text/css; charset=utf-8", STYLE),
    }

    for item in matched:
        state = "violation" if item["violates_policy"] else "compliant"
        klass = ' class="violation"' if item["violates_policy"] else ""
        detail = [
            "<!DOCTYPE html>",
            '<html lang="en"><head><meta charset="utf-8">',
            f'<title>{esc(item["receipt_id"])}</title>',
            '<link rel="stylesheet" href="/app.css"></head>',
            f"<body{klass}>",
            f'  <p id="pair-receipt-id">{esc(item["receipt_id"])}</p>',
            f'  <p id="pair-txn-id">{esc(item["txn_id"])}</p>',
            f'  <p id="pair-amount-delta">{item["amount_delta_cents"]}</p>',
            f'  <p id="pair-date-delta">{item["date_delta_days"]}</p>',
            f'  <p id="pair-category">{esc(item["category"])}</p>',
            f'  <p id="pair-policy-state" class="state">{state}</p>',
            '  <p><a href="/">back</a></p>',
            "</body></html>",
            "",
        ]
        documents[f"/pair/{item['receipt_id']}"] = (
            "text/html; charset=utf-8",
            "\\n".join(detail),
        )
    return documents


def serve_forever() -> None:
    """Serve the screens on 0.0.0.0:8080 until killed.

    The documents are rendered from a fresh answer_from_inputs call rather than
    read back from any deliverable, so the screens and the JSON are two
    renderings of one computation. This is what start.sh execs, and what the
    grader drives a browser against.
    """
    result = answer_from_inputs()
    documents = render_site(
        result["matched"],
        result["unreceipted"],
        result["unposted"],
        result["totals"]["violation_count"],
    )

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        path = scope["path"]
        found = documents.get(path) or documents.get(path.rstrip("/") or "/")
        status = 200 if found is not None else 404
        media, body = found if found is not None else ("text/plain; charset=utf-8", "not found")
        payload = body.encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", media.encode("ascii")),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="error")


APP_MODULE = """\\
#!/usr/bin/env python3
\\"\\"\\"The Ledgerline review application: it recomputes and serves.

The reconciliation is recomputed from /app/inputs on startup rather than read
back from any deliverable, so the screens this serves and the JSON on disk are
two renderings of one computation. A solver that writes the JSON and then lets
a screen drift from it fails the served-divergence check, which is the point.
\\"\\"\\"

from __future__ import annotations

import csv
import json
from datetime import date
from itertools import combinations
from pathlib import Path

import uvicorn

APP = Path(__file__).resolve().parent
INPUTS = APP / "inputs"

{shared}

if __name__ == "__main__":
    serve_forever()
"""


def _app_module() -> str:
    \'\'\'The agent-visible server module, assembled from named helpers.

    Each helper is named rather than sliced by offset, so a reader can confirm
    exactly which definitions cross into the agent-visible tree and that no
    oracle-only material does.
    \'\'\'
    shared = "\\n\\n".join(
        inspect.getsource(fn).rstrip()
        for fn in (
            cents,
            read_statement,
            read_receipts,
            answer_from_inputs,
            esc,
            render_site,
            serve_forever,
        )
    )
    return APP_MODULE.replace("{shared}", f"STYLE = {STYLE!r}\\n\\n{shared}")


def write_app() -> None:
    """Write the fifth deliverable: start.sh and the app module it serves.

    Both land under /app rather than beside this solver, because solution/ is
    oracle-only under the Harbor layout and the deliverable the grader runs
    must stand on the agent-visible tree alone. APP_MODULE is a self-contained
    literal rather than a slice of this file, so what ships is exactly what a
    reader can read, and it carries no oracle-only material. The grader runs
    start.sh and drives a browser against what it serves, so nothing written
    here describes the rendering; it writes the thing that renders.
    """
    (APP / "app.py").write_text(_app_module(), encoding="utf-8")
    start = APP / "start.sh"
    start.write_text(
        "#!/bin/bash\\n"
        "set -euo pipefail\\n"
        "\\n"
        "# The system interpreter is named explicitly rather than resolved\\n"
        "# through PATH, because the grader's image puts a virtualenv earlier\\n"
        "# on PATH whose sys.prefix excludes /usr/lib/python3/dist-packages,\\n"
        "# where the apt-installed server packages live. A bare python3 there\\n"
        "# starts and then fails to import uvicorn.\\n"
        'exec /usr/bin/python3 "$(dirname "$0")/app.py"\\n',
        encoding="utf-8",
    )
    start.chmod(0o755)


if __name__ == "__main__":
    raise SystemExit(main())
'''


def write_solve(bundle: Path, ground: dict, result: dict) -> list[Path]:
    """The executable oracle and the solver it runs.

    Both are generated from this script so the oracle cannot drift from the
    grounding the checkers and TRUTH.md derive from. The solver recomputes the
    answer from the shipped inputs rather than embedding it.
    """
    solver = bundle / "solution" / "solver.py"
    solver.write_text(
        "#!/usr/bin/env python3\n"
        f"# {BANNER}\n"
        "# Generated by solution/recompute.py from solution/grounding.yaml.\n"
        + "\n".join(canary_block("#"))
        + "\n"
        + ORACLE_BODY,
        encoding="utf-8",
    )
    solver.chmod(0o755)

    path = bundle / "solution" / "solve.sh"
    path.write_text(
        "#!/bin/bash\n"
        f"# {BANNER}\n"
        "# Generated by solution/recompute.py from solution/grounding.yaml.\n"
        + "\n".join(canary_block("#"))
        + "\n"
        "set -euo pipefail\n"
        "\n"
        "mkdir -p /app/out\n"
        'python3 "$(dirname "$0")/solver.py"\n'
        "\n"
        "# The dump is taken after the final write to the table, which is what\n"
        "# the silent snapshot-staleness mutation punishes omitting. The solver\n"
        "# has already written start.sh and the module it serves, which the\n"
        "# what render-staleness punishes omitting.\n"
        'psql "postgresql://ledgerline:ledgerline@localhost:5432/ledgerline" \\\n'
        "  --no-psqlrc --quiet --tuples-only \\\n"
        '  --command "copy reconciliation to stdout" \\\n'
        "  > /app/out/snapshot.sql\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return [solver, path]


def write_truth(bundle: Path, ground: dict, result: dict) -> Path:
    path = bundle / "solution" / "TRUTH.md"
    lines = [
        "# Golden trajectory",
        "",
        BANNER,
        "",
        "Generated by `solution/recompute.py` from `solution/grounding.yaml`.",
        "",
        "<!--",
        *canary_block(""),
        "-->",
        "",
        "## Ordered path",
        "",
    ]
    steps = [
        (
            "read-policy",
            "Read `/app/inputs/policy.json` and bind the window, tolerance and "
            "category limits from its bytes rather than from the statement.",
            "hardcoded-policy",
            ["totals_consistent"],
        ),
        (
            "parse-statement",
            "Parse `/app/inputs/statement.csv`, keeping `amount_cents` signed and "
            "comparing against its absolute value during eligibility.",
            "signed-amount-compare",
            ["unreceipted_set_exact"],
        ),
        (
            "parse-receipts",
            "Parse every `/app/inputs/receipts/*.txt`, splitting each field line on "
            "its first colon only and ignoring non-field lines.",
            "naive-colon-split",
            ["unposted_set_exact"],
        ),
        (
            "match-maximum",
            "Build the eligibility graph and choose a maximum-cardinality matching, "
            "breaking ties by amount delta, then date delta, then receipt id.",
            "greedy-matching",
            ["matched_set_exact", "matched_sorted_by_receipt_id"],
        ),
        (
            "flag-violations",
            "Flag each matched pair whose category carries a limit the receipt total "
            "strictly exceeds.",
            "totals-drift",
            ["violation_flags_exact"],
        ),
        (
            "write-json",
            "Write `/app/out/reconciliation.json` with sorted lists and consistent totals.",
            "unsorted-output",
            ["reconciliation_json_wellformed", "totals_consistent"],
        ),
        (
            "write-table",
            "Create and populate the `reconciliation` table with exactly the six "
            "declared columns, one row per matched pair.",
            "no-op",
            ["snapshot_table_shape"],
        ),
        (
            "write-summary",
            "Write `/app/out/summary.txt` with the four declared lines.",
            "unsorted-output",
            ["summary_matches_json"],
        ),
        (
            "dump-after-final-write",
            "Dump the table to `/app/out/snapshot.sql` after the final write to it, "
            "surviving the silent snapshot-staleness mutation.",
            "stale-snapshot",
            ["snapshot_agrees_with_reconciliation_json"],
        ),
        (
            "serve-the-screens",
            "Serve the review screen and one detail screen per matched pair on "
            "`127.0.0.1:8080`, carrying every declared element id.",
            "missing-detail-screens",
            ["review_screen_structure", "detail_screens_complete"],
        ),
        (
            "render-violations-as-weight",
            "Resolve the policy-state cell of a violating pair to a computed "
            "font-weight of 700 and a compliant pair to 400, so the distinction "
            "survives without colour.",
            "colour-only-violation",
            ["review_screen_structure"],
        ),
        (
            "ship-fonts-in-image",
            "Reference only a font the image ships, so every graded cell resolves "
            "and nothing is fetched at render time.",
            "external-font-reference",
            ["served_screens_request_no_external_origin"],
        ),
        (
            "lay-out-columns-on-one-edge",
            "Size the matched table's columns so every cell of one column shares a "
            "left edge at the 1280 by 800 viewport, which a per-row layout does not.",
            "no-op",
            ["review_screen_columns_aligned"],
        ),
        (
            "serve-one-reconciliation",
            "Render every screen from the same recomputation the JSON deliverable "
            "reports, so no screen diverges from the declared result.",
            "divergent-screen",
            ["served_screen_agrees_with_reconciliation_json", "review_screen_rows_sorted"],
        ),
        (
            "write-the-contested-account",
            "Write `/app/out/contested.md` as exactly three sections headed "
            "`## <txn_id>`, choosing the three rows whose matching a reviewer is "
            "most likely to dispute and naming, in every section whose row had "
            "an admissible receipt besides the one it matched, at least one of "
            "those receipts.",
            "contested-account-absent",
            [
                "contested_account_structure",
                "contested_account_names_an_alternative",
            ],
        ),
        (
            "account-for-the-matching-faithfully",
            "State in each section the pairing `/app/out/reconciliation.json` "
            "declares for that row, and give as its reason the rule these inputs "
            "make true rather than a different rule naming the same quantities.",
            "contested-account-inverts-a-pairing",
            [
                "contested_pairing_matches_declared_matching",
                "contested_reason_is_the_operative_rule",
            ],
        ),
        (
            "write-the-policy-notes",
            "Write `/app/out/policy-notes.md` with one section per matched pair "
            "the JSON marks violating, those first in ascending `receipt_id` "
            "order, then exactly one section for a pair it marks compliant, each "
            "headed `## <receipt_id>` and carrying that receipt's total in cents "
            "beside the limit `category_limits_cents` sets for its own category.",
            "policy-notes-omits-a-violation",
            [
                "policy_notes_covers_every_violation",
                "policy_notes_names_the_governing_limit",
            ],
        ),
        (
            "state-the-policy-relation-truly",
            "State in each policy-notes section the relation the receipt total "
            "and its category limit actually stand in, and the verdict that "
            "relation entails under the strictly-exceeds rule.",
            "policy-notes-inverts-the-relation",
            ["policy_verdict_follows_from_the_two_numbers"],
        ),
        (
            "state-the-policy-magnitude-truly",
            "State in each policy-notes section how far the receipt total "
            "exceeds its category limit, or falls short of it, as the "
            "arithmetic difference between those two numbers.",
            "policy-notes-falsifies-the-magnitude",
            ["policy_magnitude_is_the_arithmetic_difference"],
        ),
        (
            "write-the-parse-notes",
            "Write `/app/out/parse-notes.md` with one section per receipt whose "
            "`VENDOR` carries a colon or which carries a non-field line, headed "
            "`## <receipt_id>` in ascending order and no others, each quoting "
            "that receipt's correct value and the wrong reading a careless "
            "parse would produce.",
            "parse-notes-omits-a-hazardous-receipt",
            [
                "parse_notes_covers_every_hazard",
                "parse_notes_quotes_the_correct_value",
            ],
        ),
        (
            "attribute-each-format-rule-to-its-receipt",
            "Attribute to each hazardous receipt the format rule its own bytes "
            "exercise: the first-colon-only rule where `VENDOR` carries a colon, "
            "and the ignore-non-field-lines rule where it does not.",
            "parse-notes-misattributes-the-colon-mechanism",
            [
                "parse_notes_attributes_the_colon_mechanism",
                "parse_notes_attributes_the_nonfield_mechanism",
            ],
        ),
        (
            "write-the-convention-notes",
            "Write `/app/out/convention-notes.md` with exactly two sections, "
            "`## collapse` then `## divergence`, the second naming every "
            "`txn_id` whose eligible set differs between the absolute-value "
            "rule and the signed comparison and every `txn_id` whose set is the "
            "same under both.",
            "convention-notes-states-only-the-general-shape",
            [
                "convention_notes_carries_both_sections",
                "convention_notes_names_every_row",
            ],
        ),
        (
            "state-the-convention-collapse-truly",
            "State in the collapse section that dropping the absolute value "
            "loses eligible pairs rather than gaining them, with the counts "
            "these inputs produce, and in the divergence section assign each "
            "named row to the group its own eligible sets put it in.",
            "convention-notes-inverts-the-collapse",
            [
                "convention_collapse_is_the_true_direction",
                "convention_divergence_set_is_the_true_partition",
            ],
        ),
        (
            "leave-inputs-untouched",
            "Leave every byte under `/app/inputs/` unchanged.",
            "no-op",
            ["inputs_unmodified"],
        ),
    ]
    for name, action, rejected, checkers in steps:
        lines += [
            f"### {name}",
            "",
            action,
            "",
            f"- rejected route: known-wrong control `{rejected}`",
            f"- satisfied checkers: {', '.join(f'`{c}`' for c in checkers)}",
            "",
        ]

    lines += [
        "## Deliverables",
        "",
        "| # | deliverable | carrier |",
        "| --- | --- | --- |",
        "| 1 | reconciliation JSON | `/app/out/reconciliation.json` |",
        "| 2 | `reconciliation` table | `/app/out/snapshot.sql` (pre-teardown dump) |",
        "| 3 | summary | `/app/out/summary.txt` |",
        "| 4 | table dump | `/app/out/snapshot.sql` |",
        "| 5 | served application | `/app/start.sh` on 127.0.0.1:8080 |",
        "| 6 | written account | `/app/out/contested.md` |",
        "| 7 | policy notes | `/app/out/policy-notes.md` |",
        "| 8 | parse notes | `/app/out/parse-notes.md` |",
        "| 9 | convention notes | `/app/out/convention-notes.md` |",
        "",
        "## Red lines",
        "",
    ]
    lines += [f"- `{item['id']}`: {item['assertion'].strip()}" for item in ground["red_lines"]]
    lines += ["", "## Reconciled outcome shape", ""]
    lines += [
        "The graded outcome is the reconciliation the corpus and policy determine.",
        "Its values live in `tests/fixtures.json`, which this script generates from",
        "the same source, so no constant in this document stands unbacked.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def input_digests(bundle: Path) -> dict[str, str]:
    """The sha256 of every generated input, keyed by its path under inputs/.

    The red-line checker compares the agent's `/app/inputs/` tree against these,
    so they are derived from the bytes this script just wrote rather than
    declared.
    """
    root = bundle / "environment" / "inputs"
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def write_assets(bundle: Path, ground: dict) -> Path:
    """The item 6 asset manifest, hash-bound to the bytes just written.

    Every entry carries its digest, modality, payload role, decoy status,
    fidelity flag and source recipe, and the task-level upstream_provenance
    record travels with it so the declared origin is not merely asset-level
    metadata. Licences are closed rather than inferred: every asset is authored
    here, so none carries an uncertain upstream licence to fail open on.
    """
    root = bundle / "environment" / "inputs"
    decoys = {
        "statement.csv": ["TXN-1011"],
        "receipts/rcp-0012.txt": ["RCP-0012"],
    }
    recipes = {
        "statement.csv": "solution/recompute.py write_inputs from grounding.yaml statement",
        "policy.json": "solution/recompute.py write_inputs from grounding.yaml policy",
    }
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        entries.append(
            {
                "path": f"inputs/{relative}",
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "modality": "text",
                "media_type": "text/csv" if relative.endswith(".csv")
                else "application/json" if relative.endswith(".json")
                else "text/plain",
                "payload": "policy-parameters" if relative == "policy.json"
                else "statement-rows" if relative == "statement.csv"
                else "receipt-document",
                "decoys": decoys.get(relative, []),
                "fidelity": "synthetic-authored",
                "license": "MIT",
                "license_chain": ["authored for this project under the parent MIT licence"],
                "source_recipe": recipes.get(
                    relative,
                    "solution/recompute.py write_inputs from grounding.yaml receipts",
                ),
            }
        )

    body = [
        "# " + BANNER,
        "# Generated by solution/recompute.py from solution/grounding.yaml.",
        "#",
        "# Phase build item 6. Agent-visible by design: this manifest describes",
        "# the shipped corpus and carries no answer, no derivation and no checker",
        "# value, so the leak gate scans it and finds only what a solver may read.",
        "schema: forge.asset-manifest/v1",
        "",
        "upstream_provenance:",
        "  origin: original",
        "  source_kind: synthetic",
        '  provenance_date: "2026-10-26"',
        "  generator: solution/recompute.py",
        "  upstream_lift: none",
        "  canonical_bundle_hash: unbound-until-mint",
        "  canonical_bundle_hash_note: >-",
        "    Item 6 binds this record to the canonical bundle hash. The hash is",
        "    derived at the item 2 mint over every regular file including this one,",
        "    so a value written here before the mint would move the hash it binds.",
        "    solution/provenance.yaml carries the binding under its binding: mapping,",
        "    which the mint excludes from the hash domain; this field names that",
        "    carrier rather than duplicating it.",
        "",
        "assets:",
    ]
    for entry in entries:
        body.append(f"  - path: {entry['path']}")
        for key in ("sha256", "bytes", "modality", "media_type", "payload", "fidelity", "license"):
            body.append(f"    {key}: {entry[key]}")
        if entry["decoys"]:
            body.append("    decoys:")
            body.extend(f"      - {item}" for item in entry["decoys"])
        else:
            body.append("    decoys: []")
        body.append("    license_chain:")
        body.extend(f"      - {item}" for item in entry["license_chain"])
        body.append(f"    source_recipe: {entry['source_recipe']}")

    path = bundle / "environment" / "assets.yaml"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def write_fixtures(bundle: Path, ground: dict, result: dict) -> Path:
    path = bundle / "tests" / "fixtures.json"
    body = {
        "schema": "forge.checker-fixtures/v1",
        "expected": result,
        "table_columns": [
            ["receipt_id", "text"],
            ["txn_id", "text"],
            ["amount_delta_cents", "integer"],
            ["date_delta_days", "integer"],
            ["category", "text"],
            ["violates_policy", "boolean"],
        ],
        "screens": ground["screens"],
        "rendered_state": ground["rendered_state"],
        "input_digests": input_digests(bundle),
    }
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_provenance(bundle: Path, ground: dict) -> Path:
    """The item 10i carrier, regenerated exactly from grounding.yaml.

    Every outcome here is a producer claim, never evidence: a reader recomputes
    each one from committed bytes and the named roots. The binding block is
    applied afterwards by ``bundle_mint.py mint``, which excludes it from the
    hash domain so the bindings stay acyclic, and the detached signature at
    ``solution/provenance.sig`` is an external act this script never performs.
    """
    screening = ground.get("screening", {})
    body = [
        "# " + BANNER,
        "# Generated by solution/recompute.py from solution/grounding.yaml.",
        "#",
        "# FORGE Phase build item 10i. Private: it sits in solution/ beside TRUTH.md,",
        "# carries the item 11a canaries, and is prohibited from both ENGRAM",
        "# projections. Its canonical payload is these bytes excluding every",
        "# signature and the later binding block.",
        *canary_block("#"),
        "schema: forge.provenance/v1",
        "",
        "declared_source:",
        "  origin: original",
        "  source_kind: synthetic",
        '  provenance_date: "2026-10-26"',
        "  generator: solution/recompute.py",
        "  generation_inputs:",
        "    - solution/grounding.yaml",
        "  upstream_lift: none",
        "  upstream_lift_note: >-",
        "    No upstream task bytes are copied or derived. The reference benchmark is",
        "    a shape and ceiling reference only.",
        "",
        "ancestry_attestation:",
        "  digest: null",
        "  signer: null",
        "  state: attestation-unavailable",
        "  note: >-",
        "    Item 9a.1 requires a signed platform attestation resolvable against the",
        "    ENGRAM source-attestation trust root. No trust root is projected in this",
        "    parent, so the declaration is recorded as unavailable and caps at",
        "    HOLD:PILOT_REQUIRED rather than being accepted on trust.",
        "",
        "event_attestation:",
        "  digest: null",
        "  signer: null",
        "  state: attestation-unavailable",
        "  note: >-",
        "    A synthetic original has no task-generating event to attest, and no trust",
        "    root exists to resolve one against.",
        "",
        "screening_roots:",
        "  exclusion_list_root:",
        "    identity: null",
        "    digest: null",
        "    state: unresolved",
        "  freeze_date_table_root:",
        "    identity: null",
        "    digest: null",
        "    state: unresolved",
        "  neardup_index_root:",
        "    identity: null",
        "    digest: null",
        "    state: unresolved",
        "  note: >-",
        "    Every root is ENGRAM-owned and unresolved in this parent, so the",
        "    contamination screen caps at HOLD:PILOT_REQUIRED with reason",
        "    screening-unverified. FORGE authors none of these roots.",
        "",
        "instrument_versions:",
        "  harbor_release: 0.25.0",
        "  harbor_commit: fdba28fc411e47e8765420a1a063b4918fce3def",
        "  harbor_rewardkit: 0.2.4",
        "  base_image: >-",
        "    ubuntu:24.04@sha256:24737cef17f4e22b1977df7d9a05e77c4e28b7da5b8911f8d8ccda136bf3573c",
        "  dockerfile_frontend: >-",
        "    docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e",
        "  apt_snapshot: 20261001T000000Z",
        "  uv: 0.9.7",
        "",
        "atom_results:",
        "  freeze_holdout:",
        "    state: not-applicable",
        "    reason: >-",
        "      A synthetic original has no task-generating event timestamp to compare",
        "      against a versioned cutoff, and no freeze-date table root is resolvable.",
        "  near_duplicate_screening:",
        "    state: unresolved",
        "    reason: >-",
        "      No neardup index root and no calibrated threshold exist, so no bounded",
        "      claim can be recorded. An uncalibrated threshold would report a number",
        "      nobody has shown detects anything.",
        "  sanitization:",
        "    state: measured",
        "    reason: >-",
        "      The agent-facing closure is the instruction, the environment tree the",
        "      image ships, and the asset manifest. No version-control history, no",
        "      repository remote, no thread export and no solution fragment is copied",
        "      into it, because the corpus is generated rather than lifted.",
        "  empty_submission:",
        "    state: measured",
        "    reason: >-",
        "      The no-op control produces no deliverable, and 32 of the 33 graded",
        "      items reject it: all 8 judged, and 24 of 25 compiled. The one it",
        "      banks is inputs_unmodified, at weight 5, which it satisfies by",
        "      touching nothing. Under the bound weighted-mean rule that is a",
        "      floor of 5/115 rather than zero, which task.toml carries as",
        "      metadata.null_floor; under the superseded all-pass rule the same",
        "      submission read 0.0.",
        "  per_unit_provenance:",
        "    state: emitted",
        "    reason: This record.",
        "",
        "delivery_closure:",
        "  agent_visible:",
        "    - instruction.md",
        "    - environment/",
        "  private:",
        "    - solution/",
        "    - tests/",
        "  note: >-",
        "    Resolved against the agent_visible_boundary and private_boundary the",
        "    approved contract binds. The closure identity binds to the per-platform",
        "    image digests, which land at the item 2a.1 read-back.",
        "",
        "screening_expiry:",
        "  screening_measured_at: null",
        "  screening_interval_days: 90",
        "  screening_expires_at: null",
        "  note: >-",
        "    The two instants stay null while every screening root is unresolved,",
        "    because a measured-at stamp over an unresolved root would assert a",
        "    measurement that never happened. The interval is carried so a client can",
        "    compute expiry from the unit alone once the roots resolve.",
        "",
        "claims_are_not_evidence: >-",
        "  Every outcome in this record is a producer claim. A reader recomputes each",
        "  one from committed bytes and the named roots; an assertion here may be",
        "  contradicted and must never be relied upon.",
    ]
    path = bundle / "solution" / "provenance.yaml"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def write_criteria(bundle: Path, ground: dict) -> Path:
    """Compiled Reward Kit criteria: one per checker, carrying the implied
    relation only. Item 10f forbids criterion prose or reference text here."""
    path = bundle / "tests" / "test_output.py"
    head = [
        "#!/usr/bin/env python3",
        f"# {BANNER}",
        "# Generated by solution/recompute.py from solution/grounding.yaml.",
        "# Item 10f: this file carries the implied relation only, never criterion",
        "# prose and never a reference value; every expected value is read from",
        "# tests/fixtures.json at grade time. Reward Kit auto-registers a",
        "# zero-argument criterion whose first parameter is the workspace path,",
        "# per skills/rewardkit/SKILL.md at the pinned Harbor commit, and the",
        "# trailing block re-registers each one at its graded weight.",
        '"""Compiled deterministic criteria for the Ledgerline reconciliation task."""',
        "",
        "from __future__ import annotations",
        "",
        "import importlib.util",
        "from pathlib import Path",
        "",
        "import rewardkit as rk",
        "from rewardkit import criterion",
        "",
        "# checks.py is loaded by explicit path rather than imported by name,",
        "# because Reward Kit executes each criteria file through",
        "# importlib.util.spec_from_file_location without adding the tests",
        "# directory to sys.path, so a plain sibling import raises",
        "# ModuleNotFoundError and the verifier writes no reward at all.",
        "_spec = importlib.util.spec_from_file_location(",
        '    "ledgerline_checks", Path(__file__).resolve().parent / "checks.py"',
        ")",
        "checks = importlib.util.module_from_spec(_spec)",
        "_spec.loader.exec_module(checks)",
        "",
    ]
    body = []
    for check in ground["checkers"]:
        body += [
            "@criterion",
            f"def {check['id']}(workspace: Path) -> bool:",
            f"    return checks.{check['id']}(workspace)",
            "",
            "",
        ]
    weights = [int(check["weight"]) for check in ground["checkers"]]
    body += [
        "# The graded weight each criterion carries in solution/grounding.yaml.",
        "# The decorator's auto-registration hardcodes 1.0 and Session.register",
        "# replaces it on an explicit call, so the weight can only arrive this way:",
        "# there is no weight argument on the decorator and no per-criterion weight",
        f"# in a Reward Kit scoring table. They total {sum(weights)}, which the",
        "# top-level [[reward]] in tests/reward.toml names as this dimension's",
        "# weight. Inert under all-pass, which reads no weight, and live under the",
        f"# bound {ground['aggregation']['rule']}.",
    ]
    body += [
        f"rk.{check['id']}(weight={weight}.0)"
        for check, weight in zip(ground["checkers"], weights)
    ]
    path.write_text("\n".join(head + body).rstrip("\n") + "\n", encoding="utf-8")
    return path


def _toml_basic(value: str) -> str:
    """A TOML basic string. Only the escapes the spec requires: backslash and
    quote, then the control characters. The criterion prose is frozen in
    solution/grounding.yaml, so this must not reflow or re-wrap it."""
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{out}"'


def write_judge(bundle: Path, ground: dict) -> Path | None:
    """Judged Reward Kit criteria, one per solution/grounding.yaml judged_items
    entry. Item 10f: "Zero judged items means no tests/judge.toml, never an
    empty criterion file", so this returns None rather than writing an empty
    carrier. Reward Kit's CriterionConfig is declared extra="forbid", so no key
    outside its schema may be emitted: description, type, name, id, points,
    levels, min, max, weight, files, negate, optional, annotations. Everything
    this bundle carries beyond that set goes under annotations, which is
    free-form. [judge] is emitted only when the grounding retains a source
    value for it, and carries exactly the fields it retains: judge, model,
    version, mode and cwd. JudgeConfig is declared extra="forbid", so no key
    outside its schema may be written, and none without a source value is."""
    judged = ground.get("judged_items") or {}
    items = judged.get("items") or []
    judge_cfg = judged.get("judge") or {}
    backend = str(judge_cfg.get("backend") or "").strip()
    model = str(judge_cfg.get("model") or "").strip()
    version = str(judge_cfg.get("version") or "").strip()
    cwd = str(judge_cfg.get("cwd") or "").strip()
    mode = str(judge_cfg.get("mode") or "").strip()
    path = bundle / "tests" / "judge.toml"
    if not items:
        if path.exists():
            path.unlink()
        return None
    lines = [
        f"# {BANNER}",
        "# Generated by solution/recompute.py from solution/grounding.yaml.",
        "# Item 10f: the judged carrier. Each criterion's description is the",
        "# frozen criterion prose from the grounding, emitted byte-for-byte; the",
        "# residue recording why the judgment is not deterministically reducible",
        "# over its evaluation_target's bytes travels under annotations.",
        "#",
        "# [judge] carries every field the grounding's judged_items.judge records",
        "# as a source value, so item 10f's \"omit [judge] unless a source value",
        "# exists\" is satisfied rather than asserted. judge names the backend,",
        "# not the model: Reward Kit routes a known agent name through its",
        "# agent-judge backend, which execs that CLI, and anything else through",
        "# the HTTP messages endpoint, which refuses this bundle's subscription",
        "# token. model, version and cwd follow from that choice. mode is forced",
        "# rather than chosen: every criterion below declares its own files and",
        "# this table declares none, and reward.py refuses that combination under",
        "# the batched default because the batched path reads only the",
        "# judge-level files and would grade every criterion against nothing.",
        "# isolated is not emitted and defaults false; tests/test.sh passes",
        "# --yolo, because the isolated path mounts overlayfs and this container",
        "# cannot.",
        "#",
        "# [scoring] is emitted rather than defaulted because the rule is a bound",
        "# source value: the grounding's aggregation.rule carries it, and every",
        "# Reward Kit table reads that one value. Omitting this table would leave",
        "# the judged dimension aggregating by JudgeTomlConfig's own default,",
        "# which is a divergence from the bound rule rather than an absent value",
        "# even on the passes where the two coincide. The contract records the",
        "# consequence of letting them diverge under",
        "# uuid_prior_recording_after_scoring_table.",
        "",
    ]
    if backend or model:
        lines += ["[judge]", f"judge = {_toml_basic(backend or model)}"]
        if backend and model:
            lines.append(f"model = {_toml_basic(model)}")
        if version:
            lines.append(f"version = {_toml_basic(version)}")
        if mode:
            lines.append(f"mode = {_toml_basic(mode)}")
        if cwd:
            lines.append(f"cwd = {_toml_basic(cwd)}")
        lines.append("")
    lines += [
        "[scoring]",
        f"aggregation = {_toml_basic(ground['aggregation']['rule'])}",
        "",
    ]
    for item in items:
        lines += [
            "[[criterion]]",
            f"id = {_toml_basic(item['id'])}",
            f"name = {_toml_basic(item['id'])}",
            f"description = {_toml_basic(item['criterion'])}",
            'type = "binary"',
            f"weight = {int(item['weight'])}",
            f"negate = {'true' if item.get('negate') else 'false'}",
            "files = [",
        ]
        for ev in item["evidence"]:
            lines.append(f"    {_toml_basic(ev)},")
        lines += [
            "]",
            "",
            "[criterion.annotations]",
            'mode = "judged"',
            f"outcome_class = {_toml_basic(item['outcome_class'])}",
            f"obligation = {_toml_basic(item['obligation'])}",
            f"dimension = {_toml_basic(item['dimension'])}",
            f"evaluation_target = {_toml_basic(item['evaluation_target'])}",
            f"residue = {_toml_basic(item['residue'])}",
            "",
        ]
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return path


def main(argv: list[str]) -> int:
    bundle = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
    ground = load(bundle)
    result = answer(ground)
    written = write_inputs(bundle, ground)
    written.append(write_assets(bundle, ground))
    written.extend(write_solve(bundle, ground, result))
    written.append(write_truth(bundle, ground, result))
    written.append(write_fixtures(bundle, ground, result))
    written.append(write_criteria(bundle, ground))
    judge = write_judge(bundle, ground)
    if judge is not None:
        written.append(judge)
    written.append(write_provenance(bundle, ground))
    for path in written:
        print(path.relative_to(bundle))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
