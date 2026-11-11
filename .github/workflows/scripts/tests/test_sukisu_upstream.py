"""Upstream glue patches stay consistent and fail closed on drift."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import BuildConfig, REPO_ROOT
from kernel_builder import KernelBuilder


QUIET_PATCH = REPO_ROOT / "patches/susfs/0001-sukisu-quiet-production.patch"

# Minimal reconstructions of the upstream `builtin` branch sources around
# each glue anchor. The context lines are byte-identical to upstream
# de9bc7e6; any upstream change to them must fail the patch closed.
BUILTIN_MAKEFILE = """obj-$(CONFIG_KSU) := ksu.o
ksu-y := ksu_main.o

ccflags-y += -I$(srctree)/security/selinux -I$(srctree)/security/selinux/include
ccflags-y += -I$(objtree)/security/selinux -include $(srctree)/include/uapi/asm-generic/errno.h

ifeq ($(filter /%,$(src)),)
KSU_KERNEL_DIR := $(srctree)/$(src)
else
KSU_KERNEL_DIR := $(src)
endif
"""

BUILTIN_DISPATCH = """#ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT
        case CMD_SUSFS_ADD_OPEN_REDIRECT:
            susfs_add_open_redirect(arg);
            return 0;
#endif // #ifdef CONFIG_KSU_SUSFS_OPEN_REDIRECT
#ifdef CONFIG_KSU_SUSFS_SUS_MAP
        case CMD_SUSFS_ADD_SUS_MAP:
            susfs_add_sus_map(arg);
            return 0;
#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MAP
#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER
        case CMD_SUSFS_SET_PRIORITY_UID_FILTER:
            susfs_set_priority_uid_filter(arg);
            return 0;
#endif // #ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER
        case CMD_SUSFS_ENABLE_AVC_LOG_SPOOFING:
            susfs_set_avc_log_spoofing(arg);
            return 0;
"""


class BuiltinIntegrationTests(unittest.TestCase):
    def test_no_custom_kernelsu_shim_may_reappear(self):
        # The hand-rolled KernelSU-side SUSFS shim rotted on every upstream
        # move. The `builtin` branch owns that integration now; only small
        # fail-closed glue patches remain.
        for missing in ("0001-sukisu-main-mount-support.patch",
                        "0002-common-susfs-reboot-dispatch.patch",
                        "0003-sukisu-quiet-production.patch",
                        "0002-sukisu-no-priority-uid-filter.patch"):
            self.assertFalse(
                (REPO_ROOT / "patches/susfs" / missing).exists(), missing)
        for present in ("0001-sukisu-quiet-production.patch",
                        "0003-susfs-no-priority-uid-filter.patch"):
            self.assertTrue(
                (REPO_ROOT / "patches/susfs" / present).is_file(), present)
        self.assertFalse(
            (REPO_ROOT / ".github/workflows/scripts/susfs_integration.py").exists())

    def _apply_to_tree(self, root: Path, files: dict, patch: Path) -> None:
        for relative, text in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        builder = KernelBuilder(BuildConfig(), str(root))
        builder.shell.cwd = str(root)
        builder._apply_patch_file(patch)

    def test_quiet_patch_includes_srctree_quiet_header_once(self):
        # $(KSU_KERNEL_DIR) expanded empty in these flag evaluations and broke
        # every KernelSU object; the include must use the always-defined
        # $(srctree) path through the setup.sh symlink instead.
        for extra in ("", "\nksu-y += future.o\n"):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self._apply_to_tree(
                    root, {"kernel/Makefile": BUILTIN_MAKEFILE + extra},
                    QUIET_PATCH)
                text = (root / "kernel/Makefile").read_text(encoding="utf-8")
                flag = ("ccflags-y += -include "
                        "$(srctree)/drivers/kernelsu/include/quiet.h\n")
                self.assertEqual(text.count(flag), 1)
                self.assertNotIn("KSU_KERNEL_DIR)/include/quiet.h", text)
                self.assertEqual(
                    text.replace(flag, ""), BUILTIN_MAKEFILE + extra)
                self.assertTrue((root / "kernel/include/quiet.h").is_file())

    def test_upstream_builtin_natively_guards_priority_uid_filter(self):
        # SukiSU-Ultra builtin commit de9bc7e (issue #1045) natively adds
        # CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER to Kconfig and guards dispatch.c
        # so mount-only compiles warning-free without custom glue patches.
        self.assertIn("CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER", BUILTIN_DISPATCH)

    def test_reapplying_glue_patches_is_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {"kernel/Makefile": BUILTIN_MAKEFILE}
            for relative, text in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="\n")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            builder = KernelBuilder(BuildConfig(), str(temp))
            builder.shell.cwd = str(temp)
            builder._apply_patch_file(QUIET_PATCH)
            with self.assertRaisesRegex(RuntimeError, "Selected patch failed"):
                builder._apply_patch_file(QUIET_PATCH)

    @unittest.skipUnless(os.environ.get("SUKISU_TEST_SOURCE"),
                         "set SUKISU_TEST_SOURCE to check the complete upstream source")
    def test_glue_patches_apply_to_upstream_builtin_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = KernelBuilder(BuildConfig(), temp)
            checkout = builder.work_dir / "KernelSU"
            # Clone HEAD so an existing source's working changes cannot mask
            # incompatibilities. No network access to mutate that source.
            subprocess.run(["git", "clone", "--shared", "--quiet",
                            os.environ["SUKISU_TEST_SOURCE"], str(checkout)], check=True)
            self.assertTrue((checkout / "kernel/Makefile").is_file())
            self.assertTrue((checkout / "kernel/supercall/dispatch.c").is_file())
            # Upstream builtin branch natively carries the priority uid filter guard
            dispatch = (checkout / "kernel/supercall/dispatch.c").read_text(encoding="utf-8")
            self.assertIn("CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER", dispatch)
            kconfig = (checkout / "kernel/Kconfig").read_text(encoding="utf-8")
            self.assertIn("config KSU_SUSFS_PRIORITY_UID_FILTER", kconfig)

            builder.shell.cwd = str(checkout)
            builder._apply_patch_file(QUIET_PATCH)
            self.assertEqual(builder.applied_integration_patches,
                             [QUIET_PATCH.name])
            with self.assertRaisesRegex(RuntimeError, "Selected patch failed"):
                builder._apply_patch_file(QUIET_PATCH)


if __name__ == "__main__":
    unittest.main()
