"""ci-data, and what Discord hears about scheduled runs."""

import subprocess

import pytest

from ukci import notify
from ukci.data import Store, plan_path
from ukci.models import Plan, PlannedPR, Report, Verdict


def v(app, variant, name, cc="gcc", reason="reason"):
    return Verdict(app=app, compiler=cc, variant=variant, verdict=name, reason=reason)


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return f"file://{path}"


def store(tmp_path, remote, name):
    return Store("o/catalog-core", None, str(tmp_path / name), url=remote).open()


def test_concurrent_writers_all_land(tmp_path, remote):
    seed = store(tmp_path, remote, "seed")
    seed.write("runs/0.json", {"x": 0})
    assert seed.push("seed")
    a, b, c = (store(tmp_path, remote, n) for n in "abc")  # all start from the same tip
    for i, s in enumerate((a, b, c), 1):
        s.write(f"runs/{i}.json", {"x": i})
    assert a.push("a") and b.push("b") and c.push("c")  # b and c lose the race, then land
    d = store(tmp_path, remote, "d")
    assert [d.read(f"runs/{i}.json") for i in range(4)] == [{"x": i} for i in range(4)]


def test_unreachable_remote_is_an_error_not_an_empty_branch(tmp_path):
    with pytest.raises(RuntimeError, match="can't fetch"):
        Store("o/c", None, str(tmp_path / "x"), url=f"file://{tmp_path}/nope.git").open()


def plan(**kw):
    prs = [
        PlannedPR(ref=r, url="", title="", head_sha="", base_ref="staging", via="", used=used)
        for r, used in (("o/unikraft#1", True), ("o/lib-musl#12", True), ("o/lib-musl#9", False))
    ]
    return Plan(root="o/unikraft#1", root_head_sha="h1", run_url="u", plan_hash="p", prs=prs, **kw)


def test_record_dependents_and_reusable_result(tmp_path, remote):
    s = store(tmp_path, remote, "s")
    s.record(plan(), Report(plan_hash="p", conclusion="failure", title="t", verdicts=[v("a", "q", "FAIL")]), [], "text")
    assert s.dependents("o/lib-musl#12") == ["o/unikraft#1"]
    assert s.dependents("o/lib-musl#9") == []  # not used, so not a dependency
    assert s.read(plan_path("p")) == {"run_url": "u", "conclusion": "failure", "title": "t", "summary": "text"}


def test_a_result_with_ci_errors_is_not_reusable(tmp_path, remote):
    s = store(tmp_path, remote, "s")
    s.record(plan(), Report(plan_hash="p", conclusion="failure", title="t", verdicts=[v("a", "q", "INFRA")]), [], "")
    assert s.read(plan_path("p")) is None


# --- Discord -----------------------------------------------------------------------


PREV = {"error": "", "cells": {"a gcc q": "PASS", "b gcc q": "FAIL", "c gcc q": "FAIL", "d gcc q": "PASS"}}


def test_only_changes_are_reported():
    broke, fixed = notify.changes(
        PREV,
        [
            v("a", "q", "FAIL", reason="boom"),
            v("b", "q", "PASS"),
            v("c", "q", "FAIL"),
            v("d", "q", "FLAKY"),
            v("e", "q", "FAIL"),
        ],
    )
    assert broke == ["a gcc q: boom", "e gcc q: reason"]  # e is new, d flaky is no change, c is unchanged
    assert fixed == ["b gcc q"]


def test_ci_errors_keep_the_previous_state():
    verdicts = [v("a", "q", "INFRA"), v("b", "q", "INFRA")]
    assert notify.changes(PREV, verdicts) == ([], [])
    state = notify.next_state(PREV, verdicts, "", "u2")
    assert state["cells"] == PREV["cells"]  # so the next real result is compared to the last real one


@pytest.mark.parametrize(
    ("previous", "verdicts", "error", "expected"),
    [
        (PREV, [v("a", "q", "PASS")], "", None),  # nothing changed
        (PREV, [v("a", "q", "FAIL", reason="boom")], "", "**Newly failing**\n- a gcc q: boom"),
        (None, [v("a", "q", "PASS")], "", "first recorded run"),
        (PREV, [], "the plan job failed", "couldn't run: the plan job failed"),
        ({**PREV, "error": "x"}, [], "the plan job failed", None),  # already said so
        ({**PREV, "error": "x"}, [v("a", "q", "PASS")], "", "Running again after an error."),
    ],
)
def test_message(previous, verdicts, error, expected):
    text = notify.message(Plan(channel="staging", run_url="u"), "title", previous, verdicts, error)
    assert text is None if expected is None else expected in text


