"""The "Catalog Core" check on the PRs a run tests, and blame.

The check goes on the root PR and, when the run tests a Depends-On cycle,
on every other PR of the cycle too. Posting a check is feedback: it never
fails the run.
"""

from __future__ import annotations

from .data import Store
from .github import GitHub, GitHubError
from .models import Plan, Verdict

NAME = "Catalog Core"


def post(
    gh: GitHub,
    plan: Plan,
    *,
    status: str,
    title: str,
    summary: str,
    conclusion: str | None = None,
    check_id: int | None = None,
) -> int | None:
    """Create or update the check on the root PR and its cycle; returns the root's check id."""
    if not gh.token or not plan.root_repo or not plan.root_head_sha or plan.root_repo.endswith("/catalog-core"):
        return None  # catalog-core's own PRs show the workflow run itself
    body = {
        "name": NAME,
        "status": status,
        "details_url": plan.run_url,
        "output": {"title": title[:250], "summary": summary},
    }
    if conclusion:
        body["conclusion"] = conclusion
    root_id = None
    targets = [(plan.root_repo, plan.root_head_sha, check_id)]
    if not plan.delegate_to:  # this run tests the cycle: it also speaks for the others
        targets += [(m.repo, m.head_sha, None) for m in plan.cycle if m.ref != plan.root]
    for i, (repo, sha, known) in enumerate(targets):
        try:
            existing = known or (gh.find_check(repo, sha, NAME) if i else None)
            got = gh.put_check(repo, {**body, "head_sha": sha}, existing)
            root_id = got if i == 0 else root_id
        except GitHubError as e:
            print(f"::warning::could not update the {NAME} check on {repo}@{sha[:10]}: {e}")
    return root_id


def blame(plan: Plan, verdicts: list[Verdict], store: Store | None) -> None:
    """For each regression, say whether a dependency PR causes it on its own.

    Every PR has its own run (from its own pushes). If that run fails the
    same configuration, the failure is inherited from it.
    """
    deps = [p for p in plan.prs if p.used and p.ref != plan.root]
    if not deps or store is None:
        return
    for v in (v for v in verdicts if v.verdict == "REGRESSION"):
        inherited, unknown = [], []
        for d in deps:
            own = store.own_result(d.ref, d.head_sha)
            if own is None:
                unknown.append(d.ref)
            elif any(
                (x.app, x.compiler, x.variant, x.verdict in ("REGRESSION", "FAIL"))
                == (v.app, v.compiler, v.variant, True)
                for x in own.verdicts
            ):
                inherited.append(f"[{d.ref}]({own.run_url})")
        if inherited:
            v.inherited_from = ", ".join(x[1 : x.index("]")] for x in inherited)
            v.detail += f". **Inherited**: it also fails in the own run of {', '.join(inherited)}"
        elif unknown:
            v.detail += f". Not known yet whether it comes from {', '.join(unknown)} (no own run yet)"
        else:
            v.detail += ". The dependencies pass on their own, so it comes from this PR (or the combination)"
