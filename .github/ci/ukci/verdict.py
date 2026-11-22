from __future__ import annotations

from .models import Attempt, Plan, Result, Verdict, VerdictName

BLOCKING: set[VerdictName] = {"REGRESSION", "FAIL", "INFRA"}
ORDER: list[VerdictName] = ["REGRESSION", "FAIL", "INFRA", "FLAKY", "BROKEN_ON_BASE", "PASS"]
PHASE_ORDER = {"build": 0, "run": 1}


def judge_one(
    first: Attempt | None,
    retry: Attempt | None,
    base: Attempt | None,
    comparable: bool,
    infra_error: str | None,
    base_retry: Attempt | None = None,
) -> tuple[VerdictName, str]:
    if first is None:
        return "INFRA", infra_error or "did not run"
    if first.ok:
        return "PASS", ""
    last = retry or first
    if last.ok:
        return "FLAKY", first.reason
    if last.env_error:
        return "INFRA", f"the runner is missing a tool: {last.reason}"
    if last.fetch_error:
        return "INFRA", f"download failed: {last.reason}"
    if not comparable:
        return "FAIL", last.reason
    if base is None:
        return "INFRA", infra_error or "the comparison run without the PRs is missing"
    if base_retry is not None and not base.ok:
        base = base_retry
    if base.env_error:
        return "INFRA", f"the runner is missing a tool: {base.reason}"
    if base.fetch_error:
        return "INFRA", f"download failed without the PRs: {base.reason}"
    if base.ok:
        return "REGRESSION", last.reason
    if PHASE_ORDER[last.phase] < PHASE_ORDER[base.phase]:
        return "REGRESSION", last.reason
    return "BROKEN_ON_BASE", last.reason


def judge(plan: Plan, results: list[Result]) -> list[Verdict]:
    by_cell = {(r.app, r.compiler): r for r in results}
    verdicts = []
    for cell in plan.cells:
        result = by_cell.get((cell.app, cell.compiler))
        attempts = {}
        infra_error: str | None = "the job produced no results"
        new_variants: list[str] = []
        if result is not None:
            attempts = {(a.variant, a.attempt): a for a in result.attempts}
            infra_error = result.infra_error
            new_variants = result.new_variants
        for variant in cell.variants:
            name, reason = judge_one(
                attempts.get((variant, "first")),
                attempts.get((variant, "retry")),
                attempts.get((variant, "base")),
                plan.has_base(cell) and variant not in new_variants,
                infra_error,
                attempts.get((variant, "base_retry")),
            )
            verdicts.append(Verdict(app=cell.app, compiler=cell.compiler, variant=variant, verdict=name, reason=reason))
    return verdicts


def conclusion(plan: Plan, verdicts: list[Verdict]) -> str:
    if plan.errors:
        return "failure"
    if any(v.verdict in BLOCKING for v in verdicts):
        return "failure"
    return "success"
