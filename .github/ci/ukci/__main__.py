"""Command line: ``uv run --project .github/ci python -m ukci <command>``.

apps / lint      what CI reads from the app directories (also useful locally)
plan             (plan job)   GitHub event -> plan.json
check-start      (plan job)   put the Catalog Core check on the PR(s)
fan-out          (fan-out job) re-test the PRs that depend on this one
run              (test job)   one app and compiler -> result.json; works locally too
report           (report job) results -> verdicts, finish the check, record, notify
dashboard        build the dashboard page from a ci-data checkout
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from . import apps as apps_mod
from . import dashboard, plan, report, repos, run


def cmd_apps(args) -> int:
    print(json.dumps([a.to_dict() for a in apps_mod.discover(args.root)], indent=2))
    return 0


def cmd_lint(args) -> int:
    """Every app must be understood by CI, and setup.sh must agree with discovery."""
    found = apps_mod.discover(args.root)
    errors = [f"{a.name}: {p}" for a in found for p in a.problems]
    listed = subprocess.run(["sh", "setup.sh", "--list"], cwd=args.root, capture_output=True, text=True, check=False)
    if listed.returncode != 0:
        errors.append(f"setup.sh --list failed: {listed.stderr.strip()}")
    else:
        from_sh = {tuple(line.split()) for line in listed.stdout.splitlines() if line.strip()}
        from_dirs = {(p, repos.slug_for(p)) for p in apps_mod.all_repo_paths(found)}
        errors += [f"setup.sh --list is missing {p} ({s})" for p, s in sorted(from_dirs - from_sh)]
        errors += [f"setup.sh --list has unexpected {p} ({s})" for p, s in sorted(from_sh - from_dirs)]
    for e in errors:
        print(f"::error::{e}")
    untested = [a.name for a in found if not a.has_tests]
    if untested:
        print(f"::notice::apps without tests (not run in CI): {', '.join(untested)}")
    print(f"{len(found)} apps, {len(found) - len(untested)} with tests, {len(errors)} problems")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ukci", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="catalog-core checkout (default: .)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("apps", help="what CI reads from the app directories").set_defaults(func=cmd_apps)

    sub.add_parser("lint", help="check every app can be understood by CI").set_defaults(func=cmd_lint)

    p = sub.add_parser("plan", help="(plan job) GitHub event -> plan.json")
    p.add_argument("--out", default="plan.json")
    p.set_defaults(func=plan.main)

    p = sub.add_parser("check-start", help="(plan job) post the Catalog Core check")
    p.add_argument("--plan", default="plan.json")
    p.set_defaults(func=report.cmd_start)

    p = sub.add_parser("fan-out", help="(fan-out job) re-test the PRs that depend on this one")
    p.add_argument("--plan", default="plan.json")
    p.set_defaults(func=report.cmd_fan_out)

    p = sub.add_parser("run", help="run one app/compiler of a plan (also works locally)")
    p.add_argument("--plan", default="plan.json")
    p.add_argument("--app", required=True)
    p.add_argument("--compiler", required=True)
    p.add_argument("--variants", default="", help="comma-separated subset, e.g. qemu.x86_64")
    p.add_argument("--out", default="results")
    p.add_argument("--no-recheck", action="store_true", help="skip the retry and the comparison without the PRs")
    p.add_argument("--no-sudo", action="store_true", help="don't run the app scripts as root")
    p.add_argument("--timeout", type=int, default=1200, help="seconds per variant attempt (default: 1200)")
    p.set_defaults(func=run.main)

    p = sub.add_parser("report", help="(report job) judge results, finish the check, record, notify")
    p.add_argument("--plan", default="plan.json")
    p.add_argument("--results", default="results")
    p.add_argument("--out", default="verdicts.json")
    p.add_argument("--check-id", default="")
    p.set_defaults(func=report.cmd_finish)

    p = sub.add_parser("dashboard", help="build the dashboard from a ci-data checkout")
    p.add_argument("--data", default="ci-data")
    p.add_argument("--out", default="site")
    p.set_defaults(func=dashboard.main)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
