#!/usr/bin/env bash
# Service bring-up, shared by every environment that runs these services.
#
# Starts, as the unprivileged service user (uid 1001, "svc"):
#   * a fresh PostgreSQL 15 cluster seeded from $SERVICES_DIR/db/init/*.sql (database and role $SLUG)
#   * the SMTP mail sink (kit services/mailsink)
#   * the Stripe-compatible payments mock when $SERVICES_DIR/payments/mock.py exists and PAYMENTS != 0
# then writes $SERVICES_FILE (one NAME=value per line) with the connection details plus every line of
# $SERVICES_DIR/extra.env (task-specific settings such as TTLs).
#
# Environment (all optional): SLUG, SERVICES_DIR, SERVICES_FILE, SVC_STATE, SVC_RUN, APP_PORT, DB_PORT,
# SMTP_PORT, MAILSINK_HTTP_PORT, PAYMENTS_PORT, ADMIN_SOCKET_DIR (when set, admin surfaces move to Unix sockets),
# EXTRA_ENV_OVERRIDE (a file whose lines replace extra.env values).
set -euo pipefail

SLUG="${SLUG:-app}"
SERVICES_DIR="${SERVICES_DIR:-/opt/svc/services}"
SERVICES_FILE="${SERVICES_FILE:-/etc/$SLUG/services.env}"
SVC_STATE="${SVC_STATE:-/var/lib/svc}"
SVC_RUN="${SVC_RUN:-/run/svc}"
APP_PORT="${APP_PORT:-8080}"
DB_PORT="${DB_PORT:-5432}"
SMTP_PORT="${SMTP_PORT:-1025}"
MAILSINK_HTTP_PORT="${MAILSINK_HTTP_PORT:-8025}"
PAYMENTS_PORT="${PAYMENTS_PORT:-4242}"
ADMIN_SOCKET_DIR="${ADMIN_SOCKET_DIR:-}"
PG_BIN=/usr/lib/postgresql/15/bin
SVC_USER=svc
as_svc() { if [ "$(id -u)" = 0 ]; then python3 "$SERVICES_DIR/asuser.py" "$SVC_USER" "$@"; else "$@"; fi; }

PGDATA="$SVC_STATE/postgres"
LOG_DIR="$SVC_RUN/log"
mkdir -p "$SVC_STATE" "$SVC_RUN/postgres" "$SVC_RUN/payments" "$LOG_DIR" "$(dirname "$SERVICES_FILE")"
if [ "$(id -u)" = 0 ]; then chown -R "$SVC_USER:$SVC_USER" "$SVC_STATE" "$SVC_RUN"; fi
chmod 755 "$SVC_RUN" "$SVC_RUN/payments"
DB_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 20)"
SUPER_PASSWORD="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 20)"
PWFILE="$SVC_RUN/postgres/.super"

as_svc rm -rf "$PGDATA"
as_svc mkdir -p "$PGDATA"
chmod 700 "$PGDATA"
(umask 077; printf '%s\n' "$SUPER_PASSWORD" > "$PWFILE")
[ "$(id -u)" = 0 ] && chown "$SVC_USER:$SVC_USER" "$PWFILE"
as_svc "$PG_BIN/initdb" -D "$PGDATA" --auth=scram-sha-256 --username=postgres --pwfile="$PWFILE" > "$LOG_DIR/initdb.log" 2>&1
as_svc bash -c "cat >> '$PGDATA/postgresql.conf'" <<CONF
listen_addresses = '127.0.0.1'
port = $DB_PORT
unix_socket_directories = '$SVC_RUN/postgres'
password_encryption = 'scram-sha-256'
max_connections = 200
CONF
as_svc bash -c "cat > '$PGDATA/pg_hba.conf'" <<HBA
local   all   all                 scram-sha-256
host    all   all   127.0.0.1/32  scram-sha-256
host    all   all   ::1/128       scram-sha-256
HBA
as_svc "$PG_BIN/pg_ctl" -D "$PGDATA" -l "$LOG_DIR/postgres.log" -o "-p $DB_PORT -k $SVC_RUN/postgres" -w -t 120 start >"$LOG_DIR/pg_ctl.log" 2>&1

