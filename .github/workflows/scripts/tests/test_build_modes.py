import io
import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build import parse_args
from config import ANDROID_FAMILY, BuildConfig, REPO_ROOT
from kernel_builder import KernelBuilder
from patch_plan import make_patch_plan
from target import choose_gki_tag, choose_manifest_branch, peeled_commit, resolve_sukisu, resolve_susfs, resolve_sukisu_patch


class ResolverTests(unittest.TestCase):
    def test_latest_android13_5_15_point_release_wins(self):
        self.assertEqual(ANDROID_FAMILY, "android13-5.15")
        refs = {
            "android13-5.15.211_r00": "a" * 40,
            "android13-5.15.211_r00^{}": "b" * 40,
            "android13-5.15.216_r00": "c" * 40,
            "android13-5.15.216_r00^{}": "d" * 40,
            "android13-5.15.216_r01": "e" * 40,
            "android14-5.15.300_r00": "f" * 40,
        }
        self.assertEqual(choose_gki_tag(refs), "android13-5.15.216_r01")
        self.assertEqual(peeled_commit(refs, "android13-5.15.216_r00"), "d" * 40)
        branches = "\n".join([
            "1" * 40 + "\trefs/heads/common-android13-5.15-2026-06",
            "2" * 40 + "\trefs/heads/common-android13-5.15-2026-09",
            "3" * 40 + "\trefs/heads/common-android13-5.15-lts",
            "4" * 40 + "\trefs/heads/common-android14-6.1-2026-09",
        ])
        self.assertEqual(choose_manifest_branch(branches), "common-android13-5.15-2026-09")
        config = BuildConfig("dev", kernel_version="5.15.216",
                             gki_tag="android13-5.15.216_r00",
                             gki_commit="d" * 40,
                             manifest_branch="common-android13-5.15-2026-09")
        self.assertEqual(config.artifact_stem, "android13-5.15.216-sukisu-dev")
        self.assertEqual(config.gki_commit, "d" * 40)

    def test_sukisu_stable_and_dev_are_immutable(self):
        payload = b'{"tag_name":"v4.2.0","draft":false,"prerelease":false}'
        with patch("target.urllib.request.urlopen", return_value=io.BytesIO(payload)), \
             patch("target._git", return_value="a" * 40 + "\trefs/tags/v4.2.0\n" +
                   "b" * 40 + "\trefs/tags/v4.2.0^{}\n"):
            self.assertEqual(resolve_sukisu("stable"), ("v4.2.0", "b" * 40))
        with patch("target._git", return_value="ref: refs/heads/main\tHEAD\n" +
                   "c" * 40 + "\tHEAD\n"):
            self.assertEqual(resolve_sukisu("dev"), ("", "c" * 40))

    def test_susfs_and_patch_resolvers(self):
        with patch("target._git", return_value="a" * 40 + "\trefs/heads/gki-android13-5.15\n"):
            self.assertEqual(resolve_susfs(), "a" * 40)
        with patch("target._git", return_value="b" * 40 + "\trefs/heads/main\n"):
            self.assertEqual(resolve_sukisu_patch(), "b" * 40)


