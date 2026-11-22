from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import traceback
from dataclasses import dataclass

from pydantic import ValidationError

from . import apps, data, git
from .github import GitHub
from .models import CatalogRef, Cell, LockEntry, Part, Plan, PlannedPR, PullRequest, Reused
from .resolve import CATALOG, Found, Resolution, resolve
from .run import COMPILERS

SLUG_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
CHANNELS = ("staging", "stable")
APPROVAL_ENVIRONMENT = "ci-needs-approval"


def split_list(value: str | None) -> list[str]:
    items = []
    for item in (value or "").split(","):
        if item.strip():
            items.append(item.strip())
    return items


@dataclass
class Settings:
    trusted_associations: frozenset[str]
    max_prs: int
    stable_schedule: str

    @classmethod
    def from_env(cls) -> Settings:
        associations = os.environ.get("CI_TRUSTED_ASSOCIATIONS") or "OWNER,MEMBER,COLLABORATOR"
        return cls(
            trusted_associations=frozenset(split_list(associations)),
            max_prs=int(os.environ.get("CI_MAX_DEP_PRS") or "8"),
            stable_schedule=os.environ.get("CI_STABLE_SCHEDULE") or "",
        )


@dataclass
class Trigger:
    root: str | None
    comment_id: int | None = None
    fan_out: bool = False


class BadEvent(ValueError):
    pass


def trigger(event_name: str, event: dict, repository: str) -> Trigger:
    if event_name == "pull_request":
        return Trigger(f"{repository.lower()}#{event['number']}")
    if event_name != "workflow_dispatch":
        return Trigger(None)
    inputs = event.get("inputs") or {}
    repo = str(inputs.get("repo") or "")
    number = str(inputs.get("number") or "")
    comment = str(inputs.get("comment") or "")
    if not repo and not number:
        return Trigger(None)
    if not SLUG_RE.match(repo) or not number.isdigit():
        raise BadEvent(f"invalid inputs: repo={repo!r} number={number!r}")
    if comment and not comment.isdigit():
        raise BadEvent(f"invalid inputs: comment={comment!r}")
    comment_id = int(comment) if comment else None
    fan_out = str(inputs.get("cascade") or "true").lower() != "false"
    return Trigger(f"{repo.lower()}#{number}", comment_id, fan_out)


def schedule_channel(event: dict, settings: Settings) -> str:
    if event.get("schedule") == settings.stable_schedule:
        return "stable"
    return "staging"


