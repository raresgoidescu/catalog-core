#!/usr/bin/env bash
#
# Cross-repository dependency resolution for catalog-core CI.
#
# Problem: a PR to lib-musl may only make sense together with an
# unmerged PR to lib-libelf. Without this, a scheduled run tests
# `musl@PR / libelf@main` and gets a false FAILED, or a PR run tests
# `musl@PR / libelf@main` and gets a false PASSED that will break once
# both land.
#
# Convention (deliberately borrowed from Zuul's cross-repo "Depends-On:"
# footer, which is the closest thing to a de-facto standard for this --
# see https://zuul-ci.org/docs/zuul/reference/gating.html#cross-project-dependencies.
# We do NOT reimplement Zuul's speculative merge / dependency pipeline;
# that is a project-wide CI system, not a fit for a single test repo on
# GitHub-hosted runners. This script does the minimal useful subset:
# resolve the graph, check out every node, then let the existing test
# suite run once against the resulting composite checkout):
#
#   Depends-On: unikraft/lib-libelf#42
#   Depends-On: #17                (same repo as the triggering PR)
#
# One or more such lines anywhere in the PR body (or trailing commit
# messages, since squash-merge tooling often moves footers there) are
# picked up. Resolution is recursive (a dependency's PR body is scanned
# too) with a visited-set to guard against cycles, and depth-limited as
# a belt-and-braces backstop.
#
# Usage:
#   resolve-depends-on.sh <owner/repo> <pr_number> <local-repo-map-file>
#
# local-repo-map-file: lines of "owner/repo=local/path", e.g.
#   unikraft/unikraft=repos/unikraft
#   unikraft/lib-musl=repos/libs/musl
#   unikraft/lib-libelf=repos/libs/libelf
#   ...
# (this mirrors setup.sh's clone layout -- see repo-map.txt in this dir)
#
# Requires: gh CLI authenticated (GH_TOKEN), git.

set -euo pipefail

ROOT_REPO="$1"
ROOT_PR="$2"
MAP_FILE="$3"
MAX_DEPTH="${4:-5}"

declare -A REPO_PATH
while IFS='=' read -r repo path; do
  [ -z "$repo" ] && continue
  case "$repo" in \#*) continue ;; esac
  REPO_PATH["$repo"]="$path"
done < "$MAP_FILE"

declare -A VISITED   # "owner/repo#pr" -> 1, cycle/dup guard

checkout_pr() {
  local repo="$1" pr="$2"
  local path="${REPO_PATH[$repo]:-}"

  if [ -z "$path" ]; then
    echo "::warning::Depends-On references '$repo' but it has no entry in $MAP_FILE -- skipping checkout (it will still be picked up if it's the trigger repo itself)."
    return
  fi
  if [ ! -d "$path" ]; then
    echo "::warning::Depends-On references '$repo' (mapped to '$path') but that directory does not exist -- was setup.sh run first?"
    return
  fi

  echo "-> Resolving dependency: $repo#$pr into $path"
  (
    cd "$path"
    git fetch -fu "https://github.com/$repo" "refs/pull/$pr/head:depends-on-$pr"
    git checkout "depends-on-$pr"
  )
}

get_pr_body() {
  local repo="$1" pr="$2"
  gh pr view "$pr" --repo "$repo" --json body,commits \
    --jq '[.body, (.commits[].messageBody // "")] | join("\n")' 2>/dev/null || true
}

resolve() {
  local repo="$1" pr="$2" depth="$3"
  local key="$repo#$pr"

  if [ -n "${VISITED[$key]:-}" ]; then
    return
  fi
  VISITED["$key"]=1

  if [ "$depth" -gt "$MAX_DEPTH" ]; then
    echo "::warning::Depends-On chain exceeded max depth ($MAX_DEPTH) at $key -- stopping recursion here. Check for a cycle."
    return
  fi

  local body
  body="$(get_pr_body "$repo" "$pr")"
  [ -z "$body" ] && return

  # Matches:
  #   Depends-On: owner/repo#123
  #   Depends-On: #123            (implicitly same repo as $repo)
  #   Depends on #123             (case/hyphen-insensitive, matches the
  #                                 plain-English phrasing used in the
  #                                 issue that prompted this script)
  while IFS= read -r line; do
    [ -z "$line" ] && continue

    local dep_repo dep_pr
    if [[ "$line" =~ ^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)$ ]]; then
      dep_repo="${BASH_REMATCH[1]}"
      dep_pr="${BASH_REMATCH[2]}"
    elif [[ "$line" =~ ^#([0-9]+)$ ]]; then
      dep_repo="$repo"
      dep_pr="${BASH_REMATCH[1]}"
    else
      continue
    fi

    if [ "$dep_repo" = "$repo" ] && [ "$dep_pr" = "$pr" ]; then
      continue  # self-reference guard
    fi

    checkout_pr "$dep_repo" "$dep_pr"
    resolve "$dep_repo" "$dep_pr" "$((depth + 1))"
  done < <(printf '%s\n' "$body" \
      | grep -oiE '(depends-on:|depends on)[[:space:]]*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[0-9]+' \
      | grep -oE '([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[0-9]+')
}

resolve "$ROOT_REPO" "$ROOT_PR" 0

if [ "${#VISITED[@]}" -le 1 ]; then
  echo "No Depends-On references found for $ROOT_REPO#$ROOT_PR."
else
  echo "Resolved dependency graph:"
  for k in "${!VISITED[@]}"; do echo "  - $k"; done
fi
