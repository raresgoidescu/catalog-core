from __future__ import annotations

import collections
import glob
import os

from pydantic import BaseModel, ConfigDict, ValidationError

from . import render


class Record(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RunPR(Record):
    ref: str
    used: bool


class RunVerdict(Record):
    app: str
    compiler: str
    variant: str
    verdict: str
    reason: str = ""


class Timing(Record):
    total_s: float | None = None


class Run(Record):
    time: str
    event: str
    channel: str
    root: str | None
    run_url: str
    title: str
    prs: list[RunPR] = []
    verdicts: list[RunVerdict] = []
    timings: list[Timing] = []

    @property
    def minutes(self) -> float:
        return sum(t.total_s or 0 for t in self.timings) / 60

    @property
    def tested_with(self) -> str:
        refs = [pr.ref for pr in self.prs if pr.used and pr.ref != self.root]
        return ", ".join(refs) or "-"


def load_runs(root: str) -> tuple[list[Run], int]:
    runs = []
    unreadable = 0
    for path in glob.glob(os.path.join(root, "runs", "*", "*", "*", "*.json")):
        try:
            with open(path, "rb") as f:
                runs.append(Run.model_validate_json(f.read()))
        except (OSError, ValidationError):
            unreadable += 1
    runs.sort(key=lambda run: run.time)
    return runs, unreadable


def grid(runs: list[Run], channel: str, n: int) -> dict:
    scheduled = [run for run in runs if run.event == "schedule" and run.channel == channel][-n:]
    by_run = []
    for run in scheduled:
        by_run.append({(v.app, v.compiler, v.variant): v for v in run.verdicts})
    configs: set[tuple[str, str, str]] = set()
    for verdicts in by_run:
        configs.update(verdicts)
    rows = []
    for config in sorted(configs):
        marks: list[dict[str, str] | None] = []
        for verdicts in by_run:
            v = verdicts.get(config)
            if v is None:
                marks.append(None)
            else:
                marks.append({"verdict": v.verdict, "tip": f"{v.verdict}: {v.reason}"})
        rows.append({"config": config, "marks": marks})
    return {"runs": scheduled, "rows": rows}


def flaky(runs: list[Run]) -> list[tuple[float, int, int, str]]:
    counts: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for run in runs[-200:]:
        for v in run.verdicts:
            entry = counts[f"{v.app} {v.compiler} {v.variant}"]
            if v.verdict == "FLAKY":
                entry[0] += 1
            entry[1] += 1
    rates = []
    for config, (flakes, total) in counts.items():
        if flakes:
            rates.append((flakes / total, flakes, total, config))
    rates.sort(reverse=True)
    return rates[:20]


def main(args) -> int:
    runs, unreadable = load_runs(args.data)
    prs = [run for run in runs if run.root][-40:]
    prs.reverse()
    html = render.dashboard(
        n_runs=len(runs),
        n_bad=unreadable,
        staging=grid(runs, "staging", 30),
        stable=grid(runs, "stable", 12),
        prs=prs,
        flaky=flaky(runs),
    )
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "index.html"), "w") as f:
        f.write(html)
    print(f"wrote {args.out}/index.html from {len(runs)} runs ({unreadable} unreadable, skipped)")
    return 0
