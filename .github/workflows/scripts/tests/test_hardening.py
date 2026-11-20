"""Fail-closed regression coverage for builder and evidence boundaries."""
import contextlib
import io
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from abi import read_symvers, compare_symvers, read_exports, compare_exports
from boot_validation import verify_boot_image
from build import parse_args, main
from build_runtime import ShellCommand, log_excerpt, validate_build_environment
from config import BuildConfig, REPO_SYNC_PROJECTS, REPO_ROOT
from config_checks import ConfigPolicy
from integrity import atomic_json, read_json
from kernel_builder import KernelBuilder
from patch_plan import make_patch_plan
from source_lock import local_sources, builder_revision
from upstream_policy import make_upstream_status, expected_reviewed_ports, expected_actions


def valid_build_config():
    config = BuildConfig(gki_tag="android13-5.15-2026-11_r1", gki_commit="a" * 40,
                       manifest_branch="common-android13-5.15-2026-11", manifest_commit="b" * 40,
                       kernel_version="5.15.230", official_build_id="12345678",
                       source_projects=tuple((f"kernel/{p}", p, "a" * 40) for p in REPO_SYNC_PROJECTS),
                       sukisu_commit="c" * 40, susfs_commit="d" * 40, sukisu_patch_commit="e" * 40,
                       anykernel_commit="f" * 40, repo_commit="1" * 40)
    config.upstream_status = make_upstream_status(config, {
        "reviewed_ports": expected_reviewed_ports(), "actions": expected_actions()}, [])
    return config


def boot_bytes(payload: bytes, version: int = 4) -> bytes:
    page = 4096 if version >= 3 else 2048
    header = bytearray(page)
    header[:8] = b"ANDROID!"
    struct.pack_into("<I", header, 8, len(payload))
    struct.pack_into("<I", header, 40, version)
    if version in (3, 4):
        struct.pack_into("<I", header, 20, 1580 if version == 3 else 1584)
    else:
        struct.pack_into("<I", header, 36, page)
    return header + payload


class SymversHardeningTests(unittest.TestCase):
    ROW = "0x12345678\talloc_pages\tvmlinux\tEXPORT_SYMBOL\n"

    def test_malformed_rows_cannot_be_silently_discarded(self):
        for bad in ("oops", "0x1 missing vmlinux", "0x100000000 bad vmlinux EXPORT_SYMBOL",
                    "0x1234 bad vmlinux NOT_AN_EXPORT", "0x1 a vmlinux EXPORT_SYMBOL ns extra",
                    "wrong a module EXPORT_SYMBOL", "0x1 9bad vmlinux EXPORT_SYMBOL"):
            with self.subTest(row=bad), self.assertRaisesRegex(RuntimeError, "Malformed"):
                read_symvers(self.ROW + bad)

    def test_duplicate_symbols_are_fatal_even_with_identical_crcs(self):
        for duplicate in (self.ROW, self.ROW.replace("12345678", "87654321"),
                          self.ROW.replace("vmlinux", "drivers/test")):
            with self.subTest(row=duplicate), self.assertRaisesRegex(RuntimeError, "Duplicate"):
                read_symvers(self.ROW + duplicate)

    def test_module_exports_and_namespaces_are_parsed_without_widening_crc_scope(self):
        result = read_symvers(self.ROW + "0xABCDEF01 module_api test/mod EXPORT_SYMBOL_GPL ns\n")
        self.assertEqual(result, {"alloc_pages": "0x12345678"})
        self.assertEqual(read_symvers("0X000012AB x vmlinux EXPORT_SYMBOL_GPL ns\n"), {"x": "0x000012ab"})
        self.assertEqual(compare_symvers({"x": "0x000012ab"}, {"x": "0x12AB", "extra": "0x1"}), 1)
        with self.assertRaises(RuntimeError):
            compare_symvers({}, {})

    def test_equal_crcs_do_not_mask_export_policy_changes(self):
        reference = read_exports(self.ROW)
        self.assertEqual(compare_exports(reference, reference), 1)
        for changed in (self.ROW.replace("EXPORT_SYMBOL", "EXPORT_SYMBOL_GPL"),
                        self.ROW.rstrip() + " new_namespace\n"):
            with self.subTest(row=changed), self.assertRaisesRegex(RuntimeError, "metadata mismatch"):
                compare_exports(reference, read_exports(changed))


