from __future__ import annotations

import glob
import hashlib
import json
import os

from pydantic import ValidationError

from . import notify, render, verdict
from .data import Store
from .github import GitHub, GitHubError
from .models import Plan, Report, Result
from .plan import Settings, schedule_channel

CHECK = "Catalog Core"


class TamperedPlan(Exception):
    pass


def load_plan(path: str) -> Plan:
    with open(path, "rb") as f:
        raw = f.read()
    expected = os.environ.get("PLAN_DIGEST")
    if expected and hashlib.sha256(raw).hexdigest() != expected:
        raise TamperedPlan(f"{path} is not the plan the plan job wrote")
    return Plan.model_validate_json(raw)


def load_results(directory: str) -> list[Result]:
    results = []
    for path in sorted(glob.glob(os.path.join(directory, "**", "result.json"), recursive=True)):
        try:
            with open(path) as f:
                results.append(Result.model_validate_json(f.read()))
        except (OSError, ValidationError) as e:
            print(f"::warning::ignoring {path}: {str(e)[:300]}")
    return results


def run_url() -> str:
    return f"https://github.com/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ.get('GITHUB_RUN_ID')}"


def write_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def post_check(
    plan: Plan, status: str, title: str, text: str, conclusion: str | None = None, check_id: int | None = None
) -> int | None:
    write_summary(f"## {title}\n\n{text}")
    gh = GitHub()
    if not gh.token or not plan.root_repo or not plan.root_head_sha:
        return None
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
    plan = load_plan(args.plan)
    if plan.skip:
        return 0
    if plan.reused:
        reused = plan.reused
        check_id = post_check(plan, "completed", reused.title, render.reused(plan), reused.conclusion)
    elif plan.errors or not plan.cells:
        conclusion = "failure" if plan.errors else "success"
        check_id = post_check(plan, "completed", render.title(plan, []), render.check(plan), conclusion)
    else:
        title = f"Testing {render.plural(plan.n_configs, 'configuration')}"
        if not plan.trusted:
            title = "Waiting for a maintainer to approve the run"
        check_id = post_check(plan, "in_progress", title, render.check(plan, running=True))
    output_path = os.environ.get("GITHUB_OUTPUT")
    if check_id and output_path:
        with open(output_path, "a") as f:
            f.write(f"check_id={check_id}\n")
    return 0


def open_store(token: str | None) -> Store | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return None
    try:
        return Store(repo, token).open()
    except Exception as e:
        print(f"::warning::ci-data unavailable: {e}")
        return None


def cmd_fan_out(args) -> int:
    plan = load_plan(args.plan)
    if not plan.fan_out or not plan.root or plan.skip:
        return 0
    store = open_store(None)
    if store is None:
        return 1
    gh = GitHub()
    here = os.environ["GITHUB_REPOSITORY"]
    for ref in store.dependents(plan.root):
        repo, _, number = ref.partition("#")
        print(f"{plan.root} changed: re-testing {ref}, which was tested together with it")
        gh.dispatch(here, "catalog_pr", {"repo": repo, "number": number, "cascade": "false"})
    return 0


def declined(plan: Plan) -> dict | None:
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if plan.trusted or not repo or not run_id:
        return None
    try:
        approvals = GitHub(os.environ.get("CI_DATA_TOKEN")).run_approvals(repo, run_id)
    except GitHubError:
        return None
    for approval in approvals:
        if approval.get("state") == "rejected":
            return approval
    return None


def job_logs() -> dict[tuple[str, str], str]:
    repo = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not repo or not run_id:
        return {}
    try:
        jobs = GitHub(os.environ.get("CI_DATA_TOKEN")).run_jobs(repo, run_id)
    except GitHubError as e:
        print(f"::warning::no links to the job logs: {e}")
        return {}
    logs = {}
    for name, url in jobs.items():
        if name.startswith("test (") and name.endswith(")") and ", " in name:
            app, _, compiler = name[len("test (") : -1].partition(", ")
            logs[app, compiler] = url
    return logs


def nightly_without_plan() -> int:
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return 1
    with open(os.environ["GITHUB_EVENT_PATH"]) as f:
        channel = schedule_channel(json.load(f), Settings.from_env())
    plan = Plan(event="schedule", channel=channel, run_url=run_url())
    store = open_store(os.environ.get("CI_DATA_TOKEN"))
    notify.nightly(plan, [], "", store, "the plan job failed")
    if store:
        store.push(f"schedule: {channel}: plan job failed")
    return 1


def cmd_finish(args) -> int:
    check_id = int(args.check_id) if args.check_id else None
    try:
        plan = load_plan(args.plan)
    except TamperedPlan as e:
        print(f"::error::{e}; not reporting anything from this run")
        return 1
    except (OSError, ValidationError) as e:
        print(f"::error::no plan: {e}")
        return nightly_without_plan()
    store = open_store(os.environ.get("CI_DATA_TOKEN"))

    if plan.errors or not plan.cells:
        if plan.event == "schedule":
            error = "; ".join(plan.errors) or "no apps to test"
            notify.nightly(plan, [], render.title(plan, []), store, error)
            if store:
                store.push(f"{plan.event}: {plan.channel}: not run")
        return 1 if plan.errors else 0

    if os.environ.get("TEST_RESULT") == "cancelled":
        title = "Cancelled: replaced by a newer run, or cancelled by hand"
        post_check(plan, "completed", title, render.check(plan), "cancelled", check_id)
        return 0

    results = load_results(args.results)
    review = None
    if not results:
        review = declined(plan)
    if review is not None:
        who = (review.get("user") or {}).get("login", "a maintainer")
        banner = "A maintainer declined to run the code of these PRs."
        if review.get("comment"):
            banner = f'A maintainer declined to run the code of these PRs: "{render.oneline(review["comment"])}"'
        title = f"Not run: @{who} declined to run this code"
        post_check(plan, "completed", title, render.check(plan, banner=banner), "action_required", check_id)
        return 1

    verdicts = verdict.judge(plan, results)
    conclusion = verdict.conclusion(plan, verdicts)
    title = render.title(plan, verdicts)
    text = render.check(plan, verdicts, logs=job_logs())
    report = Report(plan_hash=plan.plan_hash, conclusion=conclusion, title=title, verdicts=verdicts)
    with open(args.out, "w") as f:
        f.write(report.model_dump_json(indent=2))
    post_check(plan, "completed", title, text, conclusion, check_id)
    print(title)
    if plan.event == "schedule":
        notify.nightly(plan, verdicts, title, store)
    if store and os.environ.get("CI_DATA_TOKEN"):
        store.record(plan, report, results, text)
        store.record_base(plan, results)
        store.push(f"{plan.event}: {plan.root or plan.channel}: {conclusion}")
    if conclusion == "success":
        return 0
    return 1
