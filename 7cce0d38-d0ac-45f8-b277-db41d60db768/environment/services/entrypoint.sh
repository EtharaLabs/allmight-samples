#!/usr/bin/env bash
# Container entrypoint: bring the services up (as uid 1001), then hand over to the container command.
set -euo pipefail
/opt/svc/services/up.sh
exec "$@"