class BootValidationTests(unittest.TestCase):
    def test_exact_kernel_identity_for_all_supported_header_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = root / "Image"
            image.write_bytes(b"kernel" * 137)
            boot = root / "boot.img"
            for version in range(5):
                with self.subTest(version=version):
                    boot.write_bytes(boot_bytes(image.read_bytes(), version))
                    report = verify_boot_image(boot, image)
                    self.assertEqual(report["header_version"], version)
                    self.assertEqual(report["kernel_size"], image.stat().st_size)

    def test_malformed_truncated_or_different_payload_is_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image, boot = root / "Image", root / "boot.img"
            image.write_bytes(b"original kernel")
            correct = boot_bytes(image.read_bytes())
            bad_header = bytearray(correct)
            struct.pack_into("<I", bad_header, 20, 1)
            bad_legacy = bytearray(boot_bytes(image.read_bytes(), 1))
            struct.pack_into("<I", bad_legacy, 36, 4095)
            for data in (b"not a boot image", correct[:-1], correct[:-1] + b"!", bad_header,
                         boot_bytes(b"longer different payload"), boot_bytes(b"", 4),
                         boot_bytes(image.read_bytes(), 5), bad_legacy):
                with self.subTest(length=len(data)), self.assertRaises(RuntimeError):
                    boot.write_bytes(data)
                    verify_boot_image(boot, image)


