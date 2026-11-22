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


def attempt(variant, kind, build="PASS", run_="PASS"):
    return {"variant": variant, "attempt": kind, "build": build, "run": run_}


class VerdictTest(unittest.TestCase):
    plan = {"channel": "staging", "plan_hash": "h", "errors": [],
            "cells": [{"app": "nginx", "compiler": "gcc", "why": "links libs/musl", "repos": [],
                       "variants": ["qemu.x86_64", "fc.x86_64", "xen.x86_64", "qemu.arm64", "fc.arm64"]}]}

    def judge(self, attempts, **extra):
        r = {"app": "nginx", "compiler": "gcc", "attempts": attempts, **extra}
        return {v.variant: v.verdict for v in V.judge(self.plan, [r])}

    def test_all_verdicts(self):
        got = self.judge([
            attempt("qemu.x86_64", "first"),
            attempt("fc.x86_64", "first", run_="FAIL"), attempt("fc.x86_64", "retry"),
            attempt("xen.x86_64", "first", build="FAIL", run_=None), attempt("xen.x86_64", "base", build="FAIL", run_=None),
            attempt("qemu.arm64", "first", run_="FAIL"), attempt("qemu.arm64", "retry", run_="FAIL"),
            attempt("qemu.arm64", "base"),
            attempt("fc.arm64", "first", build="FAIL", run_=None),
        ])
        self.assertEqual(got, {"qemu.x86_64": V.PASS, "fc.x86_64": V.FLAKY, "xen.x86_64": V.BROKEN_ON_BASE,
                               "qemu.arm64": V.REGRESSION, "fc.arm64": V.FAIL})

    def test_missing_job_is_infra(self):
        got = {v.verdict for v in V.judge(self.plan, [])}
        self.assertEqual(got, {V.INFRA})

    def test_conclusion_and_render(self):
        vs = V.judge(self.plan, [{"app": "nginx", "compiler": "gcc", "attempts": [
            attempt("qemu.x86_64", "first", run_="FAIL"), attempt("qemu.x86_64", "retry", run_="FAIL"),
            attempt("qemu.x86_64", "base")]}])
        self.assertEqual(V.conclusion(self.plan, vs), "failure")
        self.assertTrue(title(self.plan, vs).startswith("❌ 1 regression"))
        self.assertIn("| ❌ regression | nginx | gcc | qemu.x86_64 | run failed with the PRs; passes on staging |",
                      render(self.plan, vs))
        only_broken = [v for v in vs if v.variant != "qemu.x86_64"]
        self.assertEqual({v.verdict for v in only_broken}, {V.INFRA})


if __name__ == "__main__":
    unittest.main()
