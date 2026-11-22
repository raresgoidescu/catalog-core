"""Command line entry point: ``PYTHONPATH=.github/ci python3 -m ukci <command>``."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from . import apps as apps_mod
from . import repos


def cmd_apps(args) -> int:
    found = apps_mod.discover(args.root)
    if args.affected is not None:
        changed = [p for p in args.affected.split(",") if p]
        found = apps_mod.affected(found, changed)
    elif not args.all:
        found = [a for a in found if a.has_tests]
    if args.names:
        print(json.dumps([a.name for a in found]))
    else:
        print(json.dumps([a.to_dict() for a in found], indent=2))
    return 0


def cmd_lint(args) -> int:
    """Fail if an app can't be understood by CI, or setup.sh disagrees with discovery."""
    found = apps_mod.discover(args.root)
    errors = []
    for a in found:
        errors += [f"{a.name}: {p}" for p in a.problems]

    listed = subprocess.run(["sh", "setup.sh", "--list"], cwd=args.root,
                            capture_output=True, text=True, check=False)
    if listed.returncode != 0:
        errors.append(f"setup.sh --list failed: {listed.stderr.strip()}")
    else:
        from_sh = {tuple(line.split()) for line in listed.stdout.splitlines() if line.strip()}
        from_py = {(p, repos.slug_for(p)) for p in apps_mod.all_repo_paths(found)}
        for p, s in sorted(from_py - from_sh):
            errors.append(f"setup.sh --list is missing {p} ({s})")
        for p, s in sorted(from_sh - from_py):
            errors.append(f"setup.sh --list has unexpected {p} ({s})")

    untested = [a.name for a in found if not a.has_tests]
    for e in errors:
        print(f"::error::{e}")
    if untested:
        print(f"::notice::apps without tests (not run in CI): {', '.join(untested)}")
    print(f"{len(found)} apps, {len(found) - len(untested)} with tests, {len(errors)} problems")
    return 1 if errors else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ukci")
    ap.add_argument("--root", default=".", help="catalog-core checkout (default: .)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("apps", help="list apps discovered from the app directories")
    p.add_argument("--affected", metavar="PATHS",
                   help="comma-separated changed repo paths, e.g. libs/musl,libs/lwip")
    p.add_argument("--all", action="store_true", help="include apps without tests")
    p.add_argument("--names", action="store_true", help="print only a JSON list of names")
    p.set_defaults(func=cmd_apps)

    p = sub.add_parser("lint", help="check every app can be understood by CI")
    p.set_defaults(func=cmd_lint)

    from . import plan as plan_mod, report, run as run_mod

    p = sub.add_parser("plan", help="(CI) turn the GitHub event into plan.json")
    p.add_argument("--out", default="plan.json")
    p.set_defaults(func=plan_mod.main)

    p = sub.add_parser("check-start", help="(CI) post the in-progress Catalog Core check")
    p.add_argument("--plan", default="plan.json")
    p.set_defaults(func=report.cmd_start)

    p = sub.add_parser("run", help="run one app/compiler from a plan (also works locally)")
    p.add_argument("--plan", default="plan.json")
    p.add_argument("--app", required=True)
    p.add_argument("--compiler", required=True)
    p.add_argument("--variants", default="", help="comma-separated subset, e.g. qemu.x86_64")
    p.add_argument("--out", default="results")
    p.add_argument("--no-recheck", action="store_true", help="skip retry and base comparison")
    p.set_defaults(func=run_mod.main)

    p = sub.add_parser("report", help="(CI) judge results, finish the check, write the summary")
    p.add_argument("--plan", default="plan.json")
    p.add_argument("--results", default="results")
    p.add_argument("--out", default="verdicts.json")
    p.add_argument("--check-id", default="")
    p.set_defaults(func=report.cmd_finish)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