PSQL="$PG_BIN/psql -v ON_ERROR_STOP=1 -h $SVC_RUN/postgres -p $DB_PORT -U postgres"
PGPASSWORD="$SUPER_PASSWORD" $PSQL -c "CREATE ROLE $SLUG LOGIN PASSWORD '$DB_PASSWORD'" > /dev/null
PGPASSWORD="$SUPER_PASSWORD" $PSQL -c "CREATE DATABASE $SLUG OWNER $SLUG" > /dev/null
for script in "$SERVICES_DIR"/db/init/*.sql; do
	[ -e "$script" ] || continue
	PGPASSWORD="$DB_PASSWORD" $PSQL -U "$SLUG" -d "$SLUG" -q -f "$script" > /dev/null
done
rm -f "$PWFILE"

if [ -n "$ADMIN_SOCKET_DIR" ]; then
	mkdir -p "$ADMIN_SOCKET_DIR"
	chmod 700 "$ADMIN_SOCKET_DIR"
	[ "$(id -u)" = 0 ] && chown "$SVC_USER:$SVC_USER" "$ADMIN_SOCKET_DIR"
	export PAYMENTS_ADMIN_SOCKET="$ADMIN_SOCKET_DIR/payments.sock" MAILSINK_ADMIN_SOCKET="$ADMIN_SOCKET_DIR/mailsink.sock"
	export PAYMENTS_ADMIN_LOG="$ADMIN_SOCKET_DIR/admin.log" MAILSINK_ADMIN_LOG="$ADMIN_SOCKET_DIR/admin.log"
	as_svc touch "$ADMIN_SOCKET_DIR/admin.log"
	chmod 600 "$ADMIN_SOCKET_DIR/admin.log"
fi
export PAYMENTS_PORT PAYMENTS_PUBLIC_URL="http://127.0.0.1:$PAYMENTS_PORT"
export PAYMENTS_WEBHOOK_SIGNING_FILE="$SVC_RUN/payments/webhook-signing-key"
export MAILSINK_SMTP_PORT="$SMTP_PORT" MAILSINK_HTTP_PORT
USE_PAYMENTS=0
if [ -f "$SERVICES_DIR/payments/mock.py" ] && [ "${PAYMENTS:-1}" != 0 ]; then USE_PAYMENTS=1; fi
as_svc env PATH="$PATH" MAILSINK_SMTP_PORT="$SMTP_PORT" MAILSINK_HTTP_PORT="$MAILSINK_HTTP_PORT" \
	MAILSINK_ADMIN_SOCKET="${MAILSINK_ADMIN_SOCKET:-}" MAILSINK_ADMIN_LOG="${MAILSINK_ADMIN_LOG:-}" \
	nohup python3 "$SERVICES_DIR/mailsink/sink.py" >"$LOG_DIR/mailsink.log" 2>&1 &
if [ "$USE_PAYMENTS" = 1 ]; then
	as_svc env PATH="$PATH" PAYMENTS_PORT="$PAYMENTS_PORT" PAYMENTS_PUBLIC_URL="$PAYMENTS_PUBLIC_URL" \
		PAYMENTS_WEBHOOK_SIGNING_FILE="$PAYMENTS_WEBHOOK_SIGNING_FILE" PAYMENTS_ADMIN_SOCKET="${PAYMENTS_ADMIN_SOCKET:-}" \
		PAYMENTS_ADMIN_LOG="${PAYMENTS_ADMIN_LOG:-}" nohup python3 "$SERVICES_DIR/payments/mock.py" >"$LOG_DIR/payments.log" 2>&1 &
fi

ready=0
for _ in $(seq 1 120); do
	if curl -fsS "http://127.0.0.1:$MAILSINK_HTTP_PORT/healthz" >/dev/null 2>&1 &&
		{ [ "$USE_PAYMENTS" = 0 ] || { curl -fsS "http://127.0.0.1:$PAYMENTS_PORT/healthz" >/dev/null 2>&1 && [ -s "$PAYMENTS_WEBHOOK_SIGNING_FILE" ]; }; } &&
		{ [ -z "$ADMIN_SOCKET_DIR" ] || [ -S "$ADMIN_SOCKET_DIR/mailsink.sock" ]; }; then
		ready=1
		break
	fi
	sleep 0.5
done
[ "$ready" = 1 ] || { echo "services did not become ready" >&2; exit 1; }

TMP="$SERVICES_FILE.tmp"
{
	echo "DATABASE_URL=postgresql://$SLUG:$DB_PASSWORD@127.0.0.1:$DB_PORT/$SLUG"
	echo "PORT=$APP_PORT"
	echo "PUBLIC_BASE_URL=http://127.0.0.1:$APP_PORT"
	echo "SMTP_HOST=127.0.0.1"
	echo "SMTP_PORT=$SMTP_PORT"
	if ! grep -q '^MAIL_FROM=' "$SERVICES_DIR/extra.env" 2>/dev/null; then echo "MAIL_FROM=${MAIL_FROM:-no-reply@$SLUG.example}"; fi
	if [ "$USE_PAYMENTS" = 1 ]; then
		echo "PAYMENTS_API_URL=$PAYMENTS_PUBLIC_URL"
		echo "PAYMENTS_WEBHOOK_SIGNING_FILE=$PAYMENTS_WEBHOOK_SIGNING_FILE"
	fi
	extra="$SERVICES_DIR/extra.env"
	if [ -f "$extra" ]; then
		while IFS= read -r line; do
			case "$line" in ''|'#'*) continue ;; esac
			key="${line%%=*}"
			if [ -n "${EXTRA_ENV_OVERRIDE:-}" ] && grep -q "^$key=" "$EXTRA_ENV_OVERRIDE"; then
				grep "^$key=" "$EXTRA_ENV_OVERRIDE" | tail -1
			else
				echo "$line"
			fi
		done < "$extra"
	fi
} > "$TMP"
chmod 644 "$TMP"
mv "$TMP" "$SERVICES_FILE"
