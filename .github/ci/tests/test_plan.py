"""The planner, against a fake GitHub and the real app directories."""

import json
import os

import pytest

from ukci import plan as plan_mod
from ukci.models import Reused

from .test_resolve import FakeGitHub, pr

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
SETTINGS = plan_mod.Settings(
    trusted_associations=frozenset({"OWNER", "MEMBER", "COLLABORATOR"}), max_prs=8, stable_schedule="0 3 * * 0"
)


class FakeAPI(FakeGitHub):
    def __init__(self, *prs, comments=None, files=None, **kw):
        super().__init__(*prs, **kw)
        self.comments, self.files = comments or {}, files or {}
        self.environments = {"ci-needs-approval": {"protection_rules": [{"type": "required_reviewers"}]}}

    def branch_sha(self, repo, branch):
        return f"{repo}@{branch}"

    def pr_files(self, repo, number):
        return []

    def comment(self, repo, comment_id):
        return self.comments.get(comment_id)

    def file(self, repo, path, ref):
        return self.files.get(path)

    def environment(self, repo, name):
        return self.environments.get(name)


@pytest.fixture(autouse=True)
def in_catalog(monkeypatch):
    monkeypatch.chdir(ROOT)


def planner(gh):
    return plan_mod.Planner(gh, SETTINGS, repository="unikraft/catalog-core", sha="cc")


MUSL = pr("unikraft/lib-musl#12", assoc="CONTRIBUTOR")


def comment(body="recheck", login="someone", assoc="NONE", issue=12):
    return {
        "body": body,
        "user": {"login": login},
        "author_association": assoc,
        "issue_url": f"https://api.github.com/repos/unikraft/lib-musl/issues/{issue}",
    }


def test_a_musl_pr_tests_the_apps_that_link_musl_with_every_compiler():
    p = planner(FakeAPI(MUSL)).plan(plan_mod.Plan(), plan_mod.Trigger("unikraft/lib-musl#12"))
    assert p.errors == [] and not p.trusted and p.cells
    assert all("libs/musl" in c.repos for c in p.cells)
    assert {c.compiler for c in p.cells} == {"gcc", "clang"}
    assert p.lock["libs/musl"].sha == "unikraft/lib-musl#12-head-merge"
    assert p.base_lock["libs/musl"].sha == "staging-sha"
    assert p.lock["unikraft"].sha == p.base_lock["unikraft"].sha == "unikraft/unikraft@staging"


def test_the_plan_hash_only_depends_on_what_is_tested():
    a = planner(FakeAPI(MUSL)).plan(plan_mod.Plan(), plan_mod.Trigger("unikraft/lib-musl#12"))
    b = planner(FakeAPI(pr("unikraft/lib-musl#12", assoc="CONTRIBUTOR", body="new description"))).plan(
        plan_mod.Plan(), plan_mod.Trigger("unikraft/lib-musl#12")
    )
    c = planner(FakeAPI(pr("unikraft/lib-musl#12", head="new-push"))).plan(
        plan_mod.Plan(), plan_mod.Trigger("unikraft/lib-musl#12")
    )
    assert a.plan_hash == b.plan_hash != c.plan_hash


@pytest.mark.parametrize(
    ("c", "skipped"),
    [
        (comment(login="dev"), False),  # the PR's author
        (comment(assoc="MEMBER"), False),
        (comment(), True),  # anyone else
        (comment(assoc="MEMBER", body="recheck please"), True),
        (comment(assoc="MEMBER", issue=13), True),  # a comment on another PR
        (None, True),  # no such comment
    ],
)
def test_who_may_recheck(c, skipped):
    p = planner(FakeAPI(MUSL, comments={7: c} if c else {})).plan(
        plan_mod.Plan(), plan_mod.Trigger("unikraft/lib-musl#12", comment_id=7)
    )
    assert bool(p.skip) is skipped and p.recheck
    assert (p.cells == []) is skipped


def test_identical_commits_reuse_the_earlier_result_unless_rechecked():
    first = planner(FakeAPI(MUSL)).plan(plan_mod.Plan(), plan_mod.Trigger("unikraft/lib-musl#12"))
    earlier = Reused(run_url="r", conclusion="success", title="✅", summary="s").model_dump_json().encode()
    gh = FakeAPI(MUSL, comments={7: comment(login="dev")}, files={f"plans/{first.plan_hash}.json": earlier})
    again = planner(gh).plan(plan_mod.Plan(), plan_mod.Trigger("unikraft/lib-musl#12"))
    assert again.reused and again.reused.run_url == "r"
    assert plan_mod.outputs(again, "unikraft/catalog-core", "cc")["run"] == "false"
    recheck = planner(gh).plan(plan_mod.Plan(), plan_mod.Trigger("unikraft/lib-musl#12", comment_id=7))
    assert recheck.reused is None and plan_mod.outputs(recheck, "unikraft/catalog-core", "cc")["run"] == "true"


def test_a_closed_root_is_skipped_silently():
    p = planner(FakeAPI(pr("unikraft/lib-musl#12", state="merged"))).plan(
        plan_mod.Plan(), plan_mod.Trigger("unikraft/lib-musl#12")
    )
    assert p.skip and not p.errors and plan_mod.outputs(p, "x/y", "s")["run"] == "false"


