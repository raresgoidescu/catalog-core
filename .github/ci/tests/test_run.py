"""The runner, on a fake app that uses the same all.sh helpers as the real ones."""

import os
import stat
import textwrap

import pytest

from ukci import apps, run

ALL = textwrap.dedent("""\
    #!/bin/sh
    test_build()
    {
        printf "%-46s ... " build."$1"
        ./.scripts/build/"$1" > ./.scripts/test/log/build."$1" 2>&1
        if test $? -eq 0; then echo "PASSED"; else echo "FAILED"; fi
    }
    test_build_run()
    {
        test_build "$1"
        printf "    %-42s ... " run."$1"
        ./.scripts/test/wrapper.sh ./.scripts/run/"$1" 2> ./.scripts/test/log/run."$1"
    }
    ./setup.sh
    test -d ./.scripts/test/log || mkdir ./.scripts/test/log
    test_build_run qemu.x86_64
    test_build_run fc.x86_64
    test_build xen.x86_64
    """)
SETUP = '#!/bin/sh\ncheck_exists_and_create_symlink()\n{\n    :\n}\ncheck_exists_and_create_symlink "unikraft"\n'
WRAPPER = '#!/bin/sh\nif "$1"; then echo PASSED; else echo "Wrong message printed." 1>&2; echo FAILED; fi\n'
OK, HANG = "#!/bin/sh\nexit 0\n", "#!/bin/sh\nsleep 30\n"
BUILD_ERROR = "#!/bin/sh\necho '/w/repos/unikraft/lib/x.c:12:3: error: boom' 1>&2\nexit 1\n"
DOWNLOAD_ERROR = "#!/bin/sh\necho 'wget: unable to resolve host address' 1>&2\nexit 1\n"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


@pytest.fixture
def app(tmp_path):
    d = tmp_path / "app"
    write(f"{d}/setup.sh", SETUP)
    write(f"{d}/.scripts/test/all.sh", ALL)
    write(f"{d}/.scripts/test/wrapper.sh", WRAPPER)
    for v in ("qemu.x86_64", "fc.x86_64", "xen.x86_64"):
        write(f"{d}/.scripts/build/{v}", OK)
        write(f"{d}/.scripts/run/{v}", OK)
    loaded = apps.load_app(str(tmp_path), "app")

    def go(variant, timeout=30, run_timeout=30):
        v = next(x for x in loaded.variants if x.name == variant)
        return run.run_variant(
            str(d), v, "gcc", "first", timeout_s=timeout, run_timeout_s=run_timeout, sudo=False, root="/w"
        )

    go.dir = d
    return go


def test_only_the_selected_variant_runs(app):
    write(f"{app.dir}/.scripts/build/fc.x86_64", BUILD_ERROR)
    a = app("qemu.x86_64")
    assert (a.build, a.run, a.ok) == ("PASS", "PASS", True)
    assert not os.path.exists(f"{app.dir}/.scripts/test/log/build.fc.x86_64")


def test_build_failure_blocks_the_run_and_names_the_error(app):
    write(f"{app.dir}/.scripts/build/qemu.x86_64", BUILD_ERROR)
    a = app("qemu.x86_64")
    assert (a.build, a.run) == ("FAIL", "BLOCKED")
    assert a.reason == "unikraft/lib/x.c:12:3: error: boom"
    assert not a.fetch_error


def test_run_failure_names_the_wrapper_message(app):
    write(f"{app.dir}/.scripts/run/fc.x86_64", "#!/bin/sh\nexit 1\n")
    a = app("fc.x86_64")
    assert (a.build, a.run, a.reason) == ("PASS", "FAIL", "Wrong message printed.")


def test_download_failure_is_recognized(app):
    write(f"{app.dir}/.scripts/build/qemu.x86_64", DOWNLOAD_ERROR)
    assert app("qemu.x86_64").fetch_error


def test_build_only_variant(app):
    a = app("xen.x86_64")
    assert (a.build, a.run) == ("PASS", None)


def test_timeout(app):
    write(f"{app.dir}/.scripts/run/qemu.x86_64", HANG)
    a = app("qemu.x86_64", timeout=2)
    assert (a.build, a.run, a.reason) == ("PASS", "TIMEOUT", "run timed out after 2s")


def test_a_hanging_run_stops_at_the_run_timeout(app):
    write(f"{app.dir}/.scripts/run/qemu.x86_64", HANG)
    a = app("qemu.x86_64", timeout=60, run_timeout=1)
    assert (a.build, a.run, a.reason) == ("PASS", "TIMEOUT", "run timed out after 1s")
    assert a.total_s is not None and a.total_s < 15


