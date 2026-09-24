"""Tests for thebe.packages: checking the project's requirements.txt and copying a chosen one in."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from thebe.packages import CopyRefused, check_requirements, copy_requirements, requirements_file  # noqa: E402


class CopyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="thebe-test-packages-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dest = self.root / "repo" / "requirements.txt"
        self.source = self.root / "elsewhere" / "my-packages.txt"
        self.source.parent.mkdir()

    def test_a_valid_file_is_copied_byte_for_byte(self):
        data = b"# tools\nopencv-python\npytesseract\r\n--extra-index-url https://download.pytorch.org/whl/cpu\ntorch==2.14.0+cpu\n"
        self.source.write_bytes(data)
        self.source.chmod(0o600)
        check = copy_requirements(self.source, self.dest)
        self.assertEqual(self.dest.read_bytes(), data)
        self.assertEqual((check.path, check.exists, check.packages, check.problems), (self.dest, True, 3, []))
        self.assertEqual(stat.S_IMODE(self.dest.stat().st_mode), 0o644)
        self.assertFalse(self.dest.is_symlink())
        self.source.write_text("changed\n")                  # a copy, not a link
        self.assertEqual(self.dest.read_bytes(), data)

    def test_it_replaces_the_old_file_atomically(self):
        self.dest.parent.mkdir()
        self.dest.write_text("old\n")
        self.source.write_text("new\n")
        copy_requirements(self.source, self.dest)
        self.assertEqual(self.dest.read_text(), "new\n")
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ["requirements.txt"])

    def test_refused_files_leave_the_old_one(self):
        self.dest.parent.mkdir()
        self.dest.write_text("old\n")
        cases = {
            "-e ./src\n--target /x\n": "not accepted",
            "caf\xe9\n".encode("latin-1"): "not accepted",
            b"x" * (64 * 1024 + 1): "not accepted",
        }
        for content, message in cases.items():
            self.source.write_bytes(content if isinstance(content, bytes) else content.encode())
            with self.assertRaisesRegex(CopyRefused, message) as caught:
                copy_requirements(self.source, self.dest)
            self.assertTrue(caught.exception.problems)
            self.assertEqual(self.dest.read_text(), "old\n")
        self.assertIn("line 1: --editable is not allowed",
                      "\n".join(self._problems("-e ./src\n")))
        with self.assertRaisesRegex(CopyRefused, "does not exist"):
            copy_requirements(self.root / "missing.txt", self.dest)
        with self.assertRaisesRegex(CopyRefused, "not accepted"):
            copy_requirements(self.source.parent, self.dest)             # a directory
        with self.assertRaisesRegex(CopyRefused, "already is the project's requirements.txt"):
            copy_requirements(self.dest, self.dest)
        self.assertEqual(self.dest.read_text(), "old\n")

    def _problems(self, text):
        self.source.write_text(text)
        try:
            copy_requirements(self.source, self.dest)
        except CopyRefused as exc:
            return exc.problems
        return []

    def test_the_location_follows_the_installer(self):
        self.assertEqual(requirements_file({}), REPO / "requirements.txt")
        self.assertEqual(requirements_file({"JLT_REQUIREMENTS_FILE": "/srv/req.txt"}), Path("/srv/req.txt"))
        self.assertFalse(check_requirements(self.dest).exists)


if __name__ == "__main__":
    unittest.main()
