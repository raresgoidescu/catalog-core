from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import threading
import time

from . import apps, data, git
from .models import Attempt, Cell, Kind, LockEntry, Plan, Result, Status, TamperedPlan, load_plan

COMPILERS: dict[str, dict[str, str]] = {
    "gcc": {},
    "clang": {"CC": "clang", "CXX": "clang++"},
}

LINE_RE = re.compile(r"^\s*(build|run)\.(\S+)\s+\.\.\.\s+(PASSED|FAILED)\s*$")
CLEAN_ERROR_RE = re.compile(r"^\S+\.[A-Za-z]+:\d+(?::\d+)?: (?:fatal )?error: ")
COMPILER_ERROR_RE = re.compile(r"(?:^|\s)\S+?:\d+(?::\d+)?: (?:fatal )?error: ")
KNOWN_ERROR_RE = re.compile(r"No rule to make target|Require \S+ version")
ANY_ERROR_RE = re.compile(r"\berror\b", re.I)
MISSING_TOOL_RE = re.compile(r"command not found")
RUN_ERROR_RE = re.compile(r"^(Cannot .+|Wrong .+|No kernel file.*|.*[Tt]imed? ?out.*)$")
FETCH_ERROR_RE = re.compile(
    r"unable to resolve host address|Temporary failure in name resolution|"
    r"failed: (?:Connection (?:timed out|refused|reset)|No route to host|Network is unreachable)|"
    r"Connection reset by peer|Read error at byte|ERROR (?:429|50[0234])\b"
)
ORIGIN_RE = re.compile(r"\S*?/workdir/build/[^/\s]+/origin/")


def checkout(plan: Plan, lock: dict[str, LockEntry], paths: list[str], dest: str) -> None:
    for path in paths:
        entry = lock[path]
        target = os.path.join(dest, path)
        shutil.rmtree(target, ignore_errors=True)
        print(f"::group::{path}: {entry.repo}@{entry.sha[:12]} ({entry.source})", flush=True)
        url = f"https://github.com/{entry.repo}"
        if entry.parts:
            git.checkout_combined(url, plan.base_lock[path].sha, entry.parts, entry.sha, target)
        else:
            git.fetch_commit(url, entry.sha, target)
        print("::endgroup::", flush=True)


def variant_script(app_dir: str, variant: str) -> str:
    lines = []
    with open(os.path.join(app_dir, apps.TEST_SCRIPT), encoding="utf-8") as f:
        for line in f:
            m = apps.VARIANT_RE.match(line)
            if m and m.group(2) != variant:
                continue
            lines.append(line)
    path = os.path.join(app_dir, ".scripts/test/ci-variant.sh")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.chmod(path, 0o755)
    return "./.scripts/test/ci-variant.sh"


def first_match(regex: re.Pattern[str], lines: list[str]) -> str | None:
    for line in lines:
        if regex.search(line):
            return line
    return None


def clean(line: str, root: str) -> str:
    line = line.replace(root.rstrip("/") + "/", "")
    line = ORIGIN_RE.sub("", line).replace("//", "/")
    line = line.replace("error: #error ", "error: ")
    line = re.sub(r"(^|\s)repos/", r"\1", line)
    return line[:300]


def failure_reason(log_path: str, phase: str, root: str) -> tuple[str, bool, bool]:
    try:
        with open(log_path, errors="replace") as f:
            lines = [line.strip() for line in f if line.strip()]
    except OSError:
        return f"{phase} produced no log", False, False
    fetch = False
    missing_tool = False
    found = None
    if phase == "build":
        fetch = first_match(FETCH_ERROR_RE, lines) is not None
        found = first_match(MISSING_TOOL_RE, lines)
        missing_tool = found is not None
        for regex in (CLEAN_ERROR_RE, COMPILER_ERROR_RE, KNOWN_ERROR_RE, ANY_ERROR_RE):
            if found is None:
                found = first_match(regex, lines)
    else:
        found = first_match(RUN_ERROR_RE, lines)
    if found is None:
        found = lines[-1] if lines else f"{phase} failed with no output"
    return clean(found, root), fetch, missing_tool


