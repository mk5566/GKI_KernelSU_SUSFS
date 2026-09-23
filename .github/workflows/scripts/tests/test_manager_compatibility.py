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
        self.builder = KernelBuilder(BuildConfig(sub_level="211", os_patch_level="2026-09"), self.temp.name)

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

    def test_old_or_unknown_uapi_is_rejected_before_patching(self):
        requested = self.builder.config.ksu_setup_ref
        for uapi in (None, 2, 3, 5):
            with self.subTest(uapi=uapi), \
                 patch.object(self.builder, "_run_cmd"), \
                 patch.object(self.builder, "_chdir"), \
                 patch.object(self.builder, "_require_path"), \
                 patch.object(self.builder, "_read_ksu_uapi_version", return_value=uapi), \
                 patch.object(self.builder, "_apply_patch_file") as apply_patch, \
                 patch("kernel_builder.subprocess.run", return_value=
                       subprocess.CompletedProcess([], 0, stdout=requested + "\n")):
                with self.assertRaisesRegex(RuntimeError, "requires SukiSU UAPI 4"):
                    self.builder.add_kernelsu()
                apply_patch.assert_not_called()

    def test_compiled_config_requires_builtin_and_hooks(self):
        symbols = ("CONFIG_KSU", "CONFIG_KPROBES", "CONFIG_KRETPROBES",
                   "CONFIG_HAVE_SYSCALL_TRACEPOINTS", "CONFIG_KSU_SUSFS",
                   "CONFIG_KSU_SUSFS_SUS_MOUNT")
        config_file = self.builder.work_dir / ".config"
        valid = "".join(f"{symbol}=y\n" for symbol in symbols)
        config_file.write_text(valid, encoding="utf-8")
        self.builder._verify_susfs_config(config_file)
        for symbol in symbols:
            with self.subTest(symbol=symbol):
                config_file.write_text(valid.replace(f"{symbol}=y", f"{symbol}=m"),
                                       encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    self.builder._verify_susfs_config(config_file)
        config_file.write_text(valid + "CONFIG_KSU_SUSFS_SUS_PATH=y\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "SUSFS profile mismatch"):
            self.builder._verify_susfs_config(config_file)


if __name__ == "__main__":
    unittest.main()
