"""Turn a GitHub event into a Plan: what to test, at which commits, and why.

Runs in the trusted plan job: it reads the GitHub API and parses (never
executes) the app directories.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field

from . import apps as apps_mod
from . import git, repos
from .github import GitHub
from .models import CatalogRef, Cell, CycleMember, LockEntry, Part, Plan, PlannedPR, PullRequest
from .resolve import CATALOG, Found, Resolution, path_of, resolve

SLUG_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
# Sent by the per-repo dispatchers that are still upstream; same payload shape.
LEGACY_TYPES = {"unikraft_open_pr", "musl_open_pr", "lwip_open_pr", "libelf_open_pr", "elfloader_open_pr"}


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default  # unset repository variables arrive as ''


def _list(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


@dataclass
class Settings:
    """CI policy, from repository variables (see .github/ci/README.md)."""

    trusted_associations: set[str] = field(default_factory=lambda: {"OWNER", "MEMBER", "COLLABORATOR"})
    channels: set[str] = field(default_factory=lambda: {"staging", "stable"})
    compilers: list[str] = field(default_factory=lambda: ["gcc", "clang"])
    max_prs: int = 8
    upstream_org: str = repos.UPSTREAM_ORG
    stable_schedule: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            trusted_associations=set(_list(_env("CI_TRUSTED_ASSOCIATIONS", "OWNER,MEMBER,COLLABORATOR"))),
            channels=set(_list(_env("CI_CHANNELS", "staging,stable"))),
            compilers=_list(_env("CI_COMPILERS", "gcc,clang")),
            max_prs=int(_env("CI_MAX_DEP_PRS", "8")),
            upstream_org=_env("CI_UPSTREAM_ORG", repos.UPSTREAM_ORG),
            stable_schedule=_env("CI_STABLE_SCHEDULE", ""),
        )


def root_from_event(event_name: str, event: dict, repository: str) -> str | None:
    """The PR this run is about ("owner/repo#N"), or None for scheduled/manual runs."""
    if event_name == "repository_dispatch":
        payload = event.get("client_payload") or {}
        repo = payload.get("repo") or payload.get("pr_repo")
        number = str(payload.get("number") or payload.get("pr_number") or "")
        if event.get("action") != "catalog_pr" and event.get("action") not in LEGACY_TYPES:
            raise ValueError(f"unexpected dispatch type {event.get('action')!r}")
        if not repo or not SLUG_RE.match(repo) or not number.isdigit():
            raise ValueError(f"invalid dispatch payload: repo={repo!r} number={number!r}")
        return f"{repo.lower()}#{number}"
    if event_name == "pull_request":
        return f"{repository.lower()}#{event['number']}"
    return None


class Planner:
    def __init__(self, gh: GitHub, settings: Settings, *, repository: str, sha: str):
        self.gh, self.settings = gh, settings
        self.repository = repository  # the catalog-core repo running CI
        self.sha = sha  # the catalog-core commit the workflow runs from
        self._heads: dict[tuple[str, str], str] = {}

    def branch_sha(self, repo: str, branch: str) -> str:
        if (repo, branch) not in self._heads:
            self._heads[repo, branch] = self.gh.branch_sha(repo, branch)
        return self._heads[repo, branch]

    def plan(
        self,
        root: str | None,
        *,
        channel: str | None = None,
        only_apps: list[str] | None = None,
        compilers: list[str] | None = None,
    ) -> Plan:
        p = Plan(root=root, channel=channel or "staging")
        res = self._resolve(p) if root else None
        if p.errors or p.root_closed or p.delegate_to or (root and res is None):
            return finish(p)

        # The apps come from catalog-core: this commit, or a catalog-core PR in the set.
        catalog_pr = res.used[CATALOG][0].pr if res and CATALOG in res.used else None
        apps_dir = "."
        if catalog_pr:
            assert catalog_pr.merge_sha and catalog_pr.merge_base_sha
            p.catalog = CatalogRef(
                repo=catalog_pr.repo,
                sha=catalog_pr.merge_sha,
                source=f"{catalog_pr.ref} merged into {catalog_pr.base_ref}",
            )
            p.catalog_base = CatalogRef(repo=catalog_pr.repo, sha=catalog_pr.merge_base_sha, source=catalog_pr.base_ref)
            apps_dir = tempfile.mkdtemp(prefix="catalog-")
            git.fetch_commit(f"https://github.com/{catalog_pr.repo}", catalog_pr.merge_sha, apps_dir)
        else:
            p.catalog = CatalogRef(repo=self.repository, sha=self.sha, source="this workflow's commit")
        found = apps_mod.discover(apps_dir)
        p.untested_apps = [a.name for a in found if not a.has_tests]

        selected = self._select(res, found, catalog_pr, p.channel)
        if only_apps:
            known = {a.name for a in found if a.has_tests}
            if unknown := sorted(set(only_apps) - known):
                p.errors.append(f"unknown app(s), or apps without tests: {', '.join(unknown)}")
            selected = {name: (a, why) for name, (a, why) in selected.items() if name in only_apps}

        for path in sorted({r for a, _ in selected.values() for r in a.repos}):
            self._pin(p, path, res.used.get(path, []) if res else [])
        if p.errors:
            return finish(p)
        p.cells = [
            Cell(app=name, compiler=c, why=why, repos=a.repos, variants=[v.name for v in a.variants])
            for name, (a, why) in sorted(selected.items())
            for c in (compilers or self.settings.compilers)
        ]
        return finish(p)

    def _resolve(self, p: Plan) -> Resolution | None:
        s = self.settings
        res = resolve(
            self.gh,
            p.root or "",
            allowed_owners={s.upstream_org, self.repository.split("/")[0]},
            trusted_associations=s.trusted_associations,
            channels=s.channels,
            max_prs=s.max_prs,
        )
        if res.root_state in ("merged", "closed"):
            p.root_closed = True  # e.g. a late fan-out: nothing to test, nothing to report
            p.notes.append(f"{p.root} is {res.root_state}; nothing to test")
            return None
        p.errors += res.errors
        if res.root_state != "open":
            return None
        p.untrusted, p.trusted = res.untrusted, res.trusted
        root_pr = res.prs[0].pr
        p.root_repo, p.root_head_sha = root_pr.repo, root_pr.head_sha
        if path_of(root_pr.repo) != CATALOG:
            p.channel = root_pr.base_ref
        used = {f.pr.ref for group in res.used.values() for f in group}
        p.prs = [
            PlannedPR(
                ref=f.pr.ref,
                url=f.pr.url,
                title=f.pr.title,
                head_sha=f.pr.head_sha,
                base_ref=f.pr.base_ref,
                via=f.via,
                used=f.pr.ref in used,
                note=res.dropped.get(f.pr.ref, ""),
            )
            for f in res.prs
        ]
        listed = {x.ref for x in p.prs}
        p.notes += [f"{ref} is not used: {why}" for ref, why in res.dropped.items() if ref not in listed]
        # PRs in a cycle with the root resolve to this same set: test it once,
        # in the run of the first of them; the others wait for that run.
        heads = {f.pr.ref: f.pr for f in res.prs}
        cycle = res.cycle()
        p.cycle = [CycleMember(ref=r, repo=heads[r].repo, head_sha=heads[r].head_sha) for r in cycle if r in heads]
        if len(cycle) > 1 and cycle[0] != p.root and not p.errors:
            p.delegate_to = cycle[0]
            p.notes.append(
                f"{p.root} and {cycle[0]} depend on each other, so they are tested together in the run for {cycle[0]}"
            )
        return res

    def _select(
        self, res: Resolution | None, found: list[apps_mod.App], catalog_pr: PullRequest | None, channel: str
    ) -> dict[str, tuple[apps_mod.App, str]]:
        """App name -> (app, why it is tested)."""
        if res is None:
            return {a.name: (a, f"scheduled or manual run of {channel}") for a in found if a.has_tests}
        changed = [path for path in res.used if path != CATALOG]
        selected = {
            a.name: (a, "links " + ", ".join(r for r in a.repos if r in changed))
            for a in apps_mod.affected(found, changed)
        }
        if catalog_pr:
            touched = self._catalog_changes(catalog_pr, found)
            for a in found:
                if a.has_tests and a.name not in selected and (touched is None or a.name in touched):
                    why = f"changed by {catalog_pr.ref}" if touched else f"{catalog_pr.ref} changes shared files"
                    selected[a.name] = (a, why)
        return selected

    def _catalog_changes(self, pr: PullRequest, found: list[apps_mod.App]) -> set[str] | None:
        """Apps a catalog-core PR touches; None if it touches shared files."""
        names, touched = {a.name for a in found}, set()
        for f in self.gh.pr_files(pr.repo, pr.number):
            top = f.split("/", 1)[0]
            if top in names and "/" in f:
                touched.add(top)
            elif not f.endswith(".md"):
                return None
        return touched

    def _pin(self, p: Plan, path: str, prs: list[Found]) -> None:
        """One commit per repo (lock), and the same without the PRs (base_lock)."""
        if not prs:
            repo = repos.slug_for(path, self.settings.upstream_org)
            p.lock[path] = p.base_lock[path] = LockEntry(
                repo=repo, sha=self.branch_sha(repo, p.channel), source=p.channel
            )
            return
        first = prs[0].pr
        if len(prs) == 1:
            assert first.merge_sha and first.merge_base_sha
            p.lock[path] = LockEntry(
                repo=first.repo, sha=first.merge_sha, pr=first.ref, source=f"{first.ref} merged into {first.base_ref}"
            )
            p.base_lock[path] = LockEntry(repo=first.repo, sha=first.merge_base_sha, source=first.base_ref)
            return
        # Several PRs, not stacked: merge them together onto the branch.
        label = " + ".join(f.pr.ref for f in prs)
        base_sha = self.branch_sha(first.repo, first.base_ref)
        parts = [Part(label=f.pr.ref, sha=f.pr.head_sha) for f in prs]
        try:
            with tempfile.TemporaryDirectory(prefix="combine-") as tmp:
                sha = git.combine(f"https://github.com/{first.repo}", base_sha, parts, tmp)
        except git.Conflict as e:
            p.errors.append(
                f"{label} are needed together, but {e.part} conflicts with {' + '.join(e.merged)} "
                f"in {', '.join(e.files[:5]) or 'some files'}. CI can't resolve that: rebase {e.part} "
                "onto the other PR and add a Depends-On between them"
            )
            return
        p.lock[path] = LockEntry(
            repo=first.repo,
            sha=sha,
            pr=label,
            base_sha=base_sha,
            parts=parts,
            source=f"{label} merged together into {first.base_ref}",
        )
        p.base_lock[path] = LockEntry(repo=first.repo, sha=base_sha, source=first.base_ref)


def finish(p: Plan) -> Plan:
    if p.errors:
        p.cells, p.lock, p.base_lock = [], {}, {}
    hashed = json.dumps(
        {
            "lock": {k: v.model_dump() for k, v in p.lock.items()},
            "catalog": p.catalog.sha if p.catalog else None,
            "cells": [c.model_dump() for c in p.cells],
        },
        sort_keys=True,
    )
    p.plan_hash = hashlib.sha256(hashed.encode()).hexdigest()
    return p


def outputs(p: Plan, repository: str, sha: str) -> dict[str, str]:
    """$GITHUB_OUTPUT of the plan job."""
    owner, _, name = (p.root_repo or "/").partition("/")
    check_repos = {m.repo.split("/")[1] for m in p.cycle if m.repo.split("/")[0] == owner} | {name}
    matrix = [{"app": c.app, "compiler": c.compiler} for c in p.cells]
    return {
        "ok": str(p.ok).lower(),
        "trusted": str(p.trusted).lower(),
        "cells": json.dumps(matrix),
        "has_cells": str(bool(matrix)).lower(),
        "catalog_repo": p.catalog.repo if p.catalog else repository,
        "catalog_sha": p.catalog.sha if p.catalog else sha,
        "root_repo_owner": owner,
        "root_repo_name": name,
        "check_repos": ",".join(sorted(check_repos - {""})),
    }


def main(args) -> int:
    event_name = os.environ["GITHUB_EVENT_NAME"]
    with open(os.environ["GITHUB_EVENT_PATH"]) as f:
        event = json.load(f)
    repository, sha = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
    settings = Settings.from_env()
    inputs = event.get("inputs") or {}
    try:
        root = root_from_event(event_name, event, repository)
    except ValueError as e:
        p = finish(Plan(errors=[str(e)], trusted=False))
    else:
        channel = inputs.get("channel")
        if event_name == "schedule":
            channel = "stable" if event.get("schedule") == settings.stable_schedule else "staging"
        p = Planner(GitHub(), settings, repository=repository, sha=sha).plan(
            root,
            channel=channel,
            only_apps=_list(inputs.get("apps") or ""),
            compilers=_list(inputs.get("compilers") or "") or None,
        )
    p.event = event_name
    p.fan_out = (
        event_name == "repository_dispatch"
        and str((event.get("client_payload") or {}).get("cascade", "true")).lower() != "false"
    )
    p.run_url = (
        f"{_env('GITHUB_SERVER_URL', 'https://github.com')}/{repository}/actions/runs/{_env('GITHUB_RUN_ID', '0')}"
    )

    with open(args.out, "w") as f:
        f.write(p.model_dump_json(indent=2))
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as f:
            f.writelines(f"{k}={v}\n" for k, v in outputs(p, repository, sha).items())
    for err in p.errors:
        print(f"::error::{err}")
    print(f"{p.root or p.channel}: {p.n_configs} configurations in {len(p.cells)} jobs, plan {p.plan_hash[:10]}")
    return 0
