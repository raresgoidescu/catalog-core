"""Turn raw attempts into verdicts: did *this change* make things worse?

A configuration is one app + compiler + variant (e.g. nginx, gcc, qemu.x86_64).
For each one that failed, the runner retried a failed *run* once and ran it
again without the PRs (base). From that:

  PASS            passed
  FLAKY           failed, then passed on retry with the same code    shown, not blocking
  BROKEN_ON_BASE  fails the same way without the PRs                 shown, not blocking
                  (same phase, same first error)
  REGRESSION      passes without the PRs, or fails differently       BLOCKING
  FAIL            fails, and there is nothing to compare it to        BLOCKING
                  (scheduled runs: nothing is "the PR")
  INFRA           CI could not tell: a download failed, the job died, BLOCKING (re-run;
                  the base re-run is missing                          not your fault)
"""

from __future__ import annotations

from dataclasses import dataclass

PASS, FLAKY, BROKEN_ON_BASE, REGRESSION, FAIL, INFRA = \
    "PASS", "FLAKY", "BROKEN_ON_BASE", "REGRESSION", "FAIL", "INFRA"
BLOCKING = {REGRESSION, FAIL, INFRA}
ORDER = [REGRESSION, FAIL, INFRA, FLAKY, BROKEN_ON_BASE, PASS]


@dataclass
class Verdict:
    app: str
    compiler: str
    variant: str
    verdict: str
    reason: str                         # what failed, for people ("" when passed)
    detail: str = ""                    # why this verdict
    inherited_from: str | None = None   # dependency PR whose own run fails the same way


def _ok(a: dict) -> bool:
    return a["build"] == "PASS" and (a["run"] is None or a["run"] == "PASS")


def _phase(a: dict) -> str:
    return "build" if a["build"] != "PASS" else "run"


def base_label(plan: dict) -> str:
    label = plan.get("channel", "base")
    if plan.get("catalog_base"):
        label += f" and catalog-core {plan['catalog_base'].get('from', 'base')}"
    return label


def has_base(plan: dict, cell: dict) -> bool:
    """Is there anything 'without the PRs' to compare this cell to?"""
    if plan.get("catalog_base"):
        return True
    return any(plan["base_lock"].get(p, {}).get("sha") != plan["lock"].get(p, {}).get("sha")
               for p in cell.get("repos", []))


def judge_one(first: dict | None, retry: dict | None, base: dict | None, *,
              comparable: bool, where: str, infra_error: str | None) -> tuple[str, str, str]:
    """(verdict, reason, detail) for one configuration."""
    if first is None:
        return INFRA, infra_error or "did not run", "re-run the workflow"
    if _ok(first):
        return PASS, "", ""
    reason = first.get("reason") or f"{_phase(first)} failed"
    if first.get("fetch_error"):
        return INFRA, reason, "a download failed during the build; re-run the workflow"
    if retry and _ok(retry):
        return FLAKY, reason, "passed on retry with the same code"
    if not comparable:
        return FAIL, reason, ""
    if base is None:
        return INFRA, reason, infra_error or "the comparison run without the PRs is missing; re-run the workflow"
    if _ok(base):
        return REGRESSION, reason, f"passes on {where} without the PRs"
    if base.get("fetch_error"):
        return INFRA, reason, "a download failed in the comparison run; re-run the workflow"
    if base.get("signature") and base.get("signature") == first.get("signature"):
        return BROKEN_ON_BASE, reason, f"fails the same way on {where} without the PRs"
    return REGRESSION, reason, f"fails differently on {where} without the PRs: {base.get('reason') or _phase(base) + ' failed'}"


def judge(plan: dict, results: list[dict]) -> list[Verdict]:
    where = base_label(plan)
    by_cell = {(r["app"], r["compiler"]): r for r in results}
    out: list[Verdict] = []
    for cell in plan["cells"]:
        app, cc = cell["app"], cell["compiler"]
        r = by_cell.get((app, cc)) or {}
        attempts = {(a["variant"], a["attempt"]): a for a in r.get("attempts", [])}
        infra = r.get("infra_error") or (None if r else "the job produced no results (see the failing step in the run)")
        for v in cell["variants"]:
            verdict, reason, detail = judge_one(
                attempts.get((v, "first")), attempts.get((v, "retry")), attempts.get((v, "base")),
                comparable=has_base(plan, cell), where=where, infra_error=infra)
            out.append(Verdict(app, cc, v, verdict, reason, detail))
    return out


def conclusion(plan: dict, verdicts: list[Verdict]) -> str:
    if plan.get("errors"):
        return "failure"
    return "failure" if any(v.verdict in BLOCKING for v in verdicts) else "success"
