"""The ``ci-data`` branch: results as data, written only by trusted jobs.

Layout (every file is written once and never edited, except
``nightly/<channel>/latest.json``, which only the scheduled run writes):

  runs/<yyyy>/<mm>/<dd>/<run_id>-<attempt>.json   plan + verdicts + timings of one run
  pr/<owner>__<repo>/<n>/<head_sha>.json           latest verdicts of a PR at that head
  deps/<owner>__<repo>/<n>/<owner>__<repo>__<m>    marker: PR m was tested with PR n
  nightly/<channel>/latest.json                    last scheduled verdicts (for Discord)

Because files are never edited by two runs, concurrent pushes rebase
cleanly; a push is retried a few times.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import subprocess
import time

BRANCH = "ci-data"


def key(ref: str) -> str:
    """'owner/repo#12' -> ('owner__repo', '12') path segments."""
    repo, _, n = ref.partition("#")
    return f"{repo.replace('/', '__')}/{n}"


class Store:
    """A local checkout of the ci-data branch."""

    def __init__(self, repo: str, token: str | None, path: str = "ci-data", url: str | None = None):
        self.repo, self.token, self.path = repo, token, path
        self.url = url or f"https://github.com/{repo}"

    def _env(self) -> dict:
        env = dict(os.environ)
        if self.token:
            auth = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env.update({"GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth}"})
        return env

    def git(self, *args, check=True, cwd=None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=cwd or self.path, env=self._env(), check=check,
                              capture_output=True, text=True)

    def open(self) -> "Store":
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

    def read(self, rel: str):
        try:
            with open(os.path.join(self.path, rel)) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def listdir(self, rel: str) -> list[str]:
        try:
            return sorted(os.listdir(os.path.join(self.path, rel)))
        except OSError:
            return []

    def write(self, rel: str, obj) -> None:
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            if isinstance(obj, str):
                f.write(obj)
            else:
                json.dump(obj, f, indent=1, sort_keys=True)

    def commit_and_push(self, message: str, attempts: int = 5) -> bool:
        self.git("add", "-A")
        if self.git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return True
        self.git("commit", "-q", "-m", message)
        for i in range(attempts):
            if self.git("push", "-q", "origin", f"HEAD:{BRANCH}", check=False).returncode == 0:
                return True
            # Someone else pushed: replay our new files on top of theirs.
            if self.git("fetch", "-q", "--depth", "50", "origin", BRANCH, check=False).returncode == 0:
                self.git("rebase", "-q", "FETCH_HEAD", check=False)
            time.sleep(2 * (i + 1))
        print(f"::warning::could not push to {BRANCH} after {attempts} attempts")
        return False


def record_run(store: Store, plan: dict, report: dict, results: list[dict]) -> None:
    run_id = os.environ.get("GITHUB_RUN_ID", "0")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    now = dt.datetime.now(dt.timezone.utc)
    timings = [{"app": r["app"], "compiler": r["compiler"], **{k: a.get(k) for k in
                ("variant", "attempt", "build", "run", "build_s", "run_s", "total_s")}}
               for r in results for a in r.get("attempts", [])]
    record = {"run_id": run_id, "attempt": attempt, "time": now.isoformat(timespec="seconds"),
              "event": plan.get("event"), "channel": plan.get("channel"), "root": plan.get("root"),
              "run_url": plan.get("run_url"), "plan_hash": plan.get("plan_hash"),
              "prs": plan.get("prs", []), "lock": plan.get("lock", {}), "catalog": plan.get("catalog"),
              "conclusion": report["conclusion"], "title": report["title"],
              "verdicts": report["verdicts"], "timings": timings,
              "tools": next((r.get("tools") for r in results if r.get("tools")), None)}
    store.write(f"runs/{now:%Y/%m/%d}/{run_id}-{attempt}.json", record)

    if plan.get("root") and plan.get("root_head_sha"):
        store.write(f"pr/{key(plan['root'])}/{plan['root_head_sha']}.json",
                    {"run_url": plan.get("run_url"), "plan_hash": plan.get("plan_hash"),
                     "conclusion": report["conclusion"], "verdicts": report["verdicts"]})
        for p in plan.get("prs", []):
            if p["pr"] != plan["root"]:
                store.write(f"deps/{key(p['pr'])}/{plan['root'].replace('/', '__').replace('#', '__')}", "")


def dependents(store: Store, ref: str) -> list[str]:
    """PRs that were last tested together with `ref` (and so depend on it)."""
    out = []
    for name in store.listdir(f"deps/{key(ref)}"):
        owner, repo, n = name.split("__")
        out.append(f"{owner}/{repo}#{n}")
    return out


def own_result(store: Store, ref: str, head_sha: str) -> dict | None:
    return store.read(f"pr/{key(ref)}/{head_sha}.json")
