"""Exercise the byte-integrity guard with synthetic Git fixtures, not kernel C."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import BuildConfig
from kernel_builder import KernelBuilder
from patch_policy import PROTECTED_SOURCE_PATHS, FORBIDDEN_SOURCE_PATHS
from source_safety import verify_upstream_sources


class SourceSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source safety ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.common = self.root / "common"
        self.common.mkdir()
        self.report = self.root / "source-safety.json"
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        for relative in PROTECTED_SOURCE_PATHS:
            path = self.common / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"synthetic fixture for {relative}\n".encode("utf-8"))
        self.git("add", ".")
        self.commit()
        self.revision = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.common), *args],
                              text=True, capture_output=True, check=True).stdout

    def commit(self):
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture")

    def check(self):
        return verify_upstream_sources(self.common, self.revision, self.report)

    def change(self, relative="arch/arm64/lib/memcmp.S"):
        path = self.common / relative
        path.write_bytes(path.read_bytes() + b"unreviewed change\n")
        return path

    def test_clean_sources_generate_a_complete_pass_report(self):
        report = self.check()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["baseline_revision"], self.revision)
        self.assertEqual(set(report["files"]), set(PROTECTED_SOURCE_PATHS))
        for entry in report["files"].values():
            self.assertEqual(entry["actual_sha256"], entry["expected_sha256"])
        self.assertEqual(json.loads(self.report.read_text()), report)

    def test_changes_to_every_protected_path_are_detected(self):
        for relative in PROTECTED_SOURCE_PATHS:
            path = self.common / relative
            original = path.read_bytes()
            self.change(relative)
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, "differs"):
                self.check()
            path.write_bytes(original)

    def test_staged_source_change_is_not_hidden_by_clean_worktree_diff(self):
        self.change()
        self.git("add", ".")
        self.assertEqual(self.git("diff"), "")
        with self.assertRaisesRegex(RuntimeError, "differs"):
            self.check()

    def test_helper_commit_does_not_redefine_the_upstream_baseline(self):
        self.change()
        self.git("add", ".")
        self.commit()
        self.assertNotEqual(self.git("rev-parse", "HEAD").strip(), self.revision)
        self.assertEqual(self.git("status", "--porcelain"), "")
        with self.assertRaisesRegex(RuntimeError, "differs"):
            self.check()

    def test_deleted_protected_source_is_rejected(self):
        (self.common / PROTECTED_SOURCE_PATHS[0]).unlink()
        with self.assertRaisesRegex(RuntimeError, "source safety"):
            self.check()

    def test_missing_baseline_file_is_not_assumed_safe(self):
        path = self.common / PROTECTED_SOURCE_PATHS[0]
        original = path.read_bytes()
        self.git("rm", PROTECTED_SOURCE_PATHS[0])
        self.commit()
        self.revision = self.git("rev-parse", "HEAD").strip()
        path.write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, "source safety"):
            self.check()

    def test_symlink_to_even_identical_bytes_is_rejected(self):
        path = self.common / PROTECTED_SOURCE_PATHS[0]
        target = self.root / "same-bytes"
        target.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable on this host: {error}")
        with self.assertRaisesRegex(RuntimeError, "Symlink"):
            self.check()

    def test_symlinked_source_directory_is_rejected(self):
        original = self.common / "net/ipv4"
        target = self.root / "moved-ipv4"
        shutil.move(str(original), target)
        try:
            original.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable on this host: {error}")
        with self.assertRaisesRegex(RuntimeError, "Symlink"):
            self.check()

    def test_untracked_retired_backport_sources_are_rejected(self):
        for relative in FORBIDDEN_SOURCE_PATHS:
            path = self.common / relative
            path.write_bytes(b"untracked backport\n")
            with self.subTest(relative=relative), self.assertRaisesRegex(RuntimeError, "Retired"):
                self.check()
            path.unlink()

    def test_dangling_symlink_at_retired_backport_path_is_rejected(self):
        path = self.common / FORBIDDEN_SOURCE_PATHS[0]
        try:
            path.symlink_to(self.root / "does-not-exist")
        except OSError as error:
            self.skipTest(f"Symlink creation unavailable on this host: {error}")
        with self.assertRaisesRegex(RuntimeError, "Retired"):
            self.check()

    def test_invalid_and_missing_revision_fail_closed(self):
        for revision in (None, "HEAD", "main", "a" * 39, "f" * 40):
            with self.subTest(revision=revision), self.assertRaisesRegex(RuntimeError, "source safety"):
                verify_upstream_sources(self.common, revision)

    def test_blob_object_cannot_be_used_as_baseline_commit(self):
        blob = self.git("rev-parse", f"HEAD:{PROTECTED_SOURCE_PATHS[0]}").strip()
        with self.assertRaisesRegex(RuntimeError, "not a Git commit"):
            verify_upstream_sources(self.common, blob)

    def test_failure_overwrites_previous_pass_evidence(self):
        self.check()
        self.change()
        with self.assertRaises(RuntimeError):
            self.check()
        report = json.loads(self.report.read_text())
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["errors"])

    def test_unrelated_required_integration_changes_remain_possible(self):
        path = self.common / "fs/readdir.c"
        path.parent.mkdir(exist_ok=True)
        path.write_text("synthetic unrelated integration\n")
        self.assertEqual(self.check()["status"], "passed")

    def test_guard_is_bound_into_builder_with_the_expected_revision(self):
        builder = KernelBuilder(BuildConfig(sub_level="211", os_patch_level="2026-09"),
                                str(self.root), expected_common_revision=self.revision)
        builder.work_dir = self.root
        self.assertEqual(builder._verify_patch_safety()["status"], "passed")
        self.change("kernel/module.c")
        with self.assertRaisesRegex(RuntimeError, "differs"):
            builder._verify_patch_safety()

    def test_builder_without_pinned_source_fails_closed(self):
        builder = KernelBuilder(BuildConfig(sub_level="211", os_patch_level="2026-09"), str(self.root))
        builder.work_dir = self.root
        with self.assertRaisesRegex(RuntimeError, "initially synced"):
            builder._verify_patch_safety()


if __name__ == "__main__":
    unittest.main()
