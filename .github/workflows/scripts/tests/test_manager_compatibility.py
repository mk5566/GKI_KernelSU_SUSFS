import os
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
        orig_cwd = os.getcwd()
        self.addCleanup(os.chdir, orig_cwd)
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

    def test_configure_ksu_susfs_canonical_placement(self):
        defconfig = self.builder._defconfig_path()
        defconfig.parent.mkdir(parents=True, exist_ok=True)
        defconfig.write_text(
            "CONFIG_INTERCONNECT=y\n"
            "CONFIG_EXT4_FS=y\n",
            encoding="utf-8",
        )
        self.builder._configure_ksu_susfs()
        expected = (
            "CONFIG_INTERCONNECT=y\n"
            "CONFIG_KSU_SUSFS=y\n"
            "CONFIG_EXT4_FS=y\n"
        )
        self.assertEqual(defconfig.read_text(encoding="utf-8"), expected)
        # Calling again must be idempotent and not duplicate
        self.builder._configure_ksu_susfs()
        self.assertEqual(defconfig.read_text(encoding="utf-8"), expected)

        # If legacy CONFIG_KSU=y was present, it must be removed to avoid savedefconfig mismatch
        defconfig.write_text(
            "CONFIG_INTERCONNECT=y\n"
            "CONFIG_KSU=y\n"
            "CONFIG_EXT4_FS=y\n",
            encoding="utf-8",
        )
        self.builder._configure_ksu_susfs()
        self.assertEqual(defconfig.read_text(encoding="utf-8"), expected)

    def test_ccache_hardlink_is_disabled(self):
        self.assertNotIn("CCACHE_HARDLINK", self.builder.env)
        self.assertEqual(self.builder.env.get("CCACHE_NOHARDLINK"), "true")

    def test_pack_boot_image_v4_format(self):
        import struct
        kernel_data = b"ARM64_KERNEL_HEADER_DATA_12345" * 100
        kernel_path = self.builder.work_dir / "Image"
        kernel_path.write_bytes(kernel_data)
        out_boot = self.builder.work_dir / "boot.img"

        KernelBuilder._pack_boot_image_v4(
            kernel_path=kernel_path,
            output_path=out_boot,
            os_version_str="android13",
            os_patch_level_str="2026-09",
            cmdline="bootopt=64S3,32N,64N",
        )

        self.assertTrue(out_boot.is_file())
        raw = out_boot.read_bytes()
        self.assertEqual(len(raw) % 4096, 0)
        self.assertTrue(len(raw) >= 4096 + len(kernel_data))

        # Check magic
        magic = raw[:8]
        self.assertEqual(magic, b"ANDROID!")

        # Unpack header fields
        ksize, rsize, os_ver, hdr_size, r0, r1, r2, r3, hdr_ver = struct.unpack("<IIII4II", raw[8:44])
        self.assertEqual(ksize, len(kernel_data))
        self.assertEqual(rsize, 0)
        self.assertEqual(hdr_size, 1584)
        self.assertEqual(hdr_ver, 4)
        self.assertEqual((r0, r1, r2, r3), (0, 0, 0, 0))

        # Decode os_ver
        os_major = (os_ver >> 25) & 0x7F
        patch_year = ((os_ver >> 4) & 0x7F) + 2000
        patch_month = os_ver & 0xF
        self.assertEqual(os_major, 13)
        self.assertEqual(patch_year, 2026)
        self.assertEqual(patch_month, 9)

        # Check cmdline
        cmdline_raw = raw[44:44+1536].rstrip(b"\x00")
        self.assertEqual(cmdline_raw, b"bootopt=64S3,32N,64N")

        # Signature size
        sig_size = struct.unpack("<I", raw[44+1536:44+1536+4])[0]
        self.assertEqual(sig_size, 0)

        # Kernel payload at 4096
        self.assertEqual(raw[4096:4096+len(kernel_data)], kernel_data)

    def test_create_boot_image_produces_artifacts(self):
        (self.builder.work_dir / "final.config").write_text("CONFIG_FOO=y\n")
        dummy_image = self.builder.work_dir / "Image"
        dummy_image.write_bytes(b"DUMMY_IMAGE_DATA" * 50)

        with patch.object(self.builder, "_verify_patch_safety"):
            results = self.builder.create_boot_image()

        self.assertEqual(len(results), 2)
        stem_boot, std_boot = results
        self.assertTrue(Path(stem_boot).is_file())
        self.assertTrue(Path(std_boot).is_file())
        self.assertEqual(Path(stem_boot).read_bytes(), Path(std_boot).read_bytes())
        self.assertTrue(Path(std_boot).read_bytes().startswith(b"ANDROID!"))


if __name__ == "__main__":
    unittest.main()
