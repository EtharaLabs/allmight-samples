"""Planted defects used to validate the criteria: name -> (lever, file under solution/, oracle text, defect text, criterion that must fail).

Each oracle text appears exactly once in the named file, so a defect is applied
by replacing that one string in a copy of the oracle and nothing else. The
criterion named last is the one that must flip to False; every other criterion
is expected to stay True, which is what makes the defect evidence that the
named criterion is live rather than evidence that the bundle broke.

Two things govern where a defect can usefully go.

Reach: answer_from_inputs, esc, render_site and serve_forever are copied into
the agent-visible /app/app.py by _app_module, so a defect inside
answer_from_inputs moves the JSON and the served screens together and the
served-agreement criterion still passes. Only a defect confined to render_site,
to STYLE, or to the writers in main makes the screens and the JSON disagree.

Survivability: a defect must leave a bundle that still runs to completion. A
defect that makes psql reject the copy stream, or that makes solver.py fail to
parse, fails every criterion at once and so measures nothing. That is why the
table-shape defect narrows the dump in solve.sh rather than dropping a column
from DDL, and why the corpus defect appends a byte that does not change the
parse rather than truncating policy.json.

The solve.sh defect carries no inline comment because its oracle text sits
inside a backslash-continued psql invocation, where a comment line would break
the command.

Run ``python3 mutants.py check`` to confirm every entry is still applicable
against the current bundle, ``list`` to print the catalogue, and
``apply <name> --into <dir>`` to materialise one mutated copy for a trial.
"""
import argparse
import ast
import shutil
import subprocess
import sys
from pathlib import Path

