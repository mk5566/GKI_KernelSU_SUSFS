import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import BuildConfig
from kernel_builder import KernelBuilder

class ConservativeProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.builder = KernelBuilder(
            BuildConfig(sub_level="211", os_patch_level="2026-09"), self.temp.name
        )
    def test_defaults_select_bbr_and_phone_compressor(self):
        self.assertFalse(self.builder.config.use_zram)
        self.assertTrue(self.builder.config.set_default_bbr)
        self.assertIn("lz4kd-builtin", self.builder.config.artifact_stem)
        self.assertIn("bbr1-default", self.builder.config.artifact_stem)
        self.assertEqual(self.builder.config.optional_patches, ())

    def test_bbrv3_selection_is_rejected_without_silent_fallback(self):
        for default_bbr in (False, True):
            with self.subTest(default_bbr=default_bbr), self.assertRaisesRegex(ValueError, "Retired"):
                BuildConfig(sub_level="211", os_patch_level="2026-09",
                            optional_patches=("bbrv3",), set_default_bbr=default_bbr)
        self.assertFalse(hasattr(self.builder, "BBR3_CONFIG_UPDATES"))

    def test_patch_artifact_name_is_stable_for_selection_order(self):
        first = BuildConfig(sub_level="211", os_patch_level="2026-09",
                            optional_patches=("cpu-scan", "clear-page"))
        second = BuildConfig(sub_level="211", os_patch_level="2026-09",
                             optional_patches=("clear-page", "cpu-scan"))
        self.assertEqual(first.artifact_stem, second.artifact_stem)
        self.assertNotEqual(first.artifact_stem, self.builder.config.artifact_stem)

    def test_rom_tcp_remains_an_explicit_option(self):
        selected = BuildConfig(sub_level="211", os_patch_level="2026-09", set_default_bbr=False)
        self.assertIn("rom-tcp", selected.artifact_stem)
        cfg = self.builder._defconfig_path()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("CONFIG_INET_DIAG_DESTROY=y\n")
        self.builder._configure_bbr()
        text = cfg.read_text()
        self.assertIn("CONFIG_TCP_CONG_BBR=y\n", text)
        self.assertNotIn("CONFIG_TCP_CONG_BBR3", text)
        for symbol in ("BIC", "WESTWOOD", "HTCP"):
            self.assertIn(f"# CONFIG_TCP_CONG_{symbol} is not set\n", text)

    def test_bbr_and_cubic_only_congestion_control_validates(self):
        path = self.builder.work_dir / ".config"
        valid_text = (
            "CONFIG_TCP_CONG_BBR=y\n"
            "CONFIG_DEFAULT_BBR=y\n"
            'CONFIG_DEFAULT_TCP_CONG="bbr"\n'
            "CONFIG_TCP_CONG_CUBIC=y\n"
            "# CONFIG_TCP_CONG_BIC is not set\n"
        )
        path.write_text(valid_text)
        self.builder._verify_bbr_config(path)

        # Forbidden CCAs must fail verification
        for forbidden in ("CONFIG_TCP_CONG_BIC=y", "CONFIG_TCP_CONG_BIC=m",
                          "CONFIG_TCP_CONG_WESTWOOD=y", "CONFIG_TCP_CONG_WESTWOOD=m",
                          "CONFIG_TCP_CONG_HTCP=y", "CONFIG_TCP_CONG_HTCP=m"):
            path.write_text(valid_text + forbidden + "\n")
            with self.assertRaisesRegex(RuntimeError, "Unused congestion algorithm"):
                self.builder._verify_bbr_config(path)

    def test_zram_patch_only_allows_lz4kd_and_zstd(self):
        patch_path = Path(__file__).resolve().parents[4] / "patches/5.15/0002-ishtar-builtin-lz4kd.patch"
        content = patch_path.read_text(encoding="utf-8")
        self.assertIn('depends on CRYPTO_LZ4KD || CRYPTO_ZSTD', content)
        self.assertIn('"lz4kd"', content)
        self.assertIn('"zstd"', content)
        # Verify unused compressors are removed from backends
        for unused in ('"lzo"', '"lzo-rle"', '"lz4"', '"lz4hc"', '"842"'):
            self.assertIn(f'-	{unused}', content)

    def test_lz4kd_copy_never_touches_module_loader(self):
        helper = self.builder.sukisu_patch_dir / "other/zram/lz4k"
        common = self.builder.work_dir / "common"
        module = common / "kernel/module.c"
        module.parent.mkdir(parents=True)
        module.write_text("strict module version check\n")
        for relative in (
            "crypto/lz4kd.c", "include/linux/lz4kd.h",
            "lib/lz4kd/Makefile", "lib/lz4kd/lz4kd_private.h",
            "lib/lz4kd/lz4kd_encode_private.h", "lib/lz4kd/lz4kd_encode.c",
            "lib/lz4kd/lz4kd_encode_delta.c", "lib/lz4kd/lz4kd_decode.c",
            "lib/lz4kd/lz4kd_decode_delta.c",
        ):
            src = helper / relative
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(relative)
        with patch.object(self.builder, "_git_head", return_value="test-revision"):
            self.builder.apply_zram_patches()
        self.assertEqual(module.read_text(), "strict module version check\n")
        self.assertEqual((common / "crypto/lz4kd.c").read_text(), "crypto/lz4kd.c")
    def test_mount_only_does_not_rewrite_task_mmu(self):
        path = self.builder.work_dir / "common/fs/proc/task_mmu.c"
        path.parent.mkdir(parents=True)
        content = "/* not real kernel C: ensures no rewriting */\nstruct dentry *dentry;\ngoto show_pad;\n"
        path.write_text(content)
        self.builder.apply_task_mmu_fixes()
        self.assertEqual(path.read_text(), content)
    def test_native_zstd_preserves_module_deployment(self):
        cfg = self.builder._defconfig_path()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("CONFIG_ZRAM=m\nCONFIG_ZSMALLOC=m\nCONFIG_MODULE_SIG_FORCE=y\n")
        fragment = cfg.parent / "ishtar_native_zstd.fragment"
        fragment.write_text("CONFIG_CRYPTO_ZSTD=y\nCONFIG_ZRAM_DEF_COMP_ZSTD=y\n")
        module_list = self.builder.work_dir / "common/android/test_modules"
        module_list.parent.mkdir(parents=True)
        module_list.write_text("zram.ko\nzsmalloc.ko\n")
        self.builder._configure_zram()
        text = cfg.read_text()
        for setting in ("CONFIG_ZRAM=m", "CONFIG_ZSMALLOC=m", "CONFIG_MODULE_SIG_FORCE=y"):
            self.assertIn(setting, text)
        self.assertEqual(module_list.read_text(), "zram.ko\nzsmalloc.ko\n")
    def test_missing_zram_deployment_fails_validation(self):
        path = self.builder.work_dir / ".config"
        path.write_text('CONFIG_CRYPTO_ZSTD=y\nCONFIG_ZRAM_DEF_COMP_ZSTD=y\nCONFIG_ZRAM_DEF_COMP="zstd"\n')
        with self.assertRaisesRegex(RuntimeError, "validated"):
            self.builder._verify_zstd_config(path)
    def test_zstd_configuration_validates(self):
        path = self.builder.work_dir / ".config"
        path.write_text('CONFIG_ZRAM=m\nCONFIG_ZSMALLOC=m\nCONFIG_CRYPTO_ZSTD=y\nCONFIG_ZRAM_DEF_COMP_ZSTD=y\nCONFIG_ZRAM_DEF_COMP="zstd"\n')
        self.builder._verify_zstd_config(path)

    def test_ishtar_rejects_upstream_module_only_zram(self):
        path = self.builder.work_dir / ".config"
        path.write_text("CONFIG_ZRAM=m\nCONFIG_ZSMALLOC=m\nCONFIG_CRYPTO_ZSTD=y\n")
        with self.assertRaisesRegex(RuntimeError, "deployment is unresolved"):
            self.builder._verify_ishtar_zram_plan(path)

    def test_ishtar_accepts_current_builtin_lz4kd_plan(self):
        path = self.builder.work_dir / ".config"
        path.write_text(
            "CONFIG_ZRAM=y\nCONFIG_ZSMALLOC=y\n"
            "CONFIG_CRYPTO_LZ4KD=y\nCONFIG_ZRAM_DEF_COMP_LZ4KD=y\n"
        )
        self.builder._verify_ishtar_zram_plan(path)

    def test_compiled_lz4kd_must_be_builtin_and_default(self):
        path = self.builder.work_dir / ".config"
        path.write_text(
            "CONFIG_ZRAM=y\nCONFIG_ZSMALLOC=y\n"
            "CONFIG_CRYPTO_LZ4KD=y\nCONFIG_ZRAM_DEF_COMP_LZ4KD=y\n"
            'CONFIG_ZRAM_DEF_COMP="lz4kd"\n'
        )
        self.builder._verify_lz4kd_config(path)
        path.write_text(path.read_text().replace("CONFIG_ZRAM=y", "CONFIG_ZRAM=m"))
        with self.assertRaisesRegex(RuntimeError, "CONFIG_ZRAM=y"):
            self.builder._verify_lz4kd_config(path)

    def test_working_phone_tmpfs_support_cannot_be_dropped(self):
        path = self.builder.work_dir / ".config"
        path.write_text("CONFIG_TMPFS_POSIX_ACL=y\nCONFIG_ZRAM_WRITEBACK=y\n"
                        "CONFIG_IP_NF_TARGET_TTL=y\nCONFIG_IP6_NF_TARGET_HL=y\n"
                        "CONFIG_IP6_NF_MATCH_HL=y\n")
        with self.assertRaisesRegex(RuntimeError, "CONFIG_TMPFS_XATTR=y"):
            self.builder._verify_device_config(path)

    def test_vendor_module_crc_check_remains_enabled(self):
        path = self.builder.work_dir / ".config"
        path.write_text("CONFIG_TMPFS_POSIX_ACL=y\nCONFIG_TMPFS_XATTR=y\n"
                        "CONFIG_ZRAM_WRITEBACK=y\nCONFIG_IP_NF_TARGET_TTL=y\n"
                        "CONFIG_IP6_NF_TARGET_HL=y\nCONFIG_IP6_NF_MATCH_HL=y\n"
                        "# CONFIG_MODULE_SIG_FORCE is not set\n")
        with self.assertRaisesRegex(RuntimeError, "CONFIG_MODVERSIONS=y"):
            self.builder._verify_device_config(path)

    def test_kernel_name_step_preserves_upstream_scripts(self):
        scripts = self.builder.work_dir / "common/scripts"
        scripts.mkdir(parents=True)
        for name in ("setlocalversion", "mkcompile_h"):
            (scripts / name).write_text(f"original {name}\n")
        with patch.object(self.builder, "_chdir"):
            self.builder.configure_kernel_name()
        for name in ("setlocalversion", "mkcompile_h"):
            self.assertEqual((scripts / name).read_text(), f"original {name}\n")
if __name__ == "__main__": unittest.main()
