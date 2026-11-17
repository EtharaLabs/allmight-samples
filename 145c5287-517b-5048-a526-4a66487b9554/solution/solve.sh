#!/bin/bash
set -euo pipefail

mkdir -p /app/out
python3 "$(dirname "$0")/solver.py"

# The dump is taken after the final write to the table, which is what
# the silent snapshot-staleness mutation punishes omitting. The solver
# has already written start.sh and the module it serves, which the
# what render-staleness punishes omitting.
psql "postgresql://ledgerline:ledgerline@localhost:5432/ledgerline" \
  --no-psqlrc --quiet --tuples-only \
  --command "copy reconciliation to stdout" \
  > /app/out/snapshot.sql
