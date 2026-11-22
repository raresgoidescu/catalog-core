"""Verdicts and what the check says about them."""

import pytest

from ukci import render, verdict
from ukci.models import Attempt, Cell, LockEntry, Plan, Result


def attempt(variant, kind="first", build="PASS", run="PASS", sig="", fetch=False):
    failing = build != "PASS" or run not in (None, "PASS")
    errors = [f"build:{e}" for e in sig.split("+")] if failing and sig else []
    return Attempt(
        variant=variant,
        attempt=kind,
        build=build,
        run=run,
        reason=f"error {sig}" if failing else "",
        errors=errors,
        fetch_error=fetch,
    )


def plan(variants, comparable=True, **kw):
    lock = LockEntry(repo="o/lib-musl", sha="pr", source="o/lib-musl#1 merged into staging", pr="o/lib-musl#1")
    base = LockEntry(repo="o/lib-musl", sha="base" if comparable else "pr", source="staging")
    return Plan(
        root="o/lib-musl#1",
        channel="staging",
        lock={"libs/musl": lock},
        base_lock={"libs/musl": base},
        cells=[Cell(app="nginx", compiler="gcc", why="links libs/musl", repos=["libs/musl"], variants=variants)],
        **kw,
    )


def judge(attempts, comparable=True, **result):
    p = plan(sorted({a.variant for a in attempts}), comparable)
    return {
        v.variant: v.verdict
        for v in verdict.judge(p, [Result(app="nginx", compiler="gcc", plan_hash="h", attempts=attempts, **result)])
    }


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        ([attempt("v")], "PASS"),
        ([attempt("v", run="FAIL", sig="x"), attempt("v", "retry")], "FLAKY"),
        (
            [attempt("v", build="FAIL", run=None, sig="e1"), attempt("v", "base", build="FAIL", run=None, sig="e1")],
            "BROKEN_ON_BASE",
        ),
        (
            [attempt("v", build="FAIL", run=None, sig="new"), attempt("v", "base", build="FAIL", run=None, sig="old")],
            "REGRESSION",
        ),  # fails differently than on base
        (
            [attempt("v", build="FAIL", run=None, sig="a+b"), attempt("v", "base", build="FAIL", run=None, sig="b+a")],
            "BROKEN_ON_BASE",
        ),  # parallel build: other error first
        (
            [attempt("v", build="FAIL", run=None, sig="a+new"), attempt("v", "base", build="FAIL", run=None, sig="a")],
            "BROKEN_ON_BASE",
        ),  # an error in common: still broken
        (
            [
                attempt("v", run="FAIL", sig="x"),
                attempt("v", "retry", run="FAIL", sig="x"),
                attempt("v", "base", build="FAIL", run=None, sig="x"),
            ],
            "REGRESSION",
        ),  # same text, other phase
        (
            [attempt("v", run="FAIL", sig="x"), attempt("v", "retry", run="FAIL", sig="x"), attempt("v", "base")],
            "REGRESSION",
        ),  # passes on base
        ([attempt("v", build="FAIL", run=None, sig="net", fetch=True)], "INFRA"),  # download failed
        ([attempt("v", build="FAIL", run=None, sig="y")], "INFRA"),  # comparison run missing
    ],
)
def test_verdict(attempts, expected):
    assert judge(attempts)["v"] == expected


def test_nothing_to_compare_to_is_a_failure():
    assert judge([attempt("v", build="FAIL", run=None, sig="z")], comparable=False)["v"] == "FAIL"


def test_variant_new_in_the_pr_is_a_failure():
    assert judge([attempt("v", build="FAIL", run=None, sig="z")], new_variants=["v"])["v"] == "FAIL"


def test_missing_job_is_a_ci_error():
    assert {v.verdict for v in verdict.judge(plan(["a", "b"]), [])} == {"INFRA"}


def test_title_and_check_text():
    attempts = [attempt(v, build="FAIL", run=None, sig="boom") for v in "ab"]
    attempts += [attempt(v, "base") for v in "ab"] + [attempt("c")]
    p = plan(["a", "b", "c"])
    vs = verdict.judge(p, [Result(app="nginx", compiler="gcc", plan_hash="h", attempts=attempts)])
    assert verdict.conclusion(p, vs) == "failure"
    assert render.title(p, vs) == "❌ 2 regressions · 1/3 passed"
    text = render.check(p, vs)
    assert "**❌ Regression** in 2 configurations: nginx × gcc × a, b" in text  # one block, not one row each
    assert "```\nerror boom\n```" in text  # the actual error
    assert "passes on staging without the PRs" in text
    assert "✅ Passed: 1 configuration" in text


@pytest.mark.parametrize(
    ("configs", "expected"),
    [
        ([("a", "gcc", "q"), ("a", "clang", "q"), ("b", "gcc", "q"), ("b", "clang", "q")], "a, b × clang, gcc × q"),
        ([("a", "gcc", "q"), ("b", "clang", "f")], "a: gcc q; b: clang f"),
    ],
)
def test_describe(configs, expected):
    assert render.describe(configs) == expected


def test_all_passed_and_no_apps_titles():
    p = plan(["a"])
    assert (
        render.title(p, verdict.judge(p, [Result(app="nginx", compiler="gcc", plan_hash="h", attempts=[attempt("a")])]))
        == "✅ All 1 configuration passed"
    )
    assert render.title(Plan(), []) == "✅ No app uses the changed repositories"
    assert render.title(Plan(errors=["x conflicts", "y"]), []) == "❌ Not tested: x conflicts (+1 more)"
