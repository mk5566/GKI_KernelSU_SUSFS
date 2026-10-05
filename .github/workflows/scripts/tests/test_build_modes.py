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
from config import ANDROID_FAMILY, BuildConfig, REPO_ROOT, SUKISU_STABLE_REVISION
from kernel_builder import KernelBuilder, ensure_cmdline_tokens, unquote_kconfig_string
from patch_plan import make_patch_plan
from target import choose_gki_tag, choose_manifest_branch, peeled_commit, resolve_sukisu, resolve_susfs, resolve_sukisu_patch, certified_releases
from abi import compare_symvers, read_symvers


class ResolverTests(unittest.TestCase):
    def test_monthly_release_excludes_uncertified_point_tags(self):
        self.assertEqual(ANDROID_FAMILY, "android13-5.15")
        refs = {
            "android13-5.15-2026-09_r1": "a" * 40,
            "android13-5.15-2026-09_r1^{}": "b" * 40,
            "android13-5.15.216_r00": "c" * 40,
            "android13-5.15.216_r00^{}": "d" * 40,
            "android13-5.15-2026-09_r2": "e" * 40,
            "android14-5.15.300_r00": "f" * 40,
        }
        self.assertEqual(choose_gki_tag(refs), "android13-5.15-2026-09_r2")
        self.assertEqual(peeled_commit(refs, "android13-5.15-2026-09_r1"), "b" * 40)
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

    def test_only_published_certified_download_rows_are_eligible(self):
        row = ('<tr><a href="https://ci.android.com/builds/submitted/123/kernel_aarch64/latest">kernel</a>'
               '<a href="https://dl.google.com/android/gki/gki-certified-boot-'
               'android13-5.15-2026-09_r2.zip">boot</a></tr>')
        debug = row.replace('gki-certified-boot-', 'debug-boot-').replace('_r2', '_r99')
        self.assertEqual(certified_releases(row + debug), {"android13-5.15-2026-09_r2": "123"})
        with self.assertRaises(RuntimeError):
            certified_releases(debug)

    def test_frozen_symbol_crc_and_export_presence_are_required(self):
        reference = read_symvers('0x1234\talloc_pages\tvmlinux\tEXPORT_SYMBOL\n')
        self.assertEqual(compare_symvers(reference, {"alloc_pages": "0x1234", "root": "0xffff"}), 1)
        for built in ({}, {"alloc_pages": "0x4321"}):
            with self.subTest(built=built), self.assertRaisesRegex(RuntimeError, "GKI ABI mismatch"):
                compare_symvers(reference, built)
        with self.assertRaises(RuntimeError):
            read_symvers('<html>artifact unavailable</html>')

    def test_sukisu_stable_and_dev_are_immutable(self):
        self.assertEqual(resolve_sukisu("stable"), ("v4.2.0-reviewed-uapi4", SUKISU_STABLE_REVISION))
        with patch("target._git", return_value="ref: refs/heads/main\tHEAD\n" +
                   "c" * 40 + "\tHEAD\n"):
            self.assertEqual(resolve_sukisu("dev"), ("", "c" * 40))

    def test_susfs_and_patch_resolvers(self):
        with patch("target._git", return_value="a" * 40 + "\trefs/heads/gki-android13-5.15\n"):
            self.assertEqual(resolve_susfs(), "a" * 40)
        with patch("target._git", return_value="b" * 40 + "\trefs/heads/main\n"):
            self.assertEqual(resolve_sukisu_patch(), "b" * 40)

    def test_a_later_certified_month_wins(self):
        published = {
            "android13-5.15-2026-06_r5": "16466989",
            "android13-5.15-2026-09_r2": "16464335",
            "android13-5.15-2026-10_r1": "17000000",
        }
        self.assertEqual(choose_gki_tag(published), "android13-5.15-2026-10_r1")
        self.assertEqual(choose_gki_tag({
            "android13-5.15-2026-10_r1": "1",
            "android13-5.15-2027-01_r1": "2",
        }), "android13-5.15-2027-01_r1")

    def test_production_sources_do_not_pin_a_monthly_release(self):
        needles = ("android13-5.15-2026-09", "5.15.211")
        offenders = []
        for path in (REPO_ROOT / ".github" / "workflows" / "scripts").rglob("*.py"):
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            offenders.extend(f"{path.name}:{needle}" for needle in needles if needle in text)
        workflow = (REPO_ROOT / ".github" / "workflows" / "kernel-build.yml").read_text(encoding="utf-8")
        offenders.extend(f"kernel-build.yml:{needle}" for needle in needles if needle in workflow)
        self.assertEqual(offenders, [])

    def test_artifact_names_stay_on_the_existing_scheme(self):
        for channel, version in (("stable", "5.15.230"), ("dev", "5.15.190")):
            stem = BuildConfig(channel, kernel_version=version).artifact_stem
            sublevel = version.rsplit(".", 1)[-1]
            self.assertEqual(stem, f"android13-5.15.{sublevel}-sukisu-{channel}")
            for word in ("mglru", "lazy", "teo", "optimi", "tuned", "custom"):
                self.assertNotIn(word, stem.lower())