def test_an_error_keeps_the_last_known_cells():
    assert notify.next_state(PREV, [], "boom", "u")["cells"] == PREV["cells"]


def test_only_the_configured_mention_can_ping():
    assert notify.allowed_mentions("<@&123> <@456>") == {"parse": [], "roles": ["123"], "users": ["456"]}
    assert notify.allowed_mentions("") == {"parse": [], "roles": [], "users": []}


def test_the_dashboard_reads_what_ci_records(tmp_path, remote):
    from ukci import dashboard

    s = store(tmp_path, remote, "s")
    s.record(
        plan(event="schedule"),
        Report(plan_hash="p", conclusion="failure", title="t", verdicts=[v("a", "q", "FAIL")]),
        [],
        "",
    )
    runs, bad = dashboard.load_runs(str(tmp_path / "s"))
    assert [(r.root, r.tested_with, r.verdicts[0].verdict) for r in runs] == [("o/unikraft#1", "o/lib-musl#12", "FAIL")]
    assert bad == 0
    out = tmp_path / "site"
    assert dashboard.main(type("A", (), {"data": str(tmp_path / "s"), "out": str(out)})) == 0
    assert "o/unikraft#1" in (out / "index.html").read_text()


def test_scheduled_results_become_the_comparison_for_identical_commits(tmp_path, remote, monkeypatch):
    import io
    import urllib.request

    from ukci import data
    from ukci.models import Attempt, CatalogRef, Cell, LockEntry, Result

    lock = {"unikraft": LockEntry(repo="u/unikraft", sha="s1", source="staging")}
    cell = Cell(app="nginx", compiler="gcc", why="", repos=["unikraft"], variants=["q", "f"])
    tools = {"gcc": "13", "image": "ubuntu24 1"}
    attempts = [
        Attempt(variant="q", attempt="first", build="PASS", run="FAIL", reason="Wrong message printed."),
        Attempt(variant="q", attempt="retry", build="PASS", run="PASS"),  # flaky: passes
        Attempt(variant="f", attempt="first", build="FAIL", run="BLOCKED", fetch_error=True),  # unknown
    ]
    nightly = Plan(
        event="schedule",
        run_url="https://nightly",
        catalog=CatalogRef(repo="o/c", sha="c1", source=""),
        cells=[cell],
        lock=lock,
    )
    s = store(tmp_path, remote, "s")
    s.record_base(nightly, [Result(app="nginx", compiler="gcc", plan_hash="h", tools=tools, attempts=attempts)])
    path = data.base_path("nginx", "gcc", "c1", {"unikraft": "s1"}, tools)

    def urlopen(url, timeout):
        assert url == f"https://raw.githubusercontent.com/o/catalog-core/ci-data/{path}"
        return io.BytesIO(s.pending[path].encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    known = data.fetch_base("o/catalog-core", path)
    assert (
        set(known) == {"q"}
        and known["q"].ok
        and known["q"].attempt == "base"
        and known["q"].source == "https://nightly"
    )

    # A PR run never writes them, and anything else in the key means another file.
    pr_run = nightly.model_copy(update={"event": "repository_dispatch"})
    s2 = store(tmp_path, remote, "s2")
    s2.record_base(pr_run, [Result(app="nginx", compiler="gcc", plan_hash="h", tools=tools, attempts=attempts)])
    assert s2.pending == {}
    assert data.base_path("nginx", "gcc", "c1", {"unikraft": "s2"}, tools) != path
    assert data.base_path("nginx", "gcc", "c1", {"unikraft": "s1"}, {**tools, "image": "ubuntu24 2"}) != path


def test_unreadable_scheduled_results_are_ignored(monkeypatch):
    import io
    import urllib.request

    from ukci import data

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b'{"attempts": [{"variant": 1}]}'))
    assert data.fetch_base("o/c", "base/x.json") == {}


def test_the_first_nightly_announces_nothing_as_a_change():
    assert notify.changes(None, [v("a", "q", "FAIL")]) == ([], [])


def test_flaky_after_a_failure_is_not_a_fix():
    previous = {"cells": {"a gcc q": "FAIL"}}
    flaky = [v("a", "q", "FLAKY")]
    assert notify.changes(previous, flaky) == ([], [])
    assert notify.next_state(previous, flaky, "", "u")["cells"] == {"a gcc q": "FAIL"}
