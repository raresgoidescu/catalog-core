"""The trusted job commands around a run: start the check, fan out, finish."""

from __future__ import annotations

import glob
import hashlib
import json
import os

from pydantic import ValidationError

from . import notify, render, verdict
from . import plan as plan_mod
from .data import Store
from .github import GitHub, GitHubError
from .models import Plan, Report, Result

CHECK = "Catalog Core"


class TamperedPlan(Exception):
    pass


def load_plan(path: str) -> Plan:
    """plan.json, checked against the plan job's digest (PLAN_DIGEST) when one is given."""
    with open(path, "rb") as f:
        raw = f.read()
    expected = os.environ.get("PLAN_DIGEST")
    if expected and hashlib.sha256(raw).hexdigest() != expected:
        raise TamperedPlan(f"{path} is not the plan the plan job wrote")
    return Plan.model_validate_json(raw)


def load_results(directory: str) -> list[Result]:
    """result.json files from the test jobs: untrusted, so only valid ones count."""
    results = []
    for path in sorted(glob.glob(os.path.join(directory, "**", "result.json"), recursive=True)):
        try:
            with open(path) as f:
                results.append(Result.model_validate_json(f.read()))
        except (OSError, ValidationError) as e:
            print(f"::warning::ignoring {path}: {str(e)[:300]}")
    return results


def summary(text: str) -> None:
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def post_check(
    plan: Plan, *, status: str, title: str, text: str, conclusion: str | None = None, check_id: int | None = None
) -> int | None:
    """Create or update the check on the root PR. Feedback only: never fails the run."""
    summary(f"## {title}\n\n{text}")
    gh = GitHub()
    if not gh.token or not plan.root_repo or not plan.root_head_sha:
        return None  # catalog-core's own PRs, and scheduled runs: the workflow run is the result
    body: dict = {
        "name": CHECK,
        "head_sha": plan.root_head_sha,
        "status": status,
        "details_url": plan.run_url,
        "output": {"title": title[:250], "summary": text},
    }
    if conclusion:
        body["conclusion"] = conclusion
    try:
        return gh.put_check(plan.root_repo, body, check_id)
    except GitHubError as e:
        print(f"::warning::could not update the {CHECK} check on {plan.root_repo}@{plan.root_head_sha[:10]}: {e}")
        return None


def cmd_start(args) -> int:
    """Plan job: put the check on the PR before anything runs."""
    plan = load_plan(args.plan)
    if plan.skip:
        return 0
    if plan.reused:
        r = plan.reused
        check_id = post_check(
            plan, status="completed", title=r.title, text=render.reused(plan), conclusion=r.conclusion
        )
    elif plan.errors or not plan.cells:
        title = render.title(plan, [])
        conclusion = "failure" if plan.errors else "success"
        check_id = post_check(plan, status="completed", title=title, text=render.check(plan), conclusion=conclusion)
    else:
        title = (
            "⏳ Waiting for a maintainer to approve the run"
            if not plan.trusted
            else f"⏳ Testing {render.plural(plan.n_configs, 'configuration')}"
        )
        check_id = post_check(plan, status="in_progress", title=title, text=render.check(plan, running=True))
    if check_id and (path := os.environ.get("GITHUB_OUTPUT")):
        with open(path, "a") as f:
            f.write(f"check_id={check_id}\n")
    return 0


def open_store(token: str | None) -> Store | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    try:
        return Store(repo, token).open() if repo else None
    except Exception as e:
        print(f"::warning::ci-data unavailable: {e}")
        return None


def cmd_fan_out(args) -> int:
    """A PR changed: re-test the PRs that were tested together with it (once: no further fan-out)."""
    plan = load_plan(args.plan)
    if not plan.fan_out or not plan.root or plan.skip:
        return 0
    store = open_store(None)
    if store is None:
        return 1
    gh, here = GitHub(), os.environ["GITHUB_REPOSITORY"]
    for ref in store.dependents(plan.root):
        repo, _, n = ref.partition("#")
        print(f"{plan.root} changed: re-testing {ref}, which was tested together with it")
        gh.dispatch(here, "catalog_pr", {"repo": repo, "number": n, "cascade": "false"})
    return 0


