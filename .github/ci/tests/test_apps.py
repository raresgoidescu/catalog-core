import os
import stat
import tempfile
import textwrap
import unittest

from ukci import apps, repos

SETUP = textwrap.dedent("""\
    #!/bin/sh
    check_exists_and_create_symlink()
    {
        :
    }
    check_exists_and_create_symlink "unikraft"
    check_exists_and_create_symlink "libs/musl"
    """)

ALL = textwrap.dedent("""\
    #!/bin/sh
    test_build()
    {
        :
    }
    test_build_run()
    {
        :
    }
    ./setup.sh
    test_build_run qemu.x86_64
    test_build fc.arm64
    """)


def write(path, text, exe=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if exe:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


class TempCatalog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def add_app(
        self, name, setup=SETUP, all_sh=ALL, exe=True, builds=("qemu.x86_64", "fc.arm64"), runs=("qemu.x86_64",)
    ):
        write(f"{self.root}/{name}/setup.sh", setup)
        if all_sh is not None:
            write(f"{self.root}/{name}/.scripts/test/all.sh", all_sh, exe=exe)
        for b in builds:
            write(f"{self.root}/{name}/.scripts/build/{b}", "", exe=True)
        for r in runs:
            write(f"{self.root}/{name}/.scripts/run/{r}", "", exe=True)


class DiscoverTest(TempCatalog):
    def test_reads_deps_and_variants(self):
        self.add_app("hello")
        [a] = apps.discover(self.root)
        self.assertEqual(a.repos, ["unikraft", "libs/musl"])
        self.assertEqual([(v.name, v.run) for v in a.variants], [("qemu.x86_64", True), ("fc.arm64", False)])
        self.assertEqual((a.variants[1].platform, a.variants[1].arch), ("fc", "arm64"))
        self.assertTrue(a.has_tests)
        self.assertEqual(a.problems, [])

    def test_no_all_sh_means_no_tests_not_a_problem(self):
        self.add_app("zig", all_sh=None)
        [a] = apps.discover(self.root)
        self.assertFalse(a.has_tests)
        self.assertEqual(a.problems, [])

    def test_non_executable_all_sh_is_a_problem(self):
        self.add_app("x", exe=False)
        [a] = apps.discover(self.root)
        self.assertFalse(a.has_tests)
        self.assertIn(".scripts/test/all.sh is not executable", a.problems)

    def test_unparseable_symlink_call_is_reported(self):
        self.add_app("x", setup=SETUP + "check_exists_and_create_symlink libs/lwip\n")
        [a] = apps.discover(self.root)
        self.assertTrue(any("not of the form" in p for p in a.problems))

    def test_unparseable_variant_line_is_reported(self):
        self.add_app("x", all_sh=ALL + 'test_build_run "$V"\n')
        [a] = apps.discover(self.root)
        self.assertTrue(any("not of the form" in p for p in a.problems))

    def test_missing_build_or_run_script_is_reported(self):
        self.add_app("x", builds=("qemu.x86_64",), runs=())
        [a] = apps.discover(self.root)
        self.assertIn(".scripts/test/all.sh: no .scripts/build/fc.arm64", a.problems)
        self.assertIn(".scripts/test/all.sh: no .scripts/run/qemu.x86_64", a.problems)

    def test_unknown_repo_path_is_reported(self):
        self.add_app("x", setup=SETUP + 'check_exists_and_create_symlink "vendor/foo"\n')
        [a] = apps.discover(self.root)
        self.assertTrue(any("vendor/foo" in p for p in a.problems))

    def test_affected(self):
        self.add_app("a")  # unikraft + musl
        self.add_app("b", setup='check_exists_and_create_symlink "unikraft"\n')
        self.add_app("c", all_sh=None)  # no tests: never selected
        found = apps.discover(self.root)
        def names(changed):
            return [a.name for a in apps.affected(found, changed)]
        self.assertEqual(names(["libs/musl"]), ["a"])
        self.assertEqual(names(["unikraft"]), ["a", "b"])
        self.assertEqual(names(["libs/lwip"]), [])


class ReposTest(unittest.TestCase):
    def test_round_trip(self):
        for path, name in [
            ("unikraft", "unikraft"),
            ("libs/musl", "lib-musl"),
            ("libs/compiler-rt", "lib-compiler-rt"),
            ("apps/elfloader", "app-elfloader"),
        ]:
            self.assertEqual(repos.repo_name_for(path), name)
            self.assertEqual(repos.path_for(f"unikraft/{name}"), path)
            self.assertEqual(repos.path_for(f"someone/{name}"), path)  # forks map to the same path

    def test_rejects_unknown(self):
        for bad in ["catalog-core", "unikraft/catalog-core", "foo/bar"]:
            with self.assertRaises(repos.UnknownRepo):
                repos.path_for(bad)
        for bad in ["vendor/x", "libs/", "libs/a/b", "libs/$(x)"]:
            with self.assertRaises(repos.UnknownRepo):
                repos.repo_name_for(bad)


class RealCatalogTest(unittest.TestCase):
    """The real catalog-core tree must lint clean."""

    def test_repo_lints_clean(self):
        root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
        found = apps.discover(root)
        self.assertTrue(found)
        self.assertEqual([(a.name, a.problems) for a in found if a.problems], [])


if __name__ == "__main__":
    unittest.main()
