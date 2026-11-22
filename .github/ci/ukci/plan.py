"""Turn a GitHub event into plan.json: what to test, at which commits, and why.

Runs in the trusted plan job. It reads the GitHub API and parses (never
executes) the app directories. Output:

  plan.json            everything later jobs need, plus explanations
  $GITHUB_OUTPUT       cells (matrix), trusted, ok, catalog_sha, ...
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile

from . import apps as apps_mod
from . import repos
from .gh import GitHub
from .resolve import CATALOG, Ref, resolve

SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# Legacy per-repo dispatch event types still sent by older callers.
LEGACY_TYPES = {"unikraft_open_pr", "musl_open_pr", "lwip_open_pr", "libelf_open_pr", "elfloader_open_pr"}


def env_list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


class Planner:
    def __init__(self, gh: GitHub, *, repository: str, sha: str, upstream_org: str = repos.UPSTREAM_ORG):
        self.gh = gh
        self.repository = repository             # catalog-core repo running CI, "owner/catalog-core"
        self.sha = sha                           # catalog-core commit the workflow runs from
        self.upstream_org = upstream_org
        self._branch_cache: dict[tuple[str, str], str] = {}

    def branch_sha(self, repo: str, branch: str) -> str:
        key = (repo, branch)
        if key not in self._branch_cache:
            self._branch_cache[key] = self.gh.branch_sha(repo, branch)
        return self._branch_cache[key]

    def merge_base_of(self, repo: str, merge_sha: str) -> str:
        """First parent of GitHub's test merge = the base commit it merged onto."""
        return self.gh.get(f"/repos/{repo}/commits/{merge_sha}")["parents"][0]["sha"]

    def plan(self, *, root: Ref | None, channel: str | None, only_apps: list[str],
             compilers: list[str], trusted_associations: set[str], channels: set[str],
             max_prs: int, catalog_root: str | None = None) -> dict:
        plan: dict = {"version": 1, "root": str(root) if root else None, "errors": [], "notes": [],
                      "prs": [], "dropped": [], "untrusted": [], "trusted": True}
        res = None
        if root:
            res = resolve(self.gh, root, allowed_owners={self.upstream_org, self.repository.split("/")[0]},
                          trusted_associations=trusted_associations, channels=channels, max_prs=max_prs)
            plan["errors"] += res.errors
            plan["dropped"] = [{"pr": r, "reason": why} for r, why in res.dropped]
            plan["untrusted"] = res.untrusted
            plan["trusted"] = res.trusted
            selected_ids = {id(f) for f in res.selected.values()}
            plan["prs"] = [{"pr": f.pr.ref, "url": f.pr.url, "title": f.pr.title, "head_sha": f.pr.head_sha,
                            "merge_sha": f.pr.merge_sha, "base_ref": f.pr.base_ref, "via": f.via,
                            "used": id(f) in selected_ids}
                           for f in res.prs]
            if res.prs:
                root_pr = res.prs[0].pr
                plan["root_head_sha"] = root_pr.head_sha
                plan["root_repo"] = root_pr.repo
                channel = root_pr.base_ref if root_pr.repo.split("/")[1] != "catalog-core" else (channel or "staging")
        channel = channel or "staging"
        plan["channel"] = channel
        if plan["errors"]:
            return self._finish(plan, [], {}, {}, None)

        # catalog-core itself: a catalog-core PR in the set provides the apps.
        catalog_pr = res.selected.get(CATALOG) if res else None
        catalog_repo = catalog_pr.pr.repo if catalog_pr else self.repository
        catalog_sha = catalog_pr.pr.merge_sha if catalog_pr else self.sha
        plan["catalog"] = {"repo": catalog_repo, "sha": catalog_sha,
                           "from": f"{catalog_pr.pr.ref} merged into {catalog_pr.pr.base_ref}" if catalog_pr else "this workflow's commit"}
        root_dir = catalog_root or "."
        tmp = None
        if catalog_pr:
            tmp = tempfile.mkdtemp(prefix="catalog-")
            fetch_commit(f"https://github.com/{catalog_repo}", catalog_sha, tmp)
            root_dir = tmp
        found = apps_mod.discover(root_dir)

        # Which apps are affected, and why.
        dep_paths = [p for p in (res.selected if res else {}) if p != CATALOG]
        if not root:
            selected = [a for a in found if a.has_tests]
            why = {a.name: f"scheduled/manual run on {channel}" for a in selected}
        else:
            selected = apps_mod.affected(found, dep_paths)
            why = {a.name: "links " + ", ".join(p for p in a.repos if p in dep_paths) for a in selected}
            if catalog_pr:
                changed = self.catalog_changed_apps(catalog_pr.pr, found)
                for a in (found if changed is None else [x for x in found if x.name in changed]):
                    if a.has_tests and a.name not in why:
                        selected.append(a)
                        why[a.name] = f"changed by {catalog_pr.pr.ref}" if changed is not None else f"{catalog_pr.pr.ref} changes shared files"
                selected.sort(key=lambda a: a.name)
        if only_apps:
            unknown = sorted(set(only_apps) - {a.name for a in found if a.has_tests})
            if unknown:
                plan["errors"].append(f"unknown app(s) or apps without tests: {', '.join(unknown)}")
            selected = [a for a in selected if a.name in only_apps]
        plan["untested_apps"] = [a.name for a in found if not a.has_tests]

        # Lock: one commit per repo. base_lock: same, without the PRs.
        lock, base_lock = {}, {}
        for path in sorted({p for a in selected for p in a.repos}):
            f = res.selected.get(path) if res else None
            if f:
                lock[path] = {"repo": f.pr.repo, "sha": f.pr.merge_sha, "pr": f.pr.ref,
                              "from": f"{f.pr.ref} merged into {f.pr.base_ref}"}
                base_lock[path] = {"repo": f.pr.repo, "sha": self.merge_base_of(f.pr.repo, f.pr.merge_sha),
                                   "from": f"{f.pr.base_ref} (the commit {f.pr.ref} was merged onto)"}
            else:
                repo = repos.slug_for(path, self.upstream_org)
                entry = {"repo": repo, "sha": self.branch_sha(repo, channel), "from": channel}
                lock[path] = entry
                base_lock[path] = dict(entry)

        cells = [{"app": a.name, "compiler": c, "why": why[a.name],
                  "repos": a.repos, "variants": [v.name for v in a.variants]}
                 for a in selected for c in compilers]
        return self._finish(plan, cells, lock, base_lock, catalog_sha)

    def catalog_changed_apps(self, pr, found) -> set[str] | None:
        """App names a catalog-core PR touches; None if it touches shared files."""
        names = {a.name for a in found}
        changed = set()
        for f in self.gh.pr_files(pr.repo, pr.number):
            top = f.split("/", 1)[0]
            if top in names and "/" in f:
                changed.add(top)
            elif not f.endswith(".md"):
                return None
        return changed

    def _finish(self, plan, cells, lock, base_lock, catalog_sha) -> dict:
        plan["cells"] = cells
        plan["lock"] = lock
        plan["base_lock"] = base_lock
        hashed = json.dumps({"lock": lock, "catalog": catalog_sha, "cells": cells}, sort_keys=True)
        plan["plan_hash"] = hashlib.sha256(hashed.encode()).hexdigest()
        return plan


