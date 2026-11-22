import os
import subprocess
import tempfile
import unittest

from ukci import data, report, verdict as V


def v(app, variant, verdict, cc="gcc"):
    return V.Verdict(app, cc, variant, verdict, None, "detail")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.remote = os.path.join(self.tmp.name, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, name):
        return data.Store("o/catalog-core", None, os.path.join(self.tmp.name, name), url=self.remote).open()

    def test_concurrent_writers_both_land(self):
        a, b = self.store("a"), self.store("b")          # both start from an empty branch
        a.write("runs/1.json", {"x": 1})
        b.write("runs/2.json", {"x": 2})
        self.assertTrue(a.commit_and_push("a"))
        self.assertTrue(b.commit_and_push("b"))           # rebases onto a's push
        c = self.store("c")
        self.assertEqual((c.read("runs/1.json"), c.read("runs/2.json")), ({"x": 1}, {"x": 2}))

    def test_record_run_dependents_and_own_result(self):
        s = self.store("s")
        plan = {"root": "o/unikraft#1", "root_head_sha": "h1", "run_url": "u", "plan_hash": "p", "event": "repository_dispatch",
                "prs": [{"pr": "o/unikraft#1", "used": True}, {"pr": "o/lib-musl#12", "used": True}]}
        data.record_run(s, plan, {"conclusion": "failure", "title": "t", "verdicts": [{"app": "nginx"}]}, [])
        self.assertEqual(data.dependents(s, "o/lib-musl#12"), ["o/unikraft#1"])
        self.assertEqual(data.own_result(s, "o/unikraft#1", "h1")["conclusion"], "failure")
        self.assertIsNone(data.own_result(s, "o/unikraft#1", "other-head"))


class BlameTest(unittest.TestCase):
    plan = {"root": "o/unikraft#1",
            "prs": [{"pr": "o/unikraft#1", "used": True, "head_sha": "h1"},
                    {"pr": "o/lib-musl#12", "used": True, "head_sha": "m12"}]}

    class FakeStore:
        def __init__(self, own):
            self.own = own

        def read(self, rel):
            return self.own if rel == "pr/o__lib-musl/12/m12.json" else None

    def detail_with(self, own):
        vs = [v("nginx", "qemu.x86_64", V.REGRESSION)]
        report.blame(self.plan, vs, self.FakeStore(own))
        return vs[0].detail

    def test_inherited(self):
        own = {"run_url": "r12", "verdicts": [{"app": "nginx", "compiler": "gcc", "variant": "qemu.x86_64", "verdict": "REGRESSION"}]}
        self.assertIn("**inherited**: also fails in the own run of [o/lib-musl#12](r12)", self.detail_with(own))
        vs = [v("nginx", "qemu.x86_64", V.REGRESSION)]
        report.blame(self.plan, vs, self.FakeStore(own))
        self.assertEqual(report.title({"cells": [1]}, vs), "❌ 1 regression (inherited from o/lib-musl#12) · 0 passed")

    def test_caused_here(self):
        own = {"run_url": "r12", "verdicts": [{"app": "nginx", "compiler": "gcc", "variant": "qemu.x86_64", "verdict": "PASS"}]}
        self.assertIn("comes from this PR", self.detail_with(own))

    def test_unknown(self):
        self.assertIn("not known yet", self.detail_with(None))


class MemberChecksTest(unittest.TestCase):
    class FakeGH:
        token = "t"

        def __init__(self):
            self.calls = []

        def get(self, path):
            self.calls.append(("GET", path))
            return {"check_runs": [{"id": 7}]} if "lib-musl" in path else {"check_runs": []}

        def request(self, method, path, body):
            self.calls.append((method, path))
            return {"id": 1}

    def test_updates_or_creates_on_every_other_cycle_member(self):
        gh = self.FakeGH()
        plan = {"root": "o/unikraft#1", "run_url": "u", "cycle": [
            {"pr": "o/unikraft#1", "repo": "o/unikraft", "head_sha": "a"},
            {"pr": "o/lib-musl#2", "repo": "o/lib-musl", "head_sha": "b"},
            {"pr": "o/lib-lwip#3", "repo": "o/lib-lwip", "head_sha": "c"}]}
        report.post_member_checks(gh, plan, status="completed", title_text="t", summary="s", conclusion="success")
        self.assertIn(("GET", "/repos/o/lib-musl/commits/b/check-runs?check_name=Catalog+Core"), gh.calls)
        self.assertIn(("PATCH", "/repos/o/lib-musl/check-runs/7"), gh.calls)       # existing: updated
        self.assertIn(("POST", "/repos/o/lib-lwip/check-runs"), gh.calls)           # none yet: created
        self.assertFalse(any("o/unikraft" in path for _, path in gh.calls))          # the root is done elsewhere


class NightlyTest(unittest.TestCase):
    def test_only_changes_are_reported(self):
        prev = {"cells": {"a gcc q": "PASS", "b gcc q": "FAIL", "c gcc q": "FAIL", "d gcc q": "PASS"}}
        broke, fixed = report.nightly_changes(prev, [
            v("a", "q", V.FAIL), v("b", "q", V.PASS), v("c", "q", V.FAIL), v("d", "q", V.FLAKY), v("e", "q", V.FAIL)])
        self.assertEqual(broke, ["a gcc q: detail"])      # e is new, d flaky is not a failure, c unchanged
        self.assertEqual(fixed, ["b gcc q"])


if __name__ == "__main__":
    unittest.main()
