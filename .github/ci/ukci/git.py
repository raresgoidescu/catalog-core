"""The two git operations CI needs: fetch one commit, and combine PRs.

Combining merges several PR heads of one repository onto a base commit with
`git merge-tree` (no worktree; nothing from the repository is run). Author,
committer and dates are fixed, so the result has the same SHA every time:
the plan job computes it, the test job rebuilds it and checks it matches.
"""

from __future__ import annotations

import os
import subprocess

from .models import Part

FIXED_IDENTITY = {
    "GIT_AUTHOR_NAME": "catalog-core CI",
    "GIT_AUTHOR_EMAIL": "ci@catalog-core.invalid",
    "GIT_COMMITTER_NAME": "catalog-core CI",
    "GIT_COMMITTER_EMAIL": "ci@catalog-core.invalid",
    "GIT_AUTHOR_DATE": "@0 +0000",
    "GIT_COMMITTER_DATE": "@0 +0000",
}


class Conflict(Exception):
    def __init__(self, merged: list[str], part: str, files: list[str]):
        self.merged, self.part, self.files = merged, part, files
        super().__init__(f"{part} conflicts with {' + '.join(merged)} in {', '.join(files) or 'some files'}")


def git(cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=dict(os.environ, **FIXED_IDENTITY), check=check, capture_output=True, text=True
    )


def fetch_commit(url: str, sha: str, dest: str) -> None:
    """Shallow-fetch exactly one commit into `dest` and check it out."""
    os.makedirs(dest, exist_ok=True)
    git(dest, "init", "-q")
    git(dest, "fetch", "-q", "--no-tags", "--depth", "1", url, sha)
    git(dest, "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD")


def combine(url: str, base_sha: str, parts: list[Part], dest: str) -> str:
    """Merge every part onto base_sha, in order, in a repo at `dest`. Returns the commit."""
    os.makedirs(dest, exist_ok=True)
    git(dest, "init", "-q")
    # History is needed for merge bases, file contents only where merges touch
    # them: a partial clone, which must know its remote before fetching.
    git(dest, "remote", "add", "origin", url)
    git(dest, "config", "remote.origin.promisor", "true")
    git(dest, "config", "remote.origin.partialclonefilter", "blob:none")
    git(dest, "fetch", "-q", "--no-tags", "--filter=blob:none", "origin", base_sha, *(p.sha for p in parts))
    current, merged = base_sha, ["the base branch"]
    for part in parts:
        r = git(dest, "merge-tree", "--write-tree", "--name-only", "--no-messages", current, part.sha, check=False)
        lines = r.stdout.splitlines()
        if r.returncode == 1:
            raise Conflict(merged[1:] or merged, part.label, [f for f in lines[1:] if f.strip()])
        if r.returncode != 0:
            raise RuntimeError(f"git merge-tree failed: {r.stderr.strip()}")
        current = git(
            dest, "commit-tree", lines[0], "-p", current, "-p", part.sha, "-m", f"CI: combine {part.label}"
        ).stdout.strip()
        merged.append(part.label)
    return current


def checkout_combined(url: str, base_sha: str, parts: list[Part], expected: str, dest: str) -> None:
    """Rebuild a combined commit, check it is the planned one, and check it out."""
    got = combine(url, base_sha, parts, dest)
    if got != expected:
        raise RuntimeError(f"combined commit is {got}, the plan expected {expected}")
    git(dest, "-c", "advice.detachedHead=false", "checkout", "-q", got)
