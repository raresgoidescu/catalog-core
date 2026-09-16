#!/usr/bin/env python3
"""
Discovers and checks out all PR dependencies declared via "Depends-On:" tags
in PR descriptions and commit messages.

Supported syntax in PR body / commit messages:
  - Depends-On: owner/repo#pr_number
  - Depends-On: repo#pr_number or repo/#pr_number (same profile or unikraft org)
  - Depends-On: #pr_number (same repo as the referencing PR)
  - Depends-On: https://github.com/owner/repo/pull/42
  - Case/space/dash/underscore-insensitive (Depends on, depends-on, depends_on, Depends-upon, Dependent-on, Requires, Needs)
  - Markdown styles: **Depends-on:**, *Depends on:*, _Depends on:_, `Depends-on:`,
    bullet points (- / * / >), HTML comments (<!-- Depends-on: ... -->),
    markdown links ([#12](url) or [repo#12](url)), comma/"and"/space-separated lists.

Security / Fork Scope Restriction:
  - When running in the 'unikraft' organization, only dependencies within 'unikraft' are permitted.
  - When running on a fork profile, only dependencies from that fork profile or 'unikraft' are permitted.
  - Arbitrary third-party repositories/forks are strictly blocked.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass(frozen=True)
class PRRef:
    """Unique identifier for a GitHub Pull Request."""
    repo: str      # Format: 'owner/repo' (normalized to lowercase)
    number: int

    def __str__(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def owner(self) -> str:
        return self.repo.split("/")[0] if "/" in self.repo else ""

    @property
    def repo_name(self) -> str:
        return self.repo.split("/")[-1] if "/" in self.repo else self.repo


@dataclass
class PRInfo:
    """Fetched metadata for a Pull Request."""
    ref: PRRef
    title: str = ""
    body: str = ""
    commit_messages: List[str] = field(default_factory=list)
    head_ref: str = ""
    base_ref: str = ""


# Canonical repository aliases
REPO_ALIASES: Dict[str, str] = {
    "unikraft": "unikraft",
    "musl": "lib-musl",
    "lib-musl": "lib-musl",
    "lwip": "lib-lwip",
    "lib-lwip": "lib-lwip",
    "libcxx": "lib-libcxx",
    "lib-libcxx": "lib-libcxx",
    "libcxxabi": "lib-libcxxabi",
    "lib-libcxxabi": "lib-libcxxabi",
    "libunwind": "lib-libunwind",
    "lib-libunwind": "lib-libunwind",
    "compiler-rt": "lib-compiler-rt",
    "lib-compiler-rt": "lib-compiler-rt",
    "nginx": "lib-nginx",
    "lib-nginx": "lib-nginx",
    "python3": "lib-python3",
    "lib-python3": "lib-python3",
    "click": "lib-click",
    "lib-click": "lib-click",
    "libelf": "lib-libelf",
    "lib-libelf": "lib-libelf",
    "elfloader": "app-elfloader",
    "app-elfloader": "app-elfloader",
    "redis": "lib-redis",
    "lib-redis": "lib-redis",
    "wamr": "lib-wamr",
    "lib-wamr": "lib-wamr",
    "sqlite": "lib-sqlite",
    "lib-sqlite": "lib-sqlite",
}


def normalize_repo_name(name: str) -> str:
    """Normalizes repository names and aliases to their canonical name."""
    clean = name.strip().lower()
    return REPO_ALIASES.get(clean, clean)


# Regex matching header lines introducing Depends-On declarations
DEPENDS_ON_LINE_RE = re.compile(
    r"(?i)\b(?:depends(?:-|_|\s+)on|depends(?:-|_|\s+)upon|dependent(?:-|_|\s+)on|requires?|needs?)"
    r"(?::\s*|\s+)([^\r\n]+)",
    re.MULTILINE,
)

# Extractors for PR targets within a Depends-On line
MD_LINK_RE = re.compile(
    r"\[([^\]]*)\]\((https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+))\)",
    re.IGNORECASE,
)
URL_PR_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)",
    re.IGNORECASE,
)
REPO_PR_RE = re.compile(
    r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/?#(\d+)\b"
)
SHORT_REPO_PR_RE = re.compile(
    r"\b([A-Za-z0-9_.-]+)/?#(\d+)\b"
)
LOCAL_PR_RE = re.compile(
    r"(?:^|[\s,;(\[])(?:\bPR\s*#?|#)(\d+)\b",
    re.IGNORECASE,
)


def extract_dependencies(text: str, current_repo: str) -> List[PRRef]:
    """
    Parses a string (PR body or commit message) for Depends-On references.
    Returns a list of unique PRRef objects.
    """
    if not text:
        return []

    dependencies: List[PRRef] = []
    seen: Set[PRRef] = set()
    owner = current_repo.split("/")[0] if "/" in current_repo else "unikraft"

    for match in DEPENDS_ON_LINE_RE.finditer(text):
        line = match.group(1).strip()
        cleaned_line = re.sub(r"<!--\s*|\s*-->", "", line).strip()

        # 1. Match Markdown links [text](https://github.com/owner/repo/pull/123)
        for md_match in MD_LINK_RE.finditer(cleaned_line):
            repo = md_match.group(3).lower()
            num = int(md_match.group(4))
            ref = PRRef(repo=repo, number=num)
            if ref not in seen:
                seen.add(ref)
                dependencies.append(ref)
        cleaned_line = MD_LINK_RE.sub(" ", cleaned_line)

        # 2. Match raw URLs: https://github.com/owner/repo/pull/123
        for url_match in URL_PR_RE.finditer(cleaned_line):
            repo = url_match.group(1).lower()
            num = int(url_match.group(2))
            ref = PRRef(repo=repo, number=num)
            if ref not in seen:
                seen.add(ref)
                dependencies.append(ref)
        cleaned_line = URL_PR_RE.sub(" ", cleaned_line)

        # 3. Match owner/repo#123 or owner/repo/#123
        for repo_match in REPO_PR_RE.finditer(cleaned_line):
            repo = repo_match.group(1).lower()
            num = int(repo_match.group(2))
            ref = PRRef(repo=repo, number=num)
            if ref not in seen:
                seen.add(ref)
                dependencies.append(ref)
        cleaned_line = REPO_PR_RE.sub(" ", cleaned_line)

        # 4. Match short repo#123 without owner prefix (e.g. lib-musl#42, musl#42)
        for short_match in SHORT_REPO_PR_RE.finditer(cleaned_line):
            repo_name = short_match.group(1).lower()
            num = int(short_match.group(2))
            ref = PRRef(repo=f"{owner.lower()}/{repo_name}", number=num)
            if ref not in seen:
                seen.add(ref)
                dependencies.append(ref)
        cleaned_line = SHORT_REPO_PR_RE.sub(" ", cleaned_line)

        # 5. Match #123 (same repo)
        for local_match in LOCAL_PR_RE.finditer(cleaned_line):
            num = int(local_match.group(1))
            ref = PRRef(repo=current_repo.lower(), number=num)
            if ref not in seen:
                seen.add(ref)
                dependencies.append(ref)

    return dependencies


class GitHubPRFetcher:
    """Fetches PR data via gh CLI when available, with urllib fallback."""

    def __init__(
        self,
        token: Optional[str] = None,
        mock_prs: Optional[Dict[str, dict]] = None,
    ):
        self.token = token or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.mock_prs = mock_prs or {}
        self.cache: Dict[PRRef, Optional[PRInfo]] = {}

    def fetch(self, ref: PRRef) -> Optional[PRInfo]:
        if ref in self.cache:
            return self.cache[ref]

        if self.mock_prs:
            info = self._fetch_from_mock(ref)
            self.cache[ref] = info
            return info

        info = self._fetch_via_gh(ref) or self._fetch_via_api(ref)
        if info:
            self.cache[ref] = info
        return info

    def _fetch_from_mock(self, ref: PRRef) -> Optional[PRInfo]:
        key = str(ref).lower()
        if key not in self.mock_prs:
            return None
        data = self.mock_prs[key]
        return PRInfo(
            ref=ref,
            title=data.get("title", "") or "",
            body=data.get("body", "") or "",
            commit_messages=data.get("commits", []) or data.get("commit_messages", []) or [],
            head_ref=data.get("head_ref", "") or "",
            base_ref=data.get("base_ref", "") or "",
        )

    def _fetch_via_gh(self, ref: PRRef) -> Optional[PRInfo]:
        cmd = [
            "gh", "pr", "view", str(ref.number),
            "--repo", ref.repo,
            "--json", "title,body,commits,headRefName,baseRefName"
        ]
        try:
            env = os.environ.copy()
            if self.token:
                env["GH_TOKEN"] = self.token
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            if proc.returncode != 0:
                # If gh failed with token, retry without GH_TOKEN
                if self.token:
                    env_no_token = os.environ.copy()
                    env_no_token.pop("GH_TOKEN", None)
                    env_no_token.pop("GITHUB_TOKEN", None)
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        check=False,
                        env=env_no_token,
                    )
                if proc.returncode != 0:
                    err = proc.stderr.strip() or proc.stdout.strip()
                    print(f"::warning::gh pr view failed for {ref}: {err}", file=sys.stderr)
                    return None

            data = json.loads(proc.stdout)
            commits = [
                c.get("messageBody", "") or c.get("messageHeadline", "")
                for c in data.get("commits", [])
            ]
            return PRInfo(
                ref=ref,
                title=data.get("title", ""),
                body=data.get("body", "") or "",
                commit_messages=commits,
                head_ref=data.get("headRefName", ""),
                base_ref=data.get("baseRefName", ""),
            )
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"::warning::Error running gh CLI for {ref}: {e}", file=sys.stderr)
            return None

    def _fetch_via_api(self, ref: PRRef) -> Optional[PRInfo]:
        """Fallback to GitHub REST API using urllib."""
        url = f"https://api.github.com/repos/{ref.repo}/pulls/{ref.number}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "catalog-core-ci",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        data, commits_data = self._http_get_pr_and_commits(url, headers)

        # Retry unauthenticated if scoped token failed on public repo
        if data is None and self.token:
            headers_no_auth = {
                "Accept": "application/vnd.github+json",
                "User-Agent": "catalog-core-ci",
            }
            data, commits_data = self._http_get_pr_and_commits(url, headers_no_auth)

        if not data:
            return None

        commit_messages = [
            c.get("commit", {}).get("message", "")
            for c in (commits_data or [])
            if isinstance(c, dict)
        ]

        return PRInfo(
            ref=ref,
            title=data.get("title", ""),
            body=data.get("body", "") or "",
            commit_messages=commit_messages,
            head_ref=data.get("head", {}).get("ref", ""),
            base_ref=data.get("base", {}).get("ref", ""),
        )

    def _http_get_pr_and_commits(
        self, url: str, headers: Dict[str, str]
    ) -> Tuple[Optional[dict], Optional[list]]:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())

            commits_url = f"{url}/commits?per_page=100"
            commits_req = urllib.request.Request(commits_url, headers=headers)
            with urllib.request.urlopen(commits_req, timeout=15) as commits_resp:
                commits_data = json.loads(commits_resp.read().decode())

            return data, commits_data
        except urllib.error.HTTPError as e:
            return None, None
        except Exception:
            return None, None


class DependencyResolver:
    """Builds and topologically sorts the cross-repo / stacked PR dependency graph."""

    def __init__(
        self,
        fetcher: GitHubPRFetcher,
        max_depth: int = 10,
        allowed_owners: Optional[Set[str]] = None,
    ):
        self.fetcher = fetcher
        self.max_depth = max_depth
        self.allowed_owners = {o.lower() for o in allowed_owners} if allowed_owners else None
        self.errors: List[str] = []
        # adjacency: node -> list of dependencies (edges from child -> dependency)
        self.adjacency: Dict[PRRef, List[PRRef]] = defaultdict(list)
        self.pr_info: Dict[PRRef, PRInfo] = {}

    def resolve(self, root: PRRef) -> List[PRRef]:
        """Traverses the dependency tree starting from root and returns topological order."""
        visited: Set[PRRef] = set()
        in_stack: Set[PRRef] = set()

        def dfs(current: PRRef, depth: int):
            if current in in_stack:
                print(f"::warning::Cycle detected at {current} -- breaking loop", file=sys.stderr)
                return
            if current in visited:
                return
            if depth > self.max_depth:
                message = f"Max recursion depth ({self.max_depth}) reached at {current}"
                self.errors.append(message)
                print(f"::error::{message}", file=sys.stderr)
                return

            visited.add(current)
            in_stack.add(current)

            info = self.fetcher.fetch(current)
            if info:
                self.pr_info[current] = info
                combined_text = "\n".join([info.body] + info.commit_messages)
                deps = extract_dependencies(combined_text, current.repo)

                for dep in deps:
                    # Security filter: restrict dependencies to allowed profile / org
                    if self.allowed_owners and dep.owner and dep.owner not in self.allowed_owners:
                        print(
                            f"::warning::Security guard: Skipping dependency '{dep}' declared in {current} -- "
                            f"owner '{dep.owner}' is not in allowed profiles {self.allowed_owners}",
                            file=sys.stderr,
                        )
                        continue

                    # If dep on user fork is not found, try upstream unikraft org fallback
                    actual_dep = dep
                    dep_info = self.fetcher.fetch(dep)
                    if not dep_info and dep.owner != "unikraft":
                        canonical_repo = normalize_repo_name(dep.repo_name)
                        upstream_candidate = PRRef(f"unikraft/{canonical_repo}", dep.number)
                        if self.fetcher.fetch(upstream_candidate):
                            actual_dep = upstream_candidate

                    if actual_dep == current:
                        continue  # Self-reference guard

                    if actual_dep in in_stack:
                        print(f"::warning::Cycle detected from {current} to {actual_dep} -- breaking loop", file=sys.stderr)
                        continue

                    self.adjacency[current].append(actual_dep)
                    dfs(actual_dep, depth + 1)
            else:
                message = f"Could not fetch PR info for {current} -- cannot resolve its dependencies"
                self.errors.append(message)
                print(f"::error::{message}", file=sys.stderr)

            in_stack.remove(current)

        dfs(root, 0)
        return self._topological_sort(visited)

    def _topological_sort(self, nodes: Set[PRRef]) -> List[PRRef]:
        """
        Returns nodes ordered such that if A depends on B, B appears BEFORE A.
        This ensures base dependencies are checked out/merged before dependent PRs.
        """
        in_degree: Dict[PRRef, int] = {node: 0 for node in nodes}
        dependents: Dict[PRRef, List[PRRef]] = defaultdict(list)

        for src, deps in self.adjacency.items():
            for dep in deps:
                if dep in in_degree:
                    in_degree[src] = in_degree.get(src, 0) + 1
                    dependents[dep].append(src)

        # Queue nodes with in_degree == 0 (no dependencies)
        queue = deque([node for node, deg in in_degree.items() if deg == 0])
        sorted_order: List[PRRef] = []

        while queue:
            node = queue.popleft()
            sorted_order.append(node)

            for dependent in dependents[node]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        # If cyclic nodes remain, append them at the end
        for node in nodes:
            if node not in sorted_order:
                sorted_order.append(node)

        return sorted_order


# Matches:  git clone [options] https://github.com/owner/repo[.git]  local/path
_SETUP_CLONE_RE = re.compile(
    r"git\s+clone\b[^|&;\n]*?https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?\s+([^\s|&;#\n]+)",
)


def parse_setup_sh(setup_path: str) -> Dict[str, str]:
    """
    Derives the 'owner/repo' -> local-path mapping by scanning all
    'git clone ... https://github.com/owner/repo  local/path' lines in setup.sh.
    This is the single source of truth; no separate repo-map file is needed.
    """
    repo_map: Dict[str, str] = {}
    if not os.path.exists(setup_path):
        print(f"::error::setup.sh not found at: {setup_path}", file=sys.stderr)
        return repo_map

    with open(setup_path, "r", encoding="utf-8") as f:
        content = f.read()

    for m in _SETUP_CLONE_RE.finditer(content):
        repo = m.group(1).lower().rstrip(".")
        path = m.group(2).rstrip("/")
        repo_map[repo] = path

    if not repo_map:
        print(f"::error::No 'git clone' lines found in {setup_path}", file=sys.stderr)

    return repo_map


def find_local_repo_path(repo_str: str, mapping: Dict[str, str]) -> Optional[str]:
    """
    Finds the local target directory for a given repository string.
    Handles forks, short names, and aliases seamlessly.
    """
    repo_lower = repo_str.strip().lower()
    if repo_lower in mapping:
        return mapping[repo_lower]

    parts = repo_lower.split("/")
    repo_name = parts[-1]
    canonical_name = normalize_repo_name(repo_name)

    # 1. Check unikraft/canonical_name
    if f"unikraft/{canonical_name}" in mapping:
        return mapping[f"unikraft/{canonical_name}"]

    # 2. Check canonical_name directly
    if canonical_name in mapping:
        return mapping[canonical_name]

    # 3. Check suffix match
    for k, v in mapping.items():
        if k.endswith(f"/{canonical_name}") or k == canonical_name:
            return v

    return None


def apply_git_checkouts(
    resolved_prs: List[PRRef],
    repo_map: Dict[str, str],
    dry_run: bool = False
) -> bool:
    """
    Applies Git checkouts and merges for all resolved PRs grouped by local repository directory.
    For same-repo stacked PRs, merges them in topological order.
    """
    prs_by_local_path: Dict[str, List[PRRef]] = defaultdict(list)
    success = True

    for pr in resolved_prs:
        local_path = find_local_repo_path(pr.repo, repo_map)
        if not local_path:
            print(f"::warning::Repository '{pr.repo}' not found in repo-map.txt -- skipping", file=sys.stderr)
            continue
        prs_by_local_path[local_path].append(pr)

    for local_path, pr_list in prs_by_local_path.items():
        print(f"\n::group::Configuring repository at '{local_path}'")
        print(f"Applying PR stack in order: {', '.join(str(p) for p in pr_list)}")

        if dry_run:
            print("[dry-run] Git operations skipped")
            print("::endgroup::")
            continue

        if not os.path.isdir(local_path):
            print(f"::warning::Directory '{local_path}' does not exist -- skipping", file=sys.stderr)
            print("::endgroup::")
            continue

        try:
            # Configure git user for potential local merge commits
            subprocess.run(
                ["git", "config", "user.name", "github-actions[bot]"],
                cwd=local_path, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
                cwd=local_path, check=True
            )

            # Fetch all required PR branches
            for pr in pr_list:
                remote_url = f"https://github.com/{pr.repo}"
                refspec = f"refs/pull/{pr.number}/head:pr-{pr.number}"
                print(f"Fetching {pr} from {remote_url}...")
                subprocess.run(
                    ["git", "fetch", "-fu", remote_url, refspec],
                    cwd=local_path, check=True
                )

            if len(pr_list) == 1:
                # Single PR in this repository
                target_pr = pr_list[0]
                print(f"Checking out {target_pr} branch pr-{target_pr.number}...")
                subprocess.run(
                    ["git", "checkout", f"pr-{target_pr.number}"],
                    cwd=local_path, check=True
                )
            else:
                # Same-repo stacked PRs (e.g. [base_pr, dependent_pr1, dependent_pr2])
                base_pr = pr_list[0]
                composite_branch = "stacked-composite"
                print(f"Starting stacked composite branch '{composite_branch}' from base {base_pr}...")
                subprocess.run(
                    ["git", "checkout", "-B", composite_branch, f"pr-{base_pr.number}"],
                    cwd=local_path, check=True
                )

                for next_pr in pr_list[1:]:
                    pr_branch = f"pr-{next_pr.number}"
                    # Check if next_pr already contains HEAD (linear branch)
                    is_ancestor = subprocess.run(
                        ["git", "merge-base", "--is-ancestor", "HEAD", pr_branch],
                        cwd=local_path, check=False
                    ).returncode == 0

                    if is_ancestor:
                        print(f"Fast-forwarding to {next_pr} ({pr_branch})...")
                        subprocess.run(
                            ["git", "reset", "--hard", pr_branch],
                            cwd=local_path, check=True
                        )
                    else:
                        print(f"Merging {next_pr} ({pr_branch}) into {composite_branch}...")
                        merge_proc = subprocess.run(
                            ["git", "merge", "--no-edit", pr_branch],
                            cwd=local_path, capture_output=True, text=True, check=False
                        )
                        if merge_proc.returncode != 0:
                            print(
                                f"::error::Merge conflict when applying stacked PR {next_pr} onto {composite_branch}:\n"
                                f"{merge_proc.stdout}\n{merge_proc.stderr}",
                                file=sys.stderr
                            )
                            success = False
                            break

            # Print current commit info
            log_proc = subprocess.run(
                ["git", "log", "-n", "1", "--oneline"],
                cwd=local_path, capture_output=True, text=True, check=True
            )
            print(f"Checked out head at: {log_proc.stdout.strip()}")

        except subprocess.CalledProcessError as e:
            print(f"::error::Git operation failed in {local_path}: {e}", file=sys.stderr)
            success = False
        finally:
            print("::endgroup::")

    return success


def print_dependency_tree(root: PRRef, resolver: DependencyResolver, ordered: List[PRRef]):
    """Prints a clear summary of the resolved dependency DAG."""
    print("\n==========================================")
    print(f"  Dependency Resolution Graph for {root}")
    print("==========================================")
    for node in ordered:
        info = resolver.pr_info.get(node)
        title_str = f" - \"{info.title}\"" if info and info.title else ""
        deps = resolver.adjacency.get(node, [])
        deps_str = f" (Depends on: {', '.join(str(d) for d in deps)})" if deps else ""
        print(f"  * {node}{title_str}{deps_str}")
    print("==========================================\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve and check out cross-repo & stacked PR dependencies for Unikraft CI."
    )
    # Support both named flags and positional arguments for backwards compatibility
    parser.add_argument("pos_root_repo", nargs="?", help="Triggering repo (e.g. unikraft/lib-musl)")
    parser.add_argument("pos_root_pr", nargs="?", type=int, help="Triggering PR number")
    parser.add_argument("pos_setup_sh", nargs="?", help="Path to setup.sh (default: ./setup.sh)")

    parser.add_argument("--root-repo", help="Triggering repo (e.g. unikraft/lib-musl)")
    parser.add_argument("--root-pr", type=int, help="Triggering PR number")
    parser.add_argument("--setup-sh", help="Path to setup.sh (default: ./setup.sh)")
    parser.add_argument("--max-depth", type=int, default=10, help="Max recursion depth for dependencies")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without running git commands")
    parser.add_argument("--mock-prs", help="JSON string of mock PR metadata for offline testing")
    parser.add_argument("--mock-prs-file", help="JSON file path containing mock PR metadata")
    parser.add_argument("--token", help="GitHub token override")
    parser.add_argument("--allowed-owners", help="Comma-separated list of allowed owners (defaults to root owner and 'unikraft')")

    args = parser.parse_args()

    root_repo = (args.root_repo or args.pos_root_repo or "").strip().lower()
    root_pr = args.root_pr or args.pos_root_pr
    setup_sh_path = (args.setup_sh or args.pos_setup_sh or "setup.sh").strip()

    if not root_repo or not root_pr:
        print("::error::Both root-repo and root-pr must be specified.", file=sys.stderr)
        parser.print_usage(sys.stderr)
        return 1

    # Determine allowed owners for security
    root_ref = PRRef(repo=root_repo, number=root_pr)
    if args.allowed_owners:
        allowed_owners = {o.strip().lower() for o in args.allowed_owners.split(",") if o.strip()}
    else:
        root_owner = root_ref.owner
        allowed_owners = {root_owner, "unikraft"} if root_owner else {"unikraft"}

    # Load mock data if provided
    mock_data: Dict[str, dict] = {}
    if args.mock_prs:
        try:
            mock_data = json.loads(args.mock_prs)
        except Exception as e:
            print(f"::error::Failed to parse --mock-prs JSON: {e}", file=sys.stderr)
            return 1
    elif args.mock_prs_file:
        try:
            with open(args.mock_prs_file, "r", encoding="utf-8") as f:
                mock_data = json.load(f)
        except Exception as e:
            print(f"::error::Failed to read --mock-prs-file: {e}", file=sys.stderr)
            return 1

    repo_map = parse_setup_sh(setup_sh_path)
    if not repo_map:
        print(f"::error::No repositories found in {setup_sh_path}", file=sys.stderr)
        return 1

    print(f"Starting dependency resolution for {root_ref} (allowed owners: {allowed_owners})...")
    print(f"Repository map derived from {setup_sh_path}: {len(repo_map)} entries")
    fetcher = GitHubPRFetcher(token=args.token, mock_prs=mock_data)
    resolver = DependencyResolver(fetcher, max_depth=args.max_depth, allowed_owners=allowed_owners)


    resolved_order = resolver.resolve(root_ref)
    print_dependency_tree(root_ref, resolver, resolved_order)

    if resolver.errors:
        print("::error::Dependency resolution failed.", file=sys.stderr)
        return 1

    success = apply_git_checkouts(resolved_order, repo_map, dry_run=args.dry_run)
    if not success:
        print("::error::One or more dependency checkout/merge operations failed.", file=sys.stderr)
        return 1

    print(f"Successfully resolved and checked out all dependencies for {root_ref}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
