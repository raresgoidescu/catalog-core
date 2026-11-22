"""Depends-On resolution: the scenarios from PLAN.md, against a fake GitHub."""

import pytest

from ukci.models import PullRequest
from ukci.resolve import parse_depends_on, resolve


def pr(ref, *, body="", commits=(), state="open", base="staging", assoc="MEMBER", mergeable=True, head=None):
    repo, n = ref.split("#")
    head = head or f"{ref}-head"
    return PullRequest(
        repo=repo,
        number=int(n),
        state=state,
        base_ref=base,
        head_sha=head,
        mergeable=mergeable,
        merge_sha=f"{head}-merge" if mergeable else None,
        merge_base_sha=f"{base}-sha" if mergeable else None,
        author="dev",
        association=assoc,
        title=ref,
        body=body,
        commit_messages=list(commits),
    )


class FakeGitHub:
    def __init__(self, *prs, stacks=()):
        self.prs = {p.ref: p for p in prs}
        self.stacks = set(stacks)  # (ancestor head, descendant head)
        self.fetched = 0

    def pull_request(self, repo, number):
        self.fetched += 1
        return self.prs.get(f"{repo}#{number}".lower())

    def contains(self, repo, ancestor, descendant):
        return ancestor == descendant or (ancestor, descendant) in self.stacks


def run(gh, root="unikraft/unikraft#1", **kw):
    opts = {
        "allowed_owners": {"unikraft"},
        "trusted_associations": {"OWNER", "MEMBER", "COLLABORATOR"},
        "channels": {"staging", "stable"},
        **kw,
    }
    return resolve(gh, root, **opts)


def used(res):
    return {path: [f.pr.ref for f in prs] for path, prs in res.used.items()}


# --- syntax ---------------------------------------------------------------------


def test_every_form_of_depends_on():
    text = (
        "Prose that needs #99 fixed and requires lib-musl#98.\n"
        "Depends-On: https://github.com/unikraft/unikraft/pull/1745\n"
        "  depends-on: unikraft/lib-musl#12\n"
        "DEPENDS-ON: lib-lwip#40, #7\n"
    )
    refs, errors = parse_depends_on(text, "unikraft/app-elfloader")
    assert refs == [
        "unikraft/unikraft#1745",
        "unikraft/lib-musl#12",
        "unikraft/lib-lwip#40",
        "unikraft/app-elfloader#7",
    ]
    assert errors == []


def test_prose_is_never_a_dependency():
    assert parse_depends_on("This needs #12 and requires lib-musl#3. Depends on #4.", "unikraft/unikraft") == ([], [])


def test_unreadable_target_is_reported():
    refs, errors = parse_depends_on("Depends-On: musl 12", "unikraft/unikraft")
    assert refs == [] and len(errors) == 2


# --- shapes of the graph ----------------------------------------------------------


def test_single_pr():
    res = run(FakeGitHub(pr("unikraft/unikraft#1")))
    assert (res.errors, used(res), res.trusted) == ([], {"unikraft": ["unikraft/unikraft#1"]}, True)


def test_chain_across_repos_via_a_commit_trailer():
    res = run(
        FakeGitHub(
            pr("unikraft/unikraft#1", body="Depends-On: lib-musl#12"),
            pr("unikraft/lib-musl#12", commits=["musl: fix\n\nDepends-On: lib-lwip#40\n"]),
            pr("unikraft/lib-lwip#40"),
        )
    )
    assert res.errors == []
    assert set(used(res)) == {"unikraft", "libs/musl", "libs/lwip"}
    assert "commit 1" in next(f.via for f in res.prs if f.pr.ref == "unikraft/lib-lwip#40")


def test_cycle_resolves_the_same_from_both_sides():
    gh = FakeGitHub(
        pr("unikraft/unikraft#1", body="Depends-On: lib-musl#12"),
        pr("unikraft/lib-musl#12", body="Depends-On: unikraft#1"),
    )
    a, b = run(gh, "unikraft/unikraft#1"), run(gh, "unikraft/lib-musl#12")
    assert a.errors == b.errors == [] and used(a) == used(b)
    assert a.cycle() == b.cycle() == ["unikraft/lib-musl#12", "unikraft/unikraft#1"]


def test_cycle_only_contains_prs_that_depend_both_ways():
    gh = FakeGitHub(
        pr("unikraft/unikraft#1", body="Depends-On: lib-musl#12, lib-lwip#40"),
        pr("unikraft/lib-musl#12", body="Depends-On: unikraft#1"),
        pr("unikraft/lib-lwip#40"),
    )
    assert run(gh).cycle() == ["unikraft/lib-musl#12", "unikraft/unikraft#1"]
    assert run(gh, "unikraft/lib-lwip#40").cycle() == ["unikraft/lib-lwip#40"]


def test_diamond_fetches_each_pr_once():
    gh = FakeGitHub(
        pr("unikraft/unikraft#1", body="Depends-On: lib-musl#12, lib-lwip#40"),
        pr("unikraft/lib-musl#12", body="Depends-On: lib-lwip#40"),
        pr("unikraft/lib-lwip#40"),
    )
    res = run(gh)
    assert res.errors == [] and len(res.used) == 3 and gh.fetched == 3


# --- several PRs in one repo ----------------------------------------------------------


