#!/usr/bin/env bash
#
# Discovers which app directories should be tested for a given mode and
# prints them as a JSON array, e.g. ["c-hello","cpp-hello","redis"].
#
# This is the same selection logic that used to live inline in
# run-tests.sh's `for app in $apps` loop -- it is now factored out so it
# can be called once, up front, to build a GitHub Actions matrix, instead
# of being re-derived while apps are being tested sequentially.
#
# Usage: list-apps.sh <mode>
#   mode: all | musl | lwip | libelf | elfloader

set -e

MODE="${1:-all}"

case "$MODE" in
  musl)      FILTER='\$(LIBS_BASE)/musl' ;;
  lwip)      FILTER='\$(LIBS_BASE)/lwip' ;;
  libelf)    FILTER='\$(LIBS_BASE)/libelf' ;;
  elfloader) FILTER='/apps/elfloader' ;;
  all)       FILTER='' ;;
  *)
    echo "Invalid mode: $MODE" >&2
    exit 1
    ;;
esac

apps=$(find . -maxdepth 1 -type d ! -name '.' | sort | while read -r dir; do
  if [ -f "$dir/Makefile" ] && ([ -z "$FILTER" ] || grep -q "$FILTER" "$dir/Makefile"); then
    basename "$dir"
  fi
done)

if [ -z "$apps" ]; then
  echo "No apps matched mode '$MODE' -- refusing to emit an empty matrix" >&2
  exit 1
fi

json=$(printf '%s\n' "$apps" | jq -R . | jq -sc .)
echo "$json"

# For local debugging / step summaries.
echo "Apps selected for mode '$MODE':" >&2
printf '  - %s\n' $apps >&2
