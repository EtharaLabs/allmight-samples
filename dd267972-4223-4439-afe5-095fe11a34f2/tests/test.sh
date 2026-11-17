#!/usr/bin/env bash
# Verifier. Runs as root inside the separate verifier container.
#   1. fresh services (PostgreSQL seeded from tests/services/db/init, mail sink, payments mock if any)
#   2. baseline snapshot of the pre-existing records (tests/snapshot.py), before the submission runs
#   3. the submission's start.sh as uid 1000 with only the service variables
#   4. tests/workflows.py drives every workflow and writes /logs/verifier/results.json
#   5. Reward Kit scores functional (strict workflows, breach-capped) and judge (experience evidence)
# A failure of the verifier, its services or the judge ends this script nonzero, leaves the fail-closed zero
# reward and records why in evaluation-invalid.json; a failure of the application is evidence and is scored.
# Apart from the fail-closed zero below, only Reward Kit writes reward.json.
set -uo pipefail

FK_MODEL_KEY="${ANTHROPIC_API_KEY:-}"
unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_AUTH_TOKEN

TESTS=/tests
LOGS=/logs/verifier
WORKSPACE=/app
FIX=/run/fk-verifier
mkdir -p "$LOGS/ux"
install -d -m 700 -o root -g root "$FIX"
: >"$LOGS/test.log"
# Fail-closed (delivery QC rule 15): a zero reward exists from the start; Reward Kit replaces it only when
# scoring completes. An evaluation failure leaves it and records why in evaluation-invalid.json.
printf '{"reward": 0.0, "functional": 0.0, "judge": 0.0}\n' >"$LOGS/reward.json"
rm -f "$LOGS/evaluation-invalid.json"
T0=$(date +%s)   # the workflow budget below subtracts the time spent on start-up
exec > >(tee -a "$LOGS/test.log") 2>&1

# shellcheck disable=SC1091
. "$TESTS/task.env"   # SLUG, and optionally FK_SEED
export SLUG
export SERVICES_DIR="$TESTS/services" SERVICES_FILE="/etc/$SLUG/services.env" SVC_STATE=/var/lib/svc SVC_RUN=/run/svc
export APP_PORT=20080 DB_PORT=20032 SMTP_PORT=20025 MAILSINK_HTTP_PORT=20026 PAYMENTS_PORT=20042
export ADMIN_SOCKET_DIR=/run/svc/admin EXTRA_ENV_OVERRIDE="$TESTS/verifier-overrides.env"
export KH_PROXY_DEADLINE=600
PROXY_PORT=19555

invalid() {
	echo "verifier: evaluation failure: $*" >&2
	printf '{"invalid": true, "reason": "%s"}\n' "$(echo "$*" | tr -d '"\\')" >"$LOGS/evaluation-invalid.json"
	pkill -u app 2>/dev/null; sleep 1; pkill -9 -u app 2>/dev/null
	exit 3
}

# The reference pay engine is shared with the solution, so grading the solution against
# it cannot catch a pay rule that is wrong in both at once. This self-test can: its
# expected values are transcribed from instruction.md's own worked examples, not computed
# by the engine. It needs no services, so it runs before them.
if ( cd "$TESTS" && python3 -c 'from reference.payroll import selftest; selftest()' ) \
		>"$LOGS/reference-selftest.txt" 2>&1; then
	echo "verifier: reference payroll self-test passed"
else
	invalid "the reference payroll self-test failed; see reference-selftest.txt"
fi

echo "verifier: starting services"
"$SERVICES_DIR/up.sh" || invalid "the services did not start"
set -a
# shellcheck disable=SC1090
. "$SERVICES_FILE"
set +a
export MAILSINK_HTTP_URL="http://127.0.0.1:$MAILSINK_HTTP_PORT"
APP_VARS=$(grep -o '^[A-Z_][A-Z0-9_]*=' "$SERVICES_FILE" | tr -d '=' | tr '\n' ' ')

if [ -f "$TESTS/snapshot.py" ]; then
	python3 "$TESTS/snapshot.py" "$DATABASE_URL" "$FIX/baseline.json" || invalid "the baseline snapshot failed"
fi

app_env() { # the submission sees only the service variables
	local v out="PATH=/usr/local/bin:/usr/bin:/bin HOME=/home/app LANG=C.UTF-8"
	for v in $APP_VARS; do out="$out $v=${!v}"; done
	echo "$out"
}
cat >"$FIX/start-app.sh" <<EOF
#!/usr/bin/env bash
python3 $TESTS/services/asuser.py app env -i $(app_env) bash -c 'cd /app && exec ./start.sh' >>"$LOGS/app.log" 2>&1 &
pid=\$!
for _ in \$(seq 1 130); do
	[ "\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$PUBLIC_BASE_URL/healthz")" = 200 ] && exit 0
	kill -0 \$pid 2>/dev/null || exit 1
	sleep 0.5
