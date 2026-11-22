from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from . import apps
from .models import PullRequest

CATALOG = "catalog-core"

DEPENDS_RE = re.compile(r"^[ \t]*Depends-On:[ \t]*(.+?)[ \t]*$", re.I | re.M)
TARGET_RES = [
    re.compile(r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pull/(?P<n>\d+)/?$"),
    re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)#(?P<n>\d+)$"),
    re.compile(r"^(?P<repo>[\w.-]+)#(?P<n>\d+)$"),
    re.compile(r"^#(?P<n>\d+)$"),
]


class Source(Protocol):
    def pull_request(self, repo: str, number: int) -> PullRequest | None: ...
    def contains(self, repo: str, ancestor: str, descendant: str) -> bool: ...


@dataclass
class Found:
    pr: PullRequest
    via: str


@dataclass
class Resolution:
    root: str
    prs: list[Found] = field(default_factory=list)
    used: dict[str, list[Found]] = field(default_factory=dict)
    dropped: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    untrusted: list[str] = field(default_factory=list)
    root_state: str | None = None

    @property
    def trusted(self) -> bool:
        return not self.untrusted


def parse_target(token: str, owner: str, repo: str) -> str | None:
    for regex in TARGET_RES:
        m = regex.match(token)
        if m:
            groups = m.groupdict()
            target_owner = (groups.get("owner") or owner).lower()
            target_repo = (groups.get("repo") or repo).lower()
            return f"{target_owner}/{target_repo}#{groups['n']}"
    return None


def parse_depends_on(text: str, current_repo: str) -> tuple[list[str], list[str]]:
    refs = []
    errors = []
    owner, repo = current_repo.lower().split("/")
    for line in DEPENDS_RE.finditer(text or ""):
        for token in re.split(r"[,\s]+", line.group(1)):
            token = token.strip().rstrip(".;")
            if not token:
                continue
            ref = parse_target(token, owner, repo)
            if ref is None:
                errors.append(f"can't read Depends-On target `{token}` (use a PR URL, owner/repo#N, repo#N or #N)")
            else:
                refs.append(ref)
    return refs, errors


def path_of(repo: str) -> str:
    if repo.rsplit("/", 1)[-1].lower() == CATALOG:
        return CATALOG
    return apps.path_for(repo)


def crawl(gh: Source, res: Resolution, allowed: set[str], max_prs: int) -> list[Found]:
    fetched: list[Found] = []
    seen = {res.root}
    queue = deque([(res.root, "root")])
    while queue:
        ref, via = queue.popleft()
        repo, number = ref.split("#")
        if repo.split("/")[0] not in allowed:
            res.errors.append(f"{ref} (from {via}): only PRs in {', '.join(sorted(allowed))} can be dependencies")
            continue
        try:
            path_of(repo)
        except apps.UnknownRepo:
            res.errors.append(f"{ref} (from {via}): not a repository catalog-core builds with")
            continue
        pr = gh.pull_request(repo, int(number))
        if pr is None:
            res.errors.append(f"{ref} (from {via}): no such pull request")
            continue
        if ref == res.root:
            res.root_state = pr.state
            if pr.state != "open":
                return fetched
        fetched.append(Found(pr, via))
        if len(fetched) > max_prs:
            res.errors.append(f"more than {max_prs} PRs depend on each other here; split the change up")
            break
        texts = [("description", pr.body)]
        for i, message in enumerate(pr.commit_messages):
            texts.append((f"commit {i + 1}", message))
        for where, text in texts:
            deps, errors = parse_depends_on(text, pr.repo)
            for error in errors:
                res.errors.append(f"{pr.ref} {where}: {error}")
            for dep in deps:
                if dep not in seen:
                    seen.add(dep)
                    queue.append((dep, f"Depends-On in {pr.ref} {where}"))
    return fetched


def check(res: Resolution, fetched: list[Found], trusted_associations: set[str], channels: set[str]) -> None:
    root_pr = fetched[0].pr
    if root_pr.base_ref not in channels and path_of(root_pr.repo) != CATALOG:
        res.errors.append(
            f"{root_pr.ref} targets `{root_pr.base_ref}`; only PRs to {', '.join(sorted(channels))} are tested"
        )
    for found in fetched:
        pr = found.pr
        if pr.state == "merged":
            res.dropped[pr.ref] = "already merged, so its code is in the base branch"
        elif pr.state == "closed":
            res.errors.append(f"{pr.ref} is closed without being merged (from {found.via})")
        elif path_of(pr.repo) != CATALOG and pr.base_ref != root_pr.base_ref:
            res.errors.append(
                f"{pr.ref} targets `{pr.base_ref}` but {root_pr.ref} targets `{root_pr.base_ref}`; "
                "PRs tested together must target the same branch"
            )
        else:
            if pr.association not in trusted_associations:
                res.untrusted.append(f"{pr.ref} by @{pr.author} ({pr.association.lower()})")
            res.prs.append(found)


def is_below(gh: Source, lower: Found, upper: Found) -> bool:
    if lower is upper or lower.pr.head_sha == upper.pr.head_sha:
        return False
    return gh.contains(lower.pr.repo, lower.pr.head_sha, upper.pr.head_sha)


def collapse_stacks(gh: Source, res: Resolution) -> None:
    by_path: dict[str, list[Found]] = {}
    for found in res.prs:
        by_path.setdefault(path_of(found.pr.repo), []).append(found)
    for path, group in by_path.items():
        tops = []
        for candidate in group:
            if not any(is_below(gh, candidate, other) for other in group):
                tops.append(candidate)
        for found in group:
            if found in tops:
                continue
            for top in tops:
                if is_below(gh, found, top):
                    res.dropped[found.pr.ref] = f"included in {top.pr.ref}, which is stacked on top of it"
                    break
        res.used[path] = sorted(tops, key=lambda found: found.pr.number)


def check_mergeable(res: Resolution) -> None:
    for group in res.used.values():
        for found in group:
            pr = found.pr
            if pr.mergeable is False:
                res.errors.append(f"{pr.ref} can't be merged into `{pr.base_ref}` (merge conflict); please rebase it")
            elif pr.mergeable is None or not pr.merge_sha:
                res.errors.append(
                    f"GitHub hasn't computed yet whether {pr.ref} merges cleanly; comment `recheck` in a minute"
                )


def resolve(
    gh: Source,
    root: str,
    *,
    allowed_owners: set[str],
    trusted_associations: set[str],
    channels: set[str],
    max_prs: int = 8,
) -> Resolution:
    res = Resolution(root=root.lower())
    allowed = {owner.lower() for owner in allowed_owners}
    fetched = crawl(gh, res, allowed, max_prs)
    if res.root_state != "open":
        return res
    check(res, fetched, trusted_associations, channels)
    collapse_stacks(gh, res)
    check_mergeable(res)
    return res
