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
    def test_defaults_keep_tcp_unchanged_and_select_phone_compressor(self):
        self.assertFalse(self.builder.config.use_zram)
        self.assertFalse(self.builder.config.set_default_bbr)
        self.assertNotIn("CONFIG_TCP_CONG_BBR3", self.builder.BBR_CONFIG_UPDATES)
        self.assertIn("lz4kd-builtin", self.builder.config.artifact_stem)
        self.assertEqual(self.builder.config.optional_patches, ())

    def test_bbrv3_selection_configures_bbrv3_and_rejects_bbrv1(self):
        selected = BuildConfig(sub_level="211", os_patch_level="2026-09",
                               optional_patches=("bbrv3",))
        self.assertIn("bbr3-default", selected.artifact_stem)
        self.assertEqual(self.builder.BBR3_CONFIG_UPDATES["CONFIG_TCP_CONG_BBR3"], "y")
        with self.assertRaisesRegex(ValueError, "either upstream BBRv1"):
            BuildConfig(sub_level="211", os_patch_level="2026-09",
                        optional_patches=("bbrv3",), set_default_bbr=True)

    def test_patch_artifact_name_is_stable_for_selection_order(self):
        first = BuildConfig(sub_level="211", os_patch_level="2026-09",
                            optional_patches=("cpu-scan", "clear-page"))
        second = BuildConfig(sub_level="211", os_patch_level="2026-09",
                             optional_patches=("clear-page", "cpu-scan"))
        self.assertEqual(first.artifact_stem, second.artifact_stem)
        self.assertNotEqual(first.artifact_stem, self.builder.config.artifact_stem)

    def test_selected_bbrv3_compiled_config_is_checked(self):
        path = self.builder.work_dir / ".config"
        path.write_text('CONFIG_TCP_CONG_BBR3=y\nCONFIG_DEFAULT_BBR3=y\n'
                        'CONFIG_DEFAULT_TCP_CONG="bbr3"\n')
        self.builder._verify_bbr3_config(path)
        path.write_text(path.read_text().replace("CONFIG_TCP_CONG_BBR3=y", "CONFIG_TCP_CONG_BBR3=m"))
        with self.assertRaisesRegex(RuntimeError, "CONFIG_TCP_CONG_BBR3=y"):
            self.builder._verify_bbr3_config(path)

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
