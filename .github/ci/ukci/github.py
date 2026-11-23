from __future__ import annotations

import base64
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

TRANSIENT = ("HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "timeout", "connection reset", "EOF")
MERGEABLE = {"MERGEABLE": True, "CONFLICTING": False}
WAIT_MERGEABLE_S = 30.0


def run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
    return f"{server}/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '0')}"


def set_outputs(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            for key, value in values.items():
                f.write(f"{key}={value}\n")


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str | None = None):
        if token is None:
            token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.token = token

    def gh(self, *args: str, body: Any = None) -> Any:
        env = dict(os.environ, GH_TOKEN=self.token or "", GH_PROMPT_DISABLED="1", NO_COLOR="1")
        stdin = json.dumps(body) if body is not None else None
        for attempt in range(3):
            p = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, env=env, check=False)
            if p.returncode == 0:
                if not p.stdout.strip():
                    return None
                return json.loads(p.stdout)
            transient = any(s in p.stderr for s in TRANSIENT)
            if not transient or attempt == 2:
                raise GitHubError(f"gh {' '.join(args[:2])}: {p.stderr.strip()[:300]}")
            time.sleep(2 ** (attempt + 1))
        raise AssertionError("unreachable")

    def api(self, path: str, method: str = "GET", body: Any = None) -> Any:
        if body is None:
            return self.gh("api", "--method", method, path)
        return self.gh("api", "--method", method, path, "--input", "-", body=body)

    def get_or_none(self, path: str) -> Any:
        try:
            return self.api(path)
        except GitHubError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def pull_request(self, repo: str, number: int) -> PullRequest | None:
        owner, name = repo.split("/")
        deadline = time.monotonic() + WAIT_MERGEABLE_S
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
                if "Could not resolve to" in str(e):
                    return None
                raise
            repository = (data or {}).get("data", {}).get("repository") or {}
            d = repository.get("pullRequest")
            if d is None:
                return None
            if d["state"] != "OPEN" or d["mergeable"] != "UNKNOWN" or time.monotonic() > deadline:
                break
            time.sleep(3)
        mergeable = MERGEABLE.get(d["mergeable"])
        merge = d.get("potentialMergeCommit") or {}
        parents = (merge.get("parents") or {}).get("nodes") or []
        merge_sha = None
        merge_base_sha = None
        if mergeable:
            merge_sha = merge.get("oid")
            if parents:
                merge_base_sha = parents[0]["oid"]
        return PullRequest(
            repo=d["baseRepository"]["nameWithOwner"],
            number=d["number"],
            state=d["state"].lower(),
            base_ref=d["baseRefName"],
            head_sha=d["headRefOid"],
            mergeable=mergeable,
            merge_sha=merge_sha,
            merge_base_sha=merge_base_sha,
            author=(d.get("author") or {}).get("login", "ghost"),
            association=d["authorAssociation"],
            title=d.get("title") or "",
            body=d.get("body") or "",
            commit_messages=[node["commit"]["message"] for node in d["commits"]["nodes"]],
        )

    def contains(self, repo: str, ancestor: str, descendant: str) -> bool:
        status = self.api(f"/repos/{repo}/compare/{ancestor}...{descendant}")["status"]
        return status in ("ahead", "identical")

    def branch_sha(self, repo: str, branch: str) -> str:
        return self.api(f"/repos/{repo}/commits/{branch}")["sha"]

    def pr_files(self, repo: str, number: int) -> list[str]:
        pages = self.gh("api", "--paginate", "--slurp", f"/repos/{repo}/pulls/{number}/files?per_page=100")
        return [f["filename"] for page in pages for f in page]

    def comment(self, repo: str, comment_id: int) -> dict | None:
        return self.get_or_none(f"/repos/{repo}/issues/comments/{comment_id}")

    def environment(self, repo: str, name: str) -> dict | None:
        return self.get_or_none(f"/repos/{repo}/environments/{name}")

    def file(self, repo: str, path: str, ref: str) -> bytes | None:
        d = self.get_or_none(f"/repos/{repo}/contents/{path}?ref={ref}")
        if isinstance(d, dict) and d.get("encoding") == "base64":
            return base64.b64decode(d["content"])
        return None

    def put_check(self, repo: str, body: dict, check_id: int | None = None) -> int:
        if check_id:
            return self.api(f"/repos/{repo}/check-runs/{check_id}", "PATCH", body)["id"]
        return self.api(f"/repos/{repo}/check-runs", "POST", body)["id"]

    def run_workflow(self, repo: str, workflow: str, ref: str, inputs: dict[str, str]) -> None:
        self.api(f"/repos/{repo}/actions/workflows/{workflow}/dispatches", "POST", {"ref": ref, "inputs": inputs})

    def run_approvals(self, repo: str, run_id: str) -> list[dict]:
        return self.api(f"/repos/{repo}/actions/runs/{run_id}/approvals") or []

    def run_jobs(self, repo: str, run_id: str) -> dict[str, str]:
        pages = self.gh("api", "--paginate", "--slurp", f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100")
        jobs = {}
        for page in pages or []:
            for job in page.get("jobs", []):
                jobs[job["name"]] = job["html_url"]
        return jobs
