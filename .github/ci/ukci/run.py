"""Run one app with one compiler, variant by variant, and record a Result.

Each variant runs through the app's own ``.scripts/test/all.sh`` with the
other ``test_build``/``test_build_run`` lines removed: exactly what a user
running ``all.sh`` does, one variant at a time.

Then, for the variants that failed:
  * a failed *run*, or a build that failed downloading sources, is retried
    once on the same code;
  * whatever still fails runs once more without the PRs (``base_lock``, and
    for a catalog-core PR the app as it is on catalog-core's base), so the
    report can tell a regression from something already broken.

Output: <out>/result.json and <out>/logs/<attempt>/{build,run}.<variant>
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from typing import Literal

from . import apps as apps_mod
from . import git
from .models import Attempt, LockEntry, Plan, Result, Status

Kind = Literal["first", "retry", "base"]

# How the compilers are selected; this is also the list of compilers CI knows.
COMPILERS: dict[str, dict[str, str]] = {
    "gcc": {},  # Unikraft's defconfig picks the (cross) gcc
    "clang": {"CC": "clang", "CXX": "clang++"},
}

LINE_RE = re.compile(r"^\s*(build|run)\.(\S+)\s+\.\.\.\s+(PASSED|FAILED)\s*$")
# What people need to see first, in order of preference.
BUILD_ERROR_RES = [
    re.compile(r"(?:^|\s)\S+?:\d+(?::\d+)?: (?:fatal )?error: "),
    re.compile(r"command not found|No rule to make target|Require \S+ version|unknown C compiler"),
    re.compile(r"\berror\b", re.I),
]
RUN_ERROR_RE = re.compile(r"^(Cannot .+|Wrong .+|No kernel file.*|.*[Tt]imed? ?out.*)$")
# Unikraft downloads library sources with wget while building. Only these
# failures are retried; a 404 is not transient, so it isn't one of them.
FETCH_ERROR_RE = re.compile(
    r"unable to resolve host address|Temporary failure in name resolution|"
    r"failed: (?:Connection (?:timed out|refused|reset)|No route to host|Network is unreachable)|"
    r"Connection reset by peer|Read error at byte|ERROR (?:429|50[0234])\b"
)


def checkout(lock: dict[str, LockEntry], paths: list[str], dest: str) -> None:
    for path in paths:
        e = lock[path]
        target = os.path.join(dest, path)
        shutil.rmtree(target, ignore_errors=True)
        print(f"::group::{path}: {e.repo}@{e.sha[:12]} ({e.source})", flush=True)
        url = f"https://github.com/{e.repo}"
        if e.parts:
            assert e.base_sha
            git.checkout_combined(url, e.base_sha, e.parts, e.sha, target)
        else:
            git.fetch_commit(url, e.sha, target)
        print("::endgroup::", flush=True)


def variant_script(app_dir: str, variant: str) -> str:
    """all.sh with every other variant line dropped."""
    with open(os.path.join(app_dir, apps_mod.TEST_SCRIPT), encoding="utf-8") as f:
        lines = [line for line in f if not ((m := apps_mod.VARIANT_RE.match(line)) and m.group(2) != variant)]
    path = os.path.join(app_dir, ".scripts/test/ci-variant.sh")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.chmod(path, 0o755)
    return "./.scripts/test/ci-variant.sh"


def failure_reason(log_path: str, phase: str, root: str) -> tuple[str, bool]:
    """(the line people need to see, the build failed downloading sources)."""
    try:
        with open(log_path, errors="replace") as f:
            lines = [line.strip().replace(root.rstrip("/") + "/", "") for line in f if line.strip()]
    except OSError:
        return f"{phase} produced no log", False
    if phase == "build":
        fetch = any(FETCH_ERROR_RE.search(line) for line in lines)
        found = next((line for r in BUILD_ERROR_RES for line in lines if r.search(line)), None)
    else:
        fetch = False
        found = next((line for line in lines if RUN_ERROR_RE.match(line)), None)
    reason = found or (lines[-1] if lines else f"{phase} failed with no output")
    # Unpacked library sources live in the app's build directory; show them by their own name.
    reason = re.sub(r"\S*?/workdir/build/[^/\s]+/origin/", "", reason).replace("//", "/")
    return re.sub(r"(^|\s)repos/", r"\1", reason)[:300], fetch


def run_variant(
    app_dir: str, variant: apps_mod.Variant, compiler: str, kind: Kind, *, timeout_s: int, sudo: bool, root: str
) -> Attempt:
    env = [f"{k}={v}" for k, v in COMPILERS[compiler].items()]
    cmd = ["timeout", "-k", "10", str(timeout_s), "env", *env, variant_script(app_dir, variant.name)]
    if sudo:
        cmd = ["sudo", "-E", *cmd]
    a = Attempt(variant=variant.name, attempt=kind, build="MISSING", run="MISSING" if variant.run else None)
    t0 = last = time.monotonic()
    with subprocess.Popen(
        cmd, cwd=app_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    ) as proc:
        assert proc.stdout
        for line in proc.stdout:
            print(line, end="", flush=True)
            if (m := LINE_RE.match(line)) and m.group(2) == variant.name:
                now = time.monotonic()
                status: Status = "PASS" if m.group(3) == "PASSED" else "FAIL"
                if m.group(1) == "build":
                    a.build, a.build_s = status, round(now - last, 1)
                else:
                    a.run, a.run_s = status, round(now - last, 1)
                last = now
        rc = proc.wait()
    if rc in (124, 137):  # the timeout fired during the phase that has no result yet
        if a.build == "MISSING":
            a.build, a.build_s = "TIMEOUT", round(time.monotonic() - last, 1)
        elif a.run == "MISSING":
            a.run, a.run_s = "TIMEOUT", round(time.monotonic() - last, 1)
        for name in ("qemu-system", "firecracker"):  # the killed wrapper didn't clean up
            subprocess.run([*(["sudo"] if sudo else []), "pkill", "-9", name], check=False, capture_output=True)
    if variant.run and a.build != "PASS":
        a.run = "BLOCKED"  # nothing (or a stale kernel) to run
    a.total_s = round(time.monotonic() - t0, 1)
    if not a.ok:
        if (a.build if a.phase == "build" else a.run) == "TIMEOUT":
            a.reason = f"{a.phase} timed out after {timeout_s}s"
        else:
            log = os.path.join(app_dir, ".scripts/test/log", f"{a.phase}.{variant.name}")
            a.reason, a.fetch_error = failure_reason(log, a.phase, root)
    return a


def tool_versions() -> dict[str, str]:
    def first_line(*cmd: str) -> str:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return "not installed"
        return ((p.stdout or p.stderr).splitlines() or ["n/a"])[0].strip()

    return {
        "gcc": first_line("gcc", "--version"),
        "aarch64-gcc": first_line("aarch64-linux-gnu-gcc", "--version"),
        "clang": first_line("clang", "--version"),
        "qemu": first_line("qemu-system-x86_64", "--version"),
        "firecracker": first_line(f"firecracker-{platform.machine()}", "--version"),
        "kernel": platform.release(),
    }


def needs_retry(a: Attempt) -> bool:
    return not a.ok and (a.phase == "run" or a.fetch_error)


def main(args) -> int:
    with open(args.plan) as f:
        plan = Plan.model_validate_json(f.read())
    if not any(c.app == args.app and c.compiler == args.compiler for c in plan.cells):
        raise SystemExit(f"{args.app}/{args.compiler} is not in the plan")
    root = os.path.abspath(args.root)
    app_dir = os.path.join(root, args.app)
    app = apps_mod.load_app(root, args.app)
    variants = [v for v in app.variants if not args.variants or v.name in args.variants.split(",")]
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    result = Result(app=args.app, compiler=args.compiler, plan_hash=plan.plan_hash, tools=tool_versions())

    def save() -> None:
        with open(os.path.join(out, "result.json"), "w") as f:
            f.write(result.model_dump_json(indent=2))

    def attempt(kind: Kind, vs: list[apps_mod.Variant], in_dir: str) -> dict[str, Attempt]:
        done = {}
        for v in vs:
            print(f"::group::{kind}: {args.app} {v.name} ({args.compiler})", flush=True)
            a = run_variant(in_dir, v, args.compiler, kind, timeout_s=args.timeout, sudo=not args.no_sudo, root=root)
            print("::endgroup::", flush=True)
            logs = os.path.join(out, "logs", kind)
            os.makedirs(logs, exist_ok=True)
            for phase in ("build", "run"):
                src = os.path.join(in_dir, ".scripts/test/log", f"{phase}.{v.name}")
                if os.path.exists(src):
                    shutil.copyfile(src, os.path.join(logs, f"{phase}.{v.name}"))
            result.attempts.append(a)
            save()
            done[v.name] = a
        return done

    repos_dir = os.path.join(root, "repos")
    try:
        checkout(plan.lock, app.repos, repos_dir)
    except (subprocess.CalledProcessError, RuntimeError) as e:
        result.infra_error = f"checkout failed: {getattr(e, 'stderr', '') or e}"[:400]
        save()
        return 2

    final = attempt("first", variants, app_dir)
    if not args.no_recheck:
        final |= attempt("retry", [v for v in variants if needs_retry(final[v.name])], app_dir)
    failed = [v for v in variants if not final[v.name].ok and not final[v.name].fetch_error]
    changed = [p for p in app.repos if plan.base_lock[p].sha != plan.lock[p].sha]
    if args.no_recheck or not failed or not (changed or plan.catalog_base):
        save()
        return 0

    # What still fails runs without the PRs.
    try:
        checkout(plan.base_lock, changed, repos_dir)
        base_dir, base_variants = app_dir, failed
        if plan.catalog_base:
            bdir = os.path.join(root, ".ci-base-catalog")
            shutil.rmtree(bdir, ignore_errors=True)
            git.fetch_commit(f"https://github.com/{plan.catalog_base.repo}", plan.catalog_base.sha, bdir)
            os.symlink(repos_dir, os.path.join(bdir, "repos"))
            base_dir = os.path.join(bdir, args.app)
            existing = (
                {v.name for v in apps_mod.load_app(bdir, args.app).variants}
                if os.path.isfile(os.path.join(base_dir, apps_mod.TEST_SCRIPT))
                else set()
            )
            base_variants = [v for v in failed if v.name in existing]
            # An app or variant that is new in the PR has nothing to compare to.
            result.new_variants = sorted(v.name for v in failed if v.name not in existing)
        attempt("base", base_variants, base_dir)
    except (subprocess.CalledProcessError, RuntimeError) as e:
        result.infra_error = f"checkout without the PRs failed: {getattr(e, 'stderr', '') or e}"[:400]
    save()
    return 0
