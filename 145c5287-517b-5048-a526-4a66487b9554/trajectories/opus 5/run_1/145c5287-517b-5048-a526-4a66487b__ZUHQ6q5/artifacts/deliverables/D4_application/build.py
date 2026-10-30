"""Write the file and database deliverables from the computed reconciliation."""

import json
import os
import subprocess
import sys

import recon

OUT_DIR = "/app/out"
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


def psql(args, **kwargs):
    return subprocess.run(["psql", DSN, "-v", "ON_ERROR_STOP=1"] + args, check=True, **kwargs)


def main():
    result = recon.reconcile()
    os.makedirs(OUT_DIR, exist_ok=True)

    with open(os.path.join(OUT_DIR, "reconciliation.json"), "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")

    totals = result["totals"]
    with open(os.path.join(OUT_DIR, "summary.txt"), "w", encoding="utf-8") as handle:
        handle.write(
            "matched: {matched_count}\n"
            "unreceipted: {unreceipted_count}\n"
            "unposted: {unposted_count}\n"
            "violations: {violation_count}\n".format(**totals)
        )

    psql(["--command", DDL], stdout=subprocess.DEVNULL)
    copy_body = "".join(
        "{receipt_id}\t{txn_id}\t{amount_delta_cents}\t{date_delta_days}\t"
        "{category}\t{violates_policy}\n".format(
            **dict(row, violates_policy="t" if row["violates_policy"] else "f")
        )
        for row in result["matched"]
    )
    psql(
        ["--command", "copy reconciliation from stdin"],
        input=copy_body,
        text=True,
        stdout=subprocess.DEVNULL,
    )

    with open(os.path.join(OUT_DIR, "snapshot.sql"), "w", encoding="utf-8") as handle:
        subprocess.run(
            ["pg_dump", DSN, "--table=reconciliation", "--no-owner", "--no-acl"],
            check=True,
            stdout=handle,
        )

    print(json.dumps(totals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
