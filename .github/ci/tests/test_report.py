"""The report job end to end: results in; check, ci-data and Discord out."""

import subprocess

import pytest

from ukci import notify, report
from ukci.data import Store, plan_path
from ukci.models import Attempt, Cell, LockEntry, Plan, Result


@pytest.fixture
def env(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    posted, checks = [], []

    class FakeGitHub:
        token = "t"

        def __init__(self, token=None):
            pass

        def put_check(self, repo, body, check_id=None):
            checks.append((repo, body, check_id))
            return 99

        def run_jobs(self, repo, run_id):
            return {"test (nginx, gcc)": "https://job/1"}

        def run_approvals(self, repo, run_id):
            return []

    monkeypatch.setattr(report, "GitHub", FakeGitHub)
    monkeypatch.setattr(
        report,
        "open_store",
        lambda token: Store("o/catalog-core", token, str(tmp_path / "ci-data"), f"file://{remote}").open(),
    )
    monkeypatch.setattr(notify, "post", lambda url, text, mention="": posted.append((text, mention)) or True)
    for k, v in {
        "GITHUB_REPOSITORY": "o/catalog-core",
        "GITHUB_RUN_ID": "7",
        "CI_DATA_TOKEN": "x",
        "DISCORD_WEBHOOK_URL": "https://discord/hook",
        "DISCORD_MENTION": "<@&1>",
        "TEST_RESULT": "success",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PLAN_DIGEST", raising=False)

    def finish(plan, results):
        (tmp_path / "plan.json").write_text(plan.model_dump_json())
        rdir = tmp_path / "results" / "results-nginx-gcc"
        rdir.mkdir(parents=True, exist_ok=True)
        for r in results:
            (rdir / "result.json").write_text(r.model_dump_json())
        args = type(
            "A",
            (),
            {
                "plan": str(tmp_path / "plan.json"),
                "results": str(tmp_path / "results"),
                "out": str(tmp_path / "v.json"),
                "check_id": "5",
            },
        )
        code = report.cmd_finish(args)
        data = Store(
            "o/catalog-core", None, str(tmp_path / f"read{len(posted)}{len(checks)}"), f"file://{remote}"
        ).open()
        return code, data

    finish.posted, finish.checks = posted, checks
    return finish


def nightly(build="PASS"):
    p = Plan(
        event="schedule",
        channel="staging",
        run_url="https://run/7",
        plan_hash="h",
        cells=[Cell(app="nginx", compiler="gcc", why="all", repos=["unikraft"], variants=["qemu.x86_64"])],
        lock={"unikraft": LockEntry(repo="unikraft/unikraft", sha="s", source="staging")},
        base_lock={"unikraft": LockEntry(repo="unikraft/unikraft", sha="s", source="staging")},
    )
    a = Attempt(variant="qemu.x86_64", attempt="first", build=build, run="PASS" if build == "PASS" else "BLOCKED")
    if build != "PASS":
        a.reason = "x.c:1:1: error: boom"
    return p, [Result(app="nginx", compiler="gcc", plan_hash="h", attempts=[a])]


def test_nightlies_post_on_change_only_and_mention_only_new_failures(env):
    code, data = env(*nightly("PASS"))
    assert code == 0 and "first recorded run" in env.posted[-1][0] and env.posted[-1][1] == ""
    assert data.nightly("staging")["cells"] == {"nginx gcc qemu.x86_64": "PASS"}

    env(*nightly("PASS"))
    assert len(env.posted) == 1  # nothing changed: no message

    code, data = env(*nightly("FAIL"))
    assert code == 1
    text, mention = env.posted[-1]
    assert "Newly failing" in text and "x.c:1:1: error: boom" in text and mention == "<@&1>"
    assert env.checks == []  # scheduled runs have no PR to put a check on


def test_a_nightly_that_cannot_plan_says_so_once(env):
    p = Plan(event="schedule", channel="staging", run_url="https://run/7", errors=["GitHub is down"])
    env(p, [])
    env(p, [])
    assert [t for t, _ in env.posted] == [
        "**catalog-core nightly (staging)**: couldn't run: GitHub is down\nhttps://run/7"
    ]


def test_a_pr_run_finishes_its_check_and_records_a_reusable_result(env):
    p, results = nightly("FAIL")
    lock = LockEntry(repo="o/lib-musl", sha="pr", source="o/lib-musl#1 merged into staging", pr="o/lib-musl#1")
    p = p.model_copy(
        update={
            "event": "repository_dispatch",
            "root": "o/lib-musl#1",
            "root_repo": "o/lib-musl",
            "root_head_sha": "head",
            "cells": [
                Cell(app="nginx", compiler="gcc", why="links libs/musl", repos=["libs/musl"], variants=["qemu.x86_64"])
            ],
            "lock": {"libs/musl": lock},
            "base_lock": {"libs/musl": LockEntry(repo="o/lib-musl", sha="base", source="staging")},
        }
    )
    base = Attempt(variant="qemu.x86_64", attempt="base", build="PASS", run="PASS")
    results[0].attempts.append(base)
    code, data = env(p, results)
    assert code == 1
    [(repo, body, check_id)] = env.checks
    assert (repo, check_id, body["head_sha"], body["conclusion"]) == ("o/lib-musl", 5, "head", "failure")
    assert body["output"]["title"] == "1 regression, 0 of 1 passed"
    assert "| [nginx](https://job/1) | gcc | **regression** |" in body["output"]["summary"]
    reused = data.read(plan_path("h"))
    assert reused["conclusion"] == "failure" and reused["summary"] == body["output"]["summary"]
    assert env.posted == []  # PRs never go to Discord
    [run] = _runs(data)
    assert data.read(run)["root"] == "o/lib-musl#1"


def _runs(store):
    import glob
    import os

    return [os.path.relpath(p, store.path) for p in glob.glob(f"{store.path}/runs/*/*/*/*.json")]


def test_a_cancelled_run_is_marked_cancelled_and_not_recorded(env, monkeypatch):
    monkeypatch.setenv("TEST_RESULT", "cancelled")
    p, results = nightly("PASS")
    p = p.model_copy(update={"event": "repository_dispatch", "root": "o/r#1", "root_repo": "o/r", "root_head_sha": "h"})
    code, data = env(p, results)
    assert code == 0 and env.checks[0][1]["conclusion"] == "cancelled" and _runs(data) == []
