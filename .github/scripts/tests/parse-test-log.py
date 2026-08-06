#!/usr/bin/env python3
"""
Turns one app's raw .scripts/test/all.sh output into structured CSV rows.

Why Python and not another awk/grep pass: the previous approach carried
state across lines (an awk `app` variable set by a `[app]` header line
that was then implicitly relied on for every following PASSED/FAILED
line) with no way to notice when that state was wrong or missing --
which is exactly how the summary table ended up with a blank column.
Parsing each line into an explicit, typed record and writing them out
immediately removes the cross-line state entirely: there's nothing to
lose track of. This script has no dependencies beyond the standard
library, so it costs nothing extra in the CI image.

Input line format (produced by every app's .scripts/test/all.sh,
unchanged across the catalog):
    build.qemu.x86_64                             ... PASSED
        run.qemu.x86_64                           ... PASSED
    build.xen.arm64                               ... FAILED

Usage:
    parse-test-log.py --app APP --compiler COMPILER --log LOG_FILE --out OUT_CSV
"""
import argparse
import csv
import re
import sys

LINE_RE = re.compile(
    r"^\s*(?P<phase>build|run)\.(?P<platform>[A-Za-z0-9_]+)\.(?P<arch>[A-Za-z0-9_]+)"
    r"\s*\.\.\.\s*(?P<status>PASSED|FAILED)\s*$"
)


def parse(app: str, compiler: str, log_path: str):
    rows = []
    try:
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                m = LINE_RE.match(line)
                if not m:
                    continue
                rows.append(
                    {
                        "app": app,
                        "compiler": compiler,
                        "platform": m.group("platform"),
                        "arch": m.group("arch"),
                        "phase": m.group("phase"),
                        "status": m.group("status"),
                    }
                )
    except FileNotFoundError:
        print(f"::warning::{log_path} not found, no results extracted for {app}/{compiler}", file=sys.stderr)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument("--compiler", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows = parse(args.app, args.compiler, args.log)

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["app", "compiler", "platform", "arch", "phase", "status"],
            lineterminator="\n",  # csv defaults to \r\n; plain \n keeps
                                  # aggregate-results.sh's `grep ',FAILED$'`
                                  # working without a \r before the anchor
        )
        writer.writeheader()
        writer.writerows(rows)

    if not rows:
        print(f"::warning::No build/run result lines matched in {args.log} for {args.app}/{args.compiler} -- check the log format hasn't changed.", file=sys.stderr)

    print(f"Wrote {len(rows)} result row(s) to {args.out}")


if __name__ == "__main__":
    main()
