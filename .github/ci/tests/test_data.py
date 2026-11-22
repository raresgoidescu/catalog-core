"""ci-data, blame, the cycle's checks, and nightly change detection."""

import subprocess

import pytest

from ukci import checks, notify
from ukci.data import OwnResult, Store
from ukci.models import CycleMember, Plan, PlannedPR, Report, Verdict


def v(app, variant, name, cc="gcc", reason="reason"):
    return Verdict(app=app, compiler=cc, variant=variant, verdict=name, reason=reason, detail="detail")


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return str(path)


def store(tmp_path, remote, name):
    return Store("o/catalog-core", None, str(tmp_path / name), url=remote).open()


def test_concurrent_writers_both_land(tmp_path, remote):
    a, b = store(tmp_path, remote, "a"), store(tmp_path, remote, "b")  # both start from an empty branch
    a.write("runs/1.json", {"x": 1})
    b.write("runs/2.json", {"x": 2})
    assert a.push("a") and b.push("b")  # b rebases onto a's push
    c = store(tmp_path, remote, "c")
    assert (c.read("runs/1.json"), c.read("runs/2.json")) == ({"x": 1}, {"x": 2})


def test_record_dependents_and_own_result(tmp_path, remote):
    s = store(tmp_path, remote, "s")
    plan = Plan(
        root="o/unikraft#1",
        root_head_sha="h1",
        run_url="u",
        plan_hash="p",
        event="repository_dispatch",
        prs=[
            PlannedPR(ref=r, url="", title="", head_sha="", base_ref="staging", via="", used=True)
            for r in ("o/unikraft#1", "o/lib-musl#12")
        ],
    )
    s.record(plan, Report(plan_hash="p", conclusion="failure", title="t", verdicts=[v("nginx", "q", "FAIL")]), [])
    assert s.dependents("o/lib-musl#12") == ["o/unikraft#1"]
    assert s.own_result("o/unikraft#1", "h1").conclusion == "failure"
    assert s.own_result("o/unikraft#1", "other-head") is None


class FakeStore:
    def __init__(self, own):
        self.own = own

    def own_result(self, ref, head):
        return self.own if (ref, head) == ("o/lib-musl#12", "m12") else None


@pytest.mark.parametrize(
    ("own", "expected"),
    [
        (
            OwnResult(run_url="r12", plan_hash="p", conclusion="failure", verdicts=[v("nginx", "q", "REGRESSION")]),
            "**Inherited**: it also fails in the own run of [o/lib-musl#12](r12)",
        ),
        (
            OwnResult(run_url="r12", plan_hash="p", conclusion="success", verdicts=[v("nginx", "q", "PASS")]),
            "comes from this PR",
        ),
        (None, "Not known yet whether it comes from o/lib-musl#12"),
    ],
)
def test_blame(own, expected):
    plan = Plan(
        root="o/unikraft#1",
        prs=[
            PlannedPR(ref="o/unikraft#1", url="", title="", head_sha="h1", base_ref="staging", via="root", used=True),
            PlannedPR(ref="o/lib-musl#12", url="", title="", head_sha="m12", base_ref="staging", via="", used=True),
        ],
    )
    vs = [v("nginx", "q", "REGRESSION")]
    checks.blame(plan, vs, FakeStore(own))
    assert expected in vs[0].detail
    assert vs[0].inherited_from == ("o/lib-musl#12" if "Inherited" in expected else None)


class FakeGH:
    token = "t"

    def __init__(self):
        self.calls = []

    def find_check(self, repo, sha, name):
        self.calls.append(("find", repo))
        return 7 if repo == "o/lib-musl" else None

    def put_check(self, repo, body, check_id=None):
        self.calls.append(("put", repo, check_id))
        return 1


def test_the_cycle_run_finishes_every_members_check():
    gh = FakeGH()
    plan = Plan(
        root="o/unikraft#1",
        root_repo="o/unikraft",
        root_head_sha="a",
        run_url="u",
        cycle=[
            CycleMember(ref="o/unikraft#1", repo="o/unikraft", head_sha="a"),
            CycleMember(ref="o/lib-musl#2", repo="o/lib-musl", head_sha="b"),
            CycleMember(ref="o/lib-lwip#3", repo="o/lib-lwip", head_sha="c"),
        ],
    )
    assert checks.post(gh, plan, status="completed", title="t", summary="s", conclusion="success", check_id=5) == 1
    assert gh.calls == [
        ("put", "o/unikraft", 5),  # the root: by id
        ("find", "o/lib-musl"),
        ("put", "o/lib-musl", 7),  # existing check: updated
        ("find", "o/lib-lwip"),
        ("put", "o/lib-lwip", None),
    ]  # none yet: created


def test_a_delegating_run_only_touches_its_own_check():
    gh = FakeGH()
    plan = Plan(
        root="o/lib-musl#2",
        root_repo="o/lib-musl",
        root_head_sha="b",
        delegate_to="o/unikraft#1",
        cycle=[
            CycleMember(ref="o/unikraft#1", repo="o/unikraft", head_sha="a"),
            CycleMember(ref="o/lib-musl#2", repo="o/lib-musl", head_sha="b"),
        ],
    )
    checks.post(gh, plan, status="in_progress", title="t", summary="s")
    assert gh.calls == [("put", "o/lib-musl", None)]


def test_nightly_reports_only_changes():
    prev = {"a gcc q": "PASS", "b gcc q": "FAIL", "c gcc q": "FAIL", "d gcc q": "PASS"}
    broke, fixed = notify.changes(
        prev,
        [
            v("a", "q", "FAIL", reason="boom"),
            v("b", "q", "PASS"),
            v("c", "q", "FAIL"),
            v("d", "q", "FLAKY"),
            v("e", "q", "FAIL"),
        ],
    )
    assert broke == ["a gcc q: boom"]  # e is new, d flaky counts as passing, c is unchanged
    assert fixed == ["b gcc q"]
