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
if [ -x "${APP_DIR}/start.sh" ]; then
  ( cd "${APP_DIR}" && ./start.sh ) >/logs/verifier/app-start.log 2>&1 &
  echo "started ${APP_DIR}/start.sh, logging to /logs/verifier/app-start.log" >&2
else
  echo "no executable ${APP_DIR}/start.sh; rendered-surface criteria will score 0" >&2
fi

# --yolo accepts one named risk: an agent judge shares /app with the compiled
# criteria instead of an isolated copy. The isolated path is unavailable here
# because it mounts overlayfs, which this unprivileged container cannot do.
# The exposure is bounded: Harbor collects every artifact before the verifier
# starts, so the only shared state is this container's own /app, and the
# criteria read named paths under it rather than enumerating it.
exec rewardkit /tests --yolo
