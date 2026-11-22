import unittest

from ukci.gh import PR
from ukci.resolve import Ref, parse_depends_on, resolve


def pr(repo, number, *, body="", commits=(), state="open", merged=False, base="staging",
       assoc="MEMBER", mergeable=True, head=None, author="dev"):
    head = head or f"{repo.split('/')[1]}-{number}-head"
    return PR(repo=repo, number=number, state=state, merged=merged, base_ref=base,
              base_sha=f"{base}-sha", head_sha=head,
              mergeable=mergeable, merge_sha=(f"{head}-merge" if mergeable else None), author=author,
              author_association=assoc, body=body, commit_messages=[(f"c{i:012d}", m) for i, m in enumerate(commits)])


class FakeGitHub:
    def __init__(self, *prs, stacks=()):
        self.prs = {(p.repo.lower(), p.number): p for p in prs}
        self.stacks = set(stacks)      # (ancestor_head, descendant_head)
        self.fetches = 0

    def pr(self, repo, number):
        self.fetches += 1
        return self.prs.get((repo.lower(), number))

    def contains(self, repo, ancestor, descendant):
        return ancestor == descendant or (ancestor, descendant) in self.stacks


def run(gh, root="unikraft/unikraft#1", **kw):
    repo, n = root.split("#")
    opts = dict(allowed_owners={"unikraft"}, trusted_associations={"OWNER", "MEMBER", "COLLABORATOR"},
                channels={"staging", "stable"})
    opts.update(kw)
    return resolve(gh, Ref(repo, int(n)), **opts)


class ParseTest(unittest.TestCase):
    def test_forms(self):
        text = ("Some prose that needs #99 fixed first and requires lib-musl#98.\n"
                "Depends-On: unikraft/lib-musl#12\n"
                "  depends-on: lib-lwip#40, #7\n"
                "DEPENDS-ON: https://github.com/unikraft/unikraft/pull/1745\n")
        refs, errors = parse_depends_on(text, "unikraft/app-elfloader")
        self.assertEqual([str(r) for r in refs], [
            "unikraft/lib-musl#12", "unikraft/lib-lwip#40", "unikraft/app-elfloader#7", "unikraft/unikraft#1745"])
        self.assertEqual(errors, [])

    def test_prose_is_never_a_dependency(self):
        refs, _ = parse_depends_on("This needs #12 and requires lib-musl#3. Depends on #4.", "unikraft/unikraft")
        self.assertEqual(refs, [])

    def test_bad_target_is_reported(self):
        refs, errors = parse_depends_on("Depends-On: musl 12", "unikraft/unikraft")
        self.assertEqual(refs, [])
        self.assertEqual(len(errors), 2)