def test_the_run_timeout_does_not_cut_a_slow_build(app):
    write(f"{app.dir}/.scripts/build/qemu.x86_64", "#!/bin/sh\nsleep 2\n")
    a = app("qemu.x86_64", run_timeout=1)
    assert (a.build, a.run) == ("PASS", "PASS")


@pytest.mark.parametrize(
    ("lines", "reason"),
    [
        (
            ["/w/nginx/workdir/build/libmusl/origin/musl-1.2.3//src/a.c:151:52: error: pointer type mismatch"],
            "musl-1.2.3/src/a.c:151:52: error: pointer type mismatch",
        ),
        (
            ["  CC x.o", "/bin/sh: 1: aarch64-linux-gnu-gcc: command not found"],
            "/bin/sh: 1: aarch64-linux-gnu-gcc: command not found",
        ),
        (["make[2]: *** [x] Error 1"], "make[2]: *** [x] Error 1"),
        (['/w/repos/libs/libelf/elf.c:1:2: error: #error "boom"'], 'libs/libelf/elf.c:1:2: error: "boom"'),  # gcc
        (['/w/repos/libs/libelf/elf.c:1:2: error: "boom"'], 'libs/libelf/elf.c:1:2: error: "boom"'),  # clang
        (["only noise", "last line"], "last line"),
    ],
)
def test_build_failure_reason(tmp_path, lines, reason):
    log = tmp_path / "build.x"
    log.write_text("\n".join(["noise", *lines, ""]))
    assert run.failure_reason(str(log), "build", "/w")[:2] == (reason, False)


@pytest.mark.parametrize(
    ("line", "fetch"),
    [
        ("wget: unable to resolve host address 'musl.libc.org'", True),
        ("Connecting to x|1.2.3.4|:443... failed: Connection timed out.", True),
        (
            "awaiting response... 503 Service Unavailable\n2026-01-01 ERROR 503: Service Unavailable.",
            True,
        ),
        ("ERROR 404: Not Found.", False),  # not transient: the URL is wrong
        ("x.c:1:2: error: boom", False),
    ],
)
def test_download_failures_are_recognized_in_build_logs(tmp_path, line, fetch):
    log = tmp_path / "build.x"
    log.write_text(f"{line}\nmake: *** [fetch] Error 4\n")
    assert run.failure_reason(str(log), "build", "/w")[1] is fetch


def test_run_logs_never_count_as_download_failures(tmp_path):
    # An app that serves HTTP and doesn't answer: the app's fault, not the network's.
    log = tmp_path / "run.x"
    log.write_text(
        "curl: (7) Failed to connect to 172.44.0.2 port 8080: Connection refused\nCannot connect to 172.44.0.2:8080\n"
    )
    assert run.failure_reason(str(log), "run", "/w") == ("Cannot connect to 172.44.0.2:8080", False, False)


def test_a_missing_tool_is_the_runner_not_the_code(tmp_path):
    log = tmp_path / "build.x"
    log.write_text(
        "aarch64-linux-gnu-clang: unknown C compiler\n"
        "/w/c-hello/workdir/build/libkvmplat.ld.o.cmd: line 1: aarch64-linux-gnu-clang: command not found\n"
        "make[2]: *** [x] Error 127\n"
    )
    reason, fetch, missing = run.failure_reason(str(log), "build", "/w")
    assert missing and not fetch and "aarch64-linux-gnu-clang: command not found" in reason
    from ukci.models import Attempt

    a = Attempt(variant="v", attempt="first", build="FAIL", run="BLOCKED", reason=reason, env_error=True)
    assert not run.needs_retry(a) and not run.needs_base(a)


def test_unknown_c_compiler_alone_is_not_an_error(tmp_path):
    log = tmp_path / "build.x"
    log.write_text("aarch64-linux-gnu-clang: unknown C compiler\n/w/x.c:1:2: error: boom\n")
    assert run.failure_reason(str(log), "build", "/w") == ("x.c:1:2: error: boom", False, False)


def test_retry_policy():
    from ukci.models import Attempt

    assert run.needs_retry(Attempt(variant="v", attempt="first", build="PASS", run="FAIL"))
    assert run.needs_retry(Attempt(variant="v", attempt="first", build="FAIL", run="BLOCKED", fetch_error=True))
    assert not run.needs_retry(Attempt(variant="v", attempt="first", build="FAIL", run="BLOCKED"))
    assert not run.needs_retry(Attempt(variant="v", attempt="first", build="PASS", run="PASS"))