def fetch_commit(url: str, sha: str, dest: str) -> None:
    """Shallow-fetch exactly one commit into `dest` and check it out."""
    def git(*args):
        subprocess.run(["git", *args], check=True, cwd=dest, stdout=subprocess.DEVNULL)
    os.makedirs(dest, exist_ok=True)
    git("init", "-q")
    git("-c", "protocol.version=2", "fetch", "-q", "--no-tags", "--depth", "1", url, sha)
    git("-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD")


def root_from_event(event_name: str, event: dict, repository: str) -> Ref | None:
    if event_name == "repository_dispatch":
        payload = event.get("client_payload") or {}
        repo = payload.get("repo") or payload.get("pr_repo")
        number = str(payload.get("number") or payload.get("pr_number") or "")
        action = event.get("action")
        if action != "catalog_pr" and action not in LEGACY_TYPES:
            raise ValueError(f"unexpected dispatch type {action!r}")
        if not repo or not SLUG_RE.match(repo) or not number.isdigit():
            raise ValueError(f"invalid dispatch payload: repo={repo!r} number={number!r}")
        return Ref(repo.lower(), int(number))
    if event_name in ("pull_request", "pull_request_target"):
        return Ref(repository.lower(), int(event["number"]))
    return None


def main(args) -> int:
    event_name = os.environ["GITHUB_EVENT_NAME"]
    with open(os.environ["GITHUB_EVENT_PATH"]) as f:
        event = json.load(f)
    repository = os.environ["GITHUB_REPOSITORY"]
    inputs = event.get("inputs") or {}

    planner = Planner(GitHub(), repository=repository, sha=os.environ["GITHUB_SHA"],
                      upstream_org=os.environ.get("CI_UPSTREAM_ORG", repos.UPSTREAM_ORG))
    try:
        root = root_from_event(event_name, event, repository)
    except ValueError as e:
        plan = planner._finish({"version": 1, "root": None, "errors": [str(e)], "prs": [], "trusted": False,
                                "channel": "staging"}, [], {}, {}, None)
    else:
        plan = planner.plan(
            root=root,
            channel=inputs.get("channel") or None,
            only_apps=[a for a in (inputs.get("apps") or "").replace(" ", "").split(",") if a],
            compilers=[c for c in (inputs.get("compilers") or ",".join(env_list("CI_COMPILERS", "gcc,clang"))).split(",") if c],
            trusted_associations=set(env_list("CI_TRUSTED_ASSOCIATIONS", "OWNER,MEMBER,COLLABORATOR")),
            channels=set(env_list("CI_CHANNELS", "staging,stable")),
            max_prs=int(os.environ.get("CI_MAX_DEP_PRS", "8")),
        )
    plan["event"] = event_name
    plan["run_url"] = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repository}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '0')}"

    with open(args.out, "w") as f:
        json.dump(plan, f, indent=2)

    ok = not plan["errors"]
    matrix = [{"app": c["app"], "compiler": c["compiler"]} for c in plan["cells"]] if ok else []
    outputs = {
        "ok": str(ok).lower(),
        "trusted": str(plan["trusted"]).lower(),
        "cells": json.dumps(matrix),
        "has_cells": str(bool(matrix)).lower(),
        "catalog_repo": (plan.get("catalog") or {}).get("repo", repository),
        "catalog_sha": (plan.get("catalog") or {}).get("sha", os.environ["GITHUB_SHA"]),
        "root_repo": plan.get("root_repo", ""),
        "root_repo_owner": (plan.get("root_repo") or "/").split("/")[0],
        "root_repo_name": (plan.get("root_repo") or "/").split("/")[1],
        "root_head_sha": plan.get("root_head_sha", ""),
    }
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            for k, v in outputs.items():
                f.write(f"{k}={v}\n")
    for e in plan["errors"]:
        print(f"::error::{e}")
    print(json.dumps({k: plan[k] for k in ("root", "channel", "trusted", "plan_hash")}, indent=2))
    print(f"{len(matrix)} cells: {', '.join(sorted({c['app'] for c in plan['cells']}))}")
    return 0
