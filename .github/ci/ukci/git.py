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
        self.merged = merged
        self.part = part
        self.files = files
        super().__init__(f"{part} conflicts with {' + '.join(merged)} in {', '.join(files) or 'some files'}")


def git(cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, **FIXED_IDENTITY)
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=check, capture_output=True, text=True)


def fetch_commit(url: str, sha: str, dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    git(dest, "init", "-q")
    git(dest, "fetch", "-q", "--no-tags", "--depth", "1", url, sha)
    git(dest, "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD")


def combine(url: str, base_sha: str, parts: list[Part], dest: str) -> str:
    os.makedirs(dest, exist_ok=True)
    git(dest, "init", "-q")
    git(dest, "remote", "add", "origin", url)
    git(dest, "config", "remote.origin.promisor", "true")
    git(dest, "config", "remote.origin.partialclonefilter", "blob:none")
    shas = [part.sha for part in parts]
    git(dest, "fetch", "-q", "--no-tags", "--filter=blob:none", "origin", base_sha, *shas)
    current = base_sha
    merged: list[str] = []
    for part in parts:
        r = git(dest, "merge-tree", "--write-tree", "--name-only", "--no-messages", current, part.sha, check=False)
        lines = r.stdout.splitlines()
        if r.returncode == 1:
            files = [line for line in lines[1:] if line.strip()]
            raise Conflict(merged or ["the base branch"], part.label, files)
        if r.returncode != 0:
            raise RuntimeError(f"git merge-tree failed: {r.stderr.strip()}")
        tree = lines[0]
        message = f"CI: combine {part.label}"
        current = git(dest, "commit-tree", tree, "-p", current, "-p", part.sha, "-m", message).stdout.strip()
        merged.append(part.label)
    return current


def checkout_combined(url: str, base_sha: str, parts: list[Part], expected: str, dest: str) -> None:
    got = combine(url, base_sha, parts, dest)
    if got != expected:
        raise RuntimeError(f"combined commit is {got}, the plan expected {expected}")
    git(dest, "-c", "advice.detachedHead=false", "checkout", "-q", got)
