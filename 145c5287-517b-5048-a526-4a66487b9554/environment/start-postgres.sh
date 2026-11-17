#!/bin/bash
set -euo pipefail

# The image ends on root, so this script may start as uid 0, but the server
# refuses that identity outright: "root execution of the PostgreSQL server is
# not permitted". The cluster therefore runs under the identity that owns PGDATA
# and /run/postgresql, uid 10001, and only the switch happens as root. setpriv
# comes from util-linux in the base image, so this adds no dependency. When the
# container already starts as 10001 the commands run directly, which keeps this
# script working under either final USER.
as_agent() {
  if [ "$(id -u)" = 0 ]; then
    setpriv --reuid=10001 --regid=10001 --init-groups "$@"
  else
    "$@"
  fi
}

# The socket directory lives under /run, which many container runtimes mount as
# a fresh tmpfs, erasing whatever the image build put there. Recreate it on every
# boot rather than trusting the build-time copy to survive: without it the server
# aborts with 'could not create lock file "/var/run/postgresql/.s.PGSQL.5432.lock"',
# this script exits under set -e, and the container dies before exec "$@" below.
# The chown is guarded because only uid 0 may change ownership, matching as_agent.
mkdir -p /run/postgresql
if [ "$(id -u)" = 0 ]; then chown 10001:10001 /run/postgresql; fi

as_agent "$PGBIN/pg_ctl" -D "$PGDATA" -o "-c listen_addresses=localhost -p 5432" -w start

for _ in $(seq 1 60); do
  if "$PGBIN/pg_isready" --host=localhost --port=5432 --username=ledgerline --quiet; then
    break
  fi
  sleep 1
done

"$PGBIN/pg_isready" --host=localhost --port=5432 --username=ledgerline --quiet

exec "$@"
