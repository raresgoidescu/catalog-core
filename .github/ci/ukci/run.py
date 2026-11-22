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
from . import combine
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
        url = f"https://github.com/{entry['repo']}"
        if "combine" in entry:
            c = entry["combine"]
            combine.checkout(url, c["base_sha"], [combine.Part(x["label"], x["sha"]) for x in c["parts"]],
                             entry["sha"], target)
        else:
            fetch_commit(url, entry["sha"], target)
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


COMPILER_ERROR_RE = re.compile(r"(?:^|\s)(\S+?):\d+(?::\d+)?: (?:fatal )?error: (.+)$")
OTHER_ERROR_RE = re.compile(r"command not found|No rule to make target|Require \S+ version|unknown C compiler|\*\*\* ")
RUN_REASON_RE = re.compile(r"^(Cannot .+|Wrong message.*|Wrong .+ from .+|No kernel file.*|.*[Tt]imed? ?out.*)$")
# Libraries download their sources while building; a failed download is not the PR's fault.
FETCH_RE = re.compile(r"(?:wget|curl)(?:: |\[\d+\]: )|Could not resolve host|Unable to (?:fetch|download)|"
                      r"Connection (?:timed out|refused|reset)|ERROR 50[0-9]|ERROR 429|Temporary failure in name resolution", re.I)


def failure_reason(log_path: str, phase: str, root: str) -> tuple[str, str, bool]:
    """(reason for people, signature for comparing, looks like a download failure)."""
    try:
        with open(log_path, errors="replace") as f:
            lines = [line.rstrip() for line in f]
    except OSError:
        return f"{phase} produced no log", f"{phase}:no-log", False
    prefix = root.rstrip("/") + "/"
    clean = [line.replace(prefix, "").replace("//", "/") for line in lines if line.strip()]
    reason = None
    if phase == "build":
        for line in clean:
            if m := COMPILER_ERROR_RE.search(line):
                path = re.sub(r"^.*?/workdir/build/[^/]+/origin/", "", m.group(1))   # unpacked library sources
                path = re.sub(r"^repos/", "", path)
                reason = f"{path}: {m.group(2).strip()}"
                break
        reason = reason or next((line for line in clean if OTHER_ERROR_RE.search(line)), None)
    else:
        reason = next((line for line in clean if RUN_REASON_RE.match(line)), None)
    reason = (reason or (clean[-1] if clean else f"{phase} failed with no output"))[:300]
    fetch = any(FETCH_RE.search(line) for line in clean[-40:])
    signature = f"{phase}:" + re.sub(r":\d+(?::\d+)?\b|0x[0-9a-f]+|\d{3,}", "", reason)
    return reason, signature, fetch


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
    sudo = (os.environ.get("CI_SUDO") or "1") == "1"
    timeout_s = int(os.environ.get("CI_VARIANT_TIMEOUT") or "1200")

    result = {"app": args.app, "compiler": args.compiler, "plan_hash": plan["plan_hash"],
              "tools": tool_versions(), "attempts": [], "infra_error": None}

    def write():
        with open(os.path.join(out, "result.json"), "w") as f:
            json.dump(result, f, indent=2)

    repos_dir = os.path.join(root, "repos")
    try:
        checkout(plan["lock"], app.repos, repos_dir)
    except (subprocess.CalledProcessError, RuntimeError) as e:
        result["infra_error"] = f"checkout failed: {e}"
        write()
        return 2

    def attempt(kind: str, vs: list[apps_mod.Variant], in_dir: str = app_dir) -> list[dict]:
        rs = []
        for v in vs:
            print(f"::group::{kind}: {args.app} {v.name} ({args.compiler})", flush=True)
            r = run_variant(in_dir, v, args.compiler, timeout_s, sudo)
            print("::endgroup::", flush=True)
            r["attempt"] = kind
            if failed(r):
                phase = "build" if r["build"] != PASS else "run"
                if r[phase] == TIMEOUT:
                    r["reason"], r["signature"], r["fetch_error"] = f"{phase} timed out", f"{phase}:timeout", False
                else:
                    r["reason"], r["signature"], r["fetch_error"] = failure_reason(
                        os.path.join(in_dir, ".scripts/test/log", f"{phase}.{v.name}"), phase, root)
            save_logs(in_dir, out, kind, v.name)
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

    # Same variants without the PRs: dependency repos at base_lock and, for a
    # catalog-core PR, the app as it is on catalog-core's base branch.
    changed_repos = [p for p in app.repos if plan["base_lock"][p]["sha"] != plan["lock"][p]["sha"]]
    catalog_base = plan.get("catalog_base")
    if bad and (changed_repos or catalog_base) and not args.no_recheck:
        try:
            checkout(plan["base_lock"], changed_repos, repos_dir)
            base_dir, base_bad = app_dir, bad
            if catalog_base:
                bdir = os.path.join(root, ".ci-base-catalog")
                if os.path.isdir(bdir):
                    shutil.rmtree(bdir)
                fetch_commit(f"https://github.com/{catalog_base['repo']}", catalog_base["sha"], bdir)
                os.symlink(repos_dir, os.path.join(bdir, "repos"))
                base_dir = os.path.join(bdir, args.app)
                names = {v.name for v in bad}
                base_bad = ([v for v in apps_mod.load_app(bdir, args.app).variants if v.name in names]
                            if os.path.isfile(os.path.join(base_dir, apps_mod.TEST_SCRIPT)) else [])
            attempt("base", base_bad, base_dir)    # a variant new in this PR has no base: stays FAIL
        except (subprocess.CalledProcessError, RuntimeError) as e:
            result["infra_error"] = f"base checkout failed: {e}"
    write()
    return 0
