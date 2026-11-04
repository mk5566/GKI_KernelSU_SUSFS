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
GUARD_PATCH = REPO_ROOT / "patches/susfs/0002-sukisu-no-priority-uid-filter.patch"

# Minimal reconstructions of the upstream `builtin` branch sources around
# each glue anchor. The context lines are byte-identical to upstream
# 1cfa5734; any upstream change to them must fail the patch closed.
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
        case CMD_SUSFS_SET_PRIORITY_UID_FILTER:
            susfs_set_priority_uid_filter(arg);
            return 0;
        case CMD_SUSFS_ENABLE_AVC_LOG_SPOOFING:
            susfs_set_avc_log_spoofing(arg);
            return 0;
"""

BUILTIN_PKG_OBSERVER = """static void ksu_track_throne_tw_func(struct callback_head *cb)
{
    kfree(cb);
    // Runs after exit_fs() if the task is exiting; path lookups would oops
    if (current->flags & PF_EXITING)
        return;
    track_throne(false);
    // Renew the priority uid bitmap filter
    susfs_parse_priority_app_strings();
}

static void ksu_defer_track_throne(void)
{
    struct callback_head *cb;

    // Renew the getpriority uid bitmap filter
    susfs_parse_priority_app_strings();

    if (!(current->flags & PF_KTHREAD)) {
        return;
    }
}
"""


class BuiltinIntegrationTests(unittest.TestCase):
    def test_no_custom_kernelsu_shim_may_reappear(self):
        # The hand-rolled KernelSU-side SUSFS shim rotted on every upstream
        # move. The `builtin` branch owns that integration now; only small
        # fail-closed glue patches remain.
        for missing in ("0001-sukisu-main-mount-support.patch",
                        "0002-common-susfs-reboot-dispatch.patch",
                        "0003-sukisu-quiet-production.patch"):
            self.assertFalse(
                (REPO_ROOT / "patches/susfs" / missing).exists(), missing)
        for present in ("0001-sukisu-quiet-production.patch",
                        "0002-sukisu-no-priority-uid-filter.patch",
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

    def test_priority_guard_wraps_dispatch_case(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._apply_to_tree(
                root, {"kernel/supercall/dispatch.c": BUILTIN_DISPATCH,
                       "kernel/manager/pkg_observer.c": BUILTIN_PKG_OBSERVER},
                GUARD_PATCH)
            text = (root / "kernel/supercall/dispatch.c").read_text(
                encoding="utf-8")
            guarded = ("#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER\n"
                       "        case CMD_SUSFS_SET_PRIORITY_UID_FILTER:\n"
                       "            susfs_set_priority_uid_filter(arg);\n"
                       "            return 0;\n"
                       "#endif\n")
            self.assertIn(guarded, text)
            # Neighbouring feature cases stay untouched.
            self.assertIn("case CMD_SUSFS_ADD_SUS_MAP:", text)
            self.assertIn("case CMD_SUSFS_ENABLE_AVC_LOG_SPOOFING:", text)

    def test_priority_guard_wraps_both_observer_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._apply_to_tree(
                root, {"kernel/supercall/dispatch.c": BUILTIN_DISPATCH,
                       "kernel/manager/pkg_observer.c": BUILTIN_PKG_OBSERVER},
                GUARD_PATCH)
            text = (root / "kernel/manager/pkg_observer.c").read_text(
                encoding="utf-8")
            self.assertEqual(
                text.count("#ifdef CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER\n"), 2)
            self.assertEqual(
                text.count("susfs_parse_priority_app_strings();"), 2)

    def test_guard_contract_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            broken = BUILTIN_DISPATCH.replace(
                "case CMD_SUSFS_SET_PRIORITY_UID_FILTER:",
                "case CMD_SUSFS_SET_PRIORITY_UID_FILTER_V2:")
            self._apply_to_tree(
                root, {"kernel/supercall/dispatch.c": BUILTIN_DISPATCH,
                       "kernel/manager/pkg_observer.c": BUILTIN_PKG_OBSERVER},
                GUARD_PATCH)
            dispatch = root / "kernel/supercall/dispatch.c"
            observer = root / "kernel/manager/pkg_observer.c"
            before_dispatch = dispatch.read_text(encoding="utf-8")
            before_observer = observer.read_text(encoding="utf-8")
            dispatch.write_text(broken, encoding="utf-8", newline="\n")
            builder = KernelBuilder(BuildConfig(), str(temp))
            builder.shell.cwd = str(temp)
            with self.assertRaisesRegex(RuntimeError, "Selected patch failed"):
                builder._apply_patch_file(GUARD_PATCH)
            # No partial insertion: the whole multi-file patch is atomic.
            self.assertEqual(dispatch.read_text(encoding="utf-8"), broken)
            self.assertEqual(observer.read_text(encoding="utf-8"),
                             before_observer)

    def test_reapplying_glue_patches_is_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {"kernel/Makefile": BUILTIN_MAKEFILE,
                     "kernel/supercall/dispatch.c": BUILTIN_DISPATCH,
                     "kernel/manager/pkg_observer.c": BUILTIN_PKG_OBSERVER}
            for relative, text in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="\n")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            builder = KernelBuilder(BuildConfig(), str(temp))
            builder.shell.cwd = str(temp)
            builder._apply_patch_file(QUIET_PATCH)
            builder._apply_patch_file(GUARD_PATCH)
            for patch in (QUIET_PATCH, GUARD_PATCH):
                with self.subTest(patch=patch.name), \
                     self.assertRaisesRegex(RuntimeError, "Selected patch failed"):
                    builder._apply_patch_file(patch)

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
            builder.shell.cwd = str(checkout)
            builder._apply_patch_file(QUIET_PATCH)
            builder._apply_patch_file(GUARD_PATCH)
            self.assertEqual(builder.applied_integration_patches,
                             [QUIET_PATCH.name, GUARD_PATCH.name])
            with self.assertRaisesRegex(RuntimeError, "Selected patch failed"):
                builder._apply_patch_file(GUARD_PATCH)


if __name__ == "__main__":
    unittest.main()
