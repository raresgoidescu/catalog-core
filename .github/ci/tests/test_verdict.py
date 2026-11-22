"""Verdicts and what the check says about them."""

import pytest

from ukci import render, verdict
from ukci.models import Attempt, Cell, LockEntry, Plan, Result


def attempt(variant="v", kind="first", build="PASS", run="PASS", reason="", fetch=False, env_error=False):
    return Attempt(
        variant=variant, attempt=kind, build=build, run=run, reason=reason, fetch_error=fetch, env_error=env_error
    )


BUILD_FAIL = {"build": "FAIL", "run": "BLOCKED", "reason": "x.c: boom"}
RUN_FAIL = {"run": "FAIL", "reason": "Wrong message printed."}


def plan(variants, comparable=True):
    lock = LockEntry(repo="o/lib-musl", sha="pr", source="o/lib-musl#1 merged into staging", pr="o/lib-musl#1")
    base = LockEntry(repo="o/lib-musl", sha="base" if comparable else "pr", source="staging")
    return Plan(
        root="o/lib-musl#1",
        channel="staging",
        run_url="https://run",
        lock={"libs/musl": lock},
        base_lock={"libs/musl": base},
        cells=[Cell(app="nginx", compiler="gcc", why="links libs/musl", repos=["libs/musl"], variants=variants)],
    )


def judge(attempts, comparable=True, **result):
    p = plan(sorted({a.variant for a in attempts}) or ["v"], comparable)
    r = Result(app="nginx", compiler="gcc", plan_hash="h", attempts=attempts, **result)
    return {v.variant: (v.verdict, v.reason) for v in verdict.judge(p, [r])}


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        ([attempt()], "PASS"),
        ([attempt(**RUN_FAIL), attempt(kind="retry")], "FLAKY"),
        ([attempt(**BUILD_FAIL), attempt(kind="base", **BUILD_FAIL)], "BROKEN_ON_BASE"),
        # Different first error, same phase: still already broken (parallel builds reorder errors).
        ([attempt(**BUILD_FAIL), attempt(kind="base", **{**BUILD_FAIL, "reason": "other.c: bang"})], "BROKEN_ON_BASE"),
        ([attempt(**BUILD_FAIL), attempt(kind="base")], "REGRESSION"),
        # Base builds (and fails to run), the PR doesn't build: worse.
        ([attempt(**BUILD_FAIL), attempt(kind="base", **RUN_FAIL)], "REGRESSION"),
        # Base doesn't even build, the PR builds and fails to run: not worse.
        (
            [attempt(**RUN_FAIL), attempt(kind="retry", **RUN_FAIL), attempt(kind="base", **BUILD_FAIL)],
            "BROKEN_ON_BASE",
        ),
        ([attempt(**RUN_FAIL), attempt(kind="retry", **RUN_FAIL), attempt(kind="base")], "REGRESSION"),
        # A download failure that the retry got past is flaky, not a CI error.
        ([attempt(**BUILD_FAIL, fetch=True), attempt(kind="retry")], "FLAKY"),
        ([attempt(**BUILD_FAIL, fetch=True), attempt(kind="retry", **BUILD_FAIL, fetch=True)], "INFRA"),
        # The download worked on retry, then the build failed for real.
        (
            [attempt(**BUILD_FAIL, fetch=True), attempt(kind="retry", **BUILD_FAIL), attempt(kind="base")],
            "REGRESSION",
        ),
        ([attempt(**BUILD_FAIL), attempt(kind="base", **BUILD_FAIL, fetch=True)], "INFRA"),
        ([attempt(**BUILD_FAIL)], "INFRA"),  # comparison run missing
        ([attempt(**{**BUILD_FAIL, "env_error": True})], "INFRA"),  # the runner lacks a tool
        ([attempt(**BUILD_FAIL), attempt(kind="base", **{**BUILD_FAIL, "env_error": True})], "INFRA"),
    ],
)
def test_verdict(attempts, expected):
    assert judge(attempts)["v"][0] == expected


