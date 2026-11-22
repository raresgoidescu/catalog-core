"""Mapping between catalog-core's local repo paths and GitHub repositories.

Apps refer to their dependencies by local path (``unikraft``,
``libs/musl``, ``apps/elfloader``), as linked by their ``setup.sh``.
The GitHub repository follows from a naming convention, so there is no
list of repositories to maintain:

    unikraft      <->  <org>/unikraft
    libs/<name>   <->  <org>/lib-<name>
    apps/<name>   <->  <org>/app-<name>
"""

from __future__ import annotations

import re

UPSTREAM_ORG = "unikraft"

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class UnknownRepo(ValueError):
    pass


def repo_name_for(path: str) -> str:
    """Local path -> GitHub repository name (without owner)."""
    if path == "unikraft":
        return "unikraft"
    kind, _, name = path.partition("/")
    if not name or "/" in name or not _NAME_RE.match(name):
        raise UnknownRepo(f"not a catalog-core repo path: {path!r}")
    if kind == "libs":
        return f"lib-{name}"
    if kind == "apps":
        return f"app-{name}"
    raise UnknownRepo(f"not a catalog-core repo path: {path!r}")


def slug_for(path: str, org: str = UPSTREAM_ORG) -> str:
    return f"{org}/{repo_name_for(path)}"


def path_for(repo: str) -> str:
    """GitHub repository (``owner/name`` or ``name``) -> local path.

    The owner is ignored on purpose: a fork (``someone/lib-musl``) maps to
    the same local path as upstream. Whether an owner is *allowed* is a
    policy decision made by the caller.
    """
    name = repo.rsplit("/", 1)[-1].lower()
    if name.endswith(".git"):
        name = name[:-4]
    if name == "unikraft":
        return "unikraft"
    if name.startswith("lib-") and len(name) > 4:
        return f"libs/{name[4:]}"
    if name.startswith("app-") and len(name) > 4:
        return f"apps/{name[4:]}"
    raise UnknownRepo(f"not a catalog-core dependency repo: {repo!r}")