class ProcessAndWorkspaceTests(unittest.TestCase):
    def test_runner_provisioning_refuses_local_and_self_hosted_execution(self):
        helper = REPO_ROOT / ".github/workflows/scripts/ci_runner.sh"
        for mode in ("dependencies", "memory"):
            for hosted in ({}, {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "self-hosted"}):
                with self.subTest(mode=mode, env=hosted):
                    result = subprocess.run(["bash", str(helper), mode],
                                            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **hosted},
                                            text=True, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("disposable GitHub-hosted", result.stderr)

    def test_empty_environment_is_not_replaced_with_ambient_secrets(self):
        shell = ShellCommand(env={})
        self.assertEqual(shell.env, {})
        with patch("build_runtime.subprocess.run") as run:
            shell.run(["echo", "a; exit 1"])
            self.assertFalse(run.call_args.kwargs["shell"])
            self.assertEqual(run.call_args.kwargs["env"], {})

    def test_chdir_does_not_change_process_global_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = KernelBuilder(BuildConfig(), temp)
            before = Path.cwd()
            builder._chdir(builder.work_dir)
            self.assertEqual(Path.cwd(), before)
            self.assertEqual(builder.shell.cwd, str(builder.work_dir))

    def test_git_identity_is_process_scoped_and_preserves_prior_config_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = KernelBuilder(BuildConfig(), temp)
            builder.env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="test.keep", GIT_CONFIG_VALUE_0="yes")
            before = os.environ.copy()
            with patch.object(builder, "_run_cmd") as run:
                builder._ensure_git_identity()
                run.assert_not_called()
            self.assertEqual(builder.env["GIT_CONFIG_COUNT"], "3")
            self.assertEqual(builder.env["GIT_CONFIG_VALUE_0"], "yes")
            self.assertNotIn("safe.directory", builder.env.values())
            self.assertEqual(dict(os.environ), before)
            result = subprocess.run(["git", "config", "user.email"], env=builder.env,
                                    text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), "gki-builder@localhost")

    def test_ambient_build_bypasses_and_tool_overrides_are_rejected(self):
        for key, value in (("SKIP_DEFCONFIG", "1"), ("OUT_DIR", "/stale"), ("CC", "clang"),
                           ("LTO", "thin"), ("BUILD_CONFIG_FRAGMENTS", "unsafe"), ("MAKEFLAGS", "-e"),
                           ("HERMETIC_TOOLCHAIN", "0"), ("BASH_ENV", "/unreviewed"),
                           ("POST_KERNEL_BUILD_CMDS", "exit 0")):
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, key):
                validate_build_environment({key: value})
        validate_build_environment({"PATH": "/usr/bin", "SKIP_DEFCONFIG": "", "MALLOC_ARENA_MAX": "2"})

    def test_rejected_workspace_preserves_existing_failure_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = KernelBuilder(valid_build_config(), temp)
            original = builder.workspace / "BUILD_INFO.md"
            original.write_bytes(b"existing evidence must survive")
            with patch("kernel_builder.logger"), patch("kernel_builder.write_sources") as write:
                result = builder.build()
            self.assertFalse(result.success)
            self.assertIn("fresh workspace", result.message)
            self.assertEqual(original.read_bytes(), b"existing evidence must survive")
            write.assert_not_called()
            self.assertFalse((builder.workspace / ".build.lock").exists())

    def test_competing_workspace_lock_is_not_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = KernelBuilder(valid_build_config(), temp)
            lock = builder.workspace / ".build.lock"
            lock.write_text("other process")
            with patch("kernel_builder.logger"):
                result = builder.build()
            self.assertFalse(result.success)
            self.assertEqual(lock.read_text(), "other process")
            self.assertFalse((builder.workspace / "BUILD_INFO.md").exists())

    def test_module_list_filter_is_exact_not_substring_based(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = KernelBuilder(BuildConfig(), temp)
            config = builder._defconfig_path()
            config.parent.mkdir(parents=True)
            config.write_text("CONFIG_ZRAM=m\n")
            android = builder.work_dir / "common/android"
            android.mkdir()
            modules = android / "gki_modules"
            kept = "# zram is built-in\ndrivers/test/zram_helper.ko\nmm/zsmalloc_debug.ko\n"
            modules.write_text(kept + "drivers/block/zram/zram.ko\nmm/zsmalloc.ko\n")
            builder._configure_zram()
            self.assertEqual(modules.read_text(), kept)

    def test_failed_packaging_always_removes_staged_image(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = KernelBuilder(BuildConfig(), temp)
            image = builder._kernel_image_path()
            image.parent.mkdir(parents=True)
            image.write_bytes(b"image")
            builder.anykernel_dir.mkdir()
            with patch("kernel_builder.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["zip"])):
                with self.assertRaises(subprocess.CalledProcessError):
                    builder.create_anykernel_zips()
            self.assertFalse((builder.anykernel_dir / "Image").exists())

    def test_post_compile_error_is_preserved_not_replaced_with_generic_compile_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = KernelBuilder(BuildConfig(), temp)
            image = builder._kernel_image_path()
            image.parent.mkdir(parents=True)
            image.write_bytes(b"image")
            with patch.object(builder, "_run_kernel_build", return_value=subprocess.CompletedProcess([], 0)), \
                 patch.object(builder, "_verify_generated_config", side_effect=RuntimeError("CFI validation failure")), \
                 patch("kernel_builder.logger"):
                self.assertFalse(builder.build_kernel())
            self.assertIn("CFI validation failure", builder.build_error_excerpt)
            self.assertEqual(builder.abi_symbol_count, 0)

    def test_phase_timing_records_failure_without_swallowing_it(self):
        with tempfile.TemporaryDirectory() as temp:
            builder = KernelBuilder(BuildConfig(), temp)
            def failure():
                raise RuntimeError("phase failed")
            with self.assertRaisesRegex(RuntimeError, "phase failed"):
                builder._phase("test", failure)
            record = read_json(builder.workspace / "BUILD_TIMINGS.json")["test"]
            self.assertEqual(record["status"], "failed")
            self.assertGreaterEqual(record["elapsed_seconds"], 0)

    def test_log_excerpt_keeps_errors_without_reading_whole_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "build.log"
            with path.open("w") as stream:
                stream.write("translation unit error: root cause\n")
                for _ in range(20000):
                    stream.write("normal compiler output\n")
            with patch.object(Path, "read_text", side_effect=AssertionError("not bounded")):
                result = log_excerpt(path)
            self.assertIn("error: root cause", result)
            self.assertLessEqual(len(result), 6000)

    def test_log_excerpt_clips_a_single_huge_line(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "build.log"
            path.write_text("error: " + "x" * 1_000_000 + "\nlast line\n")
            result = log_excerpt(path)
            self.assertTrue(result.startswith("error: "))
            self.assertLessEqual(len(result), 6000)


class InputAndProvenanceTests(unittest.TestCase):
    def test_conflicting_cli_modes_fail_before_network_resolution(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--resolve-only", "--preflight-only"])
        with patch("build.load_sources") as load, self.assertRaisesRegex(RuntimeError, "strict-upstreams"):
            main(["--sources", "unused.json", "--strict-upstreams"])
        load.assert_not_called()

    def test_malformed_project_shapes_and_invalid_months_are_rejected(self):
        for projects in ("common", ["abc"], [("only", "two")], [("a", "b", 123)]):
            with self.subTest(projects=projects), self.assertRaisesRegex(RuntimeError, "triples"):
                BuildConfig(source_projects=projects)
        config = valid_build_config()
        config.gki_tag = "android13-5.15-2026-13_r1"
        config.manifest_branch = "common-android13-5.15-2026-13"
        with self.assertRaises(RuntimeError):
            config.validate()

    def test_json_duplicate_keys_are_rejected_at_any_depth(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "record.json"
            for data in ('{"x":1,"x":2}', '{"a":{"x":1,"x":1}}', '[]', '{"x":NaN}', '{"x":Infinity}'):
                path.write_text(data)
                with self.subTest(data=data), self.assertRaises(RuntimeError):
                    read_json(path)

    def test_failed_atomic_write_does_not_truncate_previous_record(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "record.json"
            atomic_json(path, {"before": True})
            with patch("integrity.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    atomic_json(path, {"after": True})
            self.assertEqual(read_json(path), {"before": True})
            self.assertEqual(list(Path(temp).iterdir()), [path])

    def test_source_hashes_cover_shell_policy_and_documentation(self):
        hashes = local_sources()
        for path in ("README.md", "AGENTS.md", ".github/workflows/scripts/ci_runner.sh",
                     ".github/workflows/scripts/kernel_profile.py", ".github/workflows/scripts/config_checks.py"):
            self.assertIn(path, hashes)

    def test_dangling_input_symlinks_are_not_silently_omitted(self):
        with tempfile.TemporaryDirectory() as temp, patch("source_lock.REPO_ROOT", Path(temp)):
            root = Path(temp)
            (root / "patches").mkdir()
            (root / "patches/missing.patch").symlink_to(root / "does-not-exist")
            with self.assertRaisesRegex(RuntimeError, "symlinks"):
                local_sources()

    def test_real_git_provenance_requires_actual_commit(self):
        with tempfile.TemporaryDirectory() as temp, patch("source_lock.REPO_ROOT", Path(temp)):
            with self.assertRaisesRegex(RuntimeError, "Git checkout"):
                builder_revision()
            subprocess.run(["git", "init", "-q", temp], check=True)
            subprocess.run(["git", "-C", temp, "-c", "user.name=Test", "-c", "user.email=test@invalid",
                            "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
            self.assertRegex(builder_revision(), r"^[0-9a-f]{40}$")

    def test_patch_plan_reports_actual_order(self):
        plan = make_patch_plan()
        self.assertEqual(plan.all, plan.lz4 + plan.bbr + plan.tweaks + plan.zram)

    def test_malformed_config_assignment_is_not_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".config"
            path.write_text("CONFIG_KSU\n")
            with self.assertRaisesRegex(RuntimeError, "Malformed"):
                ConfigPolicy.parse(path)

    def test_workflow_uses_pinned_actions_and_preserves_manual_only_trigger(self):
        text = (REPO_ROOT / ".github/workflows/kernel-build.yml").read_text()
        import re
        uses = re.findall(r"uses: ([^\s]+)", text)
        self.assertEqual(len(uses), 5)
        for action in uses:
            self.assertRegex(action, r"^actions/[a-z-]+@[0-9a-f]{40}$")
        self.assertIn("persist-credentials: false", text)
        self.assertIn("python-version: '3.12'", text)
        self.assertLess(text.index("Run fast offline"), text.index("Install kernel"))
        self.assertNotIn("actions/cache", text)
        trigger = text.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual(re.findall(r"^  ([a-z_]+):", trigger, re.M), ["workflow_dispatch"])
        self.assertIn("default: stable", trigger)


if __name__ == "__main__":
    unittest.main()
