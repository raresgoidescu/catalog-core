import os
import subprocess
import tempfile
import unittest

from ukci import combine


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                   GIT_COMMITTER_EMAIL="t@t")).stdout.strip()


class CombineTest(unittest.TestCase):
    """A repo with a base commit and three branches: a and b touch different
    files, c changes the same line as a."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src")
        os.makedirs(self.src)
        git(self.src, "init", "-q", "-b", "staging")
        git(self.src, "config", "uploadpack.allowAnySHA1InWant", "true")
        self.write("f.txt", "one\n")
        self.write("g.txt", "two\n")
        self.base = self.commit("base")
        self.a = self.branch("a", "f.txt", "one-a\n")
        self.b = self.branch("b", "g.txt", "two-b\n")
        self.c = self.branch("c", "f.txt", "one-c\n")
        self.url = "file://" + self.src

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        with open(os.path.join(self.src, name), "w") as f:
            f.write(text)

    def commit(self, msg):
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", msg)
        return git(self.src, "rev-parse", "HEAD")

    def branch(self, name, file, text):
        git(self.src, "checkout", "-q", "-b", name, self.base if hasattr(self, "base") else "staging")
        self.write(file, text)
        sha = self.commit(name)
        git(self.src, "checkout", "-q", "staging")
        return sha

    def dest(self, name):
        return os.path.join(self.tmp.name, name)

    def test_clean_merge_is_reproducible_and_verified(self):
        parts = [combine.Part("x#1", self.a), combine.Part("x#2", self.b)]
        first = combine.build(self.url, self.base, parts, self.dest("p1"))
        again = combine.build(self.url, self.base, parts, self.dest("p2"))
        self.assertEqual(first, again)                       # same SHA every time
        combine.checkout(self.url, self.base, parts, first, self.dest("t"))
        with open(os.path.join(self.dest("t"), "f.txt")) as f1, open(os.path.join(self.dest("t"), "g.txt")) as f2:
            self.assertEqual((f1.read(), f2.read()), ("one-a\n", "two-b\n"))

    def test_conflict_names_prs_and_files(self):
        with self.assertRaises(combine.Conflict) as cm:
            combine.build(self.url, self.base, [combine.Part("x#1", self.a), combine.Part("x#3", self.c)], self.dest("p"))
        self.assertEqual((cm.exception.first, cm.exception.second, cm.exception.files), ("base + x#1", "x#3", ["f.txt"]))

    def test_checkout_refuses_a_different_result(self):
        parts = [combine.Part("x#1", self.a)]
        with self.assertRaises(RuntimeError):
            combine.checkout(self.url, self.base, parts, "0" * 40, self.dest("t"))


if __name__ == "__main__":
    unittest.main()
