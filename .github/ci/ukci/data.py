"""The ``ci-data`` branch: results as data, written only by trusted jobs.

  runs/<yyyy>/<mm>/<dd>/<run>-<attempt>.json   one run: plan, verdicts, timings
  pr/<owner>__<repo>/<n>/<head_sha>.json        a PR's own result at that head (for blame)
  deps/<owner>__<repo>/<n>/<owner>__<repo>__<m> marker: PR m was tested with PR n (for fan-out)
  nightly/<channel>/latest.json                 last scheduled verdicts (for Discord)

Files are written once and never edited (except ``nightly/*/latest.json``,
which only scheduled runs write), so concurrent pushes rebase cleanly.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import subprocess
import time

from pydantic import BaseModel, ValidationError

from .models import Plan, Report, Result, Verdict

BRANCH = "ci-data"


class OwnResult(BaseModel):
    run_url: str
    plan_hash: str
    conclusion: str
    verdicts: list[Verdict]


def _key(ref: str) -> str:
    repo, _, n = ref.partition("#")
    return f"{repo.replace('/', '__')}/{n}"


class Store:
    """A local checkout of the ci-data branch."""

    def __init__(self, repo: str, token: str | None, path: str = "ci-data", url: str | None = None):
        self.repo, self.token, self.path = repo, token, path
        self.url = url or f"https://github.com/{repo}"

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
        if self.git("fetch", "-q", "--depth", "50", "origin", BRANCH, check=False).returncode == 0:
            self.git("checkout", "-q", "-B", BRANCH, "FETCH_HEAD")
        else:
            self.git("checkout", "-q", "--orphan", BRANCH)
        return self

    def read(self, rel: str) -> dict | None:
        try:
            with open(os.path.join(self.path, rel)) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def write(self, rel: str, obj: dict | str) -> None:
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj, indent=1, sort_keys=True))

    def push(self, message: str, attempts: int = 5) -> bool:
        self.git("add", "-A")
        if self.git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return True
        self.git("commit", "-q", "-m", message)
        for i in range(attempts):
            if self.git("push", "-q", "origin", f"HEAD:{BRANCH}", check=False).returncode == 0:
                return True
            # Someone else pushed meanwhile: replay our new files on top of theirs.
            if self.git("fetch", "-q", "--depth", "50", "origin", BRANCH, check=False).returncode == 0:
                self.git("rebase", "-q", "FETCH_HEAD", check=False)
            time.sleep(2 * (i + 1))
        print(f"::warning::could not push to {BRANCH} after {attempts} attempts")
        return False

    # --- what CI reads and writes ------------------------------------------------

    def record(self, plan: Plan, report: Report, results: list[Result]) -> None:
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
        if plan.root and plan.root_head_sha:
            own = OwnResult(
                run_url=plan.run_url, plan_hash=plan.plan_hash, conclusion=report.conclusion, verdicts=report.verdicts
            )
            self.write(f"pr/{_key(plan.root)}/{plan.root_head_sha}.json", own.model_dump())
            for p in plan.prs:
                if p.ref != plan.root:
                    self.write(f"deps/{_key(p.ref)}/{plan.root.replace('/', '__').replace('#', '__')}", "")

    def dependents(self, ref: str) -> list[str]:
        """PRs that were tested together with `ref` (so they depend on it)."""
        try:
            names = sorted(os.listdir(os.path.join(self.path, "deps", _key(ref))))
        except OSError:
            return []
        return [f"{o}/{r}#{n}" for o, r, n in (name.split("__") for name in names)]

    def own_result(self, ref: str, head_sha: str) -> OwnResult | None:
        data = self.read(f"pr/{_key(ref)}/{head_sha}.json")
        try:
            return OwnResult.model_validate(data) if data else None
        except ValidationError:
            return None  # written by an older version of CI

    def nightly(self, channel: str) -> dict[str, str] | None:
        """Last scheduled run's verdict per configuration ("app compiler variant")."""
        data = self.read(f"nightly/{channel}/latest.json")
        return data.get("cells") if data else None

    def set_nightly(self, plan: Plan, verdicts: list[Verdict]) -> None:
        self.write(
            f"nightly/{plan.channel}/latest.json",
            {"run_url": plan.run_url, "cells": {f"{v.app} {v.compiler} {v.variant}": v.verdict for v in verdicts}},
        )
