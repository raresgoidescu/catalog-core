from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import subprocess
import time
import urllib.request

from pydantic import ValidationError

from .models import Attempt, Plan, Report, Result, Reused, Verdict

BRANCH = "ci-data"


def pr_key(ref: str) -> str:
    repo, _, number = ref.partition("#")
    return f"{repo.replace('/', '__')}/{number}"


def plan_path(plan_hash: str) -> str:
    return f"plans/{plan_hash}.json"


def config_key(v: Verdict) -> str:
    return f"{v.app} {v.compiler} {v.variant}"


def base_path(app: str, compiler: str, catalog_sha: str, repos: dict[str, str], tools: dict[str, str]) -> str:
    key = {"app": app, "compiler": compiler, "catalog": catalog_sha, "repos": repos, "tools": tools}
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
    return f"base/{digest}.json"


def fetch_base(repo: str, path: str) -> dict[str, Attempt]:
    url = f"https://raw.githubusercontent.com/{repo}/{BRANCH}/{path}"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            data = json.loads(response.read())
        run_url = str(data["run_url"])[:200]
        known = {}
        for raw in data["attempts"]:
            attempt = Attempt.model_validate(raw)
            known[attempt.variant] = attempt.model_copy(update={"attempt": "base", "source": run_url})
        return known
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        return {}


class Store:
    def __init__(self, repo: str, token: str | None, path: str = "ci-data", url: str | None = None):
        self.repo = repo
        self.token = token
        self.path = path
        self.url = url or f"https://github.com/{repo}"
        self.pending: dict[str, str] = {}

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        if self.token:
            auth = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {auth}"
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
            self.git("checkout", "-q", "--orphan", BRANCH)
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
        text = obj if isinstance(obj, str) else json.dumps(obj, indent=1, sort_keys=True)
        self.pending[rel] = text
        self.write_file(rel, text)

    def write_file(self, rel: str, text: str) -> None:
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
            time.sleep(2 * (i + 1))
            if self.git("fetch", "-q", "--depth", "1", "origin", BRANCH, check=False).returncode == 0:
                self.git("reset", "-q", "--hard", "FETCH_HEAD")
                for rel, text in self.pending.items():
                    self.write_file(rel, text)
        print(f"::warning::could not push to {BRANCH} after {attempts} attempts")
        return False

    def record(self, plan: Plan, report: Report, results: list[Result], summary: str) -> None:
        now = dt.datetime.now(dt.UTC)
        run = f"{os.environ.get('GITHUB_RUN_ID', '0')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
        timings = []
        for r in results:
            for a in r.attempts:
                fields = a.model_dump(include={"variant", "attempt", "build", "run", "build_s", "run_s", "total_s"})
                timings.append({"app": r.app, "compiler": r.compiler, **fields})
        tools = {}
        for r in results:
            if r.tools:
                tools = r.tools
                break
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
                "lock": {path: e.model_dump() for path, e in plan.lock.items()},
                "conclusion": report.conclusion,
                "title": report.title,
                "verdicts": [v.model_dump() for v in report.verdicts],
                "timings": timings,
                "tools": tools,
            },
        )
        if not plan.root:
            return
        if all(v.verdict != "INFRA" for v in report.verdicts):
            reused = Reused(run_url=plan.run_url, conclusion=report.conclusion, title=report.title, summary=summary)
            self.write(plan_path(plan.plan_hash), reused.model_dump())
        dependent = plan.root.replace("/", "__").replace("#", "__")
        for p in plan.prs:
            if p.ref != plan.root and p.used:
                self.write(f"deps/{pr_key(p.ref)}/{dependent}", "")

    def record_base(self, plan: Plan, results: list[Result]) -> None:
        if plan.event != "schedule" or plan.catalog is None:
            return
        cells = {(c.app, c.compiler): c for c in plan.cells}
        for r in results:
            cell = cells.get((r.app, r.compiler))
            if cell is None or r.infra_error:
                continue
            final: dict[str, Attempt] = {}
            for a in r.attempts:
                if a.attempt in ("first", "retry"):
                    final[a.variant] = a
            known = [a for a in final.values() if not a.fetch_error and not a.env_error]
            if not known:
                continue
            repos = {path: plan.lock[path].sha for path in cell.repos}
            path = base_path(r.app, r.compiler, plan.catalog.sha, repos, r.tools)
            self.write(path, {"run_url": plan.run_url, "attempts": [a.model_dump() for a in known]})

    def dependents(self, ref: str) -> list[str]:
        try:
            names = sorted(os.listdir(os.path.join(self.path, "deps", pr_key(ref))))
        except OSError:
            return []
        refs = []
        for name in names:
            parts = name.split("__")
            if len(parts) == 3:
                refs.append(f"{parts[0]}/{parts[1]}#{parts[2]}")
        return refs

    def nightly(self, channel: str) -> dict | None:
        return self.read(f"nightly/{channel}/latest.json")

    def set_nightly(self, channel: str, state: dict) -> None:
        self.write(f"nightly/{channel}/latest.json", state)
