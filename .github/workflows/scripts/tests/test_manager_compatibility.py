import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import BuildConfig
from kernel_builder import KernelBuilder


class ManagerCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.builder = KernelBuilder(BuildConfig(), self.temp.name)

    def test_builtin_and_main_header_layouts(self):
        for relative, declaration, expected in (
            ("kernel/include/uapi/supercall.h",
             "DECLARE(__u32, KERNEL_SU_UAPI_VERSION, 2);", 2),
            ("uapi/supercall.h",
             "static const __u32 KERNEL_SU_UAPI_VERSION = 4;", 4),
        ):
            with self.subTest(relative=relative):
                header = self.builder.work_dir / "KernelSU" / relative
                header.parent.mkdir(parents=True, exist_ok=True)
                header.write_text(declaration, encoding="utf-8")
                self.assertEqual(self.builder._read_ksu_uapi_version(), expected)
                header.unlink()

    def test_missing_uapi_is_unknown(self):
        self.assertIsNone(self.builder._read_ksu_uapi_version())

    def test_setup_success_cannot_hide_wrong_checkout(self):
        requested = self.builder.config.ksu_setup_ref
        with patch.object(self.builder, "_run_cmd"), \
             patch.object(self.builder, "_chdir"), \
             patch.object(self.builder, "_require_path"), \
             patch("kernel_builder.subprocess.run", side_effect=[
                 subprocess.CompletedProcess([], 0, stdout=requested + "\n"),
                 subprocess.CompletedProcess([], 0, stdout="a" * 40 + "\n"),
             ]):
            with self.assertRaisesRegex(RuntimeError, "SukiSU checkout mismatch"):
                self.builder.add_kernelsu()


if __name__ == "__main__":
    unittest.main()
