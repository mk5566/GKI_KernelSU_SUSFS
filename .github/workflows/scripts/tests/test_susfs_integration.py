"""The full upstream SUSFS filesystem patch plus mount-only glue applies atomically."""
import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import BuildConfig
from kernel_builder import KernelBuilder


PATCH_NAME = "50_add_susfs_in_gki-android13-5.15.patch"
FS_GUARD = "0003-susfs-no-priority-uid-filter.patch"

# Byte-identical excerpt of upstream susfs4ksu fs/susfs.c around both helper
# definitions. Any upstream change here must fail the glue patch closed.
SUSFS_C_EXCERPT = (
    "\n"
    "/*\n"
    " * Switches the selinux domain of the current process to the KernelSU one, so\n"
    " * that the data paths of the apps can be accessed despite of the selinux\n"
    " * restrictions.\n"
    " * The original credentials are returned, they have to be restored with\n"
    " * susfs_restore_creds() afterwards.\n"
    " * Returns the original credentials on success, NULL on failure.\n"
    " */\n"
    "static struct cred *susfs_switch_to_ksu_domain(void) {\n"
    "\tstruct cred *orig_cred = NULL;\n"
    "\tstruct cred *cred = NULL;\n"
    "\n"
    "\t/* a copy of the current credentials, which is restored later on */\n"
    "\torig_cred = prepare_creds();\n"
    "\tif (!orig_cred) {\n"
    "\t\tSUSFS_LOGE(\"failed to prepare creds!\\n\");\n"
    "\t\treturn NULL;\n"
    "\t}\n"
    "\n"
    "\tcred = prepare_creds();\n"
    "\tif (!cred) {\n"
    "\t\tSUSFS_LOGE(\"failed to prepare creds!\\n\");\n"
    "\t\tabort_creds(orig_cred);\n"
    "\t\treturn NULL;\n"
    "\t}\n"
    "\n"
    "\tsetup_selinux(\"u:r:ksu:s0\", cred);\n"
    "\tcommit_creds(cred);\n"
    "\n"
    "\tif (!susfs_is_current_ksu_domain()) {\n"
    "\t\tSUSFS_LOGE(\"domain is not ksu, restoring the original creds\\n\");\n"
    "\t\tcommit_creds(orig_cred);\n"
    "\t\treturn NULL;\n"
    "\t}\n"
    "\n"
    "\treturn orig_cred;\n"
    "}\n"
    "\n"
    "/*\n"
    " * Restores the original credentials that were saved by\n"
    " * susfs_switch_to_ksu_domain().\n"
    " */\n"
    "static void susfs_restore_creds(struct cred *orig_cred) {\n"
    "\tif (!orig_cred)\n"
    "\t\treturn;\n"
    "\n"
    "\tcommit_creds(orig_cred);\n"
)


@contextlib.contextmanager
def restored_cwd():
    previous = Path.cwd()
    try:
        yield
    finally:
        os.chdir(previous)


def make_susfs_source(root: Path) -> None:
    fs = root / "susfs4ksu/kernel_patches/fs"
    include = root / "susfs4ksu/kernel_patches/include/linux"
    fs.mkdir(parents=True)
    include.mkdir(parents=True)
    (fs / "susfs.c").write_text(SUSFS_C_EXCERPT, encoding="utf-8", newline="\n")
    (include / "susfs.h").write_text("/* susfs.h */\n", encoding="utf-8",
                                     newline="\n")
    (root / "susfs4ksu/kernel_patches" / PATCH_NAME).write_text(
        "diff --git a/fs/Makefile b/fs/Makefile\n"
        "--- a/fs/Makefile\n"
        "+++ b/fs/Makefile\n"
        "@@ -1 +1,2 @@\n"
        " obj-y += main.o\n"
        "+obj-y += susfs.o\n",
        encoding="utf-8", newline="\n")


def make_common(root: Path) -> Path:
    common = root / "android13-5.15/common"
    (common / "fs").mkdir(parents=True)
    (common / "include/linux").mkdir(parents=True)
    (common / "fs/Makefile").write_text("obj-y += main.o\n", encoding="utf-8",
                                        newline="\n")
    subprocess.run(["git", "init", "-q", str(common)], check=True)
    return common


class SusfsPatchTests(unittest.TestCase):
    def test_full_patch_applies_and_sources_are_copied(self):
        with tempfile.TemporaryDirectory() as temp, restored_cwd():
            root = Path(temp)
            make_susfs_source(root)
            common = make_common(root)
            builder = KernelBuilder(BuildConfig(), str(root))
            builder.apply_susfs_patches()
            self.assertEqual(builder.applied_integration_patches,
                             [PATCH_NAME, FS_GUARD])
            self.assertTrue((common / "fs/susfs.c").is_file())
            self.assertTrue((common / "include/linux/susfs.h").is_file())
            self.assertIn("obj-y += susfs.o",
                          (common / "fs/Makefile").read_text(encoding="utf-8"))
            susfs_c = (common / "fs/susfs.c").read_text(encoding="utf-8")
            self.assertEqual(susfs_c.count("__maybe_unused susfs_"), 2)

    def test_missing_patch_is_fatal(self):
        with tempfile.TemporaryDirectory() as temp, restored_cwd():
            root = Path(temp)
            builder = KernelBuilder(BuildConfig(), str(root))
            (root / "android13-5.15/common/fs").mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "SUSFS patch not found"):
                builder.apply_susfs_patches()
            self.assertEqual(builder.applied_integration_patches, [])

    def test_changed_helper_signature_fails_the_glue_patch_closed(self):
        # Upstream renames a helper: the glue context no longer matches, so
        # the run stops here instead of compiling with a hidden mismatch.
        with tempfile.TemporaryDirectory() as temp, restored_cwd():
            root = Path(temp)
            make_susfs_source(root)
            renamed = ((root / "susfs4ksu/kernel_patches/fs/susfs.c")
                       .read_text(encoding="utf-8")
                       .replace("susfs_switch_to_ksu_domain",
                                "susfs_switch_to_ksu_domain_v2"))
            (root / "susfs4ksu/kernel_patches/fs/susfs.c").write_text(
                renamed, encoding="utf-8", newline="\n")
            common = make_common(root)
            builder = KernelBuilder(BuildConfig(), str(root))
            with self.assertRaisesRegex(RuntimeError, "Selected patch failed"):
                builder.apply_susfs_patches()
            # The glue file itself is untouched; the failed hunk changed nothing.
            copied = (common / "fs/susfs.c").read_text(encoding="utf-8")
            self.assertNotIn("__maybe_unused", copied)
            self.assertIn("susfs_switch_to_ksu_domain_v2", copied)

    def test_conflicting_patch_fails_closed_without_partial_state(self):
        with tempfile.TemporaryDirectory() as temp, restored_cwd():
            root = Path(temp)
            make_susfs_source(root)
            common = make_common(root)
            builder = KernelBuilder(BuildConfig(), str(root))
            builder.apply_susfs_patches()
            before = (common / "fs/Makefile").read_text(encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Selected patch failed"):
                builder.apply_susfs_patches()
            self.assertEqual((common / "fs/Makefile").read_text(encoding="utf-8"), before)
            self.assertEqual(builder.applied_integration_patches,
                             [PATCH_NAME, FS_GUARD])


if __name__ == "__main__":
    unittest.main()
