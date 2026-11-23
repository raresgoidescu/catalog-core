from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field

UPSTREAM_ORG = "unikraft"
TEST_SCRIPT = ".scripts/test/all.sh"

SYMLINK_RE = re.compile(r'^\s*check_exists_and_create_symlink\s+"([^"]+)"\s*$', re.M)
SYMLINK_ANY_RE = re.compile(r"^\s*check_exists_and_create_symlink\b(?!\s*\(\))", re.M)
VARIANT_RE = re.compile(r"^\s*(test_build_run|test_build)\s+([A-Za-z0-9_.-]+)\s*$", re.M)
VARIANT_ANY_RE = re.compile(r"^\s*(test_build_run|test_build)\b(?!\s*\(\))", re.M)
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class UnknownRepo(ValueError):
    pass


def repo_name_for(path: str) -> str:
    if path == "unikraft":
        return "unikraft"
    kind, _, name = path.partition("/")
    if not name or "/" in name or not NAME_RE.match(name):
        raise UnknownRepo(f"not a catalog-core repo path: {path!r}")
    if kind == "libs":
        return f"lib-{name}"
    if kind == "apps":
        return f"app-{name}"
    raise UnknownRepo(f"not a catalog-core repo path: {path!r}")


def slug_for(path: str) -> str:
    return f"{UPSTREAM_ORG}/{repo_name_for(path)}"


def path_for(repo: str) -> str:
    name = repo.rsplit("/", 1)[-1].lower()
    name = name.removesuffix(".git")
    if name == "unikraft":
        return "unikraft"
    if name.startswith("lib-") and len(name) > 4:
        return f"libs/{name[4:]}"
    if name.startswith("app-") and len(name) > 4:
        return f"apps/{name[4:]}"
    raise UnknownRepo(f"not a catalog-core dependency repo: {repo!r}")


@dataclass(frozen=True)
class Variant:
    name: str
    platform: str
    arch: str
    run: bool


@dataclass
class App:
    name: str
    repos: list[str] = field(default_factory=list)
    variants: list[Variant] = field(default_factory=list)
    has_tests: bool = False
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_app(root: str, name: str) -> App:
    app = App(name=name)
    app_dir = os.path.join(root, name)
    setup = read(os.path.join(app_dir, "setup.sh"))

    app.repos = SYMLINK_RE.findall(setup)
    if len(SYMLINK_ANY_RE.findall(setup)) != len(app.repos):
        app.problems.append('setup.sh: a check_exists_and_create_symlink call is not of the form "<path>"')
    for path in app.repos:
        try:
            repo_name_for(path)
        except UnknownRepo as e:
            app.problems.append(f"setup.sh: {e}")
    if "unikraft" not in app.repos:
        app.problems.append('setup.sh: does not link "unikraft"')

    test = os.path.join(app_dir, TEST_SCRIPT)
    if not os.path.isfile(test):
        return app
    if not os.access(test, os.X_OK):
        app.problems.append(f"{TEST_SCRIPT} is not executable")
    text = read(test)
    found = VARIANT_RE.findall(text)
    if len(VARIANT_ANY_RE.findall(text)) != len(found):
        app.problems.append(f"{TEST_SCRIPT}: a test_build line is not of the form test_build[_run] <platform>.<arch>")
    for kind, name in found:
        platform, _, arch = name.partition(".")
        if not arch:
            app.problems.append(f"{TEST_SCRIPT}: variant {name!r} is not <platform>.<arch>")
            continue
        if not os.path.isfile(os.path.join(app_dir, ".scripts/build", name)):
            app.problems.append(f"{TEST_SCRIPT}: no .scripts/build/{name}")
        runs = kind == "test_build_run"
        if runs and not os.path.isfile(os.path.join(app_dir, ".scripts/run", name)):
            app.problems.append(f"{TEST_SCRIPT}: no .scripts/run/{name}")
        app.variants.append(Variant(name, platform, arch, runs))
    app.has_tests = bool(app.variants) and os.access(test, os.X_OK)
    return app


def discover(root: str = ".") -> list[App]:
    apps = []
    for name in sorted(os.listdir(root)):
        if name.startswith(".") or name == "repos":
            continue
        if os.path.isfile(os.path.join(root, name, "setup.sh")):
            apps.append(load_app(root, name))
    return apps


def affected(apps: list[App], changed: list[str]) -> list[App]:
    selected = []
    for app in apps:
        if app.has_tests and set(changed) & set(app.repos):
            selected.append(app)
    return selected
