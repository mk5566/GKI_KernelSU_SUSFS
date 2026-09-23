import base64
import io
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from target import TargetSelection, parse_makefile_version, resolve_latest_target, resolve_latest_target_selection
from config import BuildConfig
from build import create_build_config


class TargetTests(unittest.TestCase):
    def test_makefile_rejects_wrong_kernel(self):
        self.assertEqual(parse_makefile_version("VERSION = 5\nPATCHLEVEL = 15\nSUBLEVEL = 211\n"), "211")
        with self.assertRaisesRegex(RuntimeError, "Expected a 5.15 kernel"):
            parse_makefile_version("VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 1\n")

    def test_resolves_newest_manifest_month_and_actual_sublevel(self):
        refs = ("a\trefs/heads/common-android13-5.15-2026-06\n"
                "b\trefs/heads/common-android13-5.15-2026-09\n")
        payload = base64.b64encode(b"VERSION = 5\nPATCHLEVEL = 15\nSUBLEVEL = 212\n")
        response = io.BytesIO(payload)
        common_sha = "a" * 40
        common_ref = f"{common_sha}\trefs/heads/android13-5.15-2026-09\n"
        with patch("target.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, refs),
            subprocess.CompletedProcess([], 0, common_ref),
        ]), \
             patch("target.urlopen", return_value=response) as request:
            selection = resolve_latest_target_selection()
        self.assertEqual(selection, TargetSelection("212", "2026-09", common_sha))
        self.assertIn(common_sha, request.call_args.args[0])

    def test_target_selection_round_trip_and_rejects_invalid_sha(self):
        selection = TargetSelection("211", "2026-09", "a" * 40)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "target.json"
            selection.write(path)
            self.assertEqual(TargetSelection.from_file(path), selection)
            path.write_text('{"schema": 1, "sublevel": "180", "month": "2025-05", '
                            '"common_revision": "not-a-sha"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "common revision"):
                TargetSelection.from_file(path)

    def test_selected_target_does_not_resolve_again(self):
        args = SimpleNamespace(
            sub_level="auto", os_patch="auto", android="android13",
            kernel="5.15", ksu_version="Dev(development)",
            ksu_commit=None, susfs_commit=None, zram=False, bbr=False,
            no_release=True, custom_version=None, optional_patches="",
        )
        selection = TargetSelection("211", "2026-09", "a" * 40)
        with patch("build.resolve_latest_target", side_effect=AssertionError("re-resolved")):
            config = create_build_config(args, selection)
        self.assertEqual(config.sub_level, "211")

    def test_no_monthly_branch_fails(self):
        with patch("target.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "")):
            with self.assertRaisesRegex(RuntimeError, "No dated"):
                resolve_latest_target()

    def test_future_sublevel_uses_same_5_15_patch_family(self):
        config = BuildConfig(sub_level="212", os_patch_level="2026-09")
        self.assertEqual(config.config_name, "android13-5.15-212")
        self.assertEqual(config.formatted_branch, "android13-5.15-2026-09")

    def test_cli_rejects_old_explicit_target(self):
        args = SimpleNamespace(
            sub_level="180", os_patch="2025-05", android="android13",
            kernel="5.15", ksu_version="Dev(development)",
            ksu_commit=None, susfs_commit=None, zram=False, bbr=False,
            no_release=True, custom_version=None, optional_patches="",
        )
        with patch("build.resolve_latest_target", return_value=("211", "2026-09")):
            with self.assertRaisesRegex(ValueError, "latest GKI"):
                create_build_config(args)
