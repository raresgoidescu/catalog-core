#!/usr/bin/env bash
#
# Usage: retry.sh <timeout> <max-attempts> <wait> -- <command...>
#
# Runs <command> up to <max-attempts> times, each bounded by <timeout>,
# sleeping <wait> between attempts. Exits with the last attempt's status.

set -uo pipefail

TIMEOUT="$1"; MAX_ATTEMPTS="$2"; WAIT="$3"; shift 3
[ "${1:-}" = "--" ] && shift

status=1
for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  echo "::group::Attempt ${attempt}/${MAX_ATTEMPTS}: $*"
  # Capture the status in the same statement: after `if cmd; then ...; fi`
  # without an else branch, $? is 0 even when cmd failed.
  timeout "$TIMEOUT" "$@" && status=0 || status=$?
  echo "::endgroup::"

  if [ "$status" -eq 0 ]; then
    exit 0
  fi

  if [ "$attempt" -eq "$MAX_ATTEMPTS" ]; then
    echo "::error::Giving up after ${MAX_ATTEMPTS} attempts (last exit ${status})."
    exit "$status"
  fi

  echo "::warning::Attempt ${attempt} failed or hung (exit ${status}) -- retrying in ${WAIT}."
  sleep "$WAIT"
done
exit "$status"