class Planner:
    def __init__(self, gh: GitHub, settings: Settings, repository: str, sha: str):
        self.gh = gh
        self.settings = settings
        self.repository = repository
        self.sha = sha
        self.heads: dict[tuple[str, str], str] = {}

    def branch_sha(self, repo: str, branch: str) -> str:
        if (repo, branch) not in self.heads:
            self.heads[repo, branch] = self.gh.branch_sha(repo, branch)
        return self.heads[repo, branch]

    def plan(
        self,
        p: Plan,
        t: Trigger,
        channel: str = "staging",
        only_apps: list[str] | None = None,
        compilers: list[str] | None = None,
    ) -> Plan:
        p.root = t.root
        p.channel = channel
        p.recheck = t.comment_id is not None
        p.fan_out = t.fan_out
        if channel not in CHANNELS:
            p.errors.append(f"unknown channel {channel!r}; one of {', '.join(CHANNELS)}")
        unknown_compilers = sorted(set(compilers or []) - set(COMPILERS))
        if unknown_compilers:
            p.errors.append(f"unknown compiler(s) {', '.join(unknown_compilers)}; known: {', '.join(COMPILERS)}")
        res = None
        if t.root:
            res = self.resolve(p, t)
            if res is None:
                return finish(p)
        if p.errors or p.skip:
            return finish(p)

        catalog_pr = None
        if res is not None and CATALOG in res.used:
            catalog_pr = res.used[CATALOG][0].pr
        found = self.discover(p, catalog_pr)
        selected = self.select(res, found, catalog_pr, p.channel)
        if only_apps:
            known = {app.name for app in found if app.has_tests}
            unknown_apps = sorted(set(only_apps) - known)
            if unknown_apps:
                p.errors.append(f"unknown app(s), or apps without tests: {', '.join(unknown_apps)}")
            selected = {name: entry for name, entry in selected.items() if name in only_apps}
        if catalog_pr is not None:
            self.check_apps(p, found, strict=True)
        else:
            self.check_apps(p, [app for app, _ in selected.values()], strict=False)

        paths = sorted({path for app, _ in selected.values() for path in app.repos})
        for path in paths:
            prs = res.used.get(path, []) if res else []
            self.pin(p, path, prs)
        if p.errors:
            return finish(p)
        for name in sorted(selected):
            app, why = selected[name]
            for compiler in compilers or list(COMPILERS):
                variants = [v.name for v in app.variants]
                p.cells.append(Cell(app=name, compiler=compiler, why=why, repos=app.repos, variants=variants))
        if not p.trusted and p.cells:
            problem = self.approval_unprotected()
            if problem:
                p.errors.append(problem)
                return finish(p)
        finish(p)
        if p.root and p.cells and not p.recheck:
            p.reused = self.reusable(p.plan_hash)
        return p

    def resolve(self, p: Plan, t: Trigger) -> Resolution | None:
        res = resolve(
            self.gh,
            p.root or "",
            allowed_owners={apps.UPSTREAM_ORG, self.repository.split("/")[0]},
            trusted_associations=set(self.settings.trusted_associations),
            channels=set(CHANNELS),
            max_prs=self.settings.max_prs,
        )
        if res.root_state in ("merged", "closed"):
            p.skip = f"{p.root} is {res.root_state}"
            return None
        p.errors += res.errors
        if res.root_state != "open":
            return None
        root_pr = res.prs[0].pr
        if t.comment_id is not None:
            refused = self.recheck_refused(root_pr, t.comment_id)
            if refused:
                p.skip = refused
                return None
        p.untrusted = res.untrusted
        p.trusted = res.trusted
        p.root_repo = root_pr.repo
        p.root_head_sha = root_pr.head_sha
        if res.channel:
            p.channel = res.channel
        p.notes += res.notes
        used = set()
        for group in res.used.values():
            for found in group:
                used.add(found.pr.ref)
        for found in res.prs:
            pr = found.pr
            p.prs.append(
                PlannedPR(
                    ref=pr.ref,
                    url=pr.url,
                    title=pr.title,
                    head_sha=pr.head_sha,
                    base_ref=pr.base_ref,
                    via=found.via,
                    used=pr.ref in used,
                    note=res.dropped.get(pr.ref, ""),
                )
            )
        listed = {pr.ref for pr in p.prs}
        for ref, why in res.dropped.items():
            if ref not in listed:
                p.notes.append(f"{ref} is not used: {why}")
        return res

    def recheck_refused(self, pr: PullRequest, comment_id: int) -> str:
        comment = self.gh.comment(pr.repo, comment_id)
        if comment is None or not str(comment.get("issue_url", "")).endswith(f"/issues/{pr.number}"):
            return f"comment {comment_id} is not on {pr.ref}"
        if (comment.get("body") or "").strip().lower() != "recheck":
            return f"comment {comment_id} is not a `recheck`"
        login = (comment.get("user") or {}).get("login", "")
        if comment.get("author_association") in self.settings.trusted_associations or login == pr.author:
            return ""
        return f"`recheck` by @{login} ignored: only trusted members and the PR's author can ask for one"

    def approval_unprotected(self) -> str:
        env = self.gh.environment(self.repository, APPROVAL_ENVIRONMENT)
        rules = (env or {}).get("protection_rules") or []
        if any(rule.get("type") == "required_reviewers" for rule in rules):
            return ""
        return (
            f"not running code by untrusted authors: the `{APPROVAL_ENVIRONMENT}` environment of "
            f"{self.repository} doesn't require a reviewer"
        )

    def reusable(self, plan_hash: str) -> Reused | None:
        try:
            raw = self.gh.file(self.repository, data.plan_path(plan_hash), data.BRANCH)
            if raw is None:
                return None
            return Reused.model_validate_json(raw)
        except (ValidationError, RuntimeError) as e:
            print(f"::warning::not reusing an earlier result: {e}")
            return None

    def discover(self, p: Plan, catalog_pr: PullRequest | None) -> list[apps.App]:
        if catalog_pr is None:
            p.catalog = CatalogRef(repo=self.repository, sha=self.sha, source="this workflow's commit")
            found = apps.discover(".")
        else:
            assert catalog_pr.merge_sha and catalog_pr.merge_base_sha
            source = f"{catalog_pr.ref} merged into {catalog_pr.base_ref}"
            p.catalog = CatalogRef(repo=catalog_pr.repo, sha=catalog_pr.merge_sha, source=source)
            p.catalog_base = CatalogRef(repo=catalog_pr.repo, sha=catalog_pr.merge_base_sha, source=catalog_pr.base_ref)
            apps_dir = tempfile.mkdtemp(prefix="catalog-")
            git.fetch_commit(f"https://github.com/{catalog_pr.repo}", catalog_pr.merge_sha, apps_dir)
            found = apps.discover(apps_dir)
        p.untested_apps = [app.name for app in found if not app.has_tests]
        return found

    def check_apps(self, p: Plan, selected: list[apps.App], strict: bool) -> None:
        for app in selected:
            for problem in app.problems:
                message = f"{app.name}: {problem}"
                if strict:
                    p.errors.append(message)
                else:
                    p.notes.append(message)

    def select(
        self, res: Resolution | None, found: list[apps.App], catalog_pr: PullRequest | None, channel: str
    ) -> dict[str, tuple[apps.App, str]]:
        if res is None:
            return {app.name: (app, f"{channel}, all apps") for app in found if app.has_tests}
        changed = [path for path in res.used if path != CATALOG]
        selected = {}
        for app in apps.affected(found, changed):
            links = [path for path in app.repos if path in changed]
            selected[app.name] = (app, "links " + ", ".join(links))
        if catalog_pr is None:
            return selected
        touched = self.catalog_changes(catalog_pr, found)
        for app in found:
            if app.name in selected or not app.has_tests:
                continue
            if touched is None:
                selected[app.name] = (app, f"{catalog_pr.ref} changes shared files")
            elif app.name in touched:
                selected[app.name] = (app, f"changed by {catalog_pr.ref}")
        return selected

    def catalog_changes(self, pr: PullRequest, found: list[apps.App]) -> set[str] | None:
        names = {app.name for app in found}
        touched = set()
        for path in self.gh.pr_files(pr.repo, pr.number):
            top = path.split("/", 1)[0]
            if top in names and "/" in path:
                touched.add(top)
            elif not path.endswith(".md"):
                return None
        return touched

    def pin(self, p: Plan, path: str, prs: list[Found]) -> None:
        if not prs:
            repo = apps.slug_for(path)
            entry = LockEntry(repo=repo, sha=self.branch_sha(repo, p.channel), source=p.channel)
            p.lock[path] = entry
            p.base_lock[path] = entry
            return
        first = prs[0].pr
        if len(prs) == 1:
            assert first.merge_sha and first.merge_base_sha
            source = f"{first.ref} merged into {first.base_ref}"
            p.lock[path] = LockEntry(repo=first.repo, sha=first.merge_sha, pr=first.ref, source=source)
            p.base_lock[path] = LockEntry(repo=first.repo, sha=first.merge_base_sha, source=first.base_ref)
            return
        label = " + ".join(found.pr.ref for found in prs)
        base_sha = self.branch_sha(first.repo, first.base_ref)
        parts = [Part(label=found.pr.ref, sha=found.pr.head_sha) for found in prs]
        try:
            with tempfile.TemporaryDirectory(prefix="combine-") as tmp:
                sha = git.combine(f"https://github.com/{first.repo}", base_sha, parts, tmp)
        except git.Conflict as e:
            files = ", ".join(e.files[:5]) or "some files"
            p.errors.append(
                f"{label} are needed together, but {e.part} conflicts with {' + '.join(e.merged)} in {files}. "
                f"Rebase {e.part} onto the other PR"
            )
            return
        source = f"{label} merged together into {first.base_ref}"
        p.lock[path] = LockEntry(repo=first.repo, sha=sha, pr=label, base_sha=base_sha, parts=parts, source=source)
        p.base_lock[path] = LockEntry(repo=first.repo, sha=base_sha, source=first.base_ref)