def test_stack_uses_the_top_pr():
    gh = FakeGitHub(
        pr("unikraft/unikraft#1", body="Depends-On: lib-musl#12, lib-musl#15"),
        pr("unikraft/lib-musl#12", head="m12"),
        pr("unikraft/lib-musl#15", head="m15"),
        stacks={("m12", "m15")},
    )
    res = run(gh)
    assert used(res)["libs/musl"] == ["unikraft/lib-musl#15"]
    assert res.dropped == {"unikraft/lib-musl#12": "included in unikraft/lib-musl#15, which is stacked on top of it"}


def test_prs_that_are_not_stacked_are_all_used():
    res = run(
        FakeGitHub(
            pr("unikraft/unikraft#1", body="Depends-On: lib-musl#15, lib-musl#12"),
            pr("unikraft/lib-musl#12"),
            pr("unikraft/lib-musl#15"),
        )
    )
    assert res.errors == [] and used(res)["libs/musl"] == ["unikraft/lib-musl#12", "unikraft/lib-musl#15"]


def test_stack_plus_an_independent_pr():
    gh = FakeGitHub(
        pr("unikraft/unikraft#1", body="Depends-On: lib-musl#12, lib-musl#15, lib-musl#20"),
        pr("unikraft/lib-musl#12", head="m12"),
        pr("unikraft/lib-musl#15", head="m15"),
        pr("unikraft/lib-musl#20", head="m20"),
        stacks={("m12", "m15")},
    )
    assert used(run(gh))["libs/musl"] == ["unikraft/lib-musl#15", "unikraft/lib-musl#20"]


# --- problems, all reported before anything runs -----------------------------------------


@pytest.mark.parametrize(
    ("prs", "expected"),
    [
        (
            [pr("unikraft/unikraft#1", body="Depends-On: lib-musl#9"), pr("unikraft/lib-musl#9", state="closed")],
            "closed without being merged",
        ),
        (
            [pr("unikraft/unikraft#1", body="Depends-On: lib-musl#12"), pr("unikraft/lib-musl#12", base="stable")],
            "must target the same branch",
        ),
        ([pr("unikraft/unikraft#1", body="Depends-On: evil/lib-musl#1")], "only PRs in unikraft"),
        (
            [pr("unikraft/unikraft#1", body="Depends-On: unikraft/kraftkit#5")],
            "not a repository catalog-core builds with",
        ),
        ([pr("unikraft/unikraft#1", body="Depends-On: lib-musl#404")], "no such pull request"),
        ([pr("unikraft/unikraft#1", mergeable=False)], "can't be merged into `staging` (merge conflict)"),
        ([pr("unikraft/unikraft#1", mergeable=None)], "re-run in a minute"),
        ([pr("unikraft/unikraft#1", base="feature-x")], "only PRs to stable, staging are tested"),
        (
            [
                pr("unikraft/unikraft#1", body="Depends-On: lib-musl#12, lib-musl#15"),
                pr("unikraft/lib-musl#12"),
                pr("unikraft/lib-musl#15", mergeable=False),
            ],
            "lib-musl#15 can't be merged",
        ),
    ],
)
def test_problem(prs, expected):
    res = run(FakeGitHub(*prs))
    assert any(expected in e for e in res.errors), res.errors


def test_too_many_prs():
    deps = ", ".join(f"lib-musl#{i}" for i in range(2, 12))
    gh = FakeGitHub(
        pr("unikraft/unikraft#1", body=f"Depends-On: {deps}"), *(pr(f"unikraft/lib-musl#{i}") for i in range(2, 12))
    )
    assert any("more than 8 PRs" in e for e in run(gh, max_prs=8).errors)


def test_merged_dependency_is_dropped():
    res = run(
        FakeGitHub(pr("unikraft/unikraft#1", body="Depends-On: lib-musl#9"), pr("unikraft/lib-musl#9", state="merged"))
    )
    assert res.errors == [] and "libs/musl" not in res.used and "unikraft/lib-musl#9" in res.dropped


def test_untrusted_dependency_makes_the_whole_set_untrusted():
    res = run(
        FakeGitHub(
            pr("unikraft/unikraft#1", body="Depends-On: lib-musl#12"),
            pr("unikraft/lib-musl#12", assoc="FIRST_TIME_CONTRIBUTOR"),
        )
    )
    assert res.errors == [] and not res.trusted and "first_time_contributor" in res.untrusted[0]


def test_catalog_core_pr_can_be_a_dependency():
    res = run(
        FakeGitHub(
            pr("unikraft/lib-musl#12", body="Depends-On: catalog-core#60"), pr("unikraft/catalog-core#60", base="main")
        ),
        "unikraft/lib-musl#12",
    )
    assert res.errors == [] and used(res) == {
        "libs/musl": ["unikraft/lib-musl#12"],
        "catalog-core": ["unikraft/catalog-core#60"],
    }


@pytest.mark.parametrize(("state", "expected"), [("open", "open"), ("merged", "merged"), ("closed", "closed")])
def test_root_state(state, expected):
    gh = FakeGitHub(pr("unikraft/unikraft#1", state=state, body="Depends-On: lib-musl#12"), pr("unikraft/lib-musl#12"))
    res = run(gh)
    assert res.root_state == expected
    assert gh.fetched == (2 if state == "open" else 1)  # a merged/closed root isn't crawled


def test_missing_root():
    res = run(FakeGitHub())
    assert res.root_state is None and "no such pull request" in res.errors[0]
