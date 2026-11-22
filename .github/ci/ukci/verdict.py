"""Turn attempts into verdicts: did *this change* make things worse?

A configuration is one app + compiler + variant (nginx, gcc, qemu.x86_64).
A failed run, and a build that failed downloading sources, are retried once
on the same code. A configuration that still fails runs again without the
PRs (base). Then:

  PASS            passed
  FLAKY           failed, then passed on retry                         -
  BROKEN_ON_BASE  fails without the PRs too, in the same phase or      -
                  an earlier one (base doesn't build, PR doesn't boot)
  REGRESSION      passes without the PRs, or fails only in a later     blocks
                  phase there (base builds, the PR doesn't)
  FAIL            fails, nothing to compare to (scheduled runs, or an  blocks
                  app/variant the PR adds)
  INFRA           CI couldn't tell: downloads failed twice, the job    blocks
                  died, the comparison run is missing                  (re-run)

Only the phase is compared, never the error text: with parallel builds,
which error is printed first changes from run to run.
"""

from __future__ import annotations

from .models import Attempt, Plan, Result, Verdict, VerdictName

BLOCKING: set[VerdictName] = {"REGRESSION", "FAIL", "INFRA"}
ORDER: list[VerdictName] = ["REGRESSION", "FAIL", "INFRA", "FLAKY", "BROKEN_ON_BASE", "PASS"]
PHASE_ORDER = {"build": 0, "run": 1}


def judge_one(
    first: Attempt | None,
    retry: Attempt | None,
    base: Attempt | None,
    *,
    comparable: bool,
    infra_error: str | None,
) -> tuple[VerdictName, str]:
    """(verdict, what failed) for one configuration."""
    if first is None:
        return "INFRA", infra_error or "did not run"
    if first.ok:
        return "PASS", ""
    last = retry or first
    if last.ok:
        return "FLAKY", first.reason
    if last.fetch_error:
        return "INFRA", f"download failed: {last.reason}"
    if not comparable:
        return "FAIL", last.reason
    if base is None:
        return "INFRA", infra_error or "the comparison run without the PRs is missing"
    if base.fetch_error:
        return "INFRA", f"download failed without the PRs: {base.reason}"
    if base.ok or PHASE_ORDER[last.phase] < PHASE_ORDER[base.phase]:
        return "REGRESSION", last.reason
    return "BROKEN_ON_BASE", last.reason


def judge(plan: Plan, results: list[Result]) -> list[Verdict]:
    by_cell = {(r.app, r.compiler): r for r in results}
    out = []
    for cell in plan.cells:
        r = by_cell.get((cell.app, cell.compiler))
        attempts = {(a.variant, a.attempt): a for a in (r.attempts if r else [])}
        infra = r.infra_error if r else "the job produced no results"
        for v in cell.variants:
            name, reason = judge_one(
                attempts.get((v, "first")),
                attempts.get((v, "retry")),
                attempts.get((v, "base")),
                comparable=plan.has_base(cell) and not (r and v in r.new_variants),
                infra_error=infra,
            )
            out.append(Verdict(app=cell.app, compiler=cell.compiler, variant=v, verdict=name, reason=reason))
    return out


def conclusion(plan: Plan, verdicts: list[Verdict]) -> str:
    return "failure" if plan.errors or any(v.verdict in BLOCKING for v in verdicts) else "success"
