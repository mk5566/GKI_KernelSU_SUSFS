"""The full upstream SUSFS filesystem patch applies atomically."""
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
    (fs / "susfs.c").write_text("/* susfs */\n", encoding="utf-8")
    (include / "susfs.h").write_text("/* susfs.h */\n", encoding="utf-8")
    (root / "susfs4ksu/kernel_patches" / PATCH_NAME).write_text(
        "diff --git a/fs/Makefile b/fs/Makefile\n"
        "--- a/fs/Makefile\n"
        "+++ b/fs/Makefile\n"
        "@@ -1 +1,2 @@\n"
        " obj-y += main.o\n"
        "+obj-y += susfs.o\n",
        encoding="utf-8")


def make_common(root: Path) -> Path:
    common = root / "android13-5.15/common"
    (common / "fs").mkdir(parents=True)
    (common / "include/linux").mkdir(parents=True)
    (common / "fs/Makefile").write_text("obj-y += main.o\n", encoding="utf-8")
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
            self.assertEqual(builder.applied_integration_patches, [PATCH_NAME])
            self.assertTrue((common / "fs/susfs.c").is_file())
            self.assertTrue((common / "include/linux/susfs.h").is_file())
            self.assertIn("obj-y += susfs.o",
                          (common / "fs/Makefile").read_text(encoding="utf-8"))

    def test_missing_patch_is_fatal(self):
        with tempfile.TemporaryDirectory() as temp, restored_cwd():
            root = Path(temp)
            builder = KernelBuilder(BuildConfig(), str(root))
            (root / "android13-5.15/common/fs").mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "SUSFS patch not found"):
                builder.apply_susfs_patches()
            self.assertEqual(builder.applied_integration_patches, [])

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
            self.assertEqual(builder.applied_integration_patches, [PATCH_NAME])


if __name__ == "__main__":
    unittest.main()