MUTANTS = {
    # ---- the matching core (shared by the JSON and the served screens)
    "greedy-smallest-matching": ("matching (cardinality)", "solver.py",
        "\n    for size in range(len(keys), 0, -1):",
        "\n    for size in range(1, len(keys) + 1):  # BUG: takes the smallest feasible set, not the maximum",
        "matched_set_exact"),
    "tolerance-boundary-exclusive": ("eligibility boundary", "solver.py",
        "\n            if abs(amount_delta) <= tolerance and abs(date_delta) <= window:",
        "\n            if abs(amount_delta) < tolerance and abs(date_delta) < window:  # BUG: a pair exactly at the tolerance or the window is dropped",
        "matched_set_exact"),
    "tie-break-nondeterministic": ("matching (tie-break)", "solver.py",
        "\n                        sorted(p[0] for p in combo),",
        "\n                        # BUG: the receipt-id tie-break is gone, so two equal-cost matchings resolve arbitrarily",
        "matched_set_exact"),
    "matched-not-sorted": ("output ordering", "solver.py",
        "            pairs = sorted(",
        "            pairs = list(  # BUG: pairs keep combination order rather than receipt-id order",
        "matched_sorted_by_receipt_id"),
    "violation-at-limit": ("policy comparison", "solver.py",
        '                and receipt["total_cents"] > limits[category],',
        '                and receipt["total_cents"] >= limits[category],  # BUG: a total exactly at the limit is flagged as a violation',
        "violation_flags_exact"),
    "unreceipted-lists-every-row": ("residual sets", "solver.py",
        '    unreceipted = sorted(r["txn_id"] for r in rows if r["txn_id"] not in posted)',
        '    unreceipted = sorted(r["txn_id"] for r in rows)  # BUG: matched rows are listed as unreceipted too',
        "unreceipted_set_exact"),
    "unposted-lists-every-receipt": ("residual sets", "solver.py",
        '    unposted = sorted(r["receipt_id"] for r in receipts if r["receipt_id"] not in receipted)',
        '    unposted = sorted(r["receipt_id"] for r in receipts)  # BUG: matched receipts are listed as unposted too',
        "unposted_set_exact"),
    "violation-count-inverted": ("totals", "solver.py",
        '    violations = sum(1 for item in matched if item["violates_policy"])',
        '    violations = sum(1 for item in matched if not item["violates_policy"])  # BUG: counts the compliant pairs instead',
        "totals_consistent"),

    # ---- the deliverable writers in main (the JSON side only)
    "json-omits-totals": ("deliverable shape", "solver.py",
        '            {key: result[key] for key in ("matched", "unreceipted", "unposted", "totals")},',
        '            {key: result[key] for key in ("matched", "unreceipted", "unposted")},  # BUG: the totals object never reaches the JSON, so the root key set is short',
        "reconciliation_json_wellformed"),
    "summary-overstates-matched": ("cross-file consistency", "solver.py",
        '        f"matched: {len(matched)}\\n"',
        '        f"matched: {len(matched) + 1}\\n"  # BUG: summary.txt disagrees with reconciliation.json',
        "summary_matches_json"),
    "snapshot-flags-all-compliant": ("table fidelity", "solver.py",
        '                "t" if item["violates_policy"] else "f",',
        '                "f",  # BUG: every row is written compliant into the table',
        "snapshot_agrees_with_reconciliation_json"),
    "snapshot-dump-narrowed": ("table shape", "solve.sh",
        '  --command "copy reconciliation to stdout" \\',
        '  --command "copy (select receipt_id, txn_id from reconciliation) to stdout" \\',
        "snapshot_table_shape"),
    "corpus-byte-appended": ("corpus immutability", "solver.py",
        "    OUT.mkdir(parents=True, exist_ok=True)",
        '    OUT.mkdir(parents=True, exist_ok=True)\n'
        '    _r = INPUTS / "receipts" / "rcp-0001.txt"\n'
        '    _r.write_text(_r.read_text(encoding="utf-8") + "\\n", encoding="utf-8")  # BUG: appends a byte to the shipped corpus, changing its digest without changing the parse',
        "inputs_unmodified"),

    # ---- the served surface (render_site and STYLE: screens only, so they
    #      diverge from a JSON that stays correct)
    "screen-hides-violations": ("served/JSON divergence", "solver.py",
        "    rows = []\n    for item in matched:",
        '    rows = []\n    for item in [m for m in matched if not m["violates_policy"]]:  # BUG: violation rows never render, so the screen contradicts the JSON',
        "served_screen_agrees_with_reconciliation_json"),
    "review-rows-reversed": ("row ordering", "solver.py",
        "        *rows,",
        "        *rows[::-1],  # BUG: the table renders in reverse receipt order",
        "review_screen_rows_sorted"),
    "table-layout-auto": ("column alignment", "solver.py",
        "table { table-layout: fixed; border-collapse: collapse; width: 1040px; }",
        "table { border-collapse: collapse; width: 1040px; }",
        "review_screen_columns_aligned"),
    "stylesheet-from-another-origin": ("self-containment", "solver.py",
        'body { font-family: "Liberation Sans", sans-serif; font-size: 16px; margin: 24px; }',
        '@import url("https://fonts.googleapis.com/css2?family=Inter");\nbody { font-family: Inter, sans-serif; font-size: 16px; margin: 24px; }',
        "served_screens_request_no_external_origin"),
    "detail-screen-drops-category": ("detail completeness", "solver.py",
        '            f\'  <p id="pair-category">{esc(item["category"])}</p>\',',
        "            # BUG: the category field never reaches the detail screen",
        "detail_screens_complete"),

    # ---- the written accounts
    "contested-never-names-the-rival": ("contested prose", "solver.py",
        '                f"{rival}, whose edge to this row sits closer at "',
        '                f"another receipt, whose edge to this row sits closer at "  # BUG: the alternative is never named',
        "contested_account_names_an_alternative"),
    "policy-note-skips-violations": ("policy coverage", "solver.py",
        "    for item in violating + clean[:1]:",
        "    for item in clean[:1]:  # BUG: no violating pair gets a section at all",
        "policy_notes_covers_every_violation"),
    "policy-note-omits-the-numbers": ("policy prose", "solver.py",
        '                f"{total} strictly exceeds {cap}, by {total - cap} cents, so "',
        '                f"this receipt exceeds its limit, so "  # BUG: neither number nor the difference is stated',
        "policy_notes_names_the_governing_limit"),
    "parse-note-skips-colon-receipts": ("parse-hazard coverage", "solver.py",
        "        if vendor_colon or extra:",
        "        if extra:  # BUG: a colon-in-vendor receipt is never recorded as a hazard",
        "parse_notes_covers_every_hazard"),
    "parse-note-quotes-the-wrong-vendor": ("quoted value", "solver.py",
        '                f"colon only, so the vendor is {vendor!r}.",',
        '                f"colon only, so the vendor is {truncated!r}.",  # BUG: quotes the wrong-reading value as the correct one',
        "parse_notes_quotes_the_correct_value"),
    "convention-note-drops-divergence": ("required sections", "solver.py",
        '        "## divergence",',
        '        "## rows",  # BUG: the divergence heading the criterion requires is gone',
        "convention_notes_carries_both_sections"),
    "convention-note-names-no-rows": ("row naming", "solver.py",
        "        f\"{', '.join(diverging) or 'none'}.\",",
        '        "the debit rows.",  # BUG: names the group instead of every txn_id',
        "convention_notes_names_every_row"),

    # ---- the two structure criteria
    "review-screen-id-renamed": ("screen structure", "solver.py",
        '        \'  <table id="matched-table"><thead><tr>\',',
        '        \'  <table id="matched"><thead><tr>\',  # BUG: the id the structure criterion selects on is renamed',
        "review_screen_structure"),
    "contested-writes-one-section": ("contested structure", "solver.py",
        "    chosen = contended[:1] + [p[0] for p in pulled][:1] + unmatched[:1]",
        "    chosen = contended[:1]  # BUG: one of the three required sections is written instead of three",
        "contested_account_structure"),
}

