#!/usr/bin/env bash
set -Eeuo pipefail

# Web and server-local ORCA have independent lifetimes, but this image needs
# one small supervisor when deployed as a single Hugging Face/container unit.
# Both processes receive exactly the same durable state directory.
STATE_DIR="${CHEMISTRY_LAB_STATE_DIR:-${ORCA_STATE_DIR:-/data}}"
export CHEMISTRY_LAB_STATE_DIR="$STATE_DIR"
export CHEMISTRY_LAB_REQUIRE_LOCAL_WORKER="${CHEMISTRY_LAB_REQUIRE_LOCAL_WORKER:-1}"
mkdir -p "$STATE_DIR"

python -m services.local_orca_worker \
  --state-dir "$STATE_DIR" \
  --poll-seconds "${ORCA_LOCAL_WORKER_POLL_SECONDS:-1}" &
worker_pid=$!

gunicorn api.main:app \
  -k uvicorn.workers.UvicornWorker \
  -w "${WEB_WORKERS:-1}" \
  -b "0.0.0.0:${PORT:-7860}" \
  --timeout "${GUNICORN_TIMEOUT:-900}" &
web_pid=$!

stop_children() {
  kill -TERM "$web_pid" "$worker_pid" 2>/dev/null || true
}
trap stop_children INT TERM EXIT

while kill -0 "$web_pid" 2>/dev/null && kill -0 "$worker_pid" 2>/dev/null; do
  sleep 1
done

# One child has exited.  Stop the survivor before waiting; otherwise `wait`
# can block forever (for example, a dead worker with a still-healthy web
# process), leaving the container apparently alive without any local-job
# execution capacity and preventing the platform restart policy from acting.
stop_children

web_status=0
worker_status=0
wait "$web_pid" 2>/dev/null || web_status=$?
wait "$worker_pid" 2>/dev/null || worker_status=$?

if [ "$web_status" -ne 0 ]; then
  exit "$web_status"
fi
exit "$worker_status"
