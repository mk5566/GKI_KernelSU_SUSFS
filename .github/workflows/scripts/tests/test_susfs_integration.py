import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import BuildConfig, SUKISU_MAIN_REVISION
from susfs_integration import MOUNT_PATCH_FILES, select_mount_patch


def section(path, addition="mount_change();"):
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1 +1,2 @@\n context\n+{addition}\n")


class MountIntegrationTests(unittest.TestCase):
    def test_selects_filesystem_changes_without_legacy_root_hooks(self):
        mount_sections = "".join(section(p) for p in sorted(MOUNT_PATCH_FILES))
        original = (section("fs/exec.c", "ksu_handle_execveat();") + mount_sections +
                    section("kernel/sys.c", "ksu_handle_setresuid();"))
        self.assertEqual(select_mount_patch(original), mount_sections)

    def test_missing_mount_file_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Missing SUSFS"):
            select_mount_patch(section("fs/Makefile"))

    def test_legacy_hook_inside_selected_file_fails_closed(self):
        original = "".join(section(p) for p in sorted(MOUNT_PATCH_FILES))
        original = original.replace("+mount_change();", "+ksu_handle_setresuid();", 1)
        with self.assertRaisesRegex(ValueError, "Legacy KernelSU hook"):
            select_mount_patch(original)

    def test_stable_and_dev_use_main_sources(self):
        self.assertEqual(BuildConfig().ksu_setup_ref, SUKISU_MAIN_REVISION)
        self.assertEqual(BuildConfig(kernelsu_version="dev").ksu_setup_ref, "main")
        self.assertEqual(BuildConfig(kernelsu_commit="deadbeef").ksu_setup_ref, "deadbeef")


if __name__ == "__main__":
    unittest.main()