VALIDATION_DIR = Path(__file__).resolve().parent
SOLUTION_DIR = VALIDATION_DIR.parent
TASK_DIR = SOLUTION_DIR.parent
CHECKS_PY = TASK_DIR / "tests" / "checks.py"


def criterion_names():
    tree = ast.parse(CHECKS_PY.read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def patched_source(source, oracle, defect):
    return source.replace(oracle, defect, 1)


def syntax_error(path, source):
    if path.suffix == ".py":
        try:
            ast.parse(source)
        except SyntaxError as exc:
            return "{} at line {}".format(exc.msg, exc.lineno)
        return ""
    if path.suffix == ".sh":
        done = subprocess.run(
            ["bash", "-n", "-"], input=source, text=True, capture_output=True
        )
        return "" if done.returncode == 0 else done.stderr.strip()
    return ""


def check():
    criteria = criterion_names()
    problems = []
    for name, (_lever, relative, oracle, defect, criterion) in sorted(MUTANTS.items()):
        target = SOLUTION_DIR / relative
        if not target.is_file():
            problems.append("{}: {} is not in the bundle".format(name, relative))
            continue
        source = target.read_text(encoding="utf-8")
        found = source.count(oracle)
        if found != 1:
            problems.append(
                "{}: replaced text occurs {} times in {}, expected exactly 1".format(
                    name, found, relative
                )
            )
            continue
        if criterion not in criteria:
            problems.append(
                "{}: {} is not defined in tests/checks.py".format(name, criterion)
            )
        detail = syntax_error(target, patched_source(source, oracle, defect))
        if detail:
            problems.append(
                "{}: patched {} does not parse: {}".format(name, relative, detail)
            )
    return problems


def apply_one(name, destination):
    _lever, relative, oracle, defect, _criterion = MUTANTS[name]
    if destination.exists():
        raise SystemExit("{} already exists; choose an empty path".format(destination))
    shutil.copytree(
        SOLUTION_DIR, destination, ignore=shutil.ignore_patterns("validation")
    )
    target = destination / relative
    source = target.read_text(encoding="utf-8")
    if source.count(oracle) != 1:
        raise SystemExit(
            "{}: replaced text is no longer unique in {}".format(name, relative)
        )
    target.write_text(patched_source(source, oracle, defect), encoding="utf-8")
    return target


def main():
    parser = argparse.ArgumentParser(description="Planted-defect catalogue for ledgerline.")
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    sub.add_parser("list", help="print the catalogue")
    sub.add_parser("check", help="verify every entry is still applicable")
    applier = sub.add_parser("apply", help="write one mutated copy of solution/")
    applier.add_argument("name", choices=sorted(MUTANTS))
    applier.add_argument("--into", required=True, type=Path)
    args = parser.parse_args()

    if args.command == "list":
        width = max(len(name) for name in MUTANTS)
        for name, (lever, relative, _o, _d, criterion) in sorted(MUTANTS.items()):
            print(
                "{:<{w}}  {:<10}  {:<28}  {}".format(
                    name, relative, lever, criterion, w=width
                )
            )
        print("\n{} defects".format(len(MUTANTS)))
        return 0

    if args.command == "check":
        problems = check()
        for problem in problems:
            print("FAIL  {}".format(problem))
        print(
            "\n{} of {} entries applicable".format(
                len(MUTANTS) - len(problems), len(MUTANTS)
            )
        )
        return 1 if problems else 0

    print("wrote {}".format(apply_one(args.name, args.into)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
