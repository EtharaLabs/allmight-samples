"""Core reconciliation: parse inputs, build the eligibility graph, choose matches.

Pure standard library.  Imported by both build.py (which writes the file and
database deliverables) and app.py (which serves the review screens), so that
every deliverable describes the same reconciliation of the same inputs.
"""

import csv
import json
import os
from datetime import date

INPUT_DIR = os.environ.get("LEDGERLINE_INPUTS", "/app/inputs")

FIELD_NAMES = {"RECEIPT-ID", "DATE", "VENDOR", "TOTAL", "CATEGORY"}


def _parse_date(text):
    year, month, day = (int(part) for part in text.strip().split("-"))
    return date(year, month, day)


def _total_to_cents(text):
    whole, _, frac = text.strip().partition(".")
    return int(whole) * 100 + int(frac)


def load_policy(input_dir=INPUT_DIR):
    with open(os.path.join(input_dir, "policy.json"), encoding="utf-8") as handle:
        return json.load(handle)


def load_statement(input_dir=INPUT_DIR):
    rows = []
    with open(os.path.join(input_dir, "statement.csv"), newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            rows.append(
                {
                    "txn_id": raw["txn_id"].strip(),
                    "posted_date": _parse_date(raw["posted_date"]),
                    "description": raw["description"],
                    "amount_cents": int(raw["amount_cents"]),
                    "currency": raw["currency"].strip(),
                }
            )
    return rows


def parse_receipt(text):
    """Read one receipt body.

    Only the first colon on a line separates the field name from its value, so
    a VENDOR value keeps any colons of its own.  A line that does not open with
    one of the five known field names is not a field line and is ignored.
    """
    fields = {}
    for line in text.splitlines():
        name, sep, value = line.partition(":")
        if not sep:
            continue
        name = name.strip()
        if name.upper() not in FIELD_NAMES:
            continue
        fields[name.upper()] = value.strip()
    return fields


def load_receipts(input_dir=INPUT_DIR):
    receipts = []
    directory = os.path.join(input_dir, "receipts")
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".txt"):
            continue
        with open(os.path.join(directory, filename), encoding="utf-8") as handle:
            fields = parse_receipt(handle.read())
        receipts.append(
            {
                "receipt_id": fields["RECEIPT-ID"],
                "date": _parse_date(fields["DATE"]),
                "vendor": fields["VENDOR"],
                "total_cents": _total_to_cents(fields["TOTAL"]),
                "category": fields["CATEGORY"],
                "source": filename,
            }
        )
    return receipts


def eligible_pairs(receipts, statement, policy, signed=False):
    """Every (receipt, row) pair that satisfies both eligibility rules.

    With signed=False the amount test compares the receipt total against the
    absolute value of the row amount, as the specification requires.  With
    signed=True it compares against the signed amount instead; that variant
    exists only to quantify the cost of the mistake in convention-notes.md.
    """
    tolerance = policy["amount_tolerance_cents"]
    window = policy["match_window_days"]
    pairs = []
    for receipt in receipts:
        for row in statement:
            row_amount = row["amount_cents"] if signed else abs(row["amount_cents"])
            amount_delta = receipt["total_cents"] - row_amount
            date_delta = (receipt["date"] - row["posted_date"]).days
            if abs(amount_delta) <= tolerance and abs(date_delta) <= window:
                pairs.append(
                    {
                        "receipt_id": receipt["receipt_id"],
                        "txn_id": row["txn_id"],
                        "amount_delta_cents": amount_delta,
                        "date_delta_days": date_delta,
                        "category": receipt["category"],
                        "total_cents": receipt["total_cents"],
                        "amount_cents": row["amount_cents"],
                    }
                )
    return pairs


def _max_matching_size(adjacency, receipt_ids):
    """Kuhn's algorithm: size of a maximum matching of the given subgraph."""
    matched_txn = {}

    def augment(receipt_id, seen):
        for txn_id in adjacency.get(receipt_id, ()):
            if txn_id in seen:
                continue
            seen.add(txn_id)
            holder = matched_txn.get(txn_id)
            if holder is None or augment(holder, seen):
                matched_txn[txn_id] = receipt_id
                return True
        return False

    size = 0
    for receipt_id in receipt_ids:
        if augment(receipt_id, set()):
            size += 1
    return size


def choose_matches(pairs):
    """Pick a maximum-size matching, breaking ties by the stated preference.

    Pairs are considered in preference order (smaller absolute amount delta,
    then smaller absolute date delta, then lexicographically smaller receipt
    id).  A pair is committed only when the rest of the graph can still be
    matched to reach the maximum size, so maximising the pair count always
    wins over the preference order, exactly as the rules require.
    """
    adjacency = {}
    for pair in pairs:
        adjacency.setdefault(pair["receipt_id"], []).append(pair["txn_id"])
    for neighbours in adjacency.values():
        neighbours.sort()
    all_receipts = sorted(adjacency)
    target = _max_matching_size(adjacency, all_receipts)

    order = sorted(
        pairs,
        key=lambda p: (
            abs(p["amount_delta_cents"]),
            abs(p["date_delta_days"]),
            p["receipt_id"],
            p["txn_id"],
        ),
    )

    chosen = []
    used_receipts = set()
    used_txns = set()
    for pair in order:
        if len(chosen) == target:
            break
        if pair["receipt_id"] in used_receipts or pair["txn_id"] in used_txns:
            continue
        trial_receipts = used_receipts | {pair["receipt_id"]}
        trial_txns = used_txns | {pair["txn_id"]}
        remainder = {
            receipt_id: [t for t in neighbours if t not in trial_txns]
            for receipt_id, neighbours in adjacency.items()
            if receipt_id not in trial_receipts
        }
        reachable = _max_matching_size(remainder, sorted(remainder))
        if len(chosen) + 1 + reachable == target:
            chosen.append(pair)
            used_receipts = trial_receipts
            used_txns = trial_txns
    return chosen


def violates(pair, policy):
    limits = policy["category_limits_cents"]
    limit = limits.get(pair["category"])
    return limit is not None and pair["total_cents"] > limit


def reconcile(input_dir=INPUT_DIR):
    policy = load_policy(input_dir)
    statement = load_statement(input_dir)
    receipts = load_receipts(input_dir)

    pairs = eligible_pairs(receipts, statement, policy)
    chosen = choose_matches(pairs)

    matched = sorted(
        (
            {
                "receipt_id": pair["receipt_id"],
                "txn_id": pair["txn_id"],
                "amount_delta_cents": pair["amount_delta_cents"],
                "date_delta_days": pair["date_delta_days"],
                "category": pair["category"],
                "violates_policy": violates(pair, policy),
            }
            for pair in chosen
        ),
        key=lambda m: m["receipt_id"],
    )

    matched_receipts = {m["receipt_id"] for m in matched}
    matched_txns = {m["txn_id"] for m in matched}
    unreceipted = sorted(r["txn_id"] for r in statement if r["txn_id"] not in matched_txns)
    unposted = sorted(r["receipt_id"] for r in receipts if r["receipt_id"] not in matched_receipts)

    return {
        "matched": matched,
        "unreceipted": unreceipted,
        "unposted": unposted,
        "totals": {
            "matched_count": len(matched),
            "unreceipted_count": len(unreceipted),
            "unposted_count": len(unposted),
            "violation_count": sum(1 for m in matched if m["violates_policy"]),
        },
    }
