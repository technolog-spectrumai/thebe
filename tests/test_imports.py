"""Tests for thebe.imports: copying host directories into <workspace>/imported/. No Qt."""

import errno
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from thebe import imports  # noqa: E402
from thebe.imports import ImportDir, ImportFailed, parse_import_dirs, plan_imports, run_imports  # noqa: E402


def tree(root: Path, files: dict) -> None:
    """Create files ({relative path: text}) under root."""
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def listing(root: Path) -> dict:
    """{relative path: text} of the regular files under root, without following links."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            found[str(path.relative_to(root))] = "<link>" if path.is_symlink() else path.read_text()
        for name in dirnames:
            if (Path(dirpath) / name).is_symlink():
                found[str((Path(dirpath) / name).relative_to(root))] = "<dir link>"
    return found


class ImportTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="thebe-test-imports-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.host = self.root / "host"
        self.workspace = self.root / "home" / "jupyter-workspace"
        self.host.mkdir()
        self.logged = []

    def plan(self, *dirs, base=None):
        return plan_imports(list(dirs), base or self.root, self.workspace)

    def copy(self, *dirs):
        plans, problems = self.plan(*dirs)
        self.assertEqual(problems, [])
        return run_imports(plans, self.workspace, self.logged.append)

    def imported(self, name=""):
        return self.workspace / "imported" / name


class ConfigTests(unittest.TestCase):
    def test_entries(self):
        dirs, problems = parse_import_dirs(["~/a", {"path": "/b", "name": "bee"}, "", None, {"path": ""}])
        self.assertEqual((dirs, problems), ([ImportDir("~/a"), ImportDir("/b", "bee")], []))
        self.assertEqual(parse_import_dirs(None), ([], []))
        self.assertEqual(parse_import_dirs([]), ([], []))

    def test_bad_entries(self):
        dirs, problems = parse_import_dirs([5, {"path": "/a", "nmae": "x"}, {"path": 3}, {"name": "x"}, "a\nb"])
        self.assertEqual(dirs, [ImportDir("/a")])
        self.assertEqual(len(problems), 5, problems)
        self.assertEqual(parse_import_dirs("/a")[1], ["import_dirs must be a list of directories (lines starting with '- ')."])

    def test_at_most_seven(self):
        dirs, problems = parse_import_dirs([f"/d{i}" for i in range(9)])
        self.assertEqual(len(dirs), imports.MAX_IMPORT_DIRS)
        self.assertEqual(problems, ["import_dirs lists 9 directories; at most 7 are copied."])

    def test_config_round_trip(self):
        from thebe.config import Config, parse_config, render_config
        config = Config()
        config.imports = [ImportDir("~/tools"), ImportDir("/opt/x y/tools", "lab tools"), ImportDir("rel/dir")]
        self.assertEqual(parse_config(render_config(config), Path("/c")), (config, []))
        self.assertIn("import_dirs: []", render_config(Config()))


class PlanTests(ImportTestCase):
    def test_limits(self):
        dirs = []
        for i in range(8):
            (self.host / f"d{i}").mkdir()
            dirs.append(ImportDir(str(self.host / f"d{i}")))
        plans, problems = self.plan(*dirs)
        self.assertEqual(len(plans), 7)
        self.assertEqual(problems, ["At most 7 directories can be imported (8 are listed)."])
        self.assertEqual(self.plan()[0:2], ([], []))            # none: nothing to do

    def test_missing_and_wrong_kinds_of_source(self):
        (self.host / "file").write_text("x")
        (self.host / "real").mkdir()
        (self.host / "link").symlink_to(self.host / "real")
        plans, problems = self.plan(ImportDir(str(self.host / "missing")), ImportDir(str(self.host / "file")),
                                    ImportDir(str(self.host / "link")))
        self.assertEqual(plans, [])
        self.assertIn("does not exist", problems[0])
        self.assertIn("is not a directory", problems[1])
        self.assertIn("is a symbolic link; select the directory it points to", problems[2])
        self.assertIn(str(self.host / "real"), problems[2])

    @unittest.skipIf(os.geteuid() == 0, "root can read every directory")
    def test_unreadable_source(self):
        closed = self.host / "closed"
        closed.mkdir(mode=0o000)
        self.addCleanup(closed.chmod, 0o700)
        self.assertIn("is not readable", self.plan(ImportDir(str(closed)))[1][0])

    def test_duplicate_names(self):
        for parent in ("a", "b"):
            (self.host / parent / "tools").mkdir(parents=True)
        a, b = ImportDir(str(self.host / "a" / "tools")), ImportDir(str(self.host / "b" / "tools"))
        plans, problems = self.plan(a, b)
        self.assertEqual(len(plans), 1)
        self.assertIn("would both be copied to imported/tools; give one of them another name", problems[0])
        plans, problems = self.plan(a, ImportDir(b.path, "tools-b"))
        self.assertEqual(([p.name for p in plans], problems), (["tools", "tools-b"], []))
        plans, problems = self.plan(ImportDir(a.path, "b"), ImportDir(str(self.host / "b"), ""))
        self.assertIn("would both be copied to imported/b", problems[0])

    def test_names(self):
        (self.host / "tools").mkdir()
        for name in ("..", "a/b", imports.MARKER, "x" * 256):
            problems = self.plan(ImportDir(str(self.host / "tools"), name))[1]
            self.assertTrue(problems and "give it another name" in problems[0], (name, problems))
        self.assertEqual(self.plan(ImportDir(str(self.host / "tools"), "My Tools é"))[1], [])

    def test_workspace_recursion_is_refused(self):
        self.workspace.mkdir(parents=True)
        (self.workspace / "project").mkdir()
        cases = {
            "the workspace": self.workspace,
            "inside": self.workspace / "project",
            "contains": self.workspace.parent,
            "via ..": self.workspace / "project" / ".." / "..",
        }
        for label, path in cases.items():
            plans, problems = self.plan(ImportDir(str(path)))
            self.assertEqual(plans, [], label)
            self.assertRegex(problems[0], "inside the workspace|contains the workspace", label)
        # Through a symlinked parent, too: the resolved paths are compared.
        (self.root / "alias").symlink_to(self.root)
        self.assertIn("contains the workspace", self.plan(ImportDir(str(self.root / "alias" / "home")))[1][0])
        self.assertIn("inside the workspace", self.plan(ImportDir(f"{self.root}/alias/home/jupyter-workspace/project"))[1][0])

    def test_relative_and_home_paths(self):
        (self.host / "tools").mkdir()
        plans, problems = plan_imports([ImportDir("tools")], self.host, self.workspace)
        self.assertEqual((plans[0].source, problems), (self.host / "tools", []))
        with mock.patch.dict(os.environ, {"HOME": str(self.host)}):
            plans, problems = plan_imports([ImportDir("~/tools")], Path("/"), self.workspace)
        self.assertEqual((plans[0].source, problems), (self.host / "tools", []))


class CopyTests(ImportTestCase):
    def setUp(self):
        super().setUp()
        self.tools = self.host / "tools"
        tree(self.tools, {"run.sh": "#!/bin/sh\necho hi\n", "lib/util.py": "X = 1\n", "lib/deep/er/data.txt": "d",
                          ".hidden": "h"})
        (self.tools / "run.sh").chmod(0o755)
        (self.tools / "empty").mkdir()

    def test_a_recursive_independent_copy(self):
        results = self.copy(ImportDir(str(self.tools)))
        dest = self.imported("tools")
        self.assertEqual(listing(dest), {"run.sh": "#!/bin/sh\necho hi\n", "lib/util.py": "X = 1\n",
                                         "lib/deep/er/data.txt": "d", ".hidden": "h",
                                         imports.MARKER: json.dumps({"source": str(self.tools)}) + "\n"})
        self.assertTrue((dest / "empty").is_dir())
        self.assertTrue(os.access(dest / "run.sh", os.X_OK))
        self.assertEqual((dest / "lib" / "util.py").stat().st_mtime_ns, (self.tools / "lib" / "util.py").stat().st_mtime_ns)
        self.assertNotEqual((dest / "run.sh").stat().st_ino, (self.tools / "run.sh").stat().st_ino)
        self.assertEqual((results[0].copied, results[0].unchanged, results[0].skipped), (4, 0, []))
        # Independent: changing the source does not change the copy until the next Deploy.
        (self.tools / "lib" / "util.py").write_text("X = 2\n")
        self.assertEqual((dest / "lib" / "util.py").read_text(), "X = 1\n")
        self.assertIn(f"  {self.tools} -> imported/tools/", self.logged)
        self.assertTrue(any("4 file(s) copied" in line for line in self.logged))

    def test_symlinks_are_skipped_and_reported(self):
        outside = self.root / "outside"
        tree(outside, {"secret.txt": "s"})
        (self.tools / "file-link").symlink_to(self.tools / "run.sh")
        (self.tools / "dir-link").symlink_to(outside)
        (self.tools / "lib" / "dangling").symlink_to(self.root / "nowhere")
        result = self.copy(ImportDir(str(self.tools)))[0]
        dest = self.imported("tools")
        for name in ("file-link", "dir-link", "lib/dangling"):
            self.assertFalse(os.path.lexists(dest / name), name)
        self.assertEqual(sorted(result.skipped), sorted([
            f"dir-link: symbolic link to {outside}", f"file-link: symbolic link to {self.tools / 'run.sh'}",
            f"lib/dangling: symbolic link to {self.root / 'nowhere'}"]))
        self.assertTrue(any("symbolic link to" in line for line in self.logged))
        for dirpath, dirnames, filenames in os.walk(self.workspace):
            for name in dirnames + filenames:
                self.assertFalse((Path(dirpath) / name).is_symlink())

    def test_a_link_to_the_workspace_inside_the_source_is_not_followed(self):
        self.workspace.mkdir(parents=True)
        (self.tools / "ws").symlink_to(self.workspace)
        result = self.copy(ImportDir(str(self.tools)))[0]
        self.assertFalse(os.path.lexists(self.imported("tools") / "ws"))
        self.assertIn(f"ws: symbolic link to {self.workspace}", result.skipped)

    def test_redeploy_updates_in_place_and_keeps_other_files(self):
        self.copy(ImportDir(str(self.tools)))
        dest = self.imported("tools")
        (dest / "notes.ipynb").write_text("mine")                 # made in JupyterLab
        again = self.copy(ImportDir(str(self.tools)))[0]
        self.assertEqual((again.copied, again.unchanged), (0, 4))
        (self.tools / "lib" / "util.py").write_text("X = 2\n")
        (self.tools / "run.sh").unlink()                           # deleted on the host
        (self.tools / "new.py").write_text("new")
        third = self.copy(ImportDir(str(self.tools)))[0]
        self.assertEqual((third.copied, third.unchanged), (2, 2))
        self.assertEqual((dest / "lib" / "util.py").read_text(), "X = 2\n")
        self.assertEqual((dest / "notes.ipynb").read_text(), "mine")
        self.assertTrue((dest / "run.sh").exists())               # never deleted
        self.assertEqual(sorted(os.listdir(self.imported())), ["tools"])      # no "tools (1)"
        self.assertEqual([n for n in os.listdir(dest) if n.endswith(".thebe-tmp")], [])

    def test_a_named_copy_and_two_sources(self):
        other = self.host / "other" / "tools"
        tree(other, {"b.txt": "b"})
        self.copy(ImportDir(str(self.tools)), ImportDir(str(other), "tools-b"))
        self.assertEqual(sorted(os.listdir(self.imported())), ["tools", "tools-b"])
        self.assertEqual((self.imported("tools-b") / "b.txt").read_text(), "b")

    def test_a_destination_of_another_source_or_a_foreign_one_is_refused(self):
        self.copy(ImportDir(str(self.tools)))
        other = self.host / "elsewhere" / "tools"
        tree(other, {"x": "x"})
        plans = self.plan(ImportDir(str(other)))[0]
        with self.assertRaisesRegex(ImportFailed, f"holds the copy of {self.tools}, not of {other}"):
            run_imports(plans, self.workspace, self.logged.append)
        self.assertFalse((self.imported("tools") / "x").exists())
        (self.imported("mine")).mkdir()
        (self.imported("mine") / "keep").write_text("k")
        mine = self.host / "mine"
        tree(mine, {"x": "x"})
        with self.assertRaisesRegex(ImportFailed, "already exists and was not made by Thebe"):
            run_imports(self.plan(ImportDir(str(mine)))[0], self.workspace, self.logged.append)
        self.assertEqual(os.listdir(self.imported("mine")), ["keep"])

    def test_links_in_the_destination_are_never_written_through(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.workspace.mkdir(parents=True)
        self.imported().symlink_to(outside)
        with self.assertRaisesRegex(ImportFailed, "not a real directory"):
            self.copy(ImportDir(str(self.tools)))
        self.assertEqual(os.listdir(outside), [])
        self.imported().unlink()
        self.copy(ImportDir(str(self.tools)))
        # A link where a source directory goes: that branch is left alone.
        target = self.imported("tools") / "lib"
        for child in sorted(target.rglob("*"), reverse=True):
            child.rmdir() if child.is_dir() else child.unlink()
        target.rmdir()
        target.symlink_to(outside)
        (self.tools / "lib" / "util.py").write_text("changed")
        result = self.copy(ImportDir(str(self.tools)))[0]
        self.assertEqual(os.listdir(outside), [])
        self.assertIn("lib/: a file or symbolic link in the copy has this name; left as it is", result.skipped)

    def test_a_failed_copy_names_the_source_and_leaves_the_rest(self):
        (self.workspace / "project").mkdir(parents=True)
        (self.workspace / "project" / "work.ipynb").write_text("keep")
        other = self.host / "second"
        tree(other, {"a.txt": "a"})
        real_write = os.write

        def failing_write(fd, data):
            if b"X = 1" in bytes(data):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        plans, problems = self.plan(ImportDir(str(other)), ImportDir(str(self.tools)))
        with mock.patch.object(imports.os, "write", failing_write):
            with self.assertRaises(ImportFailed) as caught:
                run_imports(plans, self.workspace, self.logged.append)
        message = str(caught.exception)
        self.assertIn(str(self.tools), message)
        self.assertIn("at lib/util.py", message)
        self.assertIn("No space left on device", message)
        self.assertEqual((self.workspace / "project" / "work.ipynb").read_text(), "keep")
        self.assertEqual((self.imported("second") / "a.txt").read_text(), "a")
        self.assertFalse((self.imported("tools") / "lib" / "util.py").exists())
        self.assertEqual([p.name for p in self.imported("tools").rglob("*.thebe-tmp")], [])
        # The next Deploy simply continues the interrupted copy (the marker was written first).
        self.copy(ImportDir(str(other)), ImportDir(str(self.tools)))
        self.assertEqual((self.imported("tools") / "lib" / "util.py").read_text(), "X = 1\n")

    @unittest.skipIf(os.geteuid() == 0, "root can read every file")
    def test_unreadable_entries_are_skipped(self):
        (self.tools / "lib" / "util.py").chmod(0o000)
        (self.tools / "lib" / "deep").chmod(0o000)
        self.addCleanup((self.tools / "lib" / "deep").chmod, 0o755)
        result = self.copy(ImportDir(str(self.tools)))[0]
        self.assertIn("lib/util.py: not readable", result.skipped)
        self.assertIn("lib/deep/: not readable", result.skipped)

    def test_special_files_are_skipped(self):
        os.mkfifo(self.tools / "pipe")
        result = self.copy(ImportDir(str(self.tools)))[0]
        self.assertIn("pipe: not a regular file (device, socket or pipe)", result.skipped)
        self.assertFalse(os.path.lexists(self.imported("tools") / "pipe"))

    def test_a_workspace_behind_a_symlink_and_a_missing_one(self):
        real = self.root / "disk" / "ws"
        real.mkdir(parents=True)
        self.workspace.parent.mkdir(parents=True)
        self.workspace.symlink_to(real)
        self.copy(ImportDir(str(self.tools)))
        self.assertTrue((real / "imported" / "tools" / "run.sh").is_file())
        fresh = self.root / "new" / "ws"
        run_imports(plan_imports([ImportDir(str(self.tools))], self.root, fresh)[0], fresh, self.logged.append)
        self.assertEqual(stat.S_IMODE(fresh.stat().st_mode) & 0o077, 0)       # created private, like install
        self.assertTrue((fresh / "imported" / "tools" / "lib" / "util.py").is_file())


if __name__ == "__main__":
    unittest.main()
