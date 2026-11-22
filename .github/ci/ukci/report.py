"""The trusted job commands around a run: start the check, finish it, fan out."""

from __future__ import annotations

import glob
import os

from pydantic import ValidationError

from . import checks, notify, render, verdict
from .data import Store
from .github import GitHub
from .models import Plan, Report, Result


def load_plan(path: str) -> Plan:
    with open(path) as f:
        return Plan.model_validate_json(f.read())


def load_results(directory: str) -> list[Result]:
    """result.json files from the test jobs: untrusted, so only valid ones count."""
    results = []
    for path in sorted(glob.glob(os.path.join(directory, "**", "result.json"), recursive=True)):
        try:
            with open(path) as f:
                results.append(Result.model_validate_json(f.read()))
        except (OSError, ValidationError) as e:
            print(f"::warning::ignoring {path}: {e}")
    return results


def summary(text: str) -> None:
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def open_store(token: str | None) -> Store | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    try:
        return Store(repo, token).open() if repo else None
    except Exception as e:
        print(f"::warning::ci-data unavailable: {e}")
        return None


def cmd_start(args) -> int:
    """Plan job: put the check on the PR(s) before anything runs."""
    plan = load_plan(args.plan)
    if plan.delegate_to:
        title, status, concl = f"⏳ Tested together with {plan.delegate_to}", "in_progress", None
        text = render.check(plan)
    elif plan.errors or not plan.cells:
        title, status, concl = render.title(plan, []), "completed", "failure" if plan.errors else "success"
        text = render.check(plan)
    else:
        title = (
            "⏳ Waiting for maintainer approval"
            if not plan.trusted
            else f"⏳ Testing {render.plural(plan.n_configs, 'configuration')}"
        )
        status, concl, text = "in_progress", None, render.check(plan, running=True)
    summary(f"## {title}\n\n{text}")
    if plan.root_closed:
        return 0
    check_id = checks.post(GitHub(), plan, status=status, title=title, summary=text, conclusion=concl)
    if check_id and (path := os.environ.get("GITHUB_OUTPUT")):
        with open(path, "a") as f:
            f.write(f"check_id={check_id}\n")
    return 0


def finish(gh: GitHub, plan: Plan, args, title: str, text: str, conclusion: str) -> None:
    summary(f"## {title}\n\n{text}")
    checks.post(
        gh,
        plan,
        status="completed",
        title=title,
        summary=text,
        conclusion=conclusion,
        check_id=int(args.check_id) if args.check_id else None,
    )
    print(title)


def cmd_finish(args) -> int:
    """Report job: judge the results, finish the check, record, notify."""
    plan = load_plan(args.plan)
    gh = GitHub()
    if os.environ.get("TEST_RESULT") == "cancelled":
        finish(
            gh,
            plan,
            args,
            "⏹ Cancelled: replaced by a newer run, or cancelled by hand",
            render.check(plan),
            "cancelled",
        )
        return 0  # nothing to judge, nothing to record
    results = load_results(args.results)
    if not results and (review := declined(plan)):
        who = (review.get("user") or {}).get("login", "a maintainer")
        comment = f': "{review["comment"]}"' if review.get("comment") else ""
        finish(
            gh,
            plan,
            args,
            f"⛔ Not run: @{who} declined to run this code",
            render.check(plan, banner=f"A maintainer declined to run the code of these PRs{comment}."),
            "action_required",
        )
        return 1

    verdicts = verdict.judge(plan, results)
    store = open_store(os.environ.get("CI_DATA_TOKEN"))
    checks.blame(plan, verdicts, store)
    conclusion = verdict.conclusion(plan, verdicts)
    title = render.title(plan, verdicts)
    report = Report(plan_hash=plan.plan_hash, conclusion=conclusion, title=title, verdicts=verdicts)
    with open(args.out, "w") as f:
        f.write(report.model_dump_json(indent=2))
    finish(gh, plan, args, title, render.check(plan, verdicts), conclusion)
    if plan.event == "schedule":
        notify.nightly(plan, verdicts, title, store)
    if store and os.environ.get("CI_DATA_TOKEN"):
        store.record(plan, report, results)
        store.push(f"{plan.event}: {plan.root or plan.channel}: {conclusion}")
    return 0 if conclusion == "success" else 1


def declined(plan: Plan) -> dict | None:
    """The maintainer review that declined to run this PR set's code, if any."""
    repo, run_id = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    if plan.trusted or not repo or not run_id:
        return None
    try:
        approvals = GitHub(os.environ.get("CI_DATA_TOKEN")).run_approvals(repo, run_id)
    except Exception:
        return None
    return next((r for r in approvals if r.get("state") == "rejected"), None)


def cmd_fan_out(args) -> int:
    """A PR changed: re-test the PRs that were tested together with it."""
    plan = load_plan(args.plan)
    if not plan.fan_out or not plan.root or plan.errors:
        return 0
    gh, here = GitHub(), os.environ["GITHUB_REPOSITORY"]

    def dispatch(ref: str, cascade: bool) -> None:
        repo, _, n = ref.partition("#")
        gh.dispatch(here, "catalog_pr", {"repo": repo, "number": n, "cascade": str(cascade).lower()})

    if plan.delegate_to:  # a cycle member changed: run the cycle's shared test
        print(f"{plan.root} changed: running the shared test of {plan.delegate_to}")
        dispatch(plan.delegate_to, True)
        return 0
    store = open_store(None)
    in_cycle = {m.ref for m in plan.cycle}
    for ref in store.dependents(plan.root) if store else []:
        if ref not in in_cycle:  # the cycle is covered by this run
            print(f"{plan.root} changed: re-testing {ref}, which was tested together with it")
            dispatch(ref, False)  # no further fan-out, so it always ends
    return 0