def finish(p: Plan) -> Plan:
    if p.errors:
        p.cells = []
        p.lock = {}
        p.base_lock = {}
    hashed = {
        "lock": {path: entry.model_dump() for path, entry in p.lock.items()},
        "base_lock": {path: entry.sha for path, entry in p.base_lock.items()},
        "catalog": p.catalog.sha if p.catalog else None,
        "catalog_base": p.catalog_base.sha if p.catalog_base else None,
        "cells": [cell.model_dump() for cell in p.cells],
    }
    p.plan_hash = hashlib.sha256(json.dumps(hashed, sort_keys=True).encode()).hexdigest()
    return p


def outputs(p: Plan, repository: str, sha: str) -> dict[str, str]:
    owner, _, name = (p.root_repo or "/").partition("/")
    run = not p.errors and not p.skip and p.reused is None and bool(p.cells)
    cells = []
    if run:
        cells = [{"app": cell.app, "compiler": cell.compiler} for cell in p.cells]
    return {
        "run": str(run).lower(),
        "environment": "" if p.trusted else APPROVAL_ENVIRONMENT,
        "fan_out": str(p.fan_out and bool(p.root) and not p.skip).lower(),
        "cells": json.dumps(cells),
        "catalog_repo": p.catalog.repo if p.catalog else repository,
        "catalog_sha": p.catalog.sha if p.catalog else sha,
        "root_repo_owner": owner,
        "root_repo_name": name,
    }


