import json
import os
import textwrap
import unittest

from tests.test_apps import TempCatalog, write
from ukci import apps, run, verdict as V
from ukci.report import render, title

# A fake app whose "build" and "run" are controlled by marker files, using
# the same all.sh helpers the real apps use.
ALL = textwrap.dedent("""\
    #!/bin/sh
    test_build()
    {
        printf "%-46s ... " build."$1"
        ./.scripts/build/"$1" > ./.scripts/test/log/build."$1" 2>&1
        if test $? -eq 0; then
            echo "PASSED"
        else
            echo "FAILED"
        fi
    }

    test_build_run()
    {
        printf "%-46s ... " build."$1"
        ./.scripts/build/"$1" > ./.scripts/test/log/build."$1" 2>&1
        if test $? -eq 0; then
            echo "PASSED"
        else
            echo "FAILED"
        fi

        printf "    %-42s ... " run."$1"
        ./.scripts/test/wrapper.sh ./.scripts/run/"$1" 2> ./.scripts/test/log/run."$1"
    }

    test -d ./.scripts/test/log || mkdir ./.scripts/test/log
    test_build_run qemu.x86_64
    test_build_run fc.x86_64
    test_build xen.x86_64
    """)
WRAPPER = '#!/bin/sh\nif "$1"; then echo PASSED; else echo FAILED; fi\n'
OK = "#!/bin/sh\nexit 0\n"
BAD = "#!/bin/sh\necho boom 1>&2\nexit 1\n"
HANG = "#!/bin/sh\nsleep 30\n"


class RunVariantTest(TempCatalog):
    def setUp(self):
        super().setUp()
        os.environ["CI_SUDO"] = "0"
        self.add_app("app", all_sh=ALL, builds=(), runs=())
        d = f"{self.root}/app/.scripts"
        write(f"{d}/test/wrapper.sh", WRAPPER, exe=True)
        for v in ("qemu.x86_64", "fc.x86_64", "xen.x86_64"):
            write(f"{d}/build/{v}", OK, exe=True)
            write(f"{d}/run/{v}", OK, exe=True)
        self.app = apps.load_app(self.root, "app")
        self.dir = f"{self.root}/app"

    def variant(self, name):
        return next(v for v in self.app.variants if v.name == name)

    def test_only_the_selected_variant_runs(self):
        write(f"{self.dir}/.scripts/build/fc.x86_64", BAD, exe=True)
        r = run.run_variant(self.dir, self.variant("qemu.x86_64"), "gcc", 30, sudo=False)
        self.assertEqual((r["build"], r["run"]), ("PASS", "PASS"))
        self.assertFalse(os.path.exists(f"{self.dir}/.scripts/test/log/build.fc.x86_64"))

    def test_build_failure_blocks_run(self):
        write(f"{self.dir}/.scripts/build/qemu.x86_64", BAD, exe=True)
        r = run.run_variant(self.dir, self.variant("qemu.x86_64"), "gcc", 30, sudo=False)
        self.assertEqual((r["build"], r["run"]), ("FAIL", "BLOCKED"))

    def test_run_failure(self):
        write(f"{self.dir}/.scripts/run/fc.x86_64", BAD, exe=True)
        r = run.run_variant(self.dir, self.variant("fc.x86_64"), "clang", 30, sudo=False)
        self.assertEqual((r["build"], r["run"]), ("PASS", "FAIL"))

    def test_build_only_variant(self):
        r = run.run_variant(self.dir, self.variant("xen.x86_64"), "gcc", 30, sudo=False)
        self.assertEqual((r["build"], r["run"]), ("PASS", None))

    def test_timeout(self):
        write(f"{self.dir}/.scripts/run/qemu.x86_64", HANG, exe=True)
        r = run.run_variant(self.dir, self.variant("qemu.x86_64"), "gcc", 2, sudo=False)
        self.assertEqual((r["build"], r["run"]), ("PASS", "TIMEOUT"))