class ModeTests(unittest.TestCase):
    def test_one_patch_plan_always_includes_lz4_zram_bbr_and_tweaks(self):
        plan = make_patch_plan()
        self.assertTrue(plan.lz4 and plan.zram and plan.bbr and plan.tweaks)
        self.assertEqual(len(plan.all), len(plan.lz4) + len(plan.zram) + len(plan.bbr) + len(plan.tweaks))
        names = [path.name for path in plan.all]
        self.assertIn("0002-crypto-lz4-1.10.0.patch", names)
        self.assertIn("lz4kd-integration.patch", names)
        self.assertIn("0001-bbrv3-android-kabi.patch", names)
        self.assertNotIn("silence_irq_cpu_logspam.patch", names)
        self.assertNotIn("optimise_memcmp.patch", names)
        self.assertNotIn("reduce_freeze_timeout.patch", names)
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
                            "CONFIG_CPU_FREQ_GOV_CONSERVATIVE=y\nCONFIG_KPM=y\n"
                            "CONFIG_DEBUG_INFO=y\nCONFIG_DEBUG_INFO_DWARF4=y\n"
                            "CONFIG_DEBUG_INFO_BTF=y\nCONFIG_MODULE_ALLOW_BTF_MISMATCH=y\n",
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
            self.assertIn("CONFIG_IOSCHED_BFQ=y", result)
            self.assertIn("CONFIG_CPU_FREQ_GOV_CONSERVATIVE=y", result)
            self.assertIn("CONFIG_KPM=n", result)
            for symbol in ("DEBUG_INFO", "DEBUG_INFO_DWARF4", "DEBUG_INFO_BTF",
                           "MODULE_ALLOW_BTF_MISMATCH"):
                self.assertIn(f"CONFIG_{symbol}=y", result)

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

    def test_builtin_zram_leaves_an_exact_empty_module_list(self):
        with tempfile.TemporaryDirectory() as d:
            builder = KernelBuilder(BuildConfig(), d)
            defconfig = builder._defconfig_path()
            defconfig.parent.mkdir(parents=True, exist_ok=True)
            defconfig.write_text("CONFIG_ZRAM=m\n", encoding="utf-8")
            android = builder.work_dir / "common/android"
            android.mkdir(parents=True)
            empty = android / "gki_aarch64_modules"
            mixed = android / "gki_aarch64_modules_test"
            empty.write_text("drivers/block/zram/zram.ko\nmm/zsmalloc.ko\n",
                             encoding="utf-8")
            mixed.write_text("drivers/block/zram/zram.ko\ndrivers/block/loop.ko\n",
                             encoding="utf-8")
            builder._configure_zram()
            self.assertEqual(empty.read_bytes(), b"")
            self.assertEqual(mixed.read_bytes(), b"drivers/block/loop.ko\n")

    def test_final_config_rejects_a_layout_that_is_not_this_kernel(self):
        with tempfile.TemporaryDirectory() as d:
            builder = KernelBuilder(BuildConfig(), d)
            file = Path(d) / ".config"
            file.write_text("CONFIG_TCP_CONG_BBR3=n\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Final .config mismatch"):
                builder._verify_generated_config(file)

    def test_rejected_patch_cannot_leave_a_partial_integration(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q", d], check=True)
            (root / "a.txt").write_text("old\n", encoding="utf-8")
            (root / "b.txt").write_text("unexpected\n", encoding="utf-8")
            patch_file = root / "two-files.patch"
            patch_file.write_text(
                "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
                "@@ -1 +1 @@\n-old\n+new\n"
                "diff --git a/b.txt b/b.txt\n--- a/b.txt\n+++ b/b.txt\n"
                "@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8")
            builder = KernelBuilder(BuildConfig(), d)
            builder.shell.cwd = d
            with self.assertRaisesRegex(RuntimeError, "two-files.patch"):
                builder._apply_patch_file(patch_file)
            self.assertEqual((root / "a.txt").read_text(), "old\n")
            self.assertEqual((root / "b.txt").read_text(), "unexpected\n")
            self.assertEqual(builder.applied_integration_patches, [])

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


class RuntimeConfigTests(unittest.TestCase):
    TOKENS = KernelBuilder.CMDLINE_TOKENS
    PRESERVED = ("stack_depot_disable=on kasan.stacktrace=off "
                 "kvm-arm.mode=protected cgroup_disable=pressure")

    def test_cmdline_tokens_are_appended_once(self):
        once = ensure_cmdline_tokens(self.PRESERVED, self.TOKENS)
        twice = ensure_cmdline_tokens(once, self.TOKENS)
        self.assertEqual(once, twice)
        self.assertTrue(once.startswith(self.PRESERVED + " "))
        self.assertEqual(once.split()[:4], self.PRESERVED.split())
        for token in self.TOKENS:
            self.assertEqual(once.count(token), 1)
        self.assertEqual(ensure_cmdline_tokens("  ", self.TOKENS), " ".join(self.TOKENS))
        self.assertEqual(unquote_kconfig_string('"' + once + '"'), once)

    def test_cmdline_replaces_a_conflicting_value_without_duplicating(self):
        raw = self.PRESERVED + " cpuidle.governor=menu rcu_nocbs=0 cpuidle.governor=teo"
        result = ensure_cmdline_tokens(raw, self.TOKENS)
        self.assertEqual(result.count("cpuidle.governor=teo"), 1)
        self.assertEqual(result.count("rcu_nocbs=all"), 1)
        self.assertNotIn("cpuidle.governor=menu", result)
        self.assertNotIn("rcu_nocbs=0", result)
        self.assertEqual(ensure_cmdline_tokens(result + " " + " ".join(self.TOKENS), self.TOKENS), result)
        self.assertNotIn("rcutree.enable_rcu_lazy=", result)

    def _prepare(self, root, text):
        builder = KernelBuilder(BuildConfig(), root)
        path = builder._defconfig_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        kconfig = builder.work_dir / "KernelSU/kernel/Kconfig"
        kconfig.parent.mkdir(parents=True, exist_ok=True)
        kconfig.write_text("config KSU_SUSFS\nconfig KSU_SUSFS_SUS_MOUNT\n", encoding="utf-8")
        return builder, path

    def test_defconfig_enables_mglru_lazy_rcu_and_teo_once(self):
        with tempfile.TemporaryDirectory() as root:
            original = (
                'CONFIG_CMDLINE="' + self.PRESERVED + '"\n'
                "CONFIG_CMDLINE_EXTEND=y\n"
                "CONFIG_RCU_EXPERT=y\n"
                "CONFIG_RCU_NOCB_CPU=y\n"
                "CONFIG_RCU_LAZY=y\n"
                "CONFIG_RCU_LAZY_DEFAULT_OFF=y\n"
                "CONFIG_LRU_GEN=y\n"
                "CONFIG_CPU_IDLE=y\n"
                "CONFIG_CPU_IDLE_GOV_MENU=y\n"
                "CONFIG_CPU_IDLE_GOV_TEO=y\n"
                "CONFIG_CPU_FREQ_GOV_CONSERVATIVE=y\n"
            )
            builder, path = self._prepare(root, original)
            previous = Path.cwd()
            try:
                builder.configure_kernel()
                builder.configure_kernel()
            finally:
                os.chdir(previous)
            result = path.read_text(encoding="utf-8")
            cmdline = unquote_kconfig_string(builder._defconfig_assignment("CONFIG_CMDLINE"))
            self.assertEqual(cmdline, ensure_cmdline_tokens(self.PRESERVED, self.TOKENS))
            self.assertEqual(result.count("cpuidle.governor=teo"), 1)
            self.assertEqual(result.count("rcu_nocbs=all"), 1)
            self.assertIn("CONFIG_LRU_GEN=y", result)
            self.assertIn("CONFIG_LRU_GEN_ENABLED=y", result)
            self.assertNotIn("CONFIG_LRU_GEN_STATS", result)
            self.assertIn("CONFIG_RCU_LAZY=y", result)
            self.assertIn("CONFIG_RCU_LAZY_DEFAULT_OFF=n", result)
            self.assertIn("CONFIG_RCU_NOCB_CPU=y", result)
            self.assertIn("CONFIG_CPU_IDLE_GOV_MENU=y", result)
            self.assertIn("CONFIG_CPU_IDLE_GOV_TEO=y", result)
            self.assertIn("CONFIG_CMDLINE_EXTEND=y", result)
            self.assertIn("CONFIG_CMDLINE_FORCE=n", result)
            self.assertIn("CONFIG_CPU_FREQ_GOV_CONSERVATIVE=y", result)
            self.assertNotIn("rcutree.enable_rcu_lazy=", result)

    def _valid_config(self, **overrides):
        values = {
            "CONFIG_KSU": "y", "CONFIG_KSU_SUSFS": "y",
            "CONFIG_KSU_SUSFS_SUS_MOUNT": "y", "CONFIG_KPROBES": "y",
            "CONFIG_KRETPROBES": "y", "CONFIG_HAVE_SYSCALL_TRACEPOINTS": "y",
            "CONFIG_NET_SCH_FQ": "y", "CONFIG_ZRAM": "y", "CONFIG_ZSMALLOC": "y",
            "CONFIG_CRYPTO_LZ4KD": "y", "CONFIG_LZ4KD_COMPRESS": "y",
            "CONFIG_LZ4KD_DECOMPRESS": "y", "CONFIG_ZRAM_DEF_COMP_LZ4KD": "y",
            "CONFIG_ZRAM_DEF_COMP": '"lz4kd"', "CONFIG_ZRAM_WRITEBACK": "y",
            "CONFIG_TCP_CONG_BBR": "y", "CONFIG_TCP_CONG_BBR3": "y",
            "CONFIG_DEFAULT_TCP_CONG": '"bbr3"', "CONFIG_CRYPTO_LZ4": "y",
            "CONFIG_MODVERSIONS": "y", "CONFIG_CFI_CLANG": "y",
            "CONFIG_LTO_CLANG_FULL": "y", "CONFIG_DEBUG_INFO": "y",
            "CONFIG_DEBUG_INFO_BTF": "y", "CONFIG_DEBUG_INFO_BTF_MODULES": "y",
            "CONFIG_CMDLINE": '"' + ensure_cmdline_tokens(self.PRESERVED, self.TOKENS) + '"',
        }
        values.update(KernelBuilder.RUNTIME_CONFIG)
        values.update(overrides)
        lines = []
        for key, value in values.items():
            lines.append(f"# {key} is not set" if value == "n" else f"{key}={value}")
        return "\n".join(lines) + "\n"

    def test_final_config_accepts_the_runtime_profile(self):
        with tempfile.TemporaryDirectory() as root:
            builder = KernelBuilder(BuildConfig(), root)
            path = Path(root) / ".config"
            path.write_text(self._valid_config(), encoding="utf-8")
            builder._verify_generated_config(path)

    def test_final_config_rejects_a_missing_runtime_selection(self):
        cases = {
            "mglru": {"CONFIG_LRU_GEN_ENABLED": "n"},
            "mglru stats": {"CONFIG_LRU_GEN_STATS": "y"},
            "lazy default": {"CONFIG_RCU_LAZY_DEFAULT_OFF": "y"},
            "nocb": {"CONFIG_RCU_NOCB_CPU": "n"},
            "menu": {"CONFIG_CPU_IDLE_GOV_MENU": "n"},
            "teo": {"CONFIG_CPU_IDLE_GOV_TEO": "n"},
            "forced cmdline": {"CONFIG_CMDLINE_FORCE": "y"},
            "duplicate teo": {"CONFIG_CMDLINE": '"cpuidle.governor=teo cpuidle.governor=teo rcu_nocbs=all"'},
            "missing nocbs": {"CONFIG_CMDLINE": '"cpuidle.governor=teo"'},
            "second lazy switch": {"CONFIG_CMDLINE": '"' + ensure_cmdline_tokens(
                self.PRESERVED, self.TOKENS) + ' rcutree.enable_rcu_lazy=1"'},
        }
        with tempfile.TemporaryDirectory() as root:
            builder = KernelBuilder(BuildConfig(), root)
            path = Path(root) / ".config"
            for name, overrides in cases.items():
                with self.subTest(name=name):
                    path.write_text(self._valid_config(**overrides), encoding="utf-8")
                    with self.assertRaisesRegex(RuntimeError, "Final .config"):
                        builder._verify_generated_config(path)


if __name__ == "__main__":
    unittest.main()