done
exit 1
EOF
cat >"$FIX/restart.sh" <<EOF
#!/usr/bin/env bash
pkill -u app 2>/dev/null; sleep 2; pkill -9 -u app 2>/dev/null; sleep 1
exec "$FIX/start-app.sh"
EOF
cat >"$FIX/db-stop.sh" <<EOF
#!/usr/bin/env bash
python3 $TESTS/services/asuser.py svc /usr/lib/postgresql/15/bin/pg_ctl -D $SVC_STATE/postgres -m fast -w -t 60 stop
EOF
cat >"$FIX/db-start.sh" <<EOF
#!/usr/bin/env bash
python3 $TESTS/services/asuser.py svc /usr/lib/postgresql/15/bin/pg_ctl -D $SVC_STATE/postgres -l $SVC_RUN/log/postgres.log -o "-p $DB_PORT -k $SVC_RUN/postgres" -w -t 120 start
EOF
chmod 700 "$FIX"/*.sh
chown -R app:app "$WORKSPACE"

echo "verifier: starting the submission"
started=$(date +%s)
if "$FIX/start-app.sh"; then
	echo "verifier: healthz 200 after $(($(date +%s) - started)) s"
	echo '{"healthz": 200, "seconds": '"$(($(date +%s) - started))"'}' >"$LOGS/startup.json"
else
	echo "verifier: the application never answered /healthz with 200; workflows will record the failures"
	echo '{"healthz": 0}' >"$LOGS/startup.json"
fi

echo "verifier: running workflows"
FK_BASELINE="$FIX/baseline.json" FK_RESTART_APP="$FIX/restart.sh" FK_DB_STOP="$FIX/db-stop.sh" FK_DB_START="$FIX/db-start.sh" \
	FK_WORKSPACE="$WORKSPACE" FK_UX_DIR="$LOGS/ux" FK_APP_LOG="$LOGS/app.log" PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
	FK_BUDGET_SECONDS="${FK_BUDGET_SECONDS:-$(( ${FK_VERIFIER_TIMEOUT:-10800} - 2700 - ($(date +%s) - T0) ))}" \
	python3 "$TESTS/workflows.py" --out "$LOGS/results.json" >"$LOGS/workflows.log" 2>&1
wf_exit=$?
grep -E '^\[wf\]' "$LOGS/workflows.log" || true
[ "$wf_exit" = 0 ] || invalid "the workflow runner failed (exit $wf_exit); see workflows.log"
pkill -u app 2>/dev/null; sleep 1; pkill -9 -u app 2>/dev/null
cp -f "$SVC_RUN"/log/*.log "$LOGS/" 2>/dev/null

echo "verifier: scoring"
[ -n "$FK_MODEL_KEY" ] || invalid "no model-API key reached the verifier; the judge cannot run"
install -d -m 700 -o svc -g svc /run/svc/llm
FK_PROXY_TOKEN=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
printf '%s\n%s\n' "$FK_MODEL_KEY" "$FK_PROXY_TOKEN" | python3 $TESTS/services/asuser.py svc env -i PATH="$PATH" \
	KH_PROXY_DEADLINE="$KH_PROXY_DEADLINE" KH_PROXY_PORT="$PROXY_PORT" KH_PROXY_SOCKET=/run/svc/llm/proxy.sock \
	KH_PROXY_LOG=/run/svc/llm/proxy.log KH_PROXY_UPSTREAM="${KH_PROXY_UPSTREAM:-}" \
	python3 "$TESTS/proxy/llm_proxy.py" >"$LOGS/proxy.log" 2>&1 &
unset FK_MODEL_KEY
for _ in $(seq 1 40); do curl -sf "http://127.0.0.1:$PROXY_PORT/healthz" >/dev/null && break; sleep 0.25; done
curl -sf "http://127.0.0.1:$PROXY_PORT/healthz" >/dev/null || invalid "the credential proxy did not start"
ANTHROPIC_API_BASE="http://127.0.0.1:$PROXY_PORT" ANTHROPIC_API_KEY="$FK_PROXY_TOKEN" LITELLM_LOCAL_MODEL_COST_MAP=True \
	python3 "$TESTS/proxy/supervise.py" --health "http://127.0.0.1:$PROXY_PORT/healthz" --poll 2 --stall 20 --margin 15 \
	--report "$LOGS/supervise.json" -- rewardkit "$TESTS" --workspace "$WORKSPACE"
score_exit=$?
unset FK_PROXY_TOKEN
cp -f /run/svc/llm/proxy.log "$LOGS/proxy-requests.log" 2>/dev/null
echo "verifier: done (scoring exit $score_exit)"
# fail-closed: a scoring failure replaces whatever Reward Kit wrote with the zero reward
[ "$score_exit" = 0 ] || { printf '{"reward": 0.0, "functional": 0.0, "judge": 0.0}\n' >"$LOGS/reward.json"; invalid "scoring failed (exit $score_exit); see supervise.json"; }
exit 0