def declined(plan: Plan) -> dict | None:
    """The maintainer review that declined to run this PR set's code, if any."""
    repo, run_id = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    if plan.trusted or not repo or not run_id:
        return None
    try:
        approvals = GitHub(os.environ.get("CI_DATA_TOKEN")).run_approvals(repo, run_id)
    except GitHubError:
        return None
    return next((r for r in approvals if r.get("state") == "rejected"), None)


def job_logs() -> dict[tuple[str, str], str]:
    """(app, compiler) -> URL of the test job's log."""
    repo, run_id = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    if not repo or not run_id:
        return {}
    try:
        jobs = GitHub(os.environ.get("CI_DATA_TOKEN")).run_jobs(repo, run_id)
    except GitHubError as e:
        print(f"::warning::no links to the job logs: {e}")
        return {}
    out = {}
    for name, url in jobs.items():
        if name.startswith("test (") and name.endswith(")") and ", " in name:
            app, _, cc = name[len("test (") : -1].partition(", ")
            out[app, cc] = url
    return out


def cmd_finish(args) -> int:
    """Report job: judge the results, finish the check, record, notify."""
    check_id = int(args.check_id) if args.check_id else None
    try:
        plan = load_plan(args.plan)
    except TamperedPlan as e:
        print(f"::error::{e}; not reporting anything from this run")
        return 1
    except (OSError, ValidationError) as e:  # the plan job died before writing a plan
        print(f"::error::no plan: {e}")
        if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
            with open(os.environ["GITHUB_EVENT_PATH"]) as f:
                channel = plan_mod.schedule_channel(json.load(f), plan_mod.Settings.from_env())
            repo, run_id = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
            plan = Plan(event="schedule", channel=channel, run_url=f"https://github.com/{repo}/actions/runs/{run_id}")
            store = open_store(os.environ.get("CI_DATA_TOKEN"))
            notify.nightly(plan, [], "", store, "the plan job failed")
            if store:
                store.push(f"schedule: {channel}: plan job failed")
        return 1
    store = open_store(os.environ.get("CI_DATA_TOKEN"))

    if plan.errors or not plan.cells:  # nothing ran; the plan job already finished the check
        if plan.event == "schedule":
            notify.nightly(plan, [], render.title(plan, []), store, "; ".join(plan.errors) or "no apps to test")
            if store:
                store.push(f"{plan.event}: {plan.channel}: not run")
        return 1 if plan.errors else 0

    test_result = os.environ.get("TEST_RESULT", "")
    if test_result == "cancelled":
        post_check(
            plan,
            status="completed",
            title="⏹ Cancelled: replaced by a newer run, or cancelled by hand",
            text=render.check(plan),
            conclusion="cancelled",
            check_id=check_id,
        )
        return 0  # nothing to judge, nothing to record
    results = load_results(args.results)
    if not results and (review := declined(plan)):
        who = (review.get("user") or {}).get("login", "a maintainer")
        comment = f': "{render.oneline(review["comment"])}"' if review.get("comment") else ""
        post_check(
            plan,
            status="completed",
            title=f"⛔ Not run: @{who} declined to run this code",
            text=render.check(plan, banner=f"A maintainer declined to run the code of these PRs{comment}."),
            conclusion="action_required",
            check_id=check_id,
        )
        return 1

    verdicts = verdict.judge(plan, results)
    conclusion = verdict.conclusion(plan, verdicts)
    title = render.title(plan, verdicts)
    text = render.check(plan, verdicts, logs=job_logs())
    report = Report(plan_hash=plan.plan_hash, conclusion=conclusion, title=title, verdicts=verdicts)
    with open(args.out, "w") as f:
        f.write(report.model_dump_json(indent=2))
    post_check(plan, status="completed", title=title, text=text, conclusion=conclusion, check_id=check_id)
    print(title)
    if plan.event == "schedule":
        notify.nightly(plan, verdicts, title, store)
    if store and os.environ.get("CI_DATA_TOKEN"):
        store.record(plan, report, results, text)
        store.push(f"{plan.event}: {plan.root or plan.channel}: {conclusion}")
    return 0 if conclusion == "success" else 1