class ResolveTest(unittest.TestCase):
    def paths(self, res):
        return {p: f.pr.ref for p, f in res.selected.items()}

    # 1
    def test_single_pr(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1)))
        self.assertEqual(res.errors, [])
        self.assertEqual(self.paths(res), {"unikraft": "unikraft/unikraft#1"})
        self.assertTrue(res.trusted)

    # 2
    def test_cross_repo(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: lib-musl#12"),
                             pr("unikraft/lib-musl", 12)))
        self.assertEqual(self.paths(res), {"unikraft": "unikraft/unikraft#1", "libs/musl": "unikraft/lib-musl#12"})
        self.assertIn("Depends-On in unikraft/unikraft#1 description", res.selected["libs/musl"].via)

    # 3
    def test_chain_via_commit_trailer(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: lib-musl#12"),
                             pr("unikraft/lib-musl", 12, commits=["musl: fix\n\nDepends-On: lib-lwip#40\n"]),
                             pr("unikraft/lib-lwip", 40)))
        self.assertEqual(res.errors, [])
        self.assertEqual(set(self.paths(res)), {"unikraft", "libs/musl", "libs/lwip"})
        self.assertIn("commit", res.selected["libs/lwip"].via)

    # 4
    def test_cycle_gives_same_set_from_both_sides(self):
        gh = FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: lib-musl#12"),
                        pr("unikraft/lib-musl", 12, body="Depends-On: unikraft#1"))
        a = run(gh, "unikraft/unikraft#1")
        b = run(gh, "unikraft/lib-musl#12")
        self.assertEqual(a.errors + b.errors, [])
        self.assertEqual(self.paths(a), self.paths(b))

    # 5
    def test_diamond_fetches_each_pr_once(self):
        gh = FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: lib-musl#12, lib-lwip#40"),
                        pr("unikraft/lib-musl", 12, body="Depends-On: lib-lwip#40"),
                        pr("unikraft/lib-lwip", 40))
        res = run(gh)
        self.assertEqual(res.errors, [])
        self.assertEqual(len(res.selected), 3)
        self.assertEqual(gh.fetches, 3)

    # 6
    def test_stacked_prs_use_the_top_one(self):
        gh = FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: lib-musl#12, lib-musl#15"),
                        pr("unikraft/lib-musl", 12, head="m12"),
                        pr("unikraft/lib-musl", 15, head="m15", body="Depends-On: #12"),
                        stacks={("m12", "m15")})
        res = run(gh)
        self.assertEqual(res.errors, [])
        self.assertEqual(res.selected["libs/musl"].pr.number, 15)
        self.assertIn(("unikraft/lib-musl#12", "included in unikraft/lib-musl#15, which is stacked on top of it"),
                      res.dropped)

    # 7
    def test_unstacked_prs_in_one_repo_are_combined(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: lib-musl#15, lib-musl#12"),
                             pr("unikraft/lib-musl", 12), pr("unikraft/lib-musl", 15)))
        self.assertEqual(res.errors, [])
        self.assertEqual([f.pr.number for f in res.combine["libs/musl"]], [12, 15])

    def test_stack_plus_independent_pr(self):
        gh = FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: lib-musl#12, lib-musl#15, lib-musl#20"),
                        pr("unikraft/lib-musl", 12, head="m12"), pr("unikraft/lib-musl", 15, head="m15"),
                        pr("unikraft/lib-musl", 20, head="m20"), stacks={("m12", "m15")})
        res = run(gh)
        self.assertEqual([f.pr.number for f in res.combine["libs/musl"]], [15, 20])
        self.assertEqual(res.dropped[0][0], "unikraft/lib-musl#12")

    def test_combined_pr_that_conflicts_with_base(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: lib-musl#12, lib-musl#15"),
                             pr("unikraft/lib-musl", 12), pr("unikraft/lib-musl", 15, mergeable=False)))
        self.assertIn("unikraft/lib-musl#15 conflicts with `staging`", res.errors[0])

    # 8
    def test_merged_dependency_is_dropped(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: lib-musl#9"),
                             pr("unikraft/lib-musl", 9, state="closed", merged=True)))
        self.assertEqual(res.errors, [])
        self.assertNotIn("libs/musl", res.selected)
        self.assertEqual(res.dropped[0][0], "unikraft/lib-musl#9")

    # 9
    def test_closed_dependency_is_an_error(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: lib-musl#9"),
                             pr("unikraft/lib-musl", 9, state="closed")))
        self.assertIn("closed without being merged", res.errors[0])

    # 10
    def test_mixed_branches_are_an_error(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: lib-musl#12"),
                             pr("unikraft/lib-musl", 12, base="stable")))
        self.assertIn("must target the same branch", res.errors[0])

    # 11
    def test_untrusted_dependency_makes_the_set_untrusted(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: lib-musl#12"),
                             pr("unikraft/lib-musl", 12, assoc="CONTRIBUTOR", author="stranger")))
        self.assertEqual(res.errors, [])
        self.assertFalse(res.trusted)
        self.assertIn("@stranger", res.untrusted[0])

    # 12
    def test_dependency_outside_the_org_is_refused(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: evil/lib-musl#1")))
        self.assertIn("only PRs in unikraft", res.errors[0])

    def test_not_a_dependency_repo(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: unikraft/kraftkit#5")))
        self.assertIn("not a repository catalog-core builds with", res.errors[0])

    # 13 is ParseTest.test_prose_is_never_a_dependency

    # 14
    def test_conflict_with_base(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1, mergeable=False)))
        self.assertIn("conflicts with `staging`", res.errors[0])

    def test_mergeability_unknown(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1, mergeable=None)))
        self.assertIn("re-run in a minute", res.errors[0])

    # 15
    def test_too_many_prs(self):
        prs = [pr("unikraft/unikraft", 1, body="Depends-On: " + ", ".join(f"lib-musl#{i}" for i in range(2, 12)))]
        prs += [pr("unikraft/lib-musl", i) for i in range(2, 12)]
        res = run(FakeGitHub(*prs), max_prs=8)
        self.assertTrue(any("more than 8 PRs" in e for e in res.errors))

    def test_catalog_core_pr_can_be_a_dependency(self):
        res = run(FakeGitHub(pr("unikraft/lib-musl", 12, body="Depends-On: catalog-core#60"),
                             pr("unikraft/catalog-core", 60, base="main")), "unikraft/lib-musl#12")
        self.assertEqual(res.errors, [])
        self.assertEqual(self.paths(res), {"libs/musl": "unikraft/lib-musl#12", "catalog-core": "unikraft/catalog-core#60"})

    def test_root_on_other_branch_is_not_tested(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1, base="feature-x")))
        self.assertIn("only PRs to stable, staging are tested", res.errors[0])

    def test_root_state(self):
        self.assertEqual(run(FakeGitHub(pr("unikraft/unikraft", 1))).root_state, "open")
        self.assertEqual(run(FakeGitHub(pr("unikraft/unikraft", 1, state="closed", merged=True))).root_state, "merged")
        self.assertIsNone(run(FakeGitHub()).root_state)

    def test_missing_pr(self):
        res = run(FakeGitHub(pr("unikraft/unikraft", 1, body="Depends-On: lib-musl#404")))
        self.assertIn("no such pull request", res.errors[0])


if __name__ == "__main__":
    unittest.main()
