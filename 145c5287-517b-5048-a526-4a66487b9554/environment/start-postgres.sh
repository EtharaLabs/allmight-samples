#!/bin/bash
set -euo pipefail

"$PGBIN/pg_ctl" -D "$PGDATA" -o "-c listen_addresses=localhost -p 5432" -w start

for _ in $(seq 1 60); do
  if "$PGBIN/pg_isready" --host=localhost --port=5432 --username=ledgerline --quiet; then
    break
  fi
  sleep 1
done

"$PGBIN/pg_isready" --host=localhost --port=5432 --username=ledgerline --quiet

exec "$@"
