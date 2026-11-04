"""The upstream `builtin` branch carries SUSFS; no custom shim may replace it."""
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

# Minimal reconstruction of the upstream `builtin` branch kernel/Makefile head.
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


class BuiltinIntegrationTests(unittest.TestCase):
    def test_no_custom_kernelsu_shim_may_reappear(self):
        # A hand-rolled KernelSU-side SUSFS shim rotted on every upstream move
        # and hid the real failure behind a generic link error. The `builtin`
        # branch owns that integration now; only the quiet patch remains.
        self.assertFalse(
            (REPO_ROOT / "patches/susfs/0001-sukisu-main-mount-support.patch").exists())
        self.assertFalse(
            (REPO_ROOT / "patches/susfs/0002-common-susfs-reboot-dispatch.patch").exists())
        self.assertFalse(
            (REPO_ROOT / ".github/workflows/scripts/susfs_integration.py").exists())
        self.assertTrue(QUIET_PATCH.is_file())

    def _apply_to_tree(self, root: Path, makefile: str) -> Path:
        kernel = root / "kernel"
        kernel.mkdir(parents=True)
        (kernel / "Makefile").write_text(makefile, encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        builder = KernelBuilder(BuildConfig(), str(root))
        builder.shell.cwd = str(root)
        builder._apply_patch_file(QUIET_PATCH)
        return kernel / "Makefile"

    def test_quiet_patch_inserts_one_include_flag(self):
        # Unrelated upstream neighbours around the anchor (extra object rules,
        # future conditionals, trailing content) must not fail the build.
        for extra in ("", "\nksu-y += future.o\n", "\nifeq ($(CONFIG_FUTURE),y)\n"
                                                 "ccflags-y += -DFUTURE\nendif\n"):
            with self.subTest(extra=extra):
                with tempfile.TemporaryDirectory() as temp:
                    result = self._apply_to_tree(
                        Path(temp), BUILTIN_MAKEFILE + extra)
                    text = result.read_text(encoding="utf-8")
                    flag = "ccflags-y += -include $(KSU_KERNEL_DIR)/include/quiet.h\n"
                    self.assertEqual(text.count(flag), 1)
                    self.assertEqual(
                        text.replace(flag, ""), BUILTIN_MAKEFILE + extra)
                    self.assertTrue((result.parent / "include/quiet.h").is_file())

    def test_quiet_patch_contract_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            broken = BUILTIN_MAKEFILE.replace(
                "-include $(srctree)/include/uapi/asm-generic/errno.h",
                "-include $(srctree)/include/uapi/asm-generic/future_errno.h")
            kernel_makefile = self._apply_to_tree(root, BUILTIN_MAKEFILE)
            kernel_makefile.write_text(broken, encoding="utf-8")
            builder = KernelBuilder(BuildConfig(), str(root))
            builder.shell.cwd = str(root)
            with self.assertRaisesRegex(RuntimeError, "Selected patch failed"):
                builder._apply_patch_file(QUIET_PATCH)
            # No partial insertion: the contract change blocks the whole patch.
            self.assertEqual(kernel_makefile.read_text(encoding="utf-8"), broken)

    def test_reapplying_quiet_patch_is_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._apply_to_tree(root, BUILTIN_MAKEFILE)
            builder = KernelBuilder(BuildConfig(), str(root))
            builder.shell.cwd = str(root)
            with self.assertRaisesRegex(RuntimeError, "Selected patch failed"):
                builder._apply_patch_file(QUIET_PATCH)

    @unittest.skipUnless(os.environ.get("SUKISU_TEST_SOURCE"),
                         "set SUKISU_TEST_SOURCE to check the complete upstream source")
    def test_quiet_patch_applies_to_upstream_builtin_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = KernelBuilder(BuildConfig(), temp)
            checkout = builder.work_dir / "KernelSU"
            # Clone HEAD so an existing source's working changes cannot mask
            # incompatibilities. No network access to mutate that source.
            subprocess.run(["git", "clone", "--shared", "--quiet",
                            os.environ["SUKISU_TEST_SOURCE"], str(checkout)], check=True)
            makefile = checkout / "kernel/Makefile"
            self.assertTrue(makefile.is_file())
            before = makefile.read_text(encoding="utf-8")
            builder.shell.cwd = str(checkout)
            builder._apply_patch_file(QUIET_PATCH)
            self.assertIn("include/quiet.h",
                          makefile.read_text(encoding="utf-8"))
            self.assertEqual(builder.applied_integration_patches, [QUIET_PATCH.name])
            with self.assertRaisesRegex(RuntimeError, "Selected patch failed"):
                builder._apply_patch_file(QUIET_PATCH)
            self.assertNotEqual(makefile.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