class ModeTests(unittest.TestCase):
    def test_one_patch_plan_always_includes_lz4_zram_bbr_and_tweaks(self):
        plan = make_patch_plan()
        self.assertTrue(plan.lz4 and plan.zram and plan.bbr and plan.tweaks)
        self.assertEqual(len(plan.all), len(plan.lz4) + len(plan.zram) + len(plan.bbr) + len(plan.tweaks))
        names = [path.name for path in plan.all]
        self.assertIn("0001-lz4-1.9.4.patch", names)
        self.assertIn("lz4kd-integration.patch", names)
        self.assertIn("0001-bbrv3-android-kabi.patch", names)
        self.assertNotIn("silence_irq_cpu_logspam.patch", names)
        self.assertFalse((REPO_ROOT / "patches/bbrv3").exists())
        self.assertFalse((REPO_ROOT / "patches/tweaks/silence_irq_cpu_logspam.patch").exists())

    def test_kabi_bbr_patch_keeps_the_104_byte_private_area(self):
        text = (REPO_ROOT / "patches/bbr/0001-bbrv3-android-kabi.patch").read_text(encoding="utf-8")
        self.assertIn("__GENKSYMS__", text)
        self.assertIn("__kabi_placeholder_", text)
        self.assertNotIn("icsk_ca_priv[160", text)
        self.assertIn("104-byte", text)

    def test_only_sukisu_channel_is_a_build_choice(self):
        args = parse_args(["--sukisu-channel", "dev"])
        self.assertEqual(args.sukisu_channel, "dev")
        for obsolete in ("--bbr-version", "--no-tweaks", "--sub-level", "--no-zram",
                         "--ksu-commit", "--custom-version"):
            with self.subTest(obsolete=obsolete), self.assertRaises(SystemExit), \
                 contextlib.redirect_stderr(io.StringIO()):
                parse_args([obsolete, "x"])

    def test_workflow_dispatch_has_one_input(self):
        text = (REPO_ROOT / ".github/workflows/kernel-build.yml").read_text(encoding="utf-8")
        block = text.split("    inputs:\n", 1)[1].split("\npermissions:", 1)[0]
        names = [line.strip().removesuffix(":") for line in block.splitlines()
                 if line.startswith("      ") and not line.startswith("        ")]
        self.assertEqual(names, ["sukisu_channel"])
        self.assertIn("artifacts/boot.img", text)
        self.assertIn("artifacts/AnyKernel3.zip", text)
        self.assertNotIn("BUILD_INFO.md\n", text.split("Publish boot.img", 1)[-1].split("if: failure()", 1)[0])

    def test_defconfig_is_bbrv3_lz4kd_and_no_kpm(self):
        with tempfile.TemporaryDirectory() as d:
            builder = KernelBuilder(BuildConfig(), d)
            path = builder._defconfig_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("CONFIG_TCP_CONG_CUBIC=y\nCONFIG_IOSCHED_BFQ=y\n"
                            "CONFIG_CPU_FREQ_GOV_CONSERVATIVE=y\nCONFIG_KPM=y\n",
                            encoding="utf-8")
            kconfig = builder.work_dir / "KernelSU/kernel/Kconfig"
            kconfig.parent.mkdir(parents=True, exist_ok=True)
            kconfig.write_text("config KSU_SUSFS\nconfig KSU_SUSFS_SUS_MOUNT\n",
                               encoding="utf-8")
            previous = Path.cwd()
            try:
                builder.configure_kernel()
            finally:
                os.chdir(previous)
            result = path.read_text(encoding="utf-8")
            self.assertIn('CONFIG_DEFAULT_TCP_CONG="bbr3"', result)
            self.assertIn("CONFIG_TCP_CONG_BBR=y", result)
            self.assertIn("CONFIG_TCP_CONG_BBR3=y", result)
            self.assertIn("CONFIG_TCP_CONG_CUBIC=n", result)
            self.assertIn("CONFIG_ZRAM_DEF_COMP_LZ4KD=y", result)
            self.assertIn("CONFIG_ZRAM_DEF_COMP_LZ4=n", result)
            self.assertIn("CONFIG_IOSCHED_BFQ=n", result)
            self.assertIn("CONFIG_CPU_FREQ_GOV_CONSERVATIVE=n", result)
            self.assertIn("CONFIG_CPU_FREQ_GOV_PERFORMANCE=y", result)
            self.assertIn("CONFIG_KPM=n", result)

    def test_selected_patch_missing_and_conflict_are_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q", d], check=True)
            (root / "a.txt").write_text("old\n", encoding="utf-8")
            patch_file = root / "change.patch"
            patch_file.write_text("diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
                                  "@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8")
            builder = KernelBuilder(BuildConfig(), d)
            builder.shell.cwd = d
            with self.assertRaisesRegex(RuntimeError, "missing"):
                builder._apply_patch_file(root / "missing.patch")
            builder._apply_patch_file(patch_file)
            with self.assertRaisesRegex(RuntimeError, "change.patch"):
                builder._apply_patch_file(patch_file)

    def test_final_config_rejects_a_layout_that_is_not_this_kernel(self):
        with tempfile.TemporaryDirectory() as d:
            builder = KernelBuilder(BuildConfig(), d)
            file = Path(d) / ".config"
            file.write_text("CONFIG_TCP_CONG_BBR3=n\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Final .config mismatch"):
                builder._verify_generated_config(file)

    def test_preflight_uses_manifest_selected_clang(self):
        with tempfile.TemporaryDirectory() as d:
            builder = KernelBuilder(BuildConfig(), d)
            constants = builder.work_dir / "common/build.config.constants"
            constants.parent.mkdir(parents=True, exist_ok=True)
            constants.write_text("CLANG_VERSION=r450784e\n", encoding="utf-8")
            clang_bin = (builder.work_dir / "prebuilts/clang/host/linux-x86" /
                         "clang-r450784e/bin")
            clang_bin.mkdir(parents=True)
            (clang_bin / "clang").touch()
            (clang_bin / "ld.lld").touch()
            with self.assertRaisesRegex(RuntimeError, "AOSP host sysroot"):
                builder.configure_kernel_toolchain()
            (builder.work_dir / "build/kernel/build-tools/sysroot").mkdir(parents=True)
            with patch("kernel_builder.subprocess.run", return_value=
                       subprocess.CompletedProcess([], 0, stdout="Android Clang\n")) as run:
                builder.configure_kernel_toolchain()
            self.assertEqual(run.call_args.args[0], [str(clang_bin / "clang"), "--version"])
            self.assertEqual(builder.env["PATH"].split(os.pathsep)[0], str(clang_bin))


if __name__ == "__main__":
    unittest.main()
