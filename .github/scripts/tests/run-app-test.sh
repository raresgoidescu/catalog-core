#!/usr/bin/env bash
#
# Runs all test flavours (qemu.x86_64, qemu.arm64, fc.x86_64, fc.arm64,
# xen.x86_64, xen.arm64, ...) for exactly ONE app. Flavours run
# sequentially, as before -- only the *apps* are parallelized now, via
# the matrix in the reusable workflow.
#
# Usage: run-app-test.sh <app-dir> [compiler]
#
# IMPORTANT (fixes the gcc/clang C++ discrepancy, see docs/toolchains.md):
#   Setting only $CC (e.g. CC=clang) and leaving $CXX unset means C
#   sources get compiled by clang while C++ sources still fall back to
#   whatever the default $CXX is (typically g++). Mixing a clang-built
#   libc/libmusl/libelf object graph with g++-built C++ runtime objects
#   (libcxx/libcxxabi/libunwind/compiler-rt) is an ABI hazard, not just a
#   cosmetic inconsistency, and it silently differs from a pure-gcc or
#   pure-clang build. Whenever CC is set, we now derive a matching CXX
#   unless the caller already exported one explicitly.

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

echo "[$APP] CC=${CC:-<default>} CXX=${CXX:-<default>}"

./setup.sh

mkdir -p "$APP/.scripts/test/log"

LOG_SUFFIX="$APP"
if [ -n "$COMPILER" ]; then
  LOG_SUFFIX="${APP}-${COMPILER}"
fi

echo "[$APP] (compiler: ${COMPILER:-default})"
echo ""
(
  cd "$APP"
  sudo -E env CC="$CC" CXX="$CXX" ./.scripts/test/all.sh
) 2>&1 | tee "app-output-${LOG_SUFFIX}.log"
