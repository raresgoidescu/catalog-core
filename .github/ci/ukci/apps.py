"""Discover apps and what they need, from the app directories themselves.

An app is a top-level directory with a ``setup.sh``. From it we read:

* dependencies: the ``check_exists_and_create_symlink "<path>"`` calls in
  ``setup.sh`` -- exactly what the app links into its ``workdir/``;
* variants: the ``test_build <variant>`` / ``test_build_run <variant>``
  lines in ``.scripts/test/all.sh`` -- build-only vs build-and-run.

An app without an executable ``.scripts/test/all.sh`` has no tests; it is
reported as such, not as a failure.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable

from . import repos

SYMLINK_RE = re.compile(r'^\s*check_exists_and_create_symlink\s+"([^"]+)"\s*$', re.M)
# Any mention of the helper, used to catch calls we fail to parse.
SYMLINK_ANY_RE = re.compile(r"^\s*check_exists_and_create_symlink\b(?!\s*\(\))", re.M)
VARIANT_RE = re.compile(r"^\s*(test_build_run|test_build)\s+([A-Za-z0-9_.-]+)\s*$", re.M)
VARIANT_ANY_RE = re.compile(r"^\s*(test_build_run|test_build)\b(?!\s*\(\))", re.M)

TEST_SCRIPT = ".scripts/test/all.sh"


@dataclass(frozen=True)
class Variant:
    name: str          # e.g. "qemu.x86_64"
    platform: str      # "qemu" | "fc" | "xen" | ...
    arch: str          # "x86_64" | "arm64" | ...
    run: bool          # True: build and run; False: build only


@dataclass
class App:
    name: str
    repos: list[str] = field(default_factory=list)        # local paths, e.g. ["unikraft", "libs/musl"]
    variants: list[Variant] = field(default_factory=list)
    has_tests: bool = False
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _parse_variant(name: str) -> tuple[str, str]:
    platform, _, arch = name.partition(".")
    return platform, arch


def load_app(root: str, name: str) -> App:
    app = App(name=name)
    setup = os.path.join(root, name, "setup.sh")
    text = _read(setup)

    app.repos = SYMLINK_RE.findall(text)
    if len(SYMLINK_ANY_RE.findall(text)) != len(app.repos):
        app.problems.append(
            'setup.sh: a check_exists_and_create_symlink call is not of the form '
            'check_exists_and_create_symlink "<path>"'
        )
    for path in app.repos:
        try:
            repos.repo_name_for(path)
        except repos.UnknownRepo as e:
            app.problems.append(f"setup.sh: {e}")
    if "unikraft" not in app.repos:
        app.problems.append('setup.sh: does not link "unikraft"')

    test = os.path.join(root, name, TEST_SCRIPT)
    if os.path.isfile(test):
        if not os.access(test, os.X_OK):
            app.problems.append(f"{TEST_SCRIPT} is not executable")
        ttext = _read(test)
        found = VARIANT_RE.findall(ttext)
        if len(VARIANT_ANY_RE.findall(ttext)) != len(found):
            app.problems.append(
                f"{TEST_SCRIPT}: a test_build/test_build_run line is not of the form "
                "test_build[_run] <platform>.<arch>"
            )
        for kind, vname in found:
            platform, arch = _parse_variant(vname)
            if not arch:
                app.problems.append(f"{TEST_SCRIPT}: variant {vname!r} is not <platform>.<arch>")
                continue
            if not os.path.isfile(os.path.join(root, name, ".scripts/build", vname)):
                app.problems.append(f"{TEST_SCRIPT}: no .scripts/build/{vname}")
            if kind == "test_build_run" and not os.path.isfile(os.path.join(root, name, ".scripts/run", vname)):
                app.problems.append(f"{TEST_SCRIPT}: no .scripts/run/{vname}")
            app.variants.append(Variant(vname, platform, arch, kind == "test_build_run"))
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


def all_repo_paths(apps: Iterable[App]) -> list[str]:
    return sorted({p for a in apps for p in a.repos})


def affected(apps: Iterable[App], changed: Iterable[str]) -> list[App]:
    """Apps with tests that link at least one of the changed repo paths."""
    changed = set(changed)
    return [a for a in apps if a.has_tests and changed.intersection(a.repos)]
