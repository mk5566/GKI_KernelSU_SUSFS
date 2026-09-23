import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from patch_utils import apply_patch_exact, read_patch_order
from config import BuildConfig
from kernel_builder import KernelBuilder

DIFF = "--- a/one\n+++ b/one\n@@ -1 +1 @@\n-old\n+new\n--- a/two\n+++ b/two\n@@ -1 +1 @@\n-old\n+new\n"

class PatchSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="patch audit ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tree = self.root / "tree with spaces"
        self.tree.mkdir()
        self.patch = self.root / "two files.patch"
        self.patch.write_text(DIFF)
        for name in ("one", "two"):
            (self.tree / name).write_text("old\n")
    def test_exact_application(self):
        self.assertEqual(apply_patch_exact(self.tree, self.patch), "applied")
        self.assertEqual((self.tree / "two").read_text(), "new\n")
    def test_wholly_applied_is_idempotent(self):
        apply_patch_exact(self.tree, self.patch)
        self.assertEqual(apply_patch_exact(self.tree, self.patch), "already-applied")
    def test_mixed_applied_and_broken_is_not_success(self):
        (self.tree / "one").write_text("new\n")
        (self.tree / "two").write_text("different\n")
        with self.assertRaises(RuntimeError):
            apply_patch_exact(self.tree, self.patch)
        self.assertEqual((self.tree / "two").read_text(), "different\n")
    def test_mixed_applied_and_unapplied_is_not_success(self):
        (self.tree / "one").write_text("new\n")
        with self.assertRaises(RuntimeError):
            apply_patch_exact(self.tree, self.patch)
        self.assertEqual((self.tree / "two").read_text(), "old\n")
    def test_failed_multi_file_apply_changes_nothing(self):
        (self.tree / "two").write_text("different\n")
        with self.assertRaises(RuntimeError):
            apply_patch_exact(self.tree, self.patch)
        self.assertEqual((self.tree / "one").read_text(), "old\n")
    def test_context_whitespace_is_not_ignored(self):
        self.patch.write_text("--- a/one\n+++ b/one\n@@ -1,2 +1,2 @@\n context\n-old\n+new\n")
        (self.tree / "one").write_text(" context\nold\n")
        with self.assertRaises(RuntimeError):
            apply_patch_exact(self.tree, self.patch)
    def manifest(self, text):
        (self.root / "APPLY_ORDER.txt").write_text(text)
        (self.root / "safe.patch").write_text(DIFF)
        return read_patch_order(self.root)
    def test_manifest_required_and_optional(self):
        entries = self.manifest("# note\n!safe.patch\n")
        self.assertTrue(entries[0][1])
    def test_manifest_duplicates_rejected(self):
        with self.assertRaises(ValueError): self.manifest("safe.patch\n!safe.patch\n")
    def test_manifest_traversal_rejected(self):
        with self.assertRaises(ValueError): self.manifest("../safe.patch\n")
    def test_manifest_symlink_rejected(self):
        try:
            (self.root / "link.patch").symlink_to(self.patch)
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable on this host: {error}")
        with self.assertRaises(ValueError): self.manifest("link.patch\n")
    def test_manifest_missing_rejected(self):
        with self.assertRaises(ValueError): read_patch_order(self.root)
    def test_manifest_missing_patch_rejected(self):
        with self.assertRaises(ValueError): self.manifest("missing.patch\n")
    def test_comment_only_manifest_is_valid(self):
        self.assertEqual(self.manifest("# baseline: no optional kernel patches\n"), [])
    def test_stale_clone_rejected(self):
        builder = KernelBuilder(BuildConfig(sub_level="211", os_patch_level="2026-09"), str(self.root))
        repo = self.root / "checkout"
        (repo / ".git").mkdir(parents=True)
        with patch.object(builder, "_git_head", return_value="a" * 40), \
             patch.object(builder, "_assert_clean_repo"), \
             patch.object(builder, "_run_cmd", return_value=subprocess.CompletedProcess([], 1, "", "offline")):
            with self.assertRaisesRegex(RuntimeError, "refusing stale"):
                builder._clone_or_update("source", repo, "https://example.invalid/repo")

if __name__ == "__main__": unittest.main()
