#!/usr/bin/env python3
"""Deterministic checkers for the Ledgerline reconciliation task.

Every expected value is read from ``tests/fixtures.json``, which the bundle's
private recomputation script generates from its single grounding source, so a
checker can never disagree with the oracle or the golden trajectory.

Each function reads live state through the real handles only: files the agent
wrote under the workspace, and the table dump the agent produced. None reads a
planted file, an environment secret, a random source, or a clock.

No canary block: item 11a excludes files under ``tests/`` that the separate
verifier image is built from, because item 2b.2 pins that image's digest
inside the hash preimage.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures.json"
EXPECTED_KEY = "expected"
DIGESTS_KEY = "input_digests"
SUMMARY_LINE = re.compile(r"\A(matched|unreceipted|unposted|violations): (0|[1-9][0-9]*)\Z")
MATCH_KEYS = frozenset(
    {
        "receipt_id",
        "txn_id",
        "amount_delta_cents",
        "date_delta_days",
        "category",
        "violates_policy",
    }
)
ROOT_KEYS = frozenset({"matched", "unreceipted", "unposted", "totals"})
TOTAL_KEYS = frozenset(
    {"matched_count", "unreceipted_count", "unposted_count", "violation_count"}
)


def _fixtures() -> dict:
    with FIXTURES.open(encoding="utf-8") as handle:
        return json.load(handle)


def _expected() -> dict:
    return _fixtures()[EXPECTED_KEY]


def _reconciliation(workspace: Path) -> dict | None:
    path = workspace / "out" / "reconciliation.json"
    try:
        with path.open(encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _snapshot_rows(workspace: Path) -> list[list[str]] | None:
    """Rows of the table dump, from either a COPY stream or a pg_dump body.

    ``instruction.md`` admits both carriers, so the reader accepts both: a bare
    tab-separated COPY stream, and a pg_dump whose COPY block is delimited by a
    ``COPY ... FROM stdin;`` header and a lone ``\\.`` terminator.
    """
    path = workspace / "out" / "snapshot.sql"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    lines = text.splitlines()
    inside, rows = False, []
    saw_header = False
    for line in lines:
        if not inside and line.lstrip().upper().startswith("COPY ") and "stdin" in line.lower():
            inside, saw_header = True, True
            continue
        if inside:
            if line.strip() == r"\.":
                inside = False
                continue
            if line.strip():
                rows.append(line.split("\t"))
            continue
    if saw_header:
        return rows
    return [line.split("\t") for line in lines if line.strip() and line.strip() != r"\."]


def _as_bool(cell: str) -> bool | None:
    lowered = cell.strip().lower()
    if lowered in {"t", "true"}:
        return True
    if lowered in {"f", "false"}:
        return False
    return None


def reconciliation_json_wellformed(workspace: Path) -> bool:
    found = _reconciliation(workspace)
    if found is None or set(found) != ROOT_KEYS:
        return False
    if not isinstance(found["matched"], list):
        return False
    for item in found["matched"]:
        if not isinstance(item, dict) or set(item) != MATCH_KEYS:
            return False
        if not isinstance(item["receipt_id"], str) or not isinstance(item["txn_id"], str):
            return False
        if not isinstance(item["category"], str):
            return False
        if not isinstance(item["violates_policy"], bool):
            return False
        for key in ("amount_delta_cents", "date_delta_days"):
            if not isinstance(item[key], int) or isinstance(item[key], bool):
                return False
    if not all(isinstance(value, list) for value in (found["unreceipted"], found["unposted"])):
        return False
    totals = found["totals"]
    return isinstance(totals, dict) and set(totals) == TOTAL_KEYS


def matched_set_exact(workspace: Path) -> bool:
    found = _reconciliation(workspace)
    if found is None or not isinstance(found.get("matched"), list):
        return False
    want = _expected()["matched"]
    if len(found["matched"]) != len(want):
        return False
    key = lambda item: item.get("receipt_id", "")
    return sorted(found["matched"], key=key) == sorted(want, key=key)


def matched_sorted_by_receipt_id(workspace: Path) -> bool:
    found = _reconciliation(workspace)
    if found is None or not isinstance(found.get("matched"), list):
        return False
    ids = [item.get("receipt_id") for item in found["matched"] if isinstance(item, dict)]
    if len(ids) != len(found["matched"]) or not all(isinstance(value, str) for value in ids):
        return False
    return ids == sorted(ids)


def unreceipted_set_exact(workspace: Path) -> bool:
    found = _reconciliation(workspace)
    if found is None:
        return False
    return found.get("unreceipted") == _expected()["unreceipted"]


def unposted_set_exact(workspace: Path) -> bool:
    found = _reconciliation(workspace)
    if found is None:
        return False
    return found.get("unposted") == _expected()["unposted"]


def violation_flags_exact(workspace: Path) -> bool:
    found = _reconciliation(workspace)
    if found is None or not isinstance(found.get("matched"), list):
        return False
    want = {
        item["receipt_id"]: item["violates_policy"]
        for item in _expected()["matched"]
    }
    got = {}
    for item in found["matched"]:
        if not isinstance(item, dict) or not isinstance(item.get("receipt_id"), str):
            return False
        got[item["receipt_id"]] = item.get("violates_policy")
    return got == want


def totals_consistent(workspace: Path) -> bool:
    found = _reconciliation(workspace)
    if found is None or not isinstance(found.get("totals"), dict):
        return False
    totals = found["totals"]
    matched = found.get("matched")
    if not isinstance(matched, list):
        return False
    violations = sum(
        1 for item in matched if isinstance(item, dict) and item.get("violates_policy") is True
    )
    return (
        totals.get("matched_count") == len(matched)
        and totals.get("unreceipted_count") == len(found.get("unreceipted") or [])
        and totals.get("unposted_count") == len(found.get("unposted") or [])
        and totals.get("violation_count") == violations
    )


def summary_matches_json(workspace: Path) -> bool:
    found = _reconciliation(workspace)
    if found is None or not isinstance(found.get("totals"), dict):
        return False
    try:
        text = (workspace / "out" / "summary.txt").read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if len(lines) != 4:
        return False
    order = ("matched", "unreceipted", "unposted", "violations")
    totals = found["totals"]
    want = (
        totals.get("matched_count"),
        totals.get("unreceipted_count"),
        totals.get("unposted_count"),
        totals.get("violation_count"),
    )
    for line, label, value in zip(lines, order, want, strict=True):
        parsed = SUMMARY_LINE.match(line)
        if parsed is None or parsed[1] != label or int(parsed[2]) != value:
            return False
    return True


def snapshot_table_shape(workspace: Path) -> bool:
    rows = _snapshot_rows(workspace)
    if rows is None:
        return False
    width = len(_fixtures()["table_columns"])
    return bool(rows) and all(len(row) == width for row in rows)


def snapshot_agrees_with_reconciliation_json(workspace: Path) -> bool:
    """The silent snapshot-staleness mutation's checker.

    The verifier runs in a separate offline container holding only the declared
    artifacts, so the live table is unreachable and this dump is the only
    evidence of table state. A dump taken before a later write to the table
    disagrees with the matched array and is rejected here.
    """
    found = _reconciliation(workspace)
    rows = _snapshot_rows(workspace)
    if found is None or rows is None or not isinstance(found.get("matched"), list):
        return False
    if len(rows) != len(found["matched"]):
        return False

    dumped = set()
    for row in rows:
        if len(row) != 6:
            return False
        flag = _as_bool(row[5])
        if flag is None:
            return False
        try:
            amount, days = int(row[2]), int(row[3])
        except ValueError:
            return False
        dumped.add((row[0], row[1], amount, days, row[4], flag))

    declared = {
        (
            item.get("receipt_id"),
            item.get("txn_id"),
            item.get("amount_delta_cents"),
            item.get("date_delta_days"),
            item.get("category"),
            item.get("violates_policy"),
        )
        for item in found["matched"]
        if isinstance(item, dict)
    }
    return dumped == declared


def inputs_unmodified(workspace: Path) -> bool:
    """Red line: the shipped question is unchanged.

    The digests are the input bytes the recomputation script generated into the
    image build context, recorded in the fixtures at generation time.
    """
    expected = _fixtures()[DIGESTS_KEY]
    if not expected:
        return False
    root = workspace / "inputs"
    seen = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        seen[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return seen == expected


# --- the written account ---------------------------------------------------
# Two compiled items over /app/out/contested.md. Both grade token presence,
# which is what a comparator over the carrier's bytes can settle; what the
# prose asserts about those tokens is graded by the two judged criteria in
# tests/judge.toml instead. The eligible set is recomputed here from the
# shipped inputs by the same rule the oracle applies, so neither checker reads
# a reference value out of the prose it is grading, and neither reads the
# contestable rows out of the fixtures.

CONTESTED_HEADING = re.compile(r"\A##[ \t]+(\S+)[ \t]*\Z")
IDENTIFIER = re.compile(r"\b(?:RCP-[0-9]+|TXN-[0-9]+)\b")


def _contested_sections(workspace: Path) -> list[tuple[str, list[str]]] | None:
    """Sections of the account, as (heading id, body lines) in file order."""
    try:
        text = (workspace / "out" / "contested.md").read_text(encoding="utf-8")
    except OSError:
        return None
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        found = CONTESTED_HEADING.match(line)
        if found:
            sections.append((found.group(1), []))
        elif sections:
            sections[-1][1].append(line)
    return sections


def _input_statement_rows(workspace: Path) -> list[dict] | None:
    import csv

    path = workspace / "inputs" / "statement.csv"
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return [
                {
                    "txn_id": row["txn_id"],
                    "posted_date": row["posted_date"],
                    "amount_cents": int(row["amount_cents"]),
                }
                for row in csv.DictReader(handle)
            ]
    except (OSError, KeyError, ValueError):
        return None


def _input_receipts(workspace: Path) -> list[dict] | None:
    """The same first-colon-only parse the statement admits: a vendor name may
    contain a colon and a receipt may carry trailing prose."""
    fields = {"RECEIPT-ID", "DATE", "VENDOR", "TOTAL", "CATEGORY"}
    out = []
    try:
        paths = sorted((workspace / "inputs" / "receipts").glob("*.txt"))
    except OSError:
        return None
    for path in paths:
        found = {}
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in body.splitlines():
            name, sep, value = line.partition(":")
            if sep and name.strip() in fields:
                found[name.strip()] = value.strip()
        try:
            whole, _, fraction = found["TOTAL"].strip().partition(".")
            out.append(
                {
                    "receipt_id": found["RECEIPT-ID"],
                    "date": found["DATE"],
                    "total_cents": int(whole) * 100 + int(fraction),
                    "category": found["CATEGORY"],
                }
            )
        except (KeyError, ValueError):
            return None
    return out


def _eligible_by_txn(workspace: Path) -> dict[str, set[str]] | None:
    """For each txn_id, every receipt_id the policy admits for it.

    Recomputed from workspace/inputs by the rule in the statement: the unsigned
    receipt total against the absolute row amount within the tolerance, and the
    receipt date within the window of the posted date.
    """
    from datetime import date as _date

    rows = _input_statement_rows(workspace)
    receipts = _input_receipts(workspace)
    if rows is None or receipts is None:
        return None
    try:
        policy = json.loads((workspace / "inputs" / "policy.json").read_text(encoding="utf-8"))
        window = policy["match_window_days"]
        tolerance = policy["amount_tolerance_cents"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None

    out: dict[str, set[str]] = {row["txn_id"]: set() for row in rows}
    for receipt in receipts:
        try:
            rdate = _date.fromisoformat(receipt["date"])
        except ValueError:
            return None
        for row in rows:
            try:
                pdate = _date.fromisoformat(row["posted_date"])
            except ValueError:
                return None
            if abs(receipt["total_cents"] - abs(row["amount_cents"])) > tolerance:
                continue
            if abs((rdate - pdate).days) > window:
                continue
            out[row["txn_id"]].add(receipt["receipt_id"])
    return out


def contested_account_structure(workspace: Path) -> bool:
    sections = _contested_sections(workspace)
    if sections is None or len(sections) != 3:
        return False
    rows = _input_statement_rows(workspace)
    if rows is None:
        return False
    known = {row["txn_id"] for row in rows}
    seen = set()
    for heading, body in sections:
        if heading not in known or heading in seen:
            return False
        seen.add(heading)
        if not any(line.strip() for line in body):
            return False
    return True


def contested_account_names_an_alternative(workspace: Path) -> bool:
    """Every section whose row had a losing alternative names one of them, and
    at least one section did.

    The quantifier is conditional rather than universal because the corpus does
    not give every row an alternative: a row whose only admissible receipt is
    the one it matched has none to name, and a row with no admissible receipt
    has no eligible set at all. Requiring one unconditionally would fail the
    reference solution, so the obligation is placed on the rows that can carry
    it. The final conjunct keeps it from passing vacuously on three such rows.

    The eligible set is recomputed from the inputs rather than read from the
    prose. What this cannot decide is whether the section presents that
    identifier as an alternative, which is why the judged criteria exist.
    """
    sections = _contested_sections(workspace)
    if sections is None or len(sections) != 3:
        return False
    eligible = _eligible_by_txn(workspace)
    found = _reconciliation(workspace)
    if eligible is None or found is None:
        return False
    try:
        matched = {
            item["txn_id"]: item["receipt_id"]
            for item in found["matched"]
            if isinstance(item, dict)
        }
    except (KeyError, TypeError):
        return False

    obliged = 0
    for heading, body in sections:
        admissible = eligible.get(heading) or set()
        partner = matched.get(heading)
        others = admissible - ({partner} if partner else set())
        if not others:
            continue
        obliged += 1
        named = set(IDENTIFIER.findall("\n".join(body)))
        if not (named & others):
            return False
    return obliged > 0


RECEIPT_HEADING = re.compile(r"\A##[ \t]+(RCP-[0-9]+)[ \t]*\Z")
CENTS = re.compile(r"\b([0-9]{1,9})\b")


def _policy_sections(workspace: Path) -> list[tuple[str, list[str]]] | None:
    try:
        text = (workspace / "out" / "policy-notes.md").read_text(encoding="utf-8")
    except OSError:
        return None
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        found = RECEIPT_HEADING.match(line)
        if found:
            sections.append((found.group(1), []))
        elif sections:
            sections[-1][1].append(line)
    return sections


def _policy_limits(workspace: Path) -> dict[str, int] | None:
    try:
        policy = json.loads((workspace / "inputs" / "policy.json").read_text(encoding="utf-8"))
        limits = policy["category_limits_cents"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None
    return limits if isinstance(limits, dict) else None


def policy_notes_covers_every_violation(workspace: Path) -> bool:
    sections = _policy_sections(workspace)
    found = _reconciliation(workspace)
    if sections is None or found is None:
        return False
    try:
        violating = {
            item["receipt_id"]
            for item in found["matched"]
            if isinstance(item, dict) and item["violates_policy"] is True
        }
        clean = {
            item["receipt_id"]
            for item in found["matched"]
            if isinstance(item, dict) and item["violates_policy"] is False
        }
    except (KeyError, TypeError):
        return False
    headings = [heading for heading, _ in sections]
    if len(headings) != len(set(headings)):
        return False
    named = set(headings)
    if not violating <= named:
        return False
    if len(named - violating) != 1:
        return False
    if not (named - violating) <= clean:
        return False
    if headings[:len(violating)] != sorted(violating):
        return False
    return all(any(line.strip() for line in body) for _, body in sections)


def policy_notes_names_the_governing_limit(workspace: Path) -> bool:
    sections = _policy_sections(workspace)
    limits = _policy_limits(workspace)
    receipts = _input_receipts(workspace)
    if sections is None or limits is None or receipts is None:
        return False
    by_id = {r["receipt_id"]: r for r in receipts}
    for heading, body in sections:
        receipt = by_id.get(heading)
        if receipt is None:
            return False
        text = "\n".join(body)
        numbers = {int(n) for n in CENTS.findall(text)}
        total = receipt["total_cents"]
        category = receipt["category"]
        if not numbers.issuperset({total}):
            return False
        cap = limits.get(category)
        if cap is None:
            if numbers & set(limits.values()):
                return False
            continue
        if cap not in numbers:
            return False
        if numbers & (set(limits.values()) - {cap}):
            return False
    return True


def _parse_sections(workspace: Path) -> list[tuple[str, list[str]]] | None:
    try:
        text = (workspace / "out" / "parse-notes.md").read_text(encoding="utf-8")
    except OSError:
        return None
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        found = RECEIPT_HEADING.match(line)
        if found:
            sections.append((found.group(1), []))
        elif sections:
            sections[-1][1].append(line)
    return sections


def _receipt_hazards(workspace: Path) -> dict[str, tuple[bool, list[str], str]] | None:
    """Per receipt_id: whether its VENDOR value carries a colon, its non-field
    lines, and its correct VENDOR value under the first-colon-only rule.

    Recomputed from the shipped receipt bytes by the rule the statement states,
    so no expected value is read out of the prose being graded.
    """
    names = {"RECEIPT-ID", "DATE", "VENDOR", "TOTAL", "CATEGORY"}
    try:
        paths = sorted((workspace / "inputs" / "receipts").glob("*.txt"))
    except OSError:
        return None
    out: dict[str, tuple[bool, list[str], str]] = {}
    for path in paths:
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            return None
        found: dict[str, str] = {}
        extra: list[str] = []
        vendor_colon = False
        for line in body.splitlines():
            name, sep, value = line.partition(":")
            if sep and name.strip() in names:
                found[name.strip()] = value.strip()
                if name.strip() == "VENDOR" and ":" in value:
                    vendor_colon = True
            elif line.strip():
                extra.append(line.strip())
        try:
            out[found["RECEIPT-ID"]] = (vendor_colon, extra, found["VENDOR"])
        except KeyError:
            return None
    return out


def parse_notes_covers_every_hazard(workspace: Path) -> bool:
    sections = _parse_sections(workspace)
    hazards = _receipt_hazards(workspace)
    if sections is None or hazards is None:
        return False
    expected = sorted(
        receipt_id
        for receipt_id, (vendor_colon, extra, _) in hazards.items()
        if vendor_colon or extra
    )
    if not expected:
        return False
    headings = [heading for heading, _ in sections]
    if headings != expected:
        return False
    return all(any(line.strip() for line in body) for _, body in sections)


def parse_notes_quotes_the_correct_value(workspace: Path) -> bool:
    sections = _parse_sections(workspace)
    hazards = _receipt_hazards(workspace)
    if sections is None or hazards is None:
        return False
    for heading, body in sections:
        entry = hazards.get(heading)
        if entry is None:
            return False
        vendor_colon, extra, vendor = entry
        text = "\n".join(body)
        if vendor not in text:
            return False
        if vendor_colon:
            truncated = vendor.split(":")[0].strip()
            if truncated == vendor or truncated not in text:
                return False
        else:
            if not all(line in text for line in extra):
                return False
    return True


CONVENTION_HEADING = re.compile(r"\A##[ \t]+(collapse|divergence)[ \t]*\Z")
TXN_IDENTIFIER = re.compile(r"\bTXN-[0-9]+\b")


def _convention_sections(workspace: Path) -> list[tuple[str, list[str]]] | None:
    try:
        text = (workspace / "out" / "convention-notes.md").read_text(encoding="utf-8")
    except OSError:
        return None
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        found = CONVENTION_HEADING.match(line)
        if found:
            sections.append((found.group(1), []))
        elif sections:
            sections[-1][1].append(line)
    return sections


def _convention_partition(workspace: Path) -> tuple[list[str], list[str]] | None:
    """The diverging and coinciding txn_id lists under the two comparison rules.

    Recomputed from workspace/inputs by building the eligibility graph twice,
    once against the absolute value of each row amount as the statement's rule
    requires and once against the signed amount, so neither list is read from
    the prose being graded.
    """
    from datetime import date as _date

    rows = _input_statement_rows(workspace)
    receipts = _input_receipts(workspace)
    if rows is None or receipts is None:
        return None
    try:
        policy = json.loads((workspace / "inputs" / "policy.json").read_text(encoding="utf-8"))
        window = policy["match_window_days"]
        tolerance = policy["amount_tolerance_cents"]
    except (OSError, json.JSONDecodeError, KeyError):
        return None

    absolute: dict[str, set[str]] = {row["txn_id"]: set() for row in rows}
    signed: dict[str, set[str]] = {row["txn_id"]: set() for row in rows}
    for receipt in receipts:
        try:
            receipt_date = _date.fromisoformat(receipt["date"])
        except ValueError:
            return None
        for row in rows:
            try:
                posted = _date.fromisoformat(row["posted_date"])
            except ValueError:
                return None
            if abs((receipt_date - posted).days) > window:
                continue
            amount = row["amount_cents"]
            if abs(receipt["total_cents"] - abs(amount)) <= tolerance:
                absolute[row["txn_id"]].add(receipt["receipt_id"])
            if abs(receipt["total_cents"] - amount) <= tolerance:
                signed[row["txn_id"]].add(receipt["receipt_id"])

    diverging = sorted(t for t in absolute if absolute[t] != signed[t])
    coinciding = sorted(t for t in absolute if absolute[t] == signed[t])
    return diverging, coinciding


def convention_notes_carries_both_sections(workspace: Path) -> bool:
    sections = _convention_sections(workspace)
    if sections is None:
        return False
    if [heading for heading, _ in sections] != ["collapse", "divergence"]:
        return False
    return all(any(line.strip() for line in body) for _, body in sections)


def convention_notes_names_every_row(workspace: Path) -> bool:
    sections = _convention_sections(workspace)
    partition = _convention_partition(workspace)
    if sections is None or partition is None:
        return False
    diverging, coinciding = partition
    if not diverging or not coinciding:
        return False
    bodies = {heading: "\n".join(body) for heading, body in sections}
    text = bodies.get("divergence")
    if text is None:
        return False
    named = set(TXN_IDENTIFIER.findall(text))
    return named.issuperset(set(diverging) | set(coinciding))


# --- rendered surface ------------------------------------------------------
# Every checker below measures the application the verifier serves and dials,
# never a capture of it. Harbor starts the separate verifier in its own compose
# project after stopping the agent's, so the application is brought up here
# from the uploaded deliverables and read over this project's own loopback at
# APP_PUBLIC_URL. Grading what the agent wrote about its own rendering would be
# the interface-only grading this project's README names as a measured failure
# mode, which is why the capture substrate was replaced rather than defended.

APP_URL = os.environ.get("APP_PUBLIC_URL", "http://127.0.0.1:8080").rstrip("/")
HEALTH_DEADLINE_SECONDS = int(os.environ.get("HEALTH_DEADLINE_SECONDS", "120"))


def _browser():
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def _app_ready() -> bool:
    """True once the served application answers, within the health deadline.

    A served page is the evidence every rendered-surface checker reads, so an
    application that never answers must fail those checkers rather than be
    read as vacuously satisfying them.
    """
    deadline = time.monotonic() + HEALTH_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{APP_URL}/", timeout=10) as handle:
                if handle.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _probe(workspace: Path, routes: list[str], script: str):
    """Dial each route on the served application and evaluate one script in it.

    Returns a list of per-route results, or None when the application never
    answers, a route does not return 200, or the browser cannot be driven, so a
    caller never reads a partial list as a pass.
    """
    if not routes or not _app_ready():
        return None
    viewport = _fixtures()["rendered_state"]["viewport"]
    try:
        with _browser() as play:
            browser = play.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page(
                    viewport={"width": viewport["width"], "height": viewport["height"]}
                )
                out = []
                for route in routes:
                    response = page.goto(f"{APP_URL}{route}")
                    if response is None or response.status != 200:
                        return None
                    out.append(page.evaluate(script))
                return out
            finally:
                browser.close()
    except Exception:
        return None


def _detail_routes(workspace: Path) -> list[str] | None:
    found = _reconciliation(workspace)
    if found is None or not isinstance(found.get("matched"), list):
        return None
    routes = []
    for item in found["matched"]:
        if not isinstance(item, dict) or not isinstance(item.get("receipt_id"), str):
            return None
        routes.append(f"/pair/{item['receipt_id']}")
    return routes


REVIEW_SCRIPT = """() => {
  const ids = ["matched-table", "unreceipted-list", "unposted-list",
               "totals-matched", "totals-unreceipted", "totals-unposted",
               "totals-violations"];
  const missing = ids.filter((id) => document.getElementById(id) === null);
  const table = document.getElementById("matched-table");
  const rows = table ? Array.from(table.querySelectorAll("tbody tr")) : [];
  const read = (row) => {
    const cells = Array.from(row.children);
    const state = row.querySelector(".state");
    const cs = state ? getComputedStyle(state) : null;
    return {
      receiptAttr: row.getAttribute("data-receipt-id"),
      cells: cells.map((c) => c.textContent.trim()),
      cellCount: cells.length,
      lefts: cells.map((c) => c.getBoundingClientRect().left),
      weight: cs ? cs.fontWeight : null,
      fontsOK: cs ? document.fonts.check(cs.fontWeight + " " + cs.fontSize + " " + cs.fontFamily) : false,
    };
  };
  const totals = {};
  for (const id of ids.slice(3)) {
    const el = document.getElementById(id);
    totals[id] = el ? el.textContent.trim() : null;
  }
  const listOf = (id) => {
    const el = document.getElementById(id);
    return el ? Array.from(el.children).map((c) => c.textContent.trim()) : null;
  };
  return {
    missing: missing,
    rows: rows.map(read),
    totals: totals,
    unreceipted: listOf("unreceipted-list"),
    unposted: listOf("unposted-list"),
  };
}"""

DETAIL_SCRIPT = """() => {
  const ids = ["pair-receipt-id", "pair-txn-id", "pair-amount-delta",
               "pair-date-delta", "pair-category", "pair-policy-state"];
  const out = {missing: [], values: {}, weight: null, fontsOK: true};
  for (const id of ids) {
    const el = document.getElementById(id);
    if (el === null) { out.missing.push(id); continue; }
    out.values[id] = el.textContent.trim();
    const cs = getComputedStyle(el);
    if (!document.fonts.check(cs.fontWeight + " " + cs.fontSize + " " + cs.fontFamily)) {
      out.fontsOK = false;
    }
    if (id === "pair-policy-state") { out.weight = cs.fontWeight; }
  }
  return out;
}"""


def review_screen_structure(workspace: Path) -> bool:
    found = _reconciliation(workspace)
    if found is None or not isinstance(found.get("matched"), list):
        return False
    probed = _probe(workspace, ["/"], REVIEW_SCRIPT)
    if probed is None:
        return False
    screen = probed[0]
    if screen["missing"] or len(screen["rows"]) != len(found["matched"]):
        return False
    state = _fixtures()["rendered_state"]
    want_violation = str(state["violation_weight"])
    by_id = {
        item["receipt_id"]: item
        for item in found["matched"]
        if isinstance(item, dict) and isinstance(item.get("receipt_id"), str)
    }
    for row in screen["rows"]:
        if row["cellCount"] != 6 or row["receiptAttr"] not in by_id:
            return False
        if not row["fontsOK"]:
            return False
        violating = by_id[row["receiptAttr"]].get("violates_policy") is True
        if violating and row["weight"] != want_violation:
            return False
        if not violating and row["weight"] == want_violation:
            return False
    return True


def review_screen_rows_sorted(workspace: Path) -> bool:
    probed = _probe(workspace, ["/"], REVIEW_SCRIPT)
    if probed is None:
        return False
    ids = [row["receiptAttr"] for row in probed[0]["rows"]]
    if not ids or not all(isinstance(value, str) for value in ids):
        return False
    return ids == sorted(ids)


def review_screen_columns_aligned(workspace: Path) -> bool:
    probed = _probe(workspace, ["/"], REVIEW_SCRIPT)
    if probed is None:
        return False
    rows = probed[0]["rows"]
    if len(rows) < 2 or any(row["cellCount"] != 6 for row in rows):
        return False
    tolerance = _fixtures()["rendered_state"]["column_alignment_tolerance_px"]
    for column in range(6):
        edges = [row["lefts"][column] for row in rows]
        if max(edges) - min(edges) > tolerance:
            return False
    return True


def served_screen_agrees_with_reconciliation_json(workspace: Path) -> bool:
    """The silent served-divergence mutation's checker.

    An application whose review screen renders a reconciliation other than the
    one its own JSON deliverable declares is rejected here, exactly as a stale
    dump is rejected by its own checker. Read from the served response rather
    than from any file the agent wrote about its rendering.
    """
    found = _reconciliation(workspace)
    probed = _probe(workspace, ["/"], REVIEW_SCRIPT)
    if found is None or probed is None or not isinstance(found.get("matched"), list):
        return False
    screen = probed[0]
    if len(screen["rows"]) != len(found["matched"]):
        return False

    rendered = set()
    for row in screen["rows"]:
        cells = row["cells"]
        if len(cells) != 6:
            return False
        try:
            amount, days = int(cells[2]), int(cells[3])
        except ValueError:
            return False
        rendered.add((cells[0], cells[1], amount, days, cells[4], cells[5]))

    declared = {
        (
            item.get("receipt_id"),
            item.get("txn_id"),
            item.get("amount_delta_cents"),
            item.get("date_delta_days"),
            item.get("category"),
            "violation" if item.get("violates_policy") else "compliant",
        )
        for item in found["matched"]
        if isinstance(item, dict)
    }
    if rendered != declared:
        return False

    totals = found.get("totals") or {}
    pairs = (
        ("totals-matched", "matched_count"),
        ("totals-unreceipted", "unreceipted_count"),
        ("totals-unposted", "unposted_count"),
        ("totals-violations", "violation_count"),
    )
    for element, key in pairs:
        if screen["totals"].get(element) != str(totals.get(key)):
            return False
    return (
        screen["unreceipted"] == found.get("unreceipted")
        and screen["unposted"] == found.get("unposted")
    )


def detail_screens_complete(workspace: Path) -> bool:
    found = _reconciliation(workspace)
    routes = _detail_routes(workspace)
    if found is None or routes is None or not routes:
        return False
    probed = _probe(workspace, routes, DETAIL_SCRIPT)
    if probed is None or len(probed) != len(routes):
        return False
    state = _fixtures()["rendered_state"]
    want_violation = str(state["violation_weight"])
    for item, screen in zip(found["matched"], probed, strict=True):
        if screen["missing"] or not screen["fontsOK"]:
            return False
        violating = item.get("violates_policy") is True
        if violating != (screen["weight"] == want_violation):
            return False
        expected = {
            "pair-receipt-id": str(item.get("receipt_id")),
            "pair-txn-id": str(item.get("txn_id")),
            "pair-amount-delta": str(item.get("amount_delta_cents")),
            "pair-date-delta": str(item.get("date_delta_days")),
            "pair-category": str(item.get("category")),
            "pair-policy-state": "violation" if violating else "compliant",
        }
        if screen["values"] != expected:
            return False
    return True


def served_screens_request_no_external_origin(workspace: Path) -> bool:
    """Red line: the rendered surface is completed by no external fetch.

    Observed through request interception rather than by scanning bytes for
    URL-shaped strings: the browser reports every request the page actually
    issued, including those a script composed at runtime, which a static scan
    of the served markup cannot see. Any request whose origin is not the
    application's own fails the red line.
    """
    routes = _detail_routes(workspace)
    if routes is None:
        return False
    if not _app_ready():
        return False
    viewport = _fixtures()["rendered_state"]["viewport"]
    foreign: list[str] = []
    try:
        with _browser() as play:
            browser = play.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page(
                    viewport={"width": viewport["width"], "height": viewport["height"]}
                )
                page.on(
                    "request",
                    lambda request: (
                        foreign.append(request.url)
                        if not request.url.startswith(APP_URL)
                        else None
                    ),
                )
                for route in ["/", *routes]:
                    response = page.goto(f"{APP_URL}{route}")
                    if response is None or response.status != 200:
                        return False
                    page.wait_for_load_state("load")
            finally:
                browser.close()
    except Exception:
        return False
    return not foreign
