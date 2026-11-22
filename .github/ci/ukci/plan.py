"""Turn a GitHub event into a Plan: what to test, at which commits, and why.

Runs in the trusted plan job: it reads the GitHub API and parses (never
executes) the app directories. The dispatch payload is only a pointer
(repo, PR number, comment id); everything else is read back from the API.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import traceback
from dataclasses import dataclass

from pydantic import ValidationError

from . import apps as apps_mod
from . import data, git, repos
from .github import GitHub
from .models import CatalogRef, Cell, LockEntry, Part, Plan, PlannedPR, PullRequest, Reused
from .resolve import CATALOG, Found, Resolution, path_of, resolve
from .run import COMPILERS

SLUG_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
CHANNELS = ("staging", "stable")  # the branches every dependency repo has
APPROVAL_ENVIRONMENT = "ci-needs-approval"  # the test job waits here for untrusted PR sets


def _list(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


@dataclass
class Settings:
    """CI policy: repository variables, documented in .github/ci/README.md."""

    trusted_associations: frozenset[str]
    max_prs: int
    stable_schedule: str  # the cron line of on.schedule that tests stable

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get  # unset repository variables arrive as ''
        return cls(
            trusted_associations=frozenset(_list(env("CI_TRUSTED_ASSOCIATIONS") or "OWNER,MEMBER,COLLABORATOR")),
            max_prs=int(env("CI_MAX_DEP_PRS") or "8"),
            stable_schedule=env("CI_STABLE_SCHEDULE") or "",
        )


@dataclass
class Trigger:
    root: str | None  # "owner/repo#N"; None for scheduled/manual runs
    comment_id: int | None = None  # a `recheck` comment
    fan_out: bool = False


class BadEvent(ValueError):
    pass


def trigger(event_name: str, event: dict, repository: str) -> Trigger:
    if event_name == "repository_dispatch":
        if event.get("action") != "catalog_pr":
            raise BadEvent(f"unexpected dispatch type {event.get('action')!r}")
        payload = event.get("client_payload") or {}
        repo, number, comment = payload.get("repo"), str(payload.get("number", "")), str(payload.get("comment", ""))
        if not isinstance(repo, str) or not SLUG_RE.match(repo) or not number.isdigit():
            raise BadEvent(f"invalid dispatch payload: repo={repo!r} number={number!r}")
        if comment and not comment.isdigit():
            raise BadEvent(f"invalid dispatch payload: comment={comment!r}")
        return Trigger(
            f"{repo.lower()}#{number}",
            comment_id=int(comment) if comment else None,
            fan_out=str(payload.get("cascade", "true")).lower() != "false",
        )
    if event_name == "pull_request":
        return Trigger(f"{repository.lower()}#{event['number']}")
    return Trigger(None)


def schedule_channel(event: dict, settings: Settings) -> str:
    """The channel a scheduled run tests: its cron line says which."""
    return "stable" if event.get("schedule") == settings.stable_schedule else "staging"


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
        t: Trigger,
        *,
        channel: str = "staging",
        only_apps: list[str] | None = None,
        compilers: list[str] | None = None,
        into: Plan | None = None,
    ) -> Plan:
        """Fill in `into` (so that a caller still has what was found if this raises)."""
        p = into if into is not None else Plan()
        p.root, p.channel, p.recheck, p.fan_out = t.root, channel, t.comment_id is not None, t.fan_out
        if channel not in CHANNELS:
            p.errors.append(f"unknown channel {channel!r}; one of {', '.join(CHANNELS)}")
        if unknown := sorted(set(compilers or []) - set(COMPILERS)):
            p.errors.append(f"unknown compiler(s) {', '.join(unknown)}; known: {', '.join(COMPILERS)}")
        res = self._resolve(p, t) if t.root else None
        if p.errors or p.skip or (t.root and res is None):
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
            for c in (compilers or list(COMPILERS))
        ]
        if not p.trusted and p.cells and (why := self._approval_unprotected()):
            p.errors.append(why)
            return finish(p)
        finish(p)
        if p.root and p.cells and not p.recheck:
            p.reused = self._reusable(p.plan_hash)
        return p

    def _approval_unprotected(self) -> str:
        """Fail closed: GitHub would run untrusted code in an environment without reviewers."""
        env = self.gh.environment(self.repository, APPROVAL_ENVIRONMENT)
        if env is None or not any(r.get("type") == "required_reviewers" for r in env.get("protection_rules") or []):
            return (
                f"not running code by untrusted authors: the `{APPROVAL_ENVIRONMENT}` environment of "
                f"{self.repository} doesn't require a reviewer (see .github/ci/README.md, Deploying)"
            )
        return ""

    def _resolve(self, p: Plan, t: Trigger) -> Resolution | None:
        s = self.settings
        res = resolve(
            self.gh,
            p.root or "",
            allowed_owners={repos.UPSTREAM_ORG, self.repository.split("/")[0]},
            trusted_associations=set(s.trusted_associations),
            channels=set(CHANNELS),
            max_prs=s.max_prs,
        )
        if res.root_state in ("merged", "closed"):
            p.skip = f"{p.root} is {res.root_state}"  # e.g. a late fan-out
            return None
        p.errors += res.errors
        if res.root_state != "open":
            return None
        root_pr = res.prs[0].pr
        if t.comment_id is not None and (why := self._recheck_refused(root_pr, t.comment_id)):
            p.skip = why
            return None
        p.untrusted, p.trusted = res.untrusted, res.trusted
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
        return res

    def _recheck_refused(self, pr: PullRequest, comment_id: int) -> str:
        """Why a `recheck` comment doesn't count ('' if it does)."""
        c = self.gh.comment(pr.repo, comment_id)
        if c is None or not str(c.get("issue_url", "")).endswith(f"/issues/{pr.number}"):
            return f"comment {comment_id} is not on {pr.ref}"
        if (c.get("body") or "").strip() != "recheck":
            return f"comment {comment_id} is not a `recheck`"
        login = (c.get("user") or {}).get("login", "")
        if c.get("author_association") not in self.settings.trusted_associations and login != pr.author:
            return f"`recheck` by @{login} ignored: only members and the PR's author can ask for one"
        return ""

    def _reusable(self, plan_hash: str) -> Reused | None:
        """A finished run that tested exactly these commits, if any."""
        try:
            raw = self.gh.file(self.repository, data.plan_path(plan_hash), data.BRANCH)
            return Reused.model_validate_json(raw) if raw else None
        except (ValidationError, RuntimeError) as e:
            print(f"::warning::not reusing an earlier result: {e}")
            return None

    def _select(
        self, res: Resolution | None, found: list[apps_mod.App], catalog_pr: PullRequest | None, channel: str
    ) -> dict[str, tuple[apps_mod.App, str]]:
        """App name -> (app, why it is tested)."""
        if res is None:
            return {a.name: (a, f"{channel}, all apps") for a in found if a.has_tests}
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
            repo = repos.slug_for(path)
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
                f"in {', '.join(e.files[:5]) or 'some files'}. Rebase {e.part} onto the other PR"
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
            "base_lock": {k: v.sha for k, v in p.base_lock.items()},
            "catalog": p.catalog.sha if p.catalog else None,
            "catalog_base": p.catalog_base.sha if p.catalog_base else None,
            "cells": [c.model_dump() for c in p.cells],
        },
        sort_keys=True,
    )
    p.plan_hash = hashlib.sha256(hashed.encode()).hexdigest()
    return p


