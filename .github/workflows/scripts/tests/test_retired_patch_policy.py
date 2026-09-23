"""Regression tests for the delivered build policy, not kernel runtime tests."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build import create_build_config, _validate_vendor_patches
from config import BuildConfig
from kernel_builder import KernelBuilder
from patch_policy import (RETIRED_PATCHES, PROTECTED_SOURCE_PATHS,
                          check_patch_policy, verify_no_retired_config)
from patch_utils import parse_optional_patches, read_patch_order

PROJECT = Path(__file__).resolve().parents[4]


class RetiredPatchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_all_eight_aliases_are_rejected_by_csv_config_and_manifest(self):
        for alias in RETIRED_PATCHES:
            with self.subTest(alias=alias):
                with self.assertRaisesRegex(ValueError, "Retired"):
                    parse_optional_patches(alias)
                with self.assertRaisesRegex(ValueError, "Retired"):
                    BuildConfig(sub_level="211", os_patch_level="2026-09",
                                optional_patches=(alias,))
                with self.assertRaisesRegex(ValueError, "Retired"):
                    read_patch_order(PROJECT / "patches/5.15", (alias,))

    def test_mixed_selection_is_not_silently_partially_applied(self):
        with self.assertRaisesRegex(ValueError, "Retired"):
            parse_optional_patches("cpu-scan,memcmp,clear-page")

    def test_bad_selection_fails_before_live_target_resolution(self):
        args = argparse.Namespace(optional_patches="bbrv3")
        with patch("build.resolve_latest_target") as resolver:
            with self.assertRaisesRegex(ValueError, "Retired"):
                create_build_config(args)
            resolver.assert_not_called()

    def test_all_retired_patch_files_were_removed_not_just_disabled(self):
        for filename, _ in RETIRED_PATCHES.values():
            self.assertFalse((PROJECT / "patches/5.15" / filename).exists())
        self.assertEqual(len(list((PROJECT / "patches/5.15").glob("*.patch"))), 4)

    def test_retirement_inventory_is_complete_and_has_original_hashes(self):
        data = json.loads((PROJECT / "patches/5.15/RETIRED_PATCHES.json").read_text())
        self.assertEqual({entry["alias"] for entry in data["patches"]}, set(RETIRED_PATCHES))
        for entry in data["patches"]:
            self.assertEqual(entry["file"], RETIRED_PATCHES[entry["alias"]][0])
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$")

    def test_even_unselected_retired_alias_cannot_be_restored_to_manifest(self):
        (self.root / "safe.patch").write_text("--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n")
        for alias in RETIRED_PATCHES:
            (self.root / "APPLY_ORDER.txt").write_text(f"?{alias}:safe.patch\n")
            with self.subTest(alias=alias), self.assertRaisesRegex(ValueError, "Retired"):
                read_patch_order(self.root)

    def test_even_unlisted_retired_filename_is_rejected(self):
        (self.root / "APPLY_ORDER.txt").write_text("# no selected patches\n")
        for filename, _ in RETIRED_PATCHES.values():
            path = self.root / filename
            path.write_text("not a patch\n")
            with self.subTest(filename=filename), self.assertRaisesRegex(ValueError, "Retired"):
                read_patch_order(self.root)
            path.unlink()

    def test_renaming_a_patch_does_not_allow_protected_source_changes(self):
        path = self.root / "looks-safe.patch"
        for relative in PROTECTED_SOURCE_PATHS:
            path.write_text(f"--- a/{relative}\n+++ b/{relative}\n@@ -1 +1 @@\n-old\n+new\n")
            with self.subTest(relative=relative), self.assertRaisesRegex(ValueError, "protected"):
                check_patch_policy(path)

    def test_rename_away_from_protected_file_is_also_rejected(self):
        path = self.root / "rename.patch"
        path.write_text("--- a/arch/arm64/lib/memcmp.S\n+++ b/safe.S\n")
        with self.assertRaisesRegex(ValueError, "protected"):
            check_patch_policy(path)

    def test_selected_and_unselected_renamed_patches_are_rejected(self):
        path = self.root / "renamed.patch"
        path.write_text("--- a/kernel/power/process.c\n+++ b/kernel/power/process.c\n")
        for manifest in ("!renamed.patch\n", "?cpu-scan:renamed.patch\n", "# unlisted\n"):
            (self.root / "APPLY_ORDER.txt").write_text(manifest)
            with self.subTest(manifest=manifest), self.assertRaisesRegex(ValueError, "protected"):
                read_patch_order(self.root)

    def test_bbr3_cannot_be_reenabled_in_config_as_builtin_or_module(self):
        path = self.root / ".config"
        for setting in ("CONFIG_TCP_CONG_BBR3=y", "CONFIG_TCP_CONG_BBR3=m",
                        "CONFIG_DEFAULT_BBR3=y", 'CONFIG_DEFAULT_TCP_CONG="bbr3"'):
            path.write_text(setting + "\n")
            with self.subTest(setting=setting), self.assertRaisesRegex(RuntimeError, "Retired"):
                verify_no_retired_config(path)

    def test_upstream_tcp_and_disabled_bbr3_config_are_accepted(self):
        path = self.root / ".config"
        for cca in ("cubic", "bbr", "reno"):
            path.write_text(f'CONFIG_DEFAULT_TCP_CONG="{cca}"\n'
                            '# CONFIG_TCP_CONG_BBR3 is not set\n')
            verify_no_retired_config(path)

    def test_default_and_retained_experiments_validate(self):
        for aliases in ((), ("cpu-scan",), ("clear-page",), ("cpu-scan", "clear-page")):
            config = BuildConfig(sub_level="211", os_patch_level="2026-09", optional_patches=aliases)
            self.assertEqual(_validate_vendor_patches(config), [])

    def test_mutated_config_cannot_bypass_preflight(self):
        config = BuildConfig(sub_level="211", os_patch_level="2026-09")
        builder = KernelBuilder(config, str(self.root))
        config.optional_patches = ("bbrv3",)
        with self.assertRaisesRegex(ValueError, "Retired"):
            builder._preflight()

    def test_compile_is_not_started_after_source_guard_failure(self):
        builder = KernelBuilder(BuildConfig(sub_level="211", os_patch_level="2026-09"), str(self.root))
        with patch.object(builder, "_chdir"), \
             patch.object(builder, "_verify_patch_safety", side_effect=RuntimeError("source changed")), \
             patch.object(builder, "_run_build_with_log") as compile_command:
            self.assertFalse(builder.build_kernel())
            compile_command.assert_not_called()

    def test_packaging_is_not_started_after_source_guard_failure(self):
        builder = KernelBuilder(BuildConfig(sub_level="211", os_patch_level="2026-09"), str(self.root))
        with patch.object(builder, "_verify_patch_safety", side_effect=RuntimeError("source changed")), \
             patch.object(builder, "_run_cmd") as shell_command:
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                builder.create_anykernel_zips()
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                builder.create_boot_image()
            shell_command.assert_not_called()

    def test_no_bbr3_configuration_path_remains_in_builder(self):
        self.assertFalse(hasattr(KernelBuilder, "BBR3_CONFIG_UPDATES"))
        self.assertFalse(hasattr(KernelBuilder, "_verify_bbr3_config"))

    def test_source_safety_evidence_is_packaged_and_checksummed(self):
        workflow = (PROJECT / ".github/workflows/kernel-build.yml").read_text()
        self.assertIn("manifest.lock.xml source-safety.json; do", workflow)
        self.assertIn("manifest.lock.xml source-safety.json artifact_stem.txt", workflow)
        self.assertIn("**/source-safety.json", workflow)


if __name__ == "__main__":
    unittest.main()