def attempt(variant, kind, build="PASS", run_="PASS", sig=None, fetch=False):
    a = {"variant": variant, "attempt": kind, "build": build, "run": run_}
    if build != "PASS" or (run_ not in (None, "PASS")):
        a.update(reason=f"error {sig}", signature=sig, fetch_error=fetch)
    return a


class VerdictTest(unittest.TestCase):
    def plan(self, comparable=True):
        return {"channel": "staging", "plan_hash": "h", "errors": [], "root": "o/lib-musl#1",
                "lock": {"libs/musl": {"sha": "pr", "repo": "o/lib-musl", "from": "o/lib-musl#1 merged into staging", "pr": "o/lib-musl#1"}},
                "base_lock": {"libs/musl": {"sha": "pr" if not comparable else "base", "repo": "o/lib-musl", "from": "staging"}},
                "cells": [{"app": "nginx", "compiler": "gcc", "why": "links libs/musl", "repos": ["libs/musl"],
                           "variants": ["a", "b", "c", "d", "e", "f", "g"]}]}

    def judge(self, attempts, comparable=True, **extra):
        r = {"app": "nginx", "compiler": "gcc", "attempts": attempts, **extra}
        return {v.variant: v.verdict for v in V.judge(self.plan(comparable), [r])}

    def test_verdicts(self):
        got = self.judge([
            attempt("a", "first"),
            attempt("b", "first", run_="FAIL", sig="x"), attempt("b", "retry"),                       # flaky
            attempt("c", "first", build="FAIL", run_="BLOCKED", sig="e1"),
            attempt("c", "base", build="FAIL", run_="BLOCKED", sig="e1"),                             # same failure
            attempt("d", "first", build="FAIL", run_="BLOCKED", sig="new"),
            attempt("d", "base", build="FAIL", run_="BLOCKED", sig="old"),                            # fails differently
            attempt("e", "first", run_="FAIL", sig="x"), attempt("e", "retry", run_="FAIL", sig="x"),
            attempt("e", "base"),                                                                     # passes on base
            attempt("f", "first", build="FAIL", run_="BLOCKED", sig="net", fetch=True),               # download failed
            attempt("g", "first", build="FAIL", run_="BLOCKED", sig="y"),                             # base missing
        ])
        self.assertEqual(got, {"a": V.PASS, "b": V.FLAKY, "c": V.BROKEN_ON_BASE, "d": V.REGRESSION,
                               "e": V.REGRESSION, "f": V.INFRA, "g": V.INFRA})

    def test_nothing_to_compare_is_a_failure(self):
        got = self.judge([attempt("a", "first", build="FAIL", run_="BLOCKED", sig="z")], comparable=False)
        self.assertEqual(got["a"], V.FAIL)

    def test_missing_job_is_infra(self):
        self.assertEqual({v.verdict for v in V.judge(self.plan(), [])}, {V.INFRA})

    def test_title_and_grouped_render(self):
        attempts = [attempt(v, "first", build="FAIL", run_="BLOCKED", sig="boom") for v in "ab"]
        attempts += [attempt(v, "base") for v in "ab"]
        plan = self.plan()
        plan["cells"][0]["variants"] = ["a", "b", "c"]
        attempts.append(attempt("c", "first"))
        vs = V.judge(plan, [{"app": "nginx", "compiler": "gcc", "attempts": attempts}])
        self.assertEqual(V.conclusion(plan, vs), "failure")
        self.assertEqual(title(plan, vs), "❌ 2 regressions · 1/3 passed")
        text = render(plan, vs)
        self.assertIn("**❌ Regression** in 2 configurations: nginx × gcc × a, b", text)   # one block, not two rows
        self.assertIn("```\nerror boom\n```", text)                                       # the actual error
        self.assertIn("passes on staging without the PRs", text)


if __name__ == "__main__":
    unittest.main()