def outputs(p: Plan, repository: str, sha: str) -> dict[str, str]:
    """$GITHUB_OUTPUT of the plan job. Every value is derived from validated data."""
    owner, _, name = (p.root_repo or "/").partition("/")
    run = p.ok and not p.skip and not p.reused and bool(p.cells)
    return {
        "run": str(run).lower(),
        "environment": "" if p.trusted else APPROVAL_ENVIRONMENT,
        "cells": json.dumps([{"app": c.app, "compiler": c.compiler} for c in p.cells] if run else []),
        "catalog_repo": p.catalog.repo if p.catalog else repository,
        "catalog_sha": p.catalog.sha if p.catalog else sha,
        "root_repo_owner": owner,
        "root_repo_name": name,
    }


def main(args) -> int:
    event_name = os.environ["GITHUB_EVENT_NAME"]
    with open(os.environ["GITHUB_EVENT_PATH"]) as f:
        event = json.load(f)
    repository, sha = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
    status, p = 0, Plan()
    try:
        settings = Settings.from_env()
        t = trigger(event_name, event, repository)
        inputs = event.get("inputs") or {}
        channel = inputs.get("channel") or "staging"
        if event_name == "schedule":
            channel = schedule_channel(event, settings)
        Planner(GitHub(), settings, repository=repository, sha=sha).plan(
            t,
            channel=channel,
            only_apps=_list(inputs.get("apps")),
            compilers=_list(inputs.get("compilers")) or None,
            into=p,
        )
    except BadEvent as e:  # nothing to report on
        p, status = finish(Plan(skip=str(e), trusted=False)), 1
    except Exception as e:  # report it where people look (the check, or Discord), then fail the job
        traceback.print_exc()
        p.trusted, p.reused = False, None
        p.errors.append(f"CI failed while planning: {str(e)[:300]}. Comment `recheck` to retry")
        finish(p)
        status = 1
    p.event = event_name
    server = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
    p.run_url = f"{server}/{repository}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '0')}"

    with open(args.out, "w") as f:
        f.write(p.model_dump_json(indent=2))
    if path := os.environ.get("GITHUB_OUTPUT"):
        with open(path, "a") as f:
            f.writelines(f"{k}={v}\n" for k, v in outputs(p, repository, sha).items())
    for err in p.errors:
        print(f"::error::{err}")
    if p.skip:
        print(f"::notice::nothing to do: {p.skip}")
    if p.reused:
        print(f"::notice::these exact commits were tested in {p.reused.run_url}; reusing its result")
    print(f"{p.root or p.channel}: {p.n_configs} configurations in {len(p.cells)} jobs, plan {p.plan_hash[:10]}")
    return status
