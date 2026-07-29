#!/usr/bin/env bash
#
# Fan-in step. Run after all per-app matrix jobs have uploaded their
# `app-output-<app>.log` artifact and everything has been downloaded
# (with merge-multiple) into the current directory.
#
# Produces:
#   - output.log        (concatenated, same format the old sequential
#                         run-tests.sh produced, so generate-summary.sh
#                         and downstream tooling need no changes)
#   - $GITHUB_OUTPUT: result=success|failure, result_upper=SUCCESS|FAILURE

set -e

touch output.log

# Sorted for deterministic summaries regardless of job scheduling order.
for f in $(ls app-output-*.log 2>/dev/null | sort); do
  cat "$f" >> output.log
  echo "" >> output.log
done

if [ ! -s output.log ]; then
  echo "::error::No per-app logs found to aggregate -- did every matrix job fail before producing output?"
  echo "result=failure" >> "$GITHUB_OUTPUT"
  echo "result_upper=FAILURE" >> "$GITHUB_OUTPUT"
  exit 0
fi

if grep -q 'FAILED' output.log; then
  RESULT='failure'
else
  RESULT='success'
fi

echo "result=$RESULT" >> "$GITHUB_OUTPUT"
echo "result_upper=$(echo "$RESULT" | tr 'a-z' 'A-Z')" >> "$GITHUB_OUTPUT"
