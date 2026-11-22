"""Combining PRs, against real git repositories."""

import os
import subprocess

import pytest

from ukci import git
from ukci.models import Part


def sh(cwd, *args):
    env = dict(
        os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"
    )
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A base commit and three branches: a and b touch different files, c the same line as a."""
    src = tmp_path / "src"
    src.mkdir()
    sh(src, "init", "-q", "-b", "staging")
    sh(src, "config", "uploadpack.allowAnySHA1InWant", "true")
    (src / "f.txt").write_text("one\n")
    (src / "g.txt").write_text("two\n")
    sh(src, "add", "-A")
    sh(src, "commit", "-q", "-m", "base")
    heads = {"base": sh(src, "rev-parse", "HEAD")}
    for name, file, text in [("a", "f.txt", "one-a\n"), ("b", "g.txt", "two-b\n"), ("c", "f.txt", "one-c\n")]:
        sh(src, "checkout", "-q", "-b", name, heads["base"])
        (src / file).write_text(text)
        sh(src, "commit", "-q", "-am", name)
        heads[name] = sh(src, "rev-parse", "HEAD")
    heads["url"] = f"file://{src}"
    return heads


def test_clean_merge_is_reproducible_and_verified(repo, tmp_path):
    parts = [Part(label="x#1", sha=repo["a"]), Part(label="x#2", sha=repo["b"])]
    first = git.combine(repo["url"], repo["base"], parts, str(tmp_path / "p1"))
    assert git.combine(repo["url"], repo["base"], parts, str(tmp_path / "p2")) == first  # same SHA every time
    git.checkout_combined(repo["url"], repo["base"], parts, first, str(tmp_path / "t"))
    assert (tmp_path / "t/f.txt").read_text() == "one-a\n" and (tmp_path / "t/g.txt").read_text() == "two-b\n"


def test_conflict_names_the_prs_and_files(repo, tmp_path):
    parts = [Part(label="x#1", sha=repo["a"]), Part(label="x#3", sha=repo["c"])]
    with pytest.raises(git.Conflict) as e:
        git.combine(repo["url"], repo["base"], parts, str(tmp_path / "p"))
    assert (e.value.merged, e.value.part, e.value.files) == (["x#1"], "x#3", ["f.txt"])


def test_checkout_refuses_a_different_result(repo, tmp_path):
    with pytest.raises(RuntimeError, match="the plan expected"):
        git.checkout_combined(
            repo["url"], repo["base"], [Part(label="x#1", sha=repo["a"])], "0" * 40, str(tmp_path / "t")
        )