def main(args) -> int:
    event_name = os.environ["GITHUB_EVENT_NAME"]
    with open(os.environ["GITHUB_EVENT_PATH"]) as f:
        event = json.load(f)
    repository = os.environ["GITHUB_REPOSITORY"]
    sha = os.environ["GITHUB_SHA"]
    status = 0
    p = Plan()
    try:
        settings = Settings.from_env()
        t = trigger(event_name, event, repository)
        inputs = event.get("inputs") or {}
        channel = inputs.get("channel") or "staging"
        if event_name == "schedule":
            channel = schedule_channel(event, settings)
        compilers = split_list(inputs.get("compilers")) or None
        Planner(GitHub(), settings, repository, sha).plan(p, t, channel, split_list(inputs.get("apps")), compilers)
    except BadEvent as e:
        p = finish(Plan(skip=str(e), trusted=False))
        status = 1
    except Exception as e:
        traceback.print_exc()
        p.trusted = False
        p.reused = None
        p.errors.append(f"CI failed while planning: {str(e)[:300]}. Comment `recheck` to retry")
        finish(p)
        status = 1
    p.event = event_name
    server = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
    p.run_url = f"{server}/{repository}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '0')}"

    text = p.model_dump_json(indent=2)
    with open(args.out, "w") as f:
        f.write(text)
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        values = outputs(p, repository, sha)
        values["digest"] = hashlib.sha256(text.encode()).hexdigest()
        with open(output_path, "a") as f:
            for key, value in values.items():
                f.write(f"{key}={value}\n")
    for error in p.errors:
        print(f"::error::{error}")
    if p.skip:
        print(f"::notice::nothing to do: {p.skip}")
    if p.reused:
        print(f"::notice::these exact commits were tested in {p.reused.run_url}; reusing its result")
    print(f"{p.root or p.channel}: {p.n_configs} configurations in {len(p.cells)} jobs, plan {p.plan_hash[:10]}")
    return status
