"""Minimal GitHub REST client (stdlib only) and the PR model CI needs."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")


@dataclass
class PR:
    repo: str                 # base repository, "owner/name"
    number: int
    state: str                # "open" | "closed"
    merged: bool
    base_ref: str             # e.g. "staging"
    base_sha: str
    head_sha: str
    mergeable: bool | None    # None: GitHub hasn't computed it yet
    merge_sha: str | None     # test merge of head into base (when mergeable)
    author: str
    author_association: str   # OWNER | MEMBER | COLLABORATOR | CONTRIBUTOR | ...
    title: str = ""
    body: str = ""
    commit_messages: list[tuple[str, str]] = field(default_factory=list)   # (sha, message)

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/pull/{self.number}"


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str | None = None):
        self.token = token if token is not None else os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    def request(self, method: str, path: str, body: dict | None = None, *, accept: str = "application/vnd.github+json"):
        url = path if path.startswith("http") else f"{API}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "catalog-core-ci"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        last = None
        for attempt in range(4):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                last = f"{method} {path}: HTTP {e.code}: {e.read()[:300]!r}"
                retryable = e.code >= 500 or (e.code in (403, 429) and e.headers.get("Retry-After"))
                if not retryable:
                    raise GitHubError(last) from None
                time.sleep(int(e.headers.get("Retry-After") or 2 ** attempt))
            except urllib.error.URLError as e:
                last = f"{method} {path}: {e.reason}"
                time.sleep(2 ** attempt)
        raise GitHubError(last)

    def get(self, path: str):
        return self.request("GET", path)

    # -- what the resolver and planner need -------------------------------

    def pr(self, repo: str, number: int, *, wait_mergeable: float = 30.0) -> PR | None:
        """Fetch a PR. Polls briefly while GitHub computes mergeability."""
        deadline = time.monotonic() + wait_mergeable
        while True:
            try:
                d = self.get(f"/repos/{repo}/pulls/{number}")
            except GitHubError as e:
                if "HTTP 404" in str(e):
                    return None
                raise
            if d["state"] != "open" or d.get("mergeable") is not None or time.monotonic() > deadline:
                break
            time.sleep(3)
        commits = self.get(f"/repos/{repo}/pulls/{number}/commits?per_page=100") or []
        return PR(
            repo=d["base"]["repo"]["full_name"],
            number=d["number"],
            state=d["state"],
            merged=bool(d.get("merged")),
            base_ref=d["base"]["ref"],
            base_sha=d["base"]["sha"],
            head_sha=d["head"]["sha"],
            mergeable=d.get("mergeable"),
            merge_sha=d.get("merge_commit_sha") if d.get("mergeable") else None,
            author=d["user"]["login"],
            author_association=d.get("author_association", "NONE"),
            title=d.get("title") or "",
            body=d.get("body") or "",
            commit_messages=[(c["sha"], c["commit"]["message"]) for c in commits],
        )

    def contains(self, repo: str, ancestor: str, descendant: str) -> bool:
        """True if commit `descendant` contains commit `ancestor`."""
        d = self.get(f"/repos/{repo}/compare/{ancestor}...{descendant}")
        return d["status"] in ("ahead", "identical")

    def branch_sha(self, repo: str, branch: str) -> str:
        return self.get(f"/repos/{repo}/commits/{branch}")["sha"]

    def pr_files(self, repo: str, number: int) -> list[str]:
        files, page = [], 1
        while True:
            batch = self.get(f"/repos/{repo}/pulls/{number}/files?per_page=100&page={page}") or []
            files += [f["filename"] for f in batch]
            if len(batch) < 100:
                return files
            page += 1