def test_reason_is_the_last_attempts_error():
    first = attempt(**{**BUILD_FAIL, "reason": "wget: unable to resolve host address"}, fetch=True)
    retry = attempt(kind="retry", **{**BUILD_FAIL, "reason": "real.c: error"})
    assert judge([first, retry, attempt(kind="base")])["v"] == ("REGRESSION", "real.c: error")


def test_nothing_to_compare_to_is_a_failure():
    assert judge([attempt(**BUILD_FAIL)], comparable=False)["v"][0] == "FAIL"


def test_variant_new_in_the_pr_is_a_failure():
    assert judge([attempt(**BUILD_FAIL)], new_variants=["v"])["v"][0] == "FAIL"


def test_missing_job_is_a_ci_error():
    assert {(v.verdict, v.reason) for v in verdict.judge(plan(["a", "b"]), [])} == {
        ("INFRA", "the job produced no results")
    }


def test_checkout_failure_is_a_ci_error_with_the_reason():
    assert judge([], infra_error="checkout failed: x")["v"] == ("INFRA", "checkout failed: x")


def test_title_and_check_text():
    attempts = [attempt(v, **BUILD_FAIL) for v in "ab"] + [attempt(v, "base") for v in "ab"] + [attempt("c")]
    p = plan(["a", "b", "c"])
    vs = verdict.judge(p, [Result(app="nginx", compiler="gcc", plan_hash="h", attempts=attempts)])
    assert verdict.conclusion(p, vs) == "failure"
    assert render.title(p, vs) == "2 regressions, 1 of 3 passed"
    text = render.check(p, vs, logs={("nginx", "gcc"): "https://job"})
    assert "| app | compiler | a | b | c |" in text
    assert "| [nginx](https://job) | gcc | **regression** | **regression** | passed |" in text
    assert "| **regression** | `x.c: boom` | nginx gcc: a, b |" in text
    assert "[Workflow run](https://run)" in text


def test_check_text_cannot_break_the_table():
    p = plan(["v"])
    vs = verdict.judge(
        p,
        [
            Result(
                app="nginx",
                compiler="gcc",
                plan_hash="h",
                attempts=[attempt(**{**BUILD_FAIL, "reason": "a | b `c`\nd"}), attempt(kind="base")],
            )
        ],
    )
    assert "`a \\| b 'c' d`" in render.check(p, vs)


def test_titles_without_results():
    assert render.title(Plan(), []) == "No app uses the changed repositories"
    assert render.title(Plan(errors=["x conflicts", "y"]), []) == "Not tested: x conflicts (+1 more)"


def test_one_error_everywhere_says_all():
    p = plan(["a", "b"])
    attempts = [attempt(v, **BUILD_FAIL) for v in "ab"] + [attempt(v, "base") for v in "ab"]
    vs = verdict.judge(p, [Result(app="nginx", compiler="gcc", plan_hash="h", attempts=attempts)])
    assert "| **regression** | `x.c: boom` | all 2 |" in render.check(p, vs)


def test_a_comparison_that_passes_on_retry_makes_a_regression():
    pr_runs = [attempt(**RUN_FAIL), attempt(kind="retry", **RUN_FAIL)]
    base = [attempt(kind="base", **RUN_FAIL), attempt(kind="base_retry")]
    assert judge(pr_runs + base)["v"][0] == "REGRESSION"


def test_a_comparison_that_fails_twice_is_already_broken():
    pr_runs = [attempt(**RUN_FAIL), attempt(kind="retry", **RUN_FAIL)]
    base = [attempt(kind="base", **RUN_FAIL), attempt(kind="base_retry", **RUN_FAIL)]
    assert judge(pr_runs + base)["v"][0] == "BROKEN_ON_BASE"


def test_titles_cannot_add_links_or_images():
    text = render.cell("![x](https://e/x.png) [y](https://e) <img src=x>")
    assert "<img" not in text and text.count("\\[") == 2
    assert render.code("error: [-Werror=foo]") == "`error: [-Werror=foo]`"
