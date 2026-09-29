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
from config import BuildConfig, REPO_ROOT
from kernel_builder import KernelBuilder
from patch_plan import make_patch_plan
from target import choose_gki_tag, resolve_gki, resolve_sukisu


class ResolverTests(unittest.TestCase):
    def test_exact_point_tag_selection(self):
        refs = {x: "a" * 40 for x in (
            "android13-5.15.216_r00", "android13-5.15.217_r01",
            "android13-5.15.217_r10", "android13-5.15.217_r02",
            "android13-5.15.999_r00-PSTEST", "android13-5.15.999_asb-2026-09",
            "android14-5.15.999_r00", "android13-6.1.999_r00")}
        self.assertEqual(choose_gki_tag(refs), "android13-5.15.217_r10")

    def test_resolved_annotated_tag_is_peeled_and_versioned(self):
        output = ("a" * 40 + "\trefs/tags/android13-5.15.216_r00\n" +
                  "b" * 40 + "\trefs/tags/android13-5.15.216_r00^{}\n")
        branch = "c" * 40 + "\trefs/heads/common-android13-5.15-2026-09\n"
        with patch("target._git", side_effect=[output, branch]):
            target = resolve_gki()
        self.assertEqual((target.tag, target.commit, target.kernel_version),
                         ("android13-5.15.216_r00", "b" * 40, "5.15.216"))

    def test_no_non_515_fallback(self):
        with self.assertRaisesRegex(RuntimeError, "No official"):
            choose_gki_tag({"android14-6.1.1_r00": "a" * 40})

    def test_sukisu_stable_and_dev_are_immutable(self):
        payload = b'{"tag_name":"v4.2.0","draft":false,"prerelease":false}'
        with patch("target.urllib.request.urlopen", return_value=io.BytesIO(payload)), \
             patch("target._git", return_value="a" * 40 + "\trefs/tags/v4.2.0\n" +
                   "b" * 40 + "\trefs/tags/v4.2.0^{}\n"):
            self.assertEqual(resolve_sukisu("stable"), ("v4.2.0", "b" * 40))
        with patch("target._git", return_value="ref: refs/heads/main\tHEAD\n" +
                   "c" * 40 + "\tHEAD\n"):
            self.assertEqual(resolve_sukisu("dev"), ("", "c" * 40))


class ModeTests(unittest.TestCase):
    def test_all_eight_patch_plans(self):
        for channel in ("stable", "dev"):
            for bbr in ("v1", "v3"):
                for tweaks in (False, True):
                    with self.subTest(channel=channel, bbr=bbr, tweaks=tweaks):
                        plan = make_patch_plan(BuildConfig(channel, bbr, tweaks))
                        self.assertEqual(bool(plan.bbr), bbr == "v3")
                        self.assertEqual(bool(plan.tweaks), tweaks)
                        self.assertEqual(len(plan.all), len(plan.bbr) + len(plan.tweaks))

    def test_only_three_cli_choices(self):
        args = parse_args(["--sukisu-channel", "dev", "--bbr-version", "v1", "--no-tweaks"])
        self.assertEqual((args.sukisu_channel, args.bbr_version, args.apply_tweaks),
                         ("dev", "v1", False))
        for obsolete in ("--sub-level", "--no-zram", "--ksu-commit", "--custom-version"):
            with self.subTest(obsolete=obsolete), self.assertRaises(SystemExit), \
                 contextlib.redirect_stderr(io.StringIO()):
                parse_args([obsolete, "x"])

    def test_workflow_dispatch_has_exactly_three_inputs(self):
        text = (REPO_ROOT / ".github/workflows/kernel-build.yml").read_text(encoding="utf-8")
        block = text.split("    inputs:\n", 1)[1].split("\npermissions:", 1)[0]
        names = [line.strip().removesuffix(":") for line in block.splitlines()
                 if line.startswith("      ") and not line.startswith("        ")]
        self.assertEqual(names, ["sukisu_channel", "bbr_version", "apply_tweaks"])

    def test_defconfig_modes_and_reduced_choices(self):
        for mode in ("v1", "v3"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                builder = KernelBuilder(BuildConfig(bbr_version=mode), d)
                path = builder._defconfig_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("CONFIG_TCP_CONG_CUBIC=y\nCONFIG_IOSCHED_BFQ=y\n"
                                "CONFIG_CPU_FREQ_GOV_CONSERVATIVE=y\n", encoding="utf-8")
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
                self.assertIn('CONFIG_DEFAULT_TCP_CONG="bbr' +
                              ('3' if mode == 'v3' else '') + '"', result)
                self.assertIn(f"CONFIG_TCP_CONG_BBR3={'y' if mode == 'v3' else 'n'}", result)
                self.assertIn("CONFIG_ZRAM_DEF_COMP_LZ4KD=y", result)
                self.assertIn("CONFIG_IOSCHED_BFQ=n", result)
                self.assertIn("CONFIG_TCP_CONG_CUBIC=n", result)
                self.assertIn("CONFIG_CPU_FREQ_GOV_CONSERVATIVE=n", result)

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

    def test_final_config_rejects_wrong_bbr(self):
        with tempfile.TemporaryDirectory() as d:
            builder = KernelBuilder(BuildConfig(bbr_version="v1"), d)
            file = Path(d) / ".config"
            file.write_text("CONFIG_TCP_CONG_BBR3=y\n", encoding="utf-8")
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
