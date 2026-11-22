"""Resolve ``Depends-On:`` between pull requests into the PRs used per repo.

Syntax (like Zuul's, in the PR description or a commit message, at the
start of a line):

    Depends-On: https://github.com/unikraft/unikraft/pull/1745
    Depends-On: unikraft/lib-musl#12
    Depends-On: lib-lwip#40, #7

``repo#N`` is a repo with the same owner as the PR that mentions it, ``#N``
the same repo. Prose like "needs #12" is never a dependency.

The graph is only used to *find* PRs: the result is the PRs used per repo,
so chains, diamonds and cycles all give the same answer without any merge
order. Within one repo, a PR contained in another (a stack) is covered by
the top one; PRs that aren't stacked are merged together by the planner.
Every problem is reported before anything runs, with the fix spelled out.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from . import repos
from .models import PullRequest

CATALOG = "catalog-core"  # pseudo repo path: the catalog itself (the apps)

DEPENDS_RE = re.compile(r"^[ \t]*Depends-On:[ \t]*(.+?)[ \t]*$", re.I | re.M)
TARGET_RES = [
    re.compile(r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pull/(?P<n>\d+)/?$"),
    re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)#(?P<n>\d+)$"),
    re.compile(r"^(?P<repo>[\w.-]+)#(?P<n>\d+)$"),
    re.compile(r"^#(?P<n>\d+)$"),
]


class Source(Protocol):
    """What the resolver needs from GitHub (github.GitHub, or a fake in tests)."""

    def pull_request(self, repo: str, number: int) -> PullRequest | None: ...
    def contains(self, repo: str, ancestor: str, descendant: str) -> bool: ...


@dataclass
class Found:
    pr: PullRequest
    via: str  # how it was found, for people


@dataclass
class Resolution:
    root: str  # "owner/repo#N", lower-case
    prs: list[Found] = field(default_factory=list)  # open PRs in the set
    used: dict[str, list[Found]] = field(default_factory=dict)  # repo path -> PRs whose code is used
    dropped: dict[str, str] = field(default_factory=dict)  # ref -> why it isn't used
    errors: list[str] = field(default_factory=list)
    untrusted: list[str] = field(default_factory=list)
    root_state: str | None = None  # open | merged | closed | None (not found)
    edges: set[tuple[str, str]] = field(default_factory=set)

    @property
    def trusted(self) -> bool:
        return not self.untrusted

    def cycle(self) -> list[str]:
        """Open PRs that depend on the root *and* that the root depends on.

        They all resolve to this same set, so one run tests them all.
        """

        def reach(forward: bool) -> set[str]:
            seen, todo = {self.root}, [self.root]
            while todo:
                n = todo.pop()
                for a, b in self.edges:
                    x, y = (a, b) if forward else (b, a)
                    if x == n and y not in seen:
                        seen.add(y)
                        todo.append(y)
            return seen

        open_refs = {f.pr.ref for f in self.prs}
        return sorted(reach(True) & reach(False) & (open_refs | {self.root}))


def parse_depends_on(text: str, current_repo: str) -> tuple[list[str], list[str]]:
    """(refs, errors) for every Depends-On line in `text`."""
    refs, errors = [], []
    owner, repo = current_repo.lower().split("/")
    for line in DEPENDS_RE.finditer(text or ""):
        for token in filter(None, (t.strip().rstrip(".;") for t in re.split(r"[,\s]+", line.group(1)))):
            m = next((m for r in TARGET_RES if (m := r.match(token))), None)
            if m is None:
                errors.append(f"can't read Depends-On target `{token}` (use a PR URL, owner/repo#N, repo#N or #N)")
                continue
            g = m.groupdict()
            refs.append(f"{(g.get('owner') or owner).lower()}/{(g.get('repo') or repo).lower()}#{g['n']}")
    return refs, errors


def path_of(repo: str) -> str:
    """Repo -> local path; the catalog itself is CATALOG."""
    return CATALOG if repo.rsplit("/", 1)[-1].lower() == "catalog-core" else repos.path_for(repo)


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
    allowed = {o.lower() for o in allowed_owners}

    # 1. Crawl the Depends-On graph, breadth first.
    fetched: list[Found] = []
    seen, queue = {res.root}, deque([(res.root, "root")])
    while queue:
        ref, via = queue.popleft()
        repo, number = ref.split("#")
        if repo.split("/")[0] not in allowed:
            res.errors.append(f"{ref} (from {via}): only PRs in {', '.join(sorted(allowed))} can be dependencies")
            continue
        try:
            path_of(repo)
        except repos.UnknownRepo:
            res.errors.append(f"{ref} (from {via}): not a repository catalog-core builds with")
            continue
        pr = gh.pull_request(repo, int(number))
        if pr is None:
            res.errors.append(f"{ref} (from {via}): no such pull request")
            continue
        if ref == res.root:
            res.root_state = pr.state
            if pr.state != "open":
                return res  # merged or closed meanwhile: nothing to test
        fetched.append(Found(pr, via))
        if len(fetched) > max_prs:
            res.errors.append(f"more than {max_prs} PRs depend on each other here; split the change up")
            break
        for where, text in [("description", pr.body)] + [
            (f"commit {i + 1}", m) for i, m in enumerate(pr.commit_messages)
        ]:
            deps, errs = parse_depends_on(text, pr.repo)
            res.errors += [f"{pr.ref} {where}: {e}" for e in errs]
            for d in deps:
                res.edges.add((pr.ref, d))
                if d not in seen:
                    seen.add(d)
                    queue.append((d, f"Depends-On in {pr.ref} {where}"))
    if res.root_state != "open":
        return res
    root_pr = fetched[0].pr
    if root_pr.base_ref not in channels and path_of(root_pr.repo) != CATALOG:
        res.errors.append(
            f"{root_pr.ref} targets `{root_pr.base_ref}`; only PRs to {', '.join(sorted(channels))} are tested"
        )

    # 2. Check each PR.
    for f in fetched:
        pr = f.pr
        if pr.state == "merged":
            res.dropped[pr.ref] = "already merged, so its code is in the base branch"
        elif pr.state == "closed":
            res.errors.append(f"{pr.ref} is closed without being merged (from {f.via})")
        elif path_of(pr.repo) != CATALOG and pr.base_ref != root_pr.base_ref:
            res.errors.append(
                f"{pr.ref} targets `{pr.base_ref}` but {root_pr.ref} targets `{root_pr.base_ref}`; "
                "PRs tested together must target the same branch"
            )
        else:
            if pr.association not in trusted_associations:
                res.untrusted.append(f"{pr.ref} by @{pr.author} ({pr.association.lower()})")
            res.prs.append(f)

    # 3. Per repo: drop PRs a stacked PR on top already contains.
    by_path: dict[str, list[Found]] = {}
    for f in res.prs:
        by_path.setdefault(path_of(f.pr.repo), []).append(f)
    for path, group in by_path.items():
        tops = [
            c
            for c in group
            if not any(
                o is not c and o.pr.head_sha != c.pr.head_sha and gh.contains(c.pr.repo, c.pr.head_sha, o.pr.head_sha)
                for o in group
            )
        ]
        for o in group:
            if o not in tops:
                top = next(t for t in tops if gh.contains(t.pr.repo, o.pr.head_sha, t.pr.head_sha))
                res.dropped[o.pr.ref] = f"included in {top.pr.ref}, which is stacked on top of it"
        res.used[path] = sorted(tops, key=lambda f: f.pr.number)

    # 4. Every PR whose code is used must merge cleanly into its branch.
    for f in (f for group in res.used.values() for f in group):
        if f.pr.mergeable is False:
            res.errors.append(f"{f.pr.ref} can't be merged into `{f.pr.base_ref}` (merge conflict); please rebase it")
        elif f.pr.mergeable is None or not f.pr.merge_sha:
            res.errors.append(f"GitHub hasn't computed yet whether {f.pr.ref} merges cleanly; re-run in a minute")
    return res
