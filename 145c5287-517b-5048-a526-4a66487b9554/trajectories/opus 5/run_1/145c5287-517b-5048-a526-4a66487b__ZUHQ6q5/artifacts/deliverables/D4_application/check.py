"""Cross-check every deliverable against every other one.

Run with the application serving on 127.0.0.1:8080:  python3 check.py
"""

import json
import re
import subprocess
import time
import urllib.request

BASE = "http://127.0.0.1:8080"
DSN = "postgresql://ledgerline:ledgerline@localhost:5432/ledgerline"
COLUMNS = (
    "receipt_id",
    "txn_id",
    "amount_delta_cents",
    "date_delta_days",
    "category",
    "violates_policy",
)


def fetch(path, tries=40):
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(BASE + path) as response:
                return response.read().decode()
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(0.5)


def main():
    result = json.load(open("/app/out/reconciliation.json", encoding="utf-8"))
    matched, totals = result["matched"], result["totals"]

    assert matched == sorted(matched, key=lambda r: r["receipt_id"])
    assert result["unreceipted"] == sorted(result["unreceipted"])
    assert result["unposted"] == sorted(result["unposted"])
    assert all(set(row) == set(COLUMNS) for row in matched)
    assert len({r["receipt_id"] for r in matched}) == len(matched)
    assert len({r["txn_id"] for r in matched}) == len(matched)
    assert totals["matched_count"] == len(matched)
    assert totals["unreceipted_count"] == len(result["unreceipted"])
    assert totals["unposted_count"] == len(result["unposted"])
    assert totals["violation_count"] == sum(r["violates_policy"] for r in matched)
    print("1. reconciliation.json: internally consistent")

    expected = "matched: {matched_count}\nunreceipted: {unreceipted_count}\n" \
               "unposted: {unposted_count}\nviolations: {violation_count}\n".format(**totals)
    assert open("/app/out/summary.txt", encoding="utf-8").read() == expected
    print("3. summary.txt: agrees with the totals")

    query = "select {0} from reconciliation order by receipt_id".format(", ".join(COLUMNS))
    raw = subprocess.run(
        ["psql", DSN, "-At", "-F", "|", "-c", query],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    table = [
        dict(zip(COLUMNS, (a, b, int(c), int(d), e, f == "t")))
        for a, b, c, d, e, f in (line.split("|") for line in raw)
    ]
    assert table == matched
    print("2. reconciliation table: agrees with the matched array")

    snapshot = open("/app/out/snapshot.sql", encoding="utf-8").read()
    for row in matched:
        line = "{receipt_id}\t{txn_id}\t{amount_delta_cents}\t{date_delta_days}\t" \
               "{category}\t{flag}".format(flag="t" if row["violates_policy"] else "f", **row)
        assert line in snapshot, line
    assert snapshot.count("RCP-") == len(matched)
    print("4. snapshot.sql: fresh, carries every row and no others")

    page = fetch("/")
    rows = re.findall(r'<tr data-receipt-id="([^"]+)">(.*?)</tr>', page)
    assert [r[0] for r in rows] == [r["receipt_id"] for r in matched]
    for (receipt_id, markup), row in zip(rows, matched):
        cells = re.findall(r"(<td[^>]*>.*?</td>)", markup)
        assert len(cells) == 6, cells
        text = [re.sub(r"<[^>]+>", "", cell) for cell in cells]
        assert text[:5] == [
            row["receipt_id"], row["txn_id"], str(row["amount_delta_cents"]),
            str(row["date_delta_days"]), row["category"],
        ], text
        assert ("VIOLATION" in text[5]) == row["violates_policy"]
        assert ("policy-bad" if row["violates_policy"] else "policy-ok") in cells[5]
        assert '/pair/{0}"'.format(row["receipt_id"]) in page
    for element, value in (
        ("totals-matched", totals["matched_count"]),
        ("totals-unreceipted", totals["unreceipted_count"]),
        ("totals-unposted", totals["unposted_count"]),
        ("totals-violations", totals["violation_count"]),
    ):
        assert 'id="{0}">{1}<'.format(element, value) in page, element
    for element, values in (
        ("unreceipted-list", result["unreceipted"]),
        ("unposted-list", result["unposted"]),
    ):
        segment = page[page.index('id="{0}"'.format(element)):]
        segment = segment[: segment.index("</ul>")]
        assert re.findall(r"<li>([^<]+)</li>", segment) == values, element
    assert not [c for c in page if ord(c) > 127]
    assert not [
        url for url in re.findall(r'(?:src|href)="([^"]*)"', page)
        if not url.startswith("/pair/")
    ]
    print("5. review screen: agrees with the JSON, all ASCII, no external asset")

    for row in matched:
        detail = fetch("/pair/" + row["receipt_id"])
        read = lambda name: re.search(r'id="{0}"[^>]*>([^<]*)<'.format(name), detail).group(1)
        assert [
            read("pair-receipt-id"), read("pair-txn-id"), read("pair-amount-delta"),
            read("pair-date-delta"), read("pair-category"),
        ] == [
            row["receipt_id"], row["txn_id"], str(row["amount_delta_cents"]),
            str(row["date_delta_days"]), row["category"],
        ]
        assert ("VIOLATION" in read("pair-policy-state")) == row["violates_policy"]
    print("5. {0} detail screens: agree with the JSON".format(len(matched)))

    headings = {
        "contested.md": ["TXN-1009", "TXN-1007", "TXN-1011"],
        "policy-notes.md": ["RCP-0005", "RCP-0008", "RCP-0011", "RCP-0001"],
        "parse-notes.md": ["RCP-0003", "RCP-0004", "RCP-0007"],
        "convention-notes.md": ["collapse", "divergence"],
    }
    violators = [r["receipt_id"] for r in matched if r["violates_policy"]]
    assert headings["policy-notes.md"][:-1] == violators
    assert headings["policy-notes.md"][-1] in {
        r["receipt_id"] for r in matched if not r["violates_policy"]
    }
    for name, expected_heads in headings.items():
        body = open("/app/out/" + name, encoding="utf-8").read()
        assert re.findall(r"^## (.+)$", body, re.M) == expected_heads, name
        # prose, not a table and not JSON (bare | inside prose is fine: it is
        # absolute-value notation, not a table rule)
        assert not re.search(r"^\s*\|", body, re.M), name
        assert not re.search(r"^\s*[-: ]*\|[-: |]*$", body, re.M), name
        assert not body.lstrip().startswith("{"), name
        print("{0}: sections {1}".format(name, expected_heads))

    print("\nall nine deliverables agree")


if __name__ == "__main__":
    main()