def run_variant(
    app_dir: str,
    variant: apps.Variant,
    compiler: str,
    kind: Kind,
    timeout_s: int,
    run_timeout_s: int,
    sudo: bool,
    root: str,
) -> Attempt:
    env = [f"{k}={v}" for k, v in COMPILERS[compiler].items()]
    cmd = ["timeout", "-k", "10", str(timeout_s), "env", *env, variant_script(app_dir, variant.name)]
    if sudo:
        cmd = ["sudo", "-E", *cmd]
    attempt = Attempt(variant=variant.name, attempt=kind, build="MISSING", run="MISSING" if variant.run else None)
    start = time.monotonic()
    last = start
    run_expired = threading.Event()
    watchdog = None
    with subprocess.Popen(
        cmd, cwd=app_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    ) as proc:

        def stop_run() -> None:
            run_expired.set()
            proc.terminate()

        assert proc.stdout
        for line in proc.stdout:
            print(line, end="", flush=True)
            m = LINE_RE.match(line)
            if not m or m.group(2) != variant.name:
                continue
            now = time.monotonic()
            status: Status = "PASS" if m.group(3) == "PASSED" else "FAIL"
            if m.group(1) == "build":
                attempt.build = status
                attempt.build_s = round(now - last, 1)
                if variant.run:
                    watchdog = threading.Timer(run_timeout_s, stop_run)
                    watchdog.start()
            else:
                attempt.run = status
                attempt.run_s = round(now - last, 1)
            last = now
        returncode = proc.wait()
    if watchdog is not None:
        watchdog.cancel()
    if returncode in (124, 137) or run_expired.is_set():
        elapsed = round(time.monotonic() - last, 1)
        if attempt.build == "MISSING":
            attempt.build = "TIMEOUT"
            attempt.build_s = elapsed
        elif attempt.run == "MISSING":
            attempt.run = "TIMEOUT"
            attempt.run_s = elapsed
        prefix = ["sudo"] if sudo else []
        for name in ("qemu-system", "firecracker"):
            subprocess.run([*prefix, "pkill", "-9", name], check=False, capture_output=True)
    if variant.run and attempt.build != "PASS":
        attempt.run = "BLOCKED"
    attempt.total_s = round(time.monotonic() - start, 1)
    if attempt.ok:
        return attempt
    status_of_phase = attempt.build if attempt.phase == "build" else attempt.run
    if status_of_phase == "TIMEOUT":
        limit = run_timeout_s if run_expired.is_set() else timeout_s
        attempt.reason = f"{attempt.phase} timed out after {limit}s"
        return attempt
    log = os.path.join(app_dir, ".scripts/test/log", f"{attempt.phase}.{variant.name}")
    attempt.reason, attempt.fetch_error, attempt.env_error = failure_reason(log, attempt.phase, root)
    return attempt


def first_line(*cmd: str) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return "not installed"
    lines = (p.stdout or p.stderr).splitlines()
    if not lines:
        return "n/a"
    return lines[0].strip()


def tool_versions() -> dict[str, str]:
    image = f"{os.environ.get('ImageOS', '')} {os.environ.get('ImageVersion', '')}".strip()  # noqa: SIM112
    return {
        "gcc": first_line("gcc", "--version"),
        "aarch64-gcc": first_line("aarch64-linux-gnu-gcc", "--version"),
        "clang": first_line("clang", "--version"),
        "qemu": first_line("qemu-system-x86_64", "--version"),
        "qemu-aarch64": first_line("qemu-system-aarch64", "--version"),
        "firecracker": first_line(f"firecracker-{platform.machine()}", "--version"),
        "kernel": platform.release(),
        "image": image,
    }


def needs_retry(attempt: Attempt) -> bool:
    if attempt.ok or attempt.env_error:
        return False
    return attempt.phase == "run" or attempt.fetch_error


def needs_base(attempt: Attempt) -> bool:
    return not attempt.ok and not attempt.fetch_error and not attempt.env_error


def checkout_error(what: str, e: Exception) -> str:
    return f"{what} failed: {getattr(e, 'stderr', '') or e}"[:400]


