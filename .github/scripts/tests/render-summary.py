#!/usr/bin/env python3


"""
Renders the GitHub Actions step summary from results.csv (all matrix
legs' parse-test-log.py output, concatenated).

Usage: render-summary.py --csv results.csv --tool-versions tool-versions.txt
Writes markdown to $GITHUB_STEP_SUMMARY, or stdout if unset (local runs).
"""

import argparse
import csv
import os
import sys
from collections import Counter

STATUS_ICON = {"PASSED": "✅ PASSED", "FAILED": "❌ FAILED"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--tool-versions", default=None)
    args = ap.parse_args()

    rows = []
    try:
        with open(args.csv, newline="") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        pass

    out = sys.stdout
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        out = open(summary_path, "a")

    with out:
        print("## Unikraft Catalog Core Test Results", file=out)
        import datetime
        print(f"### {datetime.datetime.now(datetime.timezone.utc).strftime('%a %b %-d %H:%M:%S UTC %Y')}", file=out)

        if not rows:
            print("\n_No structured results were found -- every matrix leg may have failed before producing output. Check the raw per-app logs in the archived artifact._\n", file=out)
        else:
            passed = sum(1 for r in rows if r["status"] == "PASSED")
            failed = sum(1 for r in rows if r["status"] == "FAILED")
            print(f"\n**{passed} passed, {failed} failed** out of {len(rows)} (app, compiler, platform, arch, phase) combinations.\n", file=out)

            print("| App | Compiler | Platform | Arch | Phase | Status |", file=out)
            print("|-----|----------|----------|------|-------|--------|", file=out)
            phase_order = {"build": 0, "run": 1}
            for r in sorted(rows, key=lambda r: (
                r["app"], r["compiler"], r["platform"], r["arch"],
                phase_order.get(r["phase"], 99),
            )):
                status = STATUS_ICON.get(r["status"], r["status"])
                print(f"| {r['app']} | {r['compiler']} | {r['platform']} | {r['arch']} | {r['phase']} | {status} |", file=out)

            if failed:
                print("\n### Failure statistics", file=out)
                print("| Compiler | Platform | Arch | Phase | Failures |", file=out)
                print("|----------|----------|------|-------|----------|", file=out)
                failures = Counter(
                    (r["compiler"], r["platform"], r["arch"], r["phase"])
                    for r in rows
                    if r["status"] == "FAILED"
                )
                for (compiler, platform, arch, phase), count in sorted(failures.items()):
                    print(f"| {compiler} | {platform} | {arch} | {phase} | {count} |", file=out)

                missing_scripts = sorted({
                    r["app"] for r in rows
                    if r["status"] == "FAILED"
                    and r["phase"] == "setup"
                    and r["platform"] == "n/a"
                })
                if missing_scripts:
                    print("\n### Apps without test scripts", file=out)
                    for app in missing_scripts:
                        print(f"- `{app}`: missing `.scripts/test/all.sh`", file=out)

        print("\n### System Configuration", file=out)
        if args.tool_versions and os.path.exists(args.tool_versions):
            with open(args.tool_versions) as f:
                for line in f:
                    print(f"- {line.rstrip()}", file=out)
        else:
            print("- (tool versions not captured for this run -- see collect-tool-versions.sh)", file=out)


if __name__ == "__main__":
    main()
