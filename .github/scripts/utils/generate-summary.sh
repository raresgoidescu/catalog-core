#!/usr/bin/env bash
#
# Generates the markdown test report and appends it to GITHUB_STEP_SUMMARY.
# Assumes results.csv and tool-versions.txt exist in the current directory
# (produced by aggregate-results.sh). Kept as a thin wrapper so nothing
# else that calls `generate-summary.sh` needs to change; the actual
# rendering logic lives in render-summary.py now -- see docs/testing.md
# for why the data-shaping was moved out of awk.

set -e

python3 "$(dirname "$0")/../tests/render-summary.py" \
  --csv results.csv \
  --tool-versions tool-versions.txt