class Runner:
    def __init__(self, args, plan: Plan, cell: Cell):
        self.args = args
        self.plan = plan
        self.cell = cell
        self.root = os.path.abspath(args.root)
        self.app_dir = os.path.join(self.root, args.app)
        self.repos_dir = os.path.join(self.root, "repos")
        self.out = os.path.abspath(args.out)
        self.app = apps.load_app(self.root, args.app)
        self.result = Result(app=args.app, compiler=args.compiler, plan_hash=plan.plan_hash, tools=tool_versions())

    def save(self) -> None:
        with open(os.path.join(self.out, "result.json"), "w") as f:
            f.write(self.result.model_dump_json(indent=2))

    def attempt(self, kind: Kind, variants: list[apps.Variant], in_dir: str) -> dict[str, Attempt]:
        done = {}
        logs = os.path.join(self.out, "logs", kind)
        os.makedirs(logs, exist_ok=True)
        for variant in variants:
            print(f"::group::{kind}: {self.args.app} {variant.name} ({self.args.compiler})", flush=True)
            sudo = not self.args.no_sudo
            a = run_variant(
                in_dir, variant, self.args.compiler, kind, self.args.timeout, self.args.run_timeout, sudo, self.root
            )
            print("::endgroup::", flush=True)
            for phase in ("build", "run"):
                src = os.path.join(in_dir, ".scripts/test/log", f"{phase}.{variant.name}")
                if os.path.exists(src):
                    shutil.copyfile(src, os.path.join(logs, f"{phase}.{variant.name}"))
            self.result.attempts.append(a)
            self.save()
            done[variant.name] = a
        return done

    def known_base(self, failed: list[apps.Variant]) -> list[apps.Variant]:
        repo = os.environ.get("GITHUB_REPOSITORY")
        path = data.cell_base_path(self.plan, self.cell, self.result.tools)
        if not repo or path is None:
            return failed
        known = data.fetch_base(repo, path)
        remaining = []
        for variant in failed:
            if variant.name in known:
                print(f"{variant.name}: without the PRs, from {known[variant.name].source}", flush=True)
                self.result.attempts.append(known[variant.name])
            else:
                remaining.append(variant)
        return remaining

    def base_dir(self, failed: list[apps.Variant]) -> tuple[str, list[apps.Variant]]:
        if self.plan.catalog_base is None:
            return self.app_dir, failed
        catalog_dir = os.path.join(self.root, ".ci-base-catalog")
        shutil.rmtree(catalog_dir, ignore_errors=True)
        git.fetch_commit(f"https://github.com/{self.plan.catalog_base.repo}", self.plan.catalog_base.sha, catalog_dir)
        os.symlink(self.repos_dir, os.path.join(catalog_dir, "repos"))
        app_dir = os.path.join(catalog_dir, self.args.app)
        existing = set()
        if os.path.isfile(os.path.join(app_dir, apps.TEST_SCRIPT)):
            existing = {v.name for v in apps.load_app(catalog_dir, self.args.app).variants}
        self.result.new_variants = sorted(v.name for v in failed if v.name not in existing)
        return app_dir, [v for v in failed if v.name in existing]

    def run(self) -> int:
        status = self.run_all()
        self.save()
        return status

    def run_all(self) -> int:
        variants = self.app.variants
        if self.args.variants:
            wanted = self.args.variants.split(",")
            variants = [v for v in variants if v.name in wanted]
        try:
            checkout(self.plan, self.plan.lock, self.cell.repos, self.repos_dir)
        except (subprocess.CalledProcessError, RuntimeError) as e:
            self.result.infra_error = checkout_error("checkout", e)
            return 2
        final = self.attempt("first", variants, self.app_dir)
        if self.args.no_recheck:
            return 0
        retry = [v for v in variants if needs_retry(final[v.name])]
        final.update(self.attempt("retry", retry, self.app_dir))
        failed = [v for v in variants if needs_base(final[v.name])]
        if not failed or not self.plan.has_base(self.cell):
            return 0
        failed = self.known_base(failed)
        if not failed:
            return 0
        changed = [path for path in self.cell.repos if self.plan.base_lock[path].sha != self.plan.lock[path].sha]
        try:
            checkout(self.plan, self.plan.base_lock, changed, self.repos_dir)
            app_dir, failed = self.base_dir(failed)
            base = self.attempt("base", failed, app_dir)
            again = [v for v in failed if final[v.name].phase == "run" and needs_retry(base[v.name])]
            self.attempt("base_retry", again, app_dir)
        except (subprocess.CalledProcessError, RuntimeError) as e:
            self.result.infra_error = checkout_error("checkout without the PRs", e)
        return 0


def main(args) -> int:
    try:
        plan = load_plan(args.plan)
    except TamperedPlan as e:
        print(f"::error::{e}; not running anything from it")
        return 1
    cell = next((c for c in plan.cells if c.app == args.app and c.compiler == args.compiler), None)
    if cell is None:
        raise SystemExit(f"{args.app}/{args.compiler} is not in the plan")
    os.makedirs(os.path.abspath(args.out), exist_ok=True)
    return Runner(args, plan, cell).run()
