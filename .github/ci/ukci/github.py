"""GitHub access through the `gh` CLI (preinstalled on GitHub's runners).

`gh` handles authentication (GH_TOKEN), pagination and GraphQL. This
module only adds the handful of calls CI needs, in CI's own terms.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any

from .models import PullRequest

PR_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number state title body mergeable authorAssociation
      author { login }
      baseRefName headRefOid
      baseRepository { nameWithOwner }
      potentialMergeCommit { oid parents(first: 1) { nodes { oid } } }
      commits(first: 100) { nodes { commit { message } } }
    }
  }
}"""


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str | None = None):
        self.token = token if token is not None else os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    def gh(self, *args: str, body: Any = None) -> Any:
        """Run `gh`, retrying transient server errors; return parsed JSON (or None)."""
        env = dict(os.environ, GH_TOKEN=self.token or "", GH_PROMPT_DISABLED="1", NO_COLOR="1")
        stdin = json.dumps(body) if body is not None else None
        for attempt in range(3):
            p = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, env=env, check=False)
            if p.returncode == 0:
                return json.loads(p.stdout) if p.stdout.strip() else None
            transient = any(s in p.stderr for s in ("HTTP 502", "HTTP 503", "HTTP 504", "timeout", "connection reset"))
            if not transient or attempt == 2:
                raise GitHubError(f"gh {' '.join(args[:2])}: {p.stderr.strip()[:300]}")
            time.sleep(2 ** (attempt + 1))
        raise AssertionError("unreachable")

    def api(self, path: str, method: str = "GET", body: Any = None) -> Any:
        args = ["api", "--method", method, path]
        return self.gh(*args, "--input", "-", body=body) if body is not None else self.gh(*args)

    # --- pull requests ---------------------------------------------------------

    def pull_request(self, repo: str, number: int, *, wait_mergeable: float = 30.0) -> PullRequest | None:
        """A PR with what the resolver needs; waits briefly while GitHub computes mergeability."""
        owner, name = repo.split("/")
        deadline = time.monotonic() + wait_mergeable
        while True:
            try:
                data = self.gh(
                    "api",
                    "graphql",
                    "-f",
                    f"query={PR_QUERY}",
                    "-F",
                    f"owner={owner}",
                    "-F",
                    f"name={name}",
                    "-F",
                    f"number={number}",
                )
            except GitHubError as e:
                if "Could not resolve to" in str(e):  # no such repository or PR
                    return None
                raise
            d = ((data or {}).get("data", {}).get("repository") or {}).get("pullRequest")
            if d is None:
                return None
            if d["state"] != "OPEN" or d["mergeable"] != "UNKNOWN" or time.monotonic() > deadline:
                break
            time.sleep(3)
        merge = d.get("potentialMergeCommit") or {}
        parents = (merge.get("parents") or {}).get("nodes") or []
        mergeable = {"MERGEABLE": True, "CONFLICTING": False}.get(d["mergeable"])
        return PullRequest(
            repo=d["baseRepository"]["nameWithOwner"],
            number=d["number"],
            state=d["state"].lower(),
            base_ref=d["baseRefName"],
            head_sha=d["headRefOid"],
            mergeable=mergeable,
            merge_sha=merge.get("oid") if mergeable else None,
            merge_base_sha=parents[0]["oid"] if mergeable and parents else None,
            author=(d.get("author") or {}).get("login", "ghost"),
            association=d["authorAssociation"],
            title=d.get("title") or "",
            body=d.get("body") or "",
            commit_messages=[n["commit"]["message"] for n in d["commits"]["nodes"]],
        )

    def contains(self, repo: str, ancestor: str, descendant: str) -> bool:
        """Does commit `descendant` contain `ancestor`?"""
        return self.api(f"/repos/{repo}/compare/{ancestor}...{descendant}")["status"] in ("ahead", "identical")

    def branch_sha(self, repo: str, branch: str) -> str:
        return self.api(f"/repos/{repo}/commits/{branch}")["sha"]

    def pr_files(self, repo: str, number: int) -> list[str]:
        pages = self.gh("api", "--paginate", "--slurp", f"/repos/{repo}/pulls/{number}/files?per_page=100")
        return [f["filename"] for page in pages for f in page]

    # --- checks, dispatches, approvals -------------------------------------------

    def find_check(self, repo: str, sha: str, name: str) -> int | None:
        d = self.gh("api", "--method", "GET", f"/repos/{repo}/commits/{sha}/check-runs", "-f", f"check_name={name}")
        runs = (d or {}).get("check_runs") or []
        return runs[0]["id"] if runs else None

    def put_check(self, repo: str, body: dict, check_id: int | None = None) -> int:
        if check_id:
            return self.api(f"/repos/{repo}/check-runs/{check_id}", "PATCH", body)["id"]
        return self.api(f"/repos/{repo}/check-runs", "POST", body)["id"]

    def dispatch(self, repo: str, event_type: str, payload: dict) -> None:
        self.api(f"/repos/{repo}/dispatches", "POST", {"event_type": event_type, "client_payload": payload})

    def run_approvals(self, repo: str, run_id: str) -> list[dict]:
        return self.api(f"/repos/{repo}/actions/runs/{run_id}/approvals") or []
