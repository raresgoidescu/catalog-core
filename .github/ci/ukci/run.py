"""Run one app with one compiler, variant by variant, and record results.

For each variant, the app's own ``.scripts/test/all.sh`` is run with every
other ``test_build``/``test_build_run`` line removed, so CI does exactly
what a user running ``all.sh`` does, one variant at a time.

Then, for the variants that failed:
  * a failed *run* is retried once on the same code (builds are
    deterministic and are not retried);
  * the variant is run once more on ``base_lock`` (the same commits
    without the PRs), so the report can tell a regression from something
    already broken on the base branch.

Output: <out>/result.json and <out>/logs/<attempt>/{build,run}.<variant>
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time

from . import apps as apps_mod
from .plan import fetch_commit

LINE_RE = re.compile(r"^\s*(build|run)\.(\S+)\s+\.\.\.\s+(PASSED|FAILED)\s*$")

PASS, FAIL, TIMEOUT, BLOCKED, MISSING = "PASS", "FAIL", "TIMEOUT", "BLOCKED", "MISSING"


def checkout(lock: dict, paths: list[str], dest: str) -> None:
    for path in paths:
        entry = lock[path]
        target = os.path.join(dest, path)
        if os.path.isdir(target):
            shutil.rmtree(target)
        print(f"::group::{path}: {entry['repo']}@{entry['sha'][:12]} ({entry['from']})", flush=True)
        fetch_commit(f"https://github.com/{entry['repo']}", entry["sha"], target)
        print("::endgroup::", flush=True)


def variant_script(app_dir: str, variant: str) -> str:
    """all.sh with every other variant line dropped."""
    src = os.path.join(app_dir, apps_mod.TEST_SCRIPT)
    out = []
    with open(src, encoding="utf-8") as f:
        for line in f:
            m = apps_mod.VARIANT_RE.match(line)
            if m and m.group(2) != variant:
                continue
            out.append(line)
    path = os.path.join(app_dir, ".scripts/test/ci-variant.sh")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(out)
    os.chmod(path, 0o755)
    return "./.scripts/test/ci-variant.sh"


def compiler_env(compiler: str) -> dict[str, str]:
    if compiler == "gcc":
        return {}          # let Unikraft's defconfig pick the (cross) gcc
    if compiler.startswith("clang"):
        return {"CC": compiler, "CXX": compiler.replace("clang", "clang++", 1)}
    raise SystemExit(f"unsupported compiler {compiler!r}")


def run_variant(app_dir: str, variant: apps_mod.Variant, compiler: str, timeout_s: int, sudo: bool) -> dict:
    script = variant_script(app_dir, variant.name)
    env = compiler_env(compiler)
    cmd = ["timeout", "-k", "10", str(timeout_s), "env", *[f"{k}={v}" for k, v in env.items()], script]
    if sudo:
        cmd = ["sudo", "-E", *cmd]
    result = {"variant": variant.name, "platform": variant.platform, "arch": variant.arch,
              "build": MISSING, "run": None if not variant.run else MISSING,
              "build_s": None, "run_s": None}
    t0 = last = time.monotonic()
    with subprocess.Popen(cmd, cwd=app_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, bufsize=1) as proc:
        for line in proc.stdout:
            print(line, end="", flush=True)
            m = LINE_RE.match(line)
            if not m or m.group(2) != variant.name:
                continue
            now = time.monotonic()
            phase, status = m.group(1), PASS if m.group(3) == "PASSED" else FAIL
            result[phase] = status
            result[f"{phase}_s"] = round(now - last, 1)
            last = now
        rc = proc.wait()
    if rc in (124, 137):     # timeout fired: the phase still MISSING is the one that hung
        for phase in ("build", "run"):
            if result[phase] == MISSING:
                result[phase] = TIMEOUT
                result[f"{phase}_s"] = round(time.monotonic() - last, 1)
                break
        cleanup_instances(sudo)
    if variant.run and result["build"] != PASS:
        result["run"] = BLOCKED          # nothing (or a stale kernel) to run; not a separate result
    result["total_s"] = round(time.monotonic() - t0, 1)
    return result


def cleanup_instances(sudo: bool) -> None:
    pre = ["sudo"] if sudo else []
    for name in ("qemu-system", "firecracker"):
        subprocess.run([*pre, "pkill", "-9", name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def failed(r: dict) -> bool:
    return r["build"] != PASS or (r["run"] is not None and r["run"] != PASS)


def save_logs(app_dir: str, out: str, attempt: str, variant: str) -> None:
    dest = os.path.join(out, "logs", attempt)
    os.makedirs(dest, exist_ok=True)
    for phase in ("build", "run"):
        src = os.path.join(app_dir, ".scripts/test/log", f"{phase}.{variant}")
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(dest, f"{phase}.{variant}"))


def tool_versions() -> dict:
    def first_line(*cmd):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            return (p.stdout or p.stderr).splitlines()[0].strip() if (p.stdout or p.stderr) else "n/a"
        except (OSError, subprocess.TimeoutExpired):
            return "not installed"
    return {
        "gcc": first_line("gcc", "--version"),
        "aarch64-gcc": first_line("aarch64-linux-gnu-gcc", "--version"),
        "clang": first_line("clang", "--version"),
        "qemu": first_line("qemu-system-x86_64", "--version"),
        "firecracker": first_line(f"firecracker-{platform.machine()}", "--version"),
        "kernel": platform.release(),
    }


def main(args) -> int:
    with open(args.plan) as f:
        plan = json.load(f)
    cell = next((c for c in plan["cells"] if c["app"] == args.app and c["compiler"] == args.compiler), None)
    if cell is None:
        raise SystemExit(f"{args.app}/{args.compiler} is not in the plan")
    root = os.path.abspath(args.root)
    app_dir = os.path.join(root, args.app)
    app = apps_mod.load_app(root, args.app)
    variants = [v for v in app.variants if not args.variants or v.name in args.variants.split(",")]
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    sudo = os.environ.get("CI_SUDO", "1") == "1"
    timeout_s = int(os.environ.get("CI_VARIANT_TIMEOUT", "1200"))

    result = {"app": args.app, "compiler": args.compiler, "plan_hash": plan["plan_hash"],
              "tools": tool_versions(), "attempts": [], "infra_error": None}

    def write():
        with open(os.path.join(out, "result.json"), "w") as f:
            json.dump(result, f, indent=2)

    repos_dir = os.path.join(root, "repos")
    try:
        checkout(plan["lock"], app.repos, repos_dir)
    except subprocess.CalledProcessError as e:
        result["infra_error"] = f"checkout failed: {e}"
        write()
        return 2

    def attempt(kind: str, vs: list[apps_mod.Variant]) -> list[dict]:
        rs = []
        for v in vs:
            print(f"::group::{kind}: {args.app} {v.name} ({args.compiler})", flush=True)
            r = run_variant(app_dir, v, args.compiler, timeout_s, sudo)
            print("::endgroup::", flush=True)
            r["attempt"] = kind
            save_logs(app_dir, out, kind, v.name)
            result["attempts"].append(r)
            write()
            rs.append(r)
        return rs

    first = attempt("first", variants)
    bad = [v for v, r in zip(variants, first) if failed(r)]

    # Retry only failed *runs* on the same code; builds are deterministic.
    run_failed = [v for v, r in zip(variants, first) if failed(r) and r["build"] == PASS]
    if run_failed and not args.no_recheck:
        attempt("retry", run_failed)

    # Same variants on the base branch, if the PRs change anything this app uses.
    differs = any(plan["base_lock"][p]["sha"] != plan["lock"][p]["sha"] for p in app.repos)
    if bad and differs and not args.no_recheck:
        try:
            checkout(plan["base_lock"], [p for p in app.repos if plan["base_lock"][p]["sha"] != plan["lock"][p]["sha"]],
                     repos_dir)
            attempt("base", bad)
        except subprocess.CalledProcessError as e:
            result["infra_error"] = f"base checkout failed: {e}"
    write()
    return 0
