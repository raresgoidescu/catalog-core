"""Resolve ``Depends-On:`` between pull requests into one commit per repo.

Syntax (PR description or commit message, at the start of a line):

    Depends-On: unikraft/lib-musl#12
    Depends-On: lib-lwip#40, #7
    Depends-On: https://github.com/unikraft/unikraft/pull/1745

``repo#N`` means a repo in the same owner as the PR that mentions it;
``#N`` means the same repo. Words like "needs" or "requires" in prose
are not dependencies.

The dependency graph is only used to *find* PRs. The result is one
commit per repository, so cycles (A <-> B), diamonds and chains all
collapse to the same answer. Several PRs in the same repo are either a
stack (the top one is used) or are merged together by the planner; if
they conflict, the authors have to rebase one onto the other.

Every problem is reported before any test runs, with the fix spelled out.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field

from . import repos
from .gh import PR

CATALOG = "catalog-core"      # pseudo repo path: the catalog itself (apps)

DEPENDS_RE = re.compile(r"^[ \t]*Depends-On:[ \t]*(.+?)[ \t]*$", re.I | re.M)
URL_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)/?$")
FULL_RE = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)$")
SHORT_RE = re.compile(r"^([A-Za-z0-9_.-]+)#(\d+)$")
LOCAL_RE = re.compile(r"^#(\d+)$")


@dataclass(frozen=True)
class Ref:
    repo: str      # "owner/name", lower-case
    number: int

    def __str__(self):
        return f"{self.repo}#{self.number}"


@dataclass
class Found:
    pr: PR
    via: str                   # human explanation of how we got here


@dataclass
class Resolution:
    root: Ref
    prs: list[Found] = field(default_factory=list)          # open PRs to test, root first
    selected: dict[str, Found] = field(default_factory=dict)  # repo path -> PR whose code is used
    dropped: list[tuple[str, str]] = field(default_factory=list)   # (ref, reason)
    errors: list[str] = field(default_factory=list)
    untrusted: list[str] = field(default_factory=list)
    root_state: str | None = None      # "open" | "merged" | "closed" | None (not fetched)
    combine: dict[str, list[Found]] = field(default_factory=dict)  # repo path -> PRs to merge together

    @property
    def trusted(self) -> bool:
        return not self.untrusted


def parse_depends_on(text: str, current_repo: str) -> tuple[list[Ref], list[str]]:
    """Return (refs, errors) for every Depends-On line in `text`."""
    refs, errors = [], []
    owner = current_repo.split("/")[0]
    for m in DEPENDS_RE.finditer(text or ""):
        for token in re.split(r"[,\s]+", m.group(1)):
            token = token.strip().rstrip(".;")
            if not token:
                continue
            if (u := URL_RE.match(token)) or (u := FULL_RE.match(token)):
                refs.append(Ref(f"{u[1]}/{u[2]}".lower(), int(u[3])))
            elif s := SHORT_RE.match(token):
                refs.append(Ref(f"{owner}/{s[1]}".lower(), int(s[2])))
            elif loc := LOCAL_RE.match(token):
                refs.append(Ref(current_repo.lower(), int(loc[1])))
            else:
                errors.append(f"can't read Depends-On target `{token}` "
                              "(use owner/repo#N, repo#N, #N or a PR URL)")
    return refs, errors


def path_of(repo: str) -> str:
    """Repo -> local path; the catalog itself maps to CATALOG."""
    if repo.rsplit("/", 1)[-1].lower() == "catalog-core":
        return CATALOG
    return repos.path_for(repo)


def resolve(gh, root: Ref, *, allowed_owners: set[str], trusted_associations: set[str],
            channels: set[str], max_prs: int = 8) -> Resolution:
    res = Resolution(root=root)
    allowed_owners = {o.lower() for o in allowed_owners}

    # 1. CRAWL -------------------------------------------------------------
    seen: set[Ref] = {root}
    queue: deque[tuple[Ref, str]] = deque([(root, "root")])
    fetched: list[Found] = []
    while queue:
        ref, via = queue.popleft()
        if ref.repo.split("/")[0] not in allowed_owners:
            res.errors.append(f"{ref} (from {via}): only PRs in {', '.join(sorted(allowed_owners))} can be dependencies")
            continue
        try:
            path_of(ref.repo)
        except repos.UnknownRepo:
            res.errors.append(f"{ref} (from {via}): not a repository catalog-core builds with")
            continue
        pr = gh.pr(ref.repo, ref.number)
        if pr is None:
            res.errors.append(f"{ref} (from {via}): no such pull request")
            continue
        fetched.append(Found(pr, via))
        if ref == root:
            res.root_state = "merged" if pr.merged else pr.state
        if len(fetched) > max_prs:
            res.errors.append(f"more than {max_prs} PRs in the dependency set; split the change up")
            break
        sources = [("description", pr.body)] + [(f"commit {sha[:12]}", msg) for sha, msg in pr.commit_messages]
        for where, text in sources:
            deps, errs = parse_depends_on(text, pr.repo)
            res.errors += [f"{pr.ref} {where}: {e}" for e in errs]
            for d in deps:
                if d not in seen:
                    seen.add(d)
                    queue.append((d, f"Depends-On in {pr.ref} {where}"))

    if not fetched:
        return res
    root_pr = fetched[0].pr
    channel = root_pr.base_ref
    if channel not in channels and path_of(root_pr.repo) != CATALOG:
        res.errors.append(f"{root_pr.ref} targets `{channel}`; only PRs to {', '.join(sorted(channels))} are tested")

    # 2. CHECK each PR -----------------------------------------------------
    open_prs: list[Found] = []
    for f in fetched:
        pr = f.pr
        if pr.merged:
            res.dropped.append((pr.ref, "already merged, so its code is in the base branch"))
            continue
        if pr.state != "open":
            res.errors.append(f"{pr.ref} is closed without being merged (from {f.via})")
            continue
        if path_of(pr.repo) != CATALOG and pr.base_ref != channel:
            res.errors.append(f"{pr.ref} targets `{pr.base_ref}` but {root_pr.ref} targets `{channel}`; "
                              "all PRs tested together must target the same branch")
            continue
        if pr.author_association not in trusted_associations:
            res.untrusted.append(f"{pr.ref} by @{pr.author} ({pr.author_association.lower()})")
        open_prs.append(f)
    res.prs = open_prs

    # 3. COLLAPSE to one commit per repo ---------------------------------
    by_path: dict[str, list[Found]] = {}
    for f in open_prs:
        by_path.setdefault(path_of(f.pr.repo), []).append(f)
    for path, group in by_path.items():
        # A PR contained in another one (a stack) is covered by the top one.
        tops = [c for c in group
                if not any(o is not c and o.pr.head_sha != c.pr.head_sha
                           and gh.contains(c.pr.repo, c.pr.head_sha, o.pr.head_sha) for o in group)]
        for o in group:
            if o not in tops:
                top = next(t for t in tops if gh.contains(t.pr.repo, o.pr.head_sha, t.pr.head_sha))
                res.dropped.append((o.pr.ref, f"included in {top.pr.ref}, which is stacked on top of it"))
        tops.sort(key=lambda f: f.pr.number)
        res.selected[path] = tops[0]
        if len(tops) > 1:
            # Not stacked: the planner merges them; a conflict is reported there.
            res.combine[path] = tops

    # 4. PIN: each PR used must merge cleanly into its base ------------------
    used = [f for path, f in res.selected.items() if path not in res.combine]
    used += [f for group in res.combine.values() for f in group]
    for f in used:
        pr = f.pr
        if pr.mergeable is False:
            res.errors.append(f"{pr.ref} conflicts with `{pr.base_ref}`; please rebase it")
        elif pr.mergeable is None or not pr.merge_sha:
            res.errors.append(f"GitHub has not computed whether {pr.ref} merges cleanly yet; re-run in a minute")
    return res