def test_a_known_scheduled_result_replaces_the_base_run(app, tmp_path, monkeypatch):
    import json as _json

    from ukci import data
    from ukci.models import Attempt, CatalogRef, Cell, LockEntry, Plan

    write(f"{app.dir}/.scripts/build/qemu.x86_64", BUILD_ERROR)
    plan = Plan(
        plan_hash="h",
        catalog=CatalogRef(repo="o/c", sha="c1", source=""),
        cells=[Cell(app="app", compiler="gcc", why="", repos=["unikraft"], variants=["qemu.x86_64"])],
        lock={"unikraft": LockEntry(repo="u/unikraft", sha="pr", source="")},
        base_lock={"unikraft": LockEntry(repo="u/unikraft", sha="base", source="")},
    )
    (tmp_path / "plan.json").write_text(plan.model_dump_json())
    checkouts = []
    monkeypatch.setattr(run, "checkout", lambda plan, lock, paths, dest: checkouts.append(lock[paths[0]].sha))
    monkeypatch.setattr(run, "tool_versions", lambda: {"gcc": "13"})
    known = Attempt(variant="qemu.x86_64", attempt="base", build="PASS", run="PASS", source="https://nightly")
    asked = []
    monkeypatch.setattr(data, "fetch_base", lambda repo, path: asked.append(path) or {"qemu.x86_64": known})
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/catalog-core")
    args = type(
        "A",
        (),
        {
            "plan": str(tmp_path / "plan.json"),
            "app": "app",
            "compiler": "gcc",
            "root": str(tmp_path),
            "variants": "qemu.x86_64",
            "out": str(tmp_path / "out"),
            "no_recheck": False,
            "no_sudo": True,
            "timeout": 30,
            "run_timeout": 30,
        },
    )
    assert run.main(args) == 0
    assert checkouts == ["pr"]  # the base was never checked out or built
    assert asked == [data.base_path("app", "gcc", "c1", {"unikraft": "base"}, {"gcc": "13"})]
    attempts = _json.loads((tmp_path / "out" / "result.json").read_text())["attempts"]
    assert [(a["attempt"], a["build"], a["source"]) for a in attempts] == [
        ("first", "FAIL", ""),
        ("base", "PASS", "https://nightly"),
    ]


def test_a_line_cut_by_parallel_output_is_not_the_reason(tmp_path):
    log = tmp_path / "build.x"
    log.write_text(
        "49:4: error: instruction requires: fp-armv8\n"
        "/w/repos/unikraft/plat/ectx.c:50:4: error: instruction requires: fp-armv8\n"
    )
    assert (
        run.failure_reason(str(log), "build", "/w")[0]
        == "unikraft/plat/ectx.c:50:4: error: instruction requires: fp-armv8"
    )


def test_a_failing_comparison_run_is_retried_once(app, tmp_path, monkeypatch):
    from ukci.models import CatalogRef, Cell, LockEntry, Plan

    write(f"{app.dir}/.scripts/run/qemu.x86_64", "#!/bin/sh\nexit 1\n")
    plan = Plan(
        plan_hash="h",
        catalog=CatalogRef(repo="o/c", sha="c1", source=""),
        cells=[Cell(app="app", compiler="gcc", why="", repos=["unikraft"], variants=["qemu.x86_64"])],
        lock={"unikraft": LockEntry(repo="u/unikraft", sha="pr", source="")},
        base_lock={"unikraft": LockEntry(repo="u/unikraft", sha="base", source="")},
    )
    (tmp_path / "plan.json").write_text(plan.model_dump_json())
    monkeypatch.setattr(run, "checkout", lambda plan, lock, paths, dest: None)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    args = type(
        "A",
        (),
        {
            "plan": str(tmp_path / "plan.json"),
            "app": "app",
            "compiler": "gcc",
            "root": str(tmp_path),
            "variants": "qemu.x86_64",
            "out": str(tmp_path / "out"),
            "no_recheck": False,
            "no_sudo": True,
            "timeout": 30,
            "run_timeout": 30,
        },
    )
    assert run.main(args) == 0
    attempts = (tmp_path / "out" / "result.json").read_text()
    for kind in ("first", "retry", "base", "base_retry"):
        assert f'"attempt": "{kind}"' in attempts
