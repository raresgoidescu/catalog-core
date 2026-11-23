from __future__ import annotations

import argparse
import json
import sys

from . import apps, dashboard, plan, report, run


def cmd_apps(args) -> int:
    print(json.dumps([app.to_dict() for app in apps.discover(args.root)], indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ukci")
    parser.add_argument("--root", default=".")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("apps").set_defaults(func=cmd_apps)

    p = sub.add_parser("plan")
    p.add_argument("--out", default="plan.json")
    p.set_defaults(func=plan.main)

    p = sub.add_parser("check-start")
    p.add_argument("--plan", default="plan.json")
    p.set_defaults(func=report.cmd_start)

    p = sub.add_parser("fan-out")
    p.add_argument("--plan", default="plan.json")
    p.set_defaults(func=report.cmd_fan_out)

    p = sub.add_parser("run")
    p.add_argument("--plan", default="plan.json")
    p.add_argument("--app", required=True)
    p.add_argument("--compiler", required=True)
    p.add_argument("--variants", default="")
    p.add_argument("--out", default="results")
    p.add_argument("--no-recheck", action="store_true")
    p.add_argument("--no-sudo", action="store_true")
    p.add_argument("--timeout", type=int, default=1200)
    p.add_argument("--run-timeout", type=int, default=300)
    p.set_defaults(func=run.main)

    p = sub.add_parser("report")
    p.add_argument("--plan", default="plan.json")
    p.add_argument("--results", default="results")
    p.add_argument("--out", default="verdicts.json")
    p.add_argument("--check-id", default="")
    p.set_defaults(func=report.cmd_finish)

    p = sub.add_parser("dashboard")
    p.add_argument("--data", default="ci-data")
    p.add_argument("--out", default="site")
    p.set_defaults(func=dashboard.main)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
