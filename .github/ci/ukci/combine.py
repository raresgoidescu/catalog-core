"""Combine several PRs of one repository into a single commit.

Used when a set of PRs needs more than one PR from the same repository and
they are not stacked. The heads are merged one by one onto the base commit
with ``git merge-tree`` (no worktree; nothing from the repository is run).
Author, committer and dates are fixed, so the result has the same SHA every
time: the plan job computes it, the test job rebuilds it and verifies it.

A conflict cannot be solved by CI; it is reported with the files involved
and the PRs have to be rebased onto each other.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

FIXED = {
    "GIT_AUTHOR_NAME": "catalog-core CI", "GIT_AUTHOR_EMAIL": "ci@catalog-core.invalid",
    "GIT_COMMITTER_NAME": "catalog-core CI", "GIT_COMMITTER_EMAIL": "ci@catalog-core.invalid",
    "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000",
}


class Conflict(Exception):
    def __init__(self, first: str, second: str, files: list[str]):
        self.first, self.second, self.files = first, second, files
        super().__init__(f"{second} conflicts with {first}: {', '.join(files) or 'unknown files'}")


@dataclass
class Part:
    label: str       # e.g. "unikraft/unikraft#1875"
    sha: str         # PR head commit


def _git(repo_dir: str, *args, check=True, env=None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ, **FIXED, **(env or {}))
    return subprocess.run(["git", *args], cwd=repo_dir, env=full_env, check=check, capture_output=True, text=True)


def fetch(url: str, shas: list[str], dest: str) -> None:
    """Fetch commits with history (merges need merge bases) but without blobs up front."""
    os.makedirs(dest, exist_ok=True)
    _git(dest, "init", "-q")
    _git(dest, "remote", "add", "origin", url, check=False)
    _git(dest, "config", "remote.origin.promisor", "true")
    _git(dest, "config", "remote.origin.partialclonefilter", "blob:none")
    _git(dest, "fetch", "-q", "--no-tags", "--filter=blob:none", "origin", *shas)


def merge(repo_dir: str, base_sha: str, parts: list[Part]) -> str:
    """Merge every part onto base_sha in order. Returns the final commit SHA."""
    current, merged = base_sha, ["base"]
    for part in parts:
        r = _git(repo_dir, "merge-tree", "--write-tree", "--name-only", "--no-messages", current, part.sha, check=False)
        lines = r.stdout.split("\n")
        if r.returncode == 1:
            raise Conflict(" + ".join(merged), part.label, [line for line in lines[1:] if line.strip()])
        if r.returncode != 0:
            raise RuntimeError(f"git merge-tree failed: {r.stderr.strip()}")
        tree = lines[0].strip()
        current = _git(repo_dir, "commit-tree", tree, "-p", current, "-p", part.sha,
                       "-m", f"CI: combine {part.label}").stdout.strip()
        merged.append(part.label)
    return current


def build(url: str, base_sha: str, parts: list[Part], dest: str) -> str:
    fetch(url, [base_sha] + [p.sha for p in parts], dest)
    return merge(dest, base_sha, parts)


def checkout(url: str, base_sha: str, parts: list[Part], expected: str, dest: str) -> None:
    """Rebuild the combined commit in `dest`, verify it, and check it out."""
    got = build(url, base_sha, parts, dest)
    if got != expected:
        raise RuntimeError(f"combined commit is {got}, the plan expected {expected}")
    _git(dest, "-c", "advice.detachedHead=false", "checkout", "-q", got)
