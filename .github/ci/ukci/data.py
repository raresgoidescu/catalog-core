"""The ``ci-data`` branch: results as data, written only by trusted jobs.

  runs/<yyyy>/<mm>/<dd>/<run>-<attempt>.json   one run: plan, verdicts, timings
  plans/<plan_hash>.json                        the finished result for those exact commits (reuse)
  deps/<owner>__<repo>/<n>/<owner>__<repo>__<m> marker: PR m was tested with PR n (fan-out)
  nightly/<channel>/latest.json                 last scheduled state per configuration (Discord)

Files are written once and never edited (except ``nightly/*/latest.json``,
which only scheduled runs write, one at a time), so concurrent pushes rebase
cleanly.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import subprocess
import time

from .models import Plan, Report, Result, Reused, Verdict

BRANCH = "ci-data"


def _key(ref: str) -> str:
    repo, _, n = ref.partition("#")
    return f"{repo.replace('/', '__')}/{n}"


def plan_path(plan_hash: str) -> str:
    return f"plans/{plan_hash}.json"


def config_key(v: Verdict) -> str:
    return f"{v.app} {v.compiler} {v.variant}"


class Store:
    """A local checkout of the ci-data branch."""

    def __init__(self, repo: str, token: str | None, path: str = "ci-data", url: str | None = None):
        self.repo, self.token, self.path = repo, token, path
        self.url = url or f"https://github.com/{repo}"
        self.pending: dict[str, str] = {}  # written since open(), re-applied if a push races

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        if self.token:  # auth for this command only; nothing is stored in .git/config
            auth = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env |= {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth}",
            }
        return subprocess.run(["git", *args], cwd=self.path, env=env, check=check, capture_output=True, text=True)

    def open(self) -> Store:
        os.makedirs(self.path, exist_ok=True)
        self.git("init", "-q")
        self.git("remote", "add", "origin", self.url, check=False)
        self.git("config", "user.name", "catalog-core CI")
        self.git("config", "user.email", "ci@catalog-core.invalid")
        fetched = self.git("fetch", "-q", "--depth", "1", "origin", BRANCH, check=False)
        if fetched.returncode == 0:
            self.git("checkout", "-q", "-B", BRANCH, "FETCH_HEAD")
        elif "couldn't find remote ref" in fetched.stderr:
            self.git("checkout", "-q", "--orphan", BRANCH)  # first run ever
        else:
            raise RuntimeError(f"can't fetch {BRANCH}: {fetched.stderr.strip()}")
        return self

    def read(self, rel: str) -> dict | None:
        try:
            with open(os.path.join(self.path, rel)) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def write(self, rel: str, obj: dict | str) -> None:
        self.pending[rel] = obj if isinstance(obj, str) else json.dumps(obj, indent=1, sort_keys=True)
        self._write(rel, self.pending[rel])

    def _write(self, rel: str, text: str) -> None:
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)

    def push(self, message: str, attempts: int = 5) -> bool:
        for i in range(attempts):
            self.git("add", "-A")
            if self.git("diff", "--cached", "--quiet", check=False).returncode == 0:
                return True
            self.git("commit", "-q", "-m", message)
            if self.git("push", "-q", "origin", f"HEAD:{BRANCH}", check=False).returncode == 0:
                return True
            # Someone else pushed meanwhile. Nothing here is ever edited by two
            # runs, so: take their branch and write our files again.
            time.sleep(2 * (i + 1))
            if self.git("fetch", "-q", "--depth", "1", "origin", BRANCH, check=False).returncode == 0:
                self.git("reset", "-q", "--hard", "FETCH_HEAD")
                for rel, text in self.pending.items():
                    self._write(rel, text)
        print(f"::warning::could not push to {BRANCH} after {attempts} attempts")
        return False

    # --- what CI reads and writes ------------------------------------------------

    def record(self, plan: Plan, report: Report, results: list[Result], summary: str) -> None:
        now = dt.datetime.now(dt.UTC)
        run = f"{os.environ.get('GITHUB_RUN_ID', '0')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
        self.write(
            f"runs/{now:%Y/%m/%d}/{run}.json",
            {
                "time": now.isoformat(timespec="seconds"),
                "event": plan.event,
                "channel": plan.channel,
                "root": plan.root,
                "run_url": plan.run_url,
                "plan_hash": plan.plan_hash,
                "prs": [p.model_dump() for p in plan.prs],
                "lock": {k: v.model_dump() for k, v in plan.lock.items()},
                "conclusion": report.conclusion,
                "title": report.title,
                "verdicts": [v.model_dump() for v in report.verdicts],
                "timings": [
                    {
                        "app": r.app,
                        "compiler": r.compiler,
                        **a.model_dump(include={"variant", "attempt", "build", "run", "build_s", "run_s", "total_s"}),
                    }
                    for r in results
                    for a in r.attempts
                ],
                "tools": next((r.tools for r in results if r.tools), {}),
            },
        )
        if not plan.root:
            return
        # A result is worth reusing only if CI could tell for every configuration.
        if all(v.verdict != "INFRA" for v in report.verdicts):
            reused = Reused(run_url=plan.run_url, conclusion=report.conclusion, title=report.title, summary=summary)
            self.write(plan_path(plan.plan_hash), reused.model_dump())
        for p in plan.prs:
            if p.ref != plan.root and p.used:
                self.write(f"deps/{_key(p.ref)}/{plan.root.replace('/', '__').replace('#', '__')}", "")

    def dependents(self, ref: str) -> list[str]:
        """PRs that were tested together with `ref` (so they depend on it)."""
        try:
            names = sorted(os.listdir(os.path.join(self.path, "deps", _key(ref))))
        except OSError:
            return []
        return [f"{o}/{r}#{n}" for o, r, n in (name.split("__") for name in names if name.count("__") == 2)]

    def nightly(self, channel: str) -> dict | None:
        """The last scheduled run of `channel`: {"run_url", "error", "cells": {config: verdict}}."""
        return self.read(f"nightly/{channel}/latest.json")

    def set_nightly(self, channel: str, state: dict) -> None:
        self.write(f"nightly/{channel}/latest.json", state)