def test_scheduled_runs_test_every_app():
    p = planner(FakeAPI()).plan(plan_mod.Plan(), plan_mod.Trigger(None), channel="stable")
    assert p.cells and p.channel == "stable" and all(e.sha.endswith("@stable") for e in p.lock.values())


@pytest.mark.parametrize(
    ("kw", "error"),
    [
        ({"channel": "main"}, "unknown channel 'main'"),
        ({"compilers": ["icc"]}, "unknown compiler(s) icc"),
        ({"only_apps": ["nope"]}, "unknown app(s)"),
    ],
)
def test_bad_manual_inputs(kw, error):
    p = planner(FakeAPI()).plan(plan_mod.Plan(), plan_mod.Trigger(None), **kw)
    assert any(error in e for e in p.errors) and p.cells == []


@pytest.mark.parametrize(
    "event",
    [
        {"action": "musl_open_pr", "client_payload": {"repo": "unikraft/lib-musl", "number": 1}},
        {"action": "catalog_pr", "client_payload": {"repo": "a/b/c", "number": 1}},
        {"action": "catalog_pr", "client_payload": {"repo": "unikraft/lib-musl", "number": "1; rm"}},
        {"action": "catalog_pr", "client_payload": {"repo": "unikraft/lib-musl", "number": 1, "comment": "x"}},
    ],
)
def test_malformed_dispatches_are_refused(event):
    with pytest.raises(plan_mod.BadEvent):
        plan_mod.trigger("repository_dispatch", event, "unikraft/catalog-core")


def test_dispatch_payload():
    t = plan_mod.trigger(
        "repository_dispatch",
        {
            "action": "catalog_pr",
            "client_payload": {"repo": "Unikraft/Lib-Musl", "number": "12", "comment": "7", "cascade": "false"},
        },
        "unikraft/catalog-core",
    )
    assert (t.root, t.comment_id, t.fan_out) == ("unikraft/lib-musl#12", 7, False)


@pytest.mark.parametrize(
    ("fan_out", "state", "expected"), [(True, "open", "true"), (False, "open", "false"), (True, "closed", "false")]
)
def test_fan_out_only_when_a_pr_changed(fan_out, state, expected):
    p = planner(FakeAPI(pr("unikraft/lib-musl#12", state=state))).plan(
        plan_mod.Plan(), plan_mod.Trigger("unikraft/lib-musl#12", fan_out=fan_out)
    )
    assert plan_mod.outputs(p, "x/y", "s")["fan_out"] == expected


def test_schedule_channel():
    assert plan_mod.schedule_channel({"schedule": "0 3 * * 0"}, SETTINGS) == "stable"
    assert plan_mod.schedule_channel({"schedule": "0 1 * * *"}, SETTINGS) == "staging"


def test_a_crash_while_planning_still_produces_a_plan_and_outputs(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps({"action": "catalog_pr", "client_payload": {"repo": "unikraft/lib-musl", "number": 12}})
    )
    out, gh_out = tmp_path / "plan.json", tmp_path / "out"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "repository_dispatch")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_REPOSITORY", "unikraft/catalog-core")
    monkeypatch.setenv("GITHUB_SHA", "cc")
    monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))

    class Boom(FakeAPI):
        def branch_sha(self, repo, branch):
            raise RuntimeError("HTTP 502")

    monkeypatch.setattr(plan_mod, "GitHub", lambda: Boom(MUSL))
    assert plan_mod.main(type("A", (), {"out": str(out)})) == 1
    p = plan_mod.Plan.model_validate_json(out.read_text())
    assert p.root_head_sha == MUSL.head_sha  # enough to put a check on the PR
    assert "CI failed while planning: HTTP 502" in p.errors[0] and p.cells == []
    assert "run=false" in gh_out.read_text()


@pytest.mark.parametrize("env", [None, {"protection_rules": []}, {"protection_rules": [{"type": "wait_timer"}]}])
def test_untrusted_code_never_runs_without_a_reviewer(env):
    gh = FakeAPI(MUSL)
    gh.environments = {"ci-needs-approval": env} if env else {}
    p = planner(gh).plan(plan_mod.Plan(), plan_mod.Trigger("unikraft/lib-musl#12"))
    assert p.cells == [] and "doesn't require a reviewer" in p.errors[0]
    assert plan_mod.outputs(p, "x/y", "s")["run"] == "false"


def test_trusted_code_runs_without_an_environment():
    p = planner(FakeAPI(pr("unikraft/lib-musl#12"))).plan(plan_mod.Plan(), plan_mod.Trigger("unikraft/lib-musl#12"))
    assert p.cells and plan_mod.outputs(p, "x/y", "s")["environment"] == ""


def test_trusted_jobs_refuse_a_replaced_plan(tmp_path, monkeypatch):
    import hashlib

    from ukci import report

    path = tmp_path / "plan.json"
    path.write_text(plan_mod.Plan(root="o/r#1").model_dump_json())
    monkeypatch.setenv("PLAN_DIGEST", hashlib.sha256(path.read_bytes()).hexdigest())
    assert report.load_plan(str(path)).root == "o/r#1"
    path.write_text(plan_mod.Plan(root="o/r#2").model_dump_json())  # what a test job could do
    with pytest.raises(report.TamperedPlan):
        report.load_plan(str(path))
    assert report.cmd_finish(type("A", (), {"plan": str(path), "check_id": "", "results": "", "out": ""})) == 1
