"""Turn attempts into verdicts: did *this change* make things worse?

A configuration is one app + compiler + variant (nginx, gcc, qemu.x86_64).
A configuration that failed was retried (if its *run* failed) and run again
without the PRs (base). From that:

  PASS            passed
  FLAKY           failed, then passed on retry with the same code   not blocking
  BROKEN_ON_BASE  fails the same way without the PRs                not blocking
                  (same phase, and an error in common)
  REGRESSION      passes without the PRs, or fails differently      BLOCKING
  FAIL            fails, nothing to compare it to (scheduled runs,  BLOCKING
                  or an app/variant the PR adds)
  INFRA           CI couldn't tell: a download failed, the job died BLOCKING; re-run,
                  or the comparison run is missing                  not the PR's fault
"""

from __future__ import annotations

from .models import Attempt, Plan, Result, Verdict, VerdictName

BLOCKING = {"REGRESSION", "FAIL", "INFRA"}
ORDER: list[VerdictName] = ["REGRESSION", "FAIL", "INFRA", "FLAKY", "BROKEN_ON_BASE", "PASS"]


def base_label(plan: Plan) -> str:
    return plan.channel + (f" and catalog-core {plan.catalog_base.source}" if plan.catalog_base else "")


def judge_one(
    first: Attempt | None,
    retry: Attempt | None,
    base: Attempt | None,
    *,
    comparable: bool,
    where: str,
    infra_error: str | None,
) -> tuple[VerdictName, str, str]:
    """(verdict, what failed, why this verdict) for one configuration."""
    if first is None:
        return "INFRA", infra_error or "did not run", "re-run the workflow"
    if first.ok:
        return "PASS", "", ""
    reason = first.reason or f"{first.phase} failed"
    if first.fetch_error:
        return "INFRA", reason, "a download failed during the build; re-run the workflow"
    if retry and retry.ok:
        return "FLAKY", reason, "passed on retry with the same code"
    if not comparable:
        return "FAIL", reason, ""
    if base is None:
        return "INFRA", reason, infra_error or "the comparison run without the PRs is missing; re-run the workflow"
    if base.ok:
        return "REGRESSION", reason, f"passes on {where} without the PRs"
    if base.fetch_error:
        return "INFRA", reason, "a download failed in the comparison run; re-run the workflow"
    if first.same_failure(base):
        return "BROKEN_ON_BASE", reason, f"fails the same way on {where} without the PRs"
    there = base.reason or f"{base.phase} failed"
    return "REGRESSION", reason, f"fails differently on {where} without the PRs: {there}"


def judge(plan: Plan, results: list[Result]) -> list[Verdict]:
    where = base_label(plan)
    by_cell = {(r.app, r.compiler): r for r in results}
    out = []
    for cell in plan.cells:
        r = by_cell.get((cell.app, cell.compiler))
        attempts = {(a.variant, a.attempt): a for a in (r.attempts if r else [])}
        infra = r.infra_error if r else "the job produced no results (see the failing step in the run)"
        for v in cell.variants:
            verdict, reason, detail = judge_one(
                attempts.get((v, "first")),
                attempts.get((v, "retry")),
                attempts.get((v, "base")),
                comparable=plan.has_base(cell) and not (r and v in r.new_variants),
                where=where,
                infra_error=infra,
            )
            out.append(
                Verdict(app=cell.app, compiler=cell.compiler, variant=v, verdict=verdict, reason=reason, detail=detail)
            )
    return out


def conclusion(plan: Plan, verdicts: list[Verdict]) -> str:
    return "failure" if plan.errors or any(v.verdict in BLOCKING for v in verdicts) else "success"
