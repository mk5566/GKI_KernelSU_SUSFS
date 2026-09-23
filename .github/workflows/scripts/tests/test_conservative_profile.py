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
    def test_defaults_make_no_automatic_tuning_request(self):
        self.assertFalse(self.builder.config.use_zram)
        self.assertFalse(self.builder.config.set_default_bbr)
        self.assertNotIn("CONFIG_TCP_CONG_BBR3", self.builder.BBR_CONFIG_UPDATES)
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
