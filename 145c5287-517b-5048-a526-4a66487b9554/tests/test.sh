#!/bin/bash
set -euo pipefail

# The Harbor verifier entry point. Reward Kit is the whole path from criterion
# outcome to reward bytes, so this script writes no reward of its own: rewardkit
# writes /logs/verifier/reward.json, the single authoritative carrier the
# contract binds, and attributes every zero score to a machine-readable reason
# in /logs/verifier/reward-details.json.
#
# rewardkit is already installed in the verifier image at the exact release the
# contract binds, so it is invoked from PATH and never fetched here: the
# verifier runs offline at both its baseline and its phase.

APP_DIR="${APP_DIR:-/app}"
APP_PUBLIC_URL="${APP_PUBLIC_URL:-http://127.0.0.1:8080}"
export APP_PUBLIC_URL

# The agent's own environment is stopped before this verifier starts, so the
# application is brought up here, inside the verifier's own compose project,
# from the /app deliverable Harbor uploaded. Grading a file the agent wrote
# about its rendering instead would be the interface-only grading this
# project's README names as a measured failure mode.
#
# A start that fails is never fatal here: the rendered-surface criteria must
# score zero against an application that does not serve, which is what the
# app-never-answers control proves. Exiting non-zero instead would lose every
# backend and database result alongside it.
# Both the key notice below and the app-start redirect further down write into
# this directory, so it has to exist before either of them runs.
mkdir -p /logs/verifier

# Fail closed from here on. Every exit path below this line - including a set -e
# abort before Reward Kit ever runs - leaves a reward behind, so the trial
# reports 0 rather than erroring and losing both dimensions. Reward Kit
# overwrites this same file with the real score, so the zero survives only when
# nothing else wrote one. The three keys mirror the [[reward]] tables in
# tests/reward.toml; harbor reads the top-level "reward" key.
printf '{"reward": 0.0, "test_output_score": 0.0, "judge_score": 0.0}\n' \
  > /logs/verifier/reward.json

# The judged dimension carries 30 of the 115 graded weight (tests/reward.toml).
# Without a model key the 8 judged criteria cannot answer, Reward Kit raises
# inside the task group, and the run ends before the compiled dimension scores
# at all - so the trial reports the fail-closed zero written above rather than a
# partial 85/115. solution/validation/README.md records that behaviour. This
# notice exists only because that zero does not say why on its own; it changes
# no outcome, and it deliberately does not exit, because this script has no
# invalid() helper to report through and the fail-closed reward is already set.
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  printf 'no model-API key reached the verifier: the 8 judged criteria cannot score, so this run reports the fail-closed reward of 0\n' \
    | tee /logs/verifier/judge-key-missing.txt >&2
fi

# start.sh is a deliverable the submission wrote, and it runs in this container,
# where /tests holds fixtures.json - the expected answer. So it must not run as
# an identity that can read /tests, and it must not be able to rewrite
# /app/inputs, which four criteria re-derive their expectations from. Harbor
# runs this verifier as root, so both are enforceable here: drop to app (10001,
# "other" against /tests at 750) and hand back /app except the corpus.
#
# Without root neither is possible, so the app is not started at all: the
# rendered-surface criteria score 0 exactly as they do for an app that never
# answers, and the marker below says why. Starting it anyway would be the one
# outcome worth avoiding, because an unisolated start.sh can read the answer.
if [ "$(id -u)" -ne 0 ]; then
  printf 'the verifier is not root (uid %s), so start.sh cannot be isolated from /tests; the application was not started and the rendered-surface criteria score 0\n' \
    "$(id -u)" | tee /logs/verifier/app-not-isolated.txt >&2
elif [ -x "${APP_DIR}/start.sh" ]; then
  # inputs/ is the question and out/ is the answer already submitted, so both go
  # back to root with every write bit cleared. That alone stops an edit, but not
  # a swap: on POSIX, renaming or unlinking a child is authorised by write on the
  # PARENT directory, not by the child's own mode. With /app writable the app
  # could "mv out .out; mkdir out" and hand the compiled checks a doctored answer
  # after submission. So /app itself is locked too, last, after the recursive
  # chown that would otherwise undo it.
  #
  # The app keeps read and traverse on all three, which is everything it needs to
  # serve: the reference application writes nothing at run time. An application
  # that wants scratch space still has $HOME (/home/app, created by useradd) and
  # /tmp. The instruction promises a writable /app nowhere, so nothing an honest
  # submission was told it could do is taken away here.
  chown -R app:app "${APP_DIR}"
  for frozen in inputs out; do
    if [ -d "${APP_DIR}/${frozen}" ]; then
      chown -R 0:0 "${APP_DIR}/${frozen}"
      chmod -R a-w "${APP_DIR}/${frozen}"
    fi
  done
  chown 0:0 "${APP_DIR}"
  chmod 755 "${APP_DIR}"
  python3 /tests/asuser.py app sh -c 'cd "$0" && exec ./start.sh' "${APP_DIR}" \
    >/logs/verifier/app-start.log 2>&1 &
  echo "started ${APP_DIR}/start.sh as app (uid 10001), logging to /logs/verifier/app-start.log" >&2
else
  echo "no executable ${APP_DIR}/start.sh; rendered-surface criteria will score 0" >&2
fi

set +e
rewardkit /tests --yolo
status=$?
set -e
# Fail closed: a crashed reward run must still leave a reward behind, or the
# trial errors and the whole deterministic dimension is lost.
if [ ! -s /logs/verifier/reward.json ] && [ ! -s /logs/verifier/reward.txt ]; then
  printf '0\n' > /logs/verifier/reward.txt
fi

# The auditor joins each gold-subset trial to this report by test name, and a
# declared ID absent from it leaves that trial unresolved rather than failed. A
# cheat check carries no graded weight, so it is not a Reward Kit criterion and
# does not appear in the reward carrier: this report is its only record.
#
# It is written whatever the reward run exited, so a declared ID always carries
# a resolved status, and the reward exit code is preserved afterwards.
python3 - <<'CTRF' || echo "cheat-check report not written" >&2
import importlib.util, json, os
from pathlib import Path

tests = Path("/tests")
declared = json.loads((tests / "cheat-checks.json").read_text(encoding="utf-8"))["ids"]
spec = importlib.util.spec_from_file_location("ledgerline_checks", tests / "checks.py")
checks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checks)

workspace = Path(os.environ.get("APP_DIR", "/app"))
rows = []
for name in declared:
    try:
        passed = bool(getattr(checks, name)(workspace))
    except Exception:
        passed = False
    rows.append({"name": name, "status": "passed" if passed else "failed"})

out = Path("/logs/verifier/ctrf.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(
    json.dumps({"results": {"tool": {"name": "ledgerline-cheat-checks"}, "tests": rows}}, indent=2)
    + "\n",
    encoding="utf-8",
)
CTRF

exit $status
