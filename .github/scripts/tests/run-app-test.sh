#!/usr/bin/env bash
#
# Runs all test flavours (qemu.x86_64, qemu.arm64, fc.x86_64, fc.arm64,
# xen.x86_64, xen.arm64, ...) for exactly ONE app. Flavours run
# sequentially -- only the *apps* (and now compilers) are parallelized,
# via the matrix in the reusable workflow.
#
# Usage: run-app-test.sh <app-dir> [compiler]
#
# CC/CXX pairing (fixes the gcc/clang C++ ABI hazard, see docs/toolchains.md):
#   Setting only $CC and leaving $CXX unset means C++ apps get a mixed
#   clang/g++ (or gcc/clang++) build. Whenever CC is set, we derive a
#   matching CXX unless the caller already exported one explicitly.

set -e

APP="$1"
COMPILER="$2"

if [ -z "$APP" ]; then
  echo "Usage: run-app-test.sh <app-dir> [compiler]" >&2
  exit 1
fi

if [ -n "$COMPILER" ]; then
  export CC="$COMPILER"
  if [ -z "$CXX" ]; then
    case "$COMPILER" in
      *clang*) export CXX="${COMPILER/clang/clang++}" ;;
      *gcc*)   export CXX="${COMPILER/gcc/g++}" ;;
      cc)      export CXX="c++" ;;
      *)
        echo "::warning::Unrecognized compiler '$COMPILER' - no matching CXX could be derived, leaving CXX unset. This app may silently mix toolchains." >&2
        ;;
    esac
  fi
fi

./setup.sh

mkdir -p "$APP/.scripts/test/log"

LOG_SUFFIX="$APP"
if [ -n "$COMPILER" ]; then
  LOG_SUFFIX="${APP}-${COMPILER}"
fi
LOG_FILE="app-output-${LOG_SUFFIX}.log"

# Everything below -- the header AND the actual test run -- goes through
# the same pipe into the same file. Previously the header echo happened
# outside the `... | tee` pipeline, so it only ever reached the live job
# console, never the file that generate-summary.sh reads -- which is
# exactly why the summary table's App/Compiler column came out blank.
{
  echo "[$APP] CC=${CC:-<default>} CXX=${CXX:-<default>} compiler=${COMPILER:-default}"
  echo ""
  (
    cd "$APP"
    sudo -E env CC="$CC" CXX="$CXX" ./.scripts/test/all.sh
  )
} 2>&1 | tee "$LOG_FILE"

# Structured results, in addition to the human log above. See
# docs/testing.md ("why not just bash-parse the log") for the reasoning
# -- short version: text-scraping a log with awk/grep for a report is
# fragile (see the bug this replaces); computing platform/arch/status
# once, here, into a small CSV that the aggregator just concatenates is
# both simpler to read and harder to get subtly wrong.
python3 "$(dirname "$0")/parse-test-log.py" \
  --app "$APP" \
  --compiler "${COMPILER:-default}" \
  --log "$LOG_FILE" \
  --out "results-${LOG_SUFFIX}.csv"
