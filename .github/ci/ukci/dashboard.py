"""Build the static dashboard (one HTML page) from a ci-data checkout."""

from __future__ import annotations

import collections
import glob
import json
import os

from . import render


def load_runs(root: str) -> list[dict]:
    runs = []
    for path in glob.glob(os.path.join(root, "runs", "*", "*", "*", "*.json")):
        try:
            with open(path) as f:
                run = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        runs.append(run)
    return sorted(runs, key=lambda r: r.get("time", ""))


def grid(runs: list[dict], channel: str, n: int) -> dict:
    """Scheduled runs of a channel as rows of configurations x columns of runs."""
    sched = [r for r in runs if r.get("event") == "schedule" and r.get("channel") == channel][-n:]
    cells = sorted({(v["app"], v["compiler"], v["variant"]) for r in sched for v in r.get("verdicts", [])})
    rows = []
    for c in cells:
        marks = []
        for r in sched:
            v = next((x for x in r.get("verdicts", []) if (x["app"], x["compiler"], x["variant"]) == c), None)
            marks.append({"verdict": v["verdict"], "tip": f"{v['verdict']}: {v.get('reason', '')}"} if v else None)
        rows.append({"config": c, "marks": marks})
    return {"runs": sched, "rows": rows}


def flaky(runs: list[dict]) -> list[tuple[float, int, int, str]]:
    counts: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for r in runs[-200:]:
        for v in r.get("verdicts", []):
            c = counts[f"{v['app']} {v['compiler']} {v['variant']}"]
            c[0] += v["verdict"] == "FLAKY"
            c[1] += 1
    return sorted(((f / t, f, t, k) for k, (f, t) in counts.items() if f), reverse=True)[:20]


def main(args) -> int:
    runs = load_runs(args.data)
    prs = [
        dict(r, minutes=sum(t.get("total_s") or 0 for t in r.get("timings", [])) / 60) for r in runs if r.get("root")
    ][-40:][::-1]
    html = render.dashboard(
        n_runs=len(runs), staging=grid(runs, "staging", 30), stable=grid(runs, "stable", 12), prs=prs, flaky=flaky(runs)
    )
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "index.html"), "w") as f:
        f.write(html)
    print(f"wrote {args.out}/index.html from {len(runs)} runs")
    return 0
