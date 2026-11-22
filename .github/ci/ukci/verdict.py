"""Turn raw attempts into verdicts: did *this change* make things worse?

Per app / compiler / variant:

  PASS            passed first time
  FLAKY           failed, passed on retry (same code)         non-blocking
  BROKEN_ON_BASE  fails with and without the PRs              non-blocking
  REGRESSION      fails with the PRs, passes without them     BLOCKING
  FAIL            fails, and there is no base to compare to   BLOCKING
                  (scheduled runs, or the PRs don't touch it)
  INFRA           CI itself failed (checkout, runner, ...)    BLOCKING,
                  but says so: re-run, don't fix your PR
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
    phase: str | None          # "build" | "run" | None
    detail: str
    inherited_from: str | None = None   # dependency PR whose own run fails the same way


def _failed_phase(a: dict) -> str | None:
    if a["build"] != "PASS":
        return "build"
    if a["run"] is not None and a["run"] != "PASS":
        return "run"
    return None


def _describe(a: dict) -> str:
    phase = _failed_phase(a)
    if phase is None:
        return "passed"
    status = a[phase]
    return {"TIMEOUT": f"{phase} timed out", "MISSING": f"{phase} produced no result"}.get(status, f"{phase} failed")


def base_label(plan: dict) -> str:
    label = plan.get("channel", "base")
    if plan.get("catalog_base"):
        label += f" and catalog-core {plan['catalog_base'].get('from', 'base')}"
    return label


def judge(plan: dict, results: list[dict]) -> list[Verdict]:
    where = base_label(plan)
    by_cell = {(r["app"], r["compiler"]): r for r in results}
    out: list[Verdict] = []
    for cell in plan["cells"]:
        app, cc = cell["app"], cell["compiler"]
        r = by_cell.get((app, cc))
        if r is None or (r.get("infra_error") and not r.get("attempts")):
            why = (r or {}).get("infra_error") or "the job produced no results (see the run for the failing step)"
            out += [Verdict(app, cc, v, INFRA, None, why) for v in cell["variants"]]
            continue
        attempts: dict[tuple[str, str], dict] = {(a["variant"], a["attempt"]): a for a in r["attempts"]}
        for v in cell["variants"]:
            first = attempts.get((v, "first"))
            if first is None:
                out.append(Verdict(app, cc, v, INFRA, None, r.get("infra_error") or "variant did not run"))
                continue
            phase = _failed_phase(first)
            if phase is None:
                out.append(Verdict(app, cc, v, PASS, None, "passed"))
                continue
            retry, base = attempts.get((v, "retry")), attempts.get((v, "base"))
            if retry and _failed_phase(retry) is None:
                out.append(Verdict(app, cc, v, FLAKY, phase, f"{_describe(first)}, passed on retry"))
            elif base:
                if _failed_phase(base) is None:
                    out.append(Verdict(app, cc, v, REGRESSION, phase,
                                       f"{_describe(first)} with the PRs; passes on {where} without them"))
                else:
                    out.append(Verdict(app, cc, v, BROKEN_ON_BASE, phase,
                                       f"{_describe(first)}; also fails on {where} without the PRs"))
            else:
                out.append(Verdict(app, cc, v, FAIL, phase, _describe(first)))
    return out


def conclusion(plan: dict, verdicts: list[Verdict]) -> str:
    if plan.get("errors"):
        return "failure"
    return "failure" if any(v.verdict in BLOCKING for v in verdicts) else "success"
