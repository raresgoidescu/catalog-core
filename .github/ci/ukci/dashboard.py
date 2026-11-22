"""Build the static dashboard (one HTML page) from a ci-data checkout."""

from __future__ import annotations

import collections
import glob
import os

from pydantic import BaseModel, ConfigDict, ValidationError

from . import render


class _Record(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RunPR(_Record):
    ref: str
    used: bool


class RunVerdict(_Record):
    app: str
    compiler: str
    variant: str
    verdict: str
    reason: str = ""


class Timing(_Record):
    total_s: float | None = None


class Run(_Record):
    """What the dashboard reads from runs/**.json (written by data.Store.record)."""

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
        return ", ".join(p.ref for p in self.prs if p.used and p.ref != self.root) or "–"


def load_runs(root: str) -> tuple[list[Run], int]:
    """(runs, how many files couldn't be read)."""
    runs, bad = [], 0
    for path in glob.glob(os.path.join(root, "runs", "*", "*", "*", "*.json")):
        try:
            with open(path, "rb") as f:
                runs.append(Run.model_validate_json(f.read()))
        except (OSError, ValidationError):
            bad += 1
    return sorted(runs, key=lambda r: r.time), bad


def grid(runs: list[Run], channel: str, n: int) -> dict:
    """Scheduled runs of a channel as rows of configurations x columns of runs."""
    sched = [r for r in runs if r.event == "schedule" and r.channel == channel][-n:]
    by_run = [{(v.app, v.compiler, v.variant): v for v in r.verdicts} for r in sched]
    rows = []
    for c in sorted({k for verdicts in by_run for k in verdicts}):
        marks = [{"verdict": v.verdict, "tip": f"{v.verdict}: {v.reason}"} if (v := m.get(c)) else None for m in by_run]
        rows.append({"config": c, "marks": marks})
    return {"runs": sched, "rows": rows}


def flaky(runs: list[Run]) -> list[tuple[float, int, int, str]]:
    counts: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for r in runs[-200:]:
        for v in r.verdicts:
            c = counts[f"{v.app} {v.compiler} {v.variant}"]
            c[0] += v.verdict == "FLAKY"
            c[1] += 1
    return sorted(((f / t, f, t, k) for k, (f, t) in counts.items() if f), reverse=True)[:20]


def main(args) -> int:
    runs, bad = load_runs(args.data)
    prs = [r for r in runs if r.root][-40:][::-1]
    html = render.dashboard(
        n_runs=len(runs),
        n_bad=bad,
        staging=grid(runs, "staging", 30),
        stable=grid(runs, "stable", 12),
        prs=prs,
        flaky=flaky(runs),
    )
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "index.html"), "w") as f:
        f.write(html)
    print(f"wrote {args.out}/index.html from {len(runs)} runs ({bad} unreadable, skipped)")
    return 0
