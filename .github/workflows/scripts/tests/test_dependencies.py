import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build import main
from config import BBR_REVISION, LZ4_RELEASE, BuildConfig, REPO_SYNC_PROJECTS
from kernel_builder import KernelBuilder
from target import (GKITarget, check_reviewed_upstreams, resolve_anykernel,
                    resolve_susfs, resolve_sukisu_patch)


class DependencyTests(unittest.TestCase):
    def test_saved_source_record_never_reresolves_dependencies(self):
        config = BuildConfig(sukisu_commit="a" * 40)
        with patch("build.load_sources", return_value=config) as load, \
             patch("build.resolve_gki", side_effect=AssertionError("must not resolve GKI")), \
             patch("build.resolve_sukisu", side_effect=AssertionError("must not resolve SukiSU")), \
             patch("build.check_reviewed_upstreams", side_effect=AssertionError("must not recheck ports")), \
             patch("build.resolve_susfs", side_effect=AssertionError("must not resolve helpers")), \
             patch("build.resolve_anykernel", side_effect=AssertionError("must not resolve helpers")), \
             patch("build.resolve_branch", side_effect=AssertionError("must not resolve tools")), \
             patch("build.logging.basicConfig"), patch("build.KernelBuilder") as builder:
            builder.return_value.build.return_value.success = True
            self.assertEqual(main(["--sources", "sources.json"]), 0)
            load.assert_called_once_with("sources.json")
            self.assertIs(builder.call_args.args[0], config)
            with self.assertRaisesRegex(RuntimeError, "Do not override"):
                main(["--sources", "sources.json", "--susfs-commit", "b" * 40])

    def test_actual_tool_or_helper_checkout_mismatch_is_fatal(self):
        config = BuildConfig(gki_commit="a" * 40, sukisu_commit="b" * 40, susfs_commit="c" * 40,
                             sukisu_patch_commit="d" * 40, anykernel_commit="e" * 40, repo_commit="f" * 40,
                             source_projects=tuple((path, path, "a" * 40) for path in REPO_SYNC_PROJECTS))
        with tempfile.TemporaryDirectory() as temp:
            builder = KernelBuilder(config, temp)
            for failing_directory in ("tools/mkbootimg", ".repo/repo", "SukiSU_patch"):
                def head(command, **kwargs):
                    directory = command[2].replace("\\", "/")
                    if directory.endswith(failing_directory):
                        value = "0" * 40
                    elif directory.endswith("/.repo/repo"):
                        value = config.repo_commit
                    elif directory.endswith("/KernelSU"):
                        value = config.sukisu_commit
                    else:
                        value = {"susfs4ksu": config.susfs_commit, "SukiSU_patch": config.sukisu_patch_commit,
                                 "AnyKernel3": config.anykernel_commit}.get(directory.rsplit("/", 1)[-1], "a" * 40)
                    return subprocess.CompletedProcess(command, 0, stdout=value)
                with self.subTest(directory=failing_directory), patch("kernel_builder.subprocess.run", side_effect=head):
                    with self.assertRaisesRegex(RuntimeError, "Source checkout mismatch"):
                        builder.verify_checkouts()

    def test_verify_checkouts_reuses_cached_verified_checkouts(self):
        projects = (("common", "common", "1" * 40),
                    ("build/kernel", "build/kernel", "2" * 40),
                    ("kernel/configs", "kernel/configs", "3" * 40),
                    ("prebuilts/clang/host/linux-x86", "prebuilts/clang/host/linux-x86", "4" * 40),
                    ("prebuilts/build-tools", "prebuilts/build-tools", "5" * 40),
                    ("prebuilts/kernel-build-tools", "prebuilts/kernel-build-tools", "6" * 40),
                    ("prebuilts/gcc/linux-x86/host/x86_64-linux-glibc2.17-4.8", "prebuilts/gcc/linux-x86/host/x86_64-linux-glibc2.17-4.8", "7" * 40),
                    ("prebuilts/ndk-r23", "prebuilts/ndk-r23", "8" * 40),
                    ("tools/mkbootimg", "tools/mkbootimg", "9" * 40))
        config = BuildConfig(gki_commit="1" * 40, manifest_commit="a" * 40, sukisu_commit="b" * 40,
                             susfs_commit="c" * 40, sukisu_patch_commit="d" * 40, anykernel_commit="e" * 40,
                             repo_commit="f" * 40, gki_tag="android13-5.15-2026-10_r1",
                             manifest_branch="common-android13-5.15-2026-10", kernel_version="5.15.200",
                             official_build_id="12345", source_projects=projects)
        with tempfile.TemporaryDirectory() as temp:
            builder = KernelBuilder(config, temp)
            builder._verified_checkouts[builder.work_dir / "KernelSU"] = config.sukisu_commit
            builder._verified_checkouts[builder.susfs_dir] = config.susfs_commit
            builder._verified_checkouts[builder.sukisu_patch_dir] = config.sukisu_patch_commit
            builder._verified_checkouts[builder.anykernel_dir] = config.anykernel_commit
            for _, path, rev in projects:
                builder._verified_checkouts[builder.work_dir / path] = rev
            builder._verified_checkouts[builder.work_dir / ".repo/repo"] = config.repo_commit
            with patch("kernel_builder.subprocess.run") as run:
                actual = builder.verify_checkouts()
                run.assert_not_called()
                self.assertEqual(len(actual), len(projects) + 5)

    def test_final_audit_never_reuses_a_cached_checkout_revision(self):
        projects = (("common", "common", "1" * 40),
                    ("build/kernel", "build/kernel", "2" * 40),
                    ("kernel/configs", "kernel/configs", "3" * 40),
                    ("prebuilts/clang/host/linux-x86", "prebuilts/clang/host/linux-x86", "4" * 40),
                    ("prebuilts/build-tools", "prebuilts/build-tools", "5" * 40),
                    ("prebuilts/kernel-build-tools", "prebuilts/kernel-build-tools", "6" * 40),
                    ("prebuilts/gcc/linux-x86/host/x86_64-linux-glibc2.17-4.8", "prebuilts/gcc/linux-x86/host/x86_64-linux-glibc2.17-4.8", "7" * 40),
                    ("prebuilts/ndk-r23", "prebuilts/ndk-r23", "8" * 40),
                    ("tools/mkbootimg", "tools/mkbootimg", "9" * 40))
        config = BuildConfig(gki_commit="1" * 40, manifest_commit="a" * 40, sukisu_commit="b" * 40,
                             susfs_commit="c" * 40, sukisu_patch_commit="d" * 40,
                             anykernel_commit="e" * 40, repo_commit="f" * 40,
                             gki_tag="android13-5.15-2026-10_r1",
                             manifest_branch="common-android13-5.15-2026-10",
                             kernel_version="5.15.200", official_build_id="12345",
                             source_projects=projects)
        # One live query per required tree: the sync projects, repo and helpers.
        trees = len(projects) + 5

        def heads(**changed):
            def head(command, **kwargs):
                directory = command[2].replace("\\", "/")
                mutated = changed.get(directory.rsplit("/", 1)[-1])
                if mutated is None:
                    for _, path, revision in reversed(projects):
                        if directory.endswith(path):
                            mutated = revision
                            break
                if mutated is None and directory.endswith("/.repo/repo"):
                    mutated = config.repo_commit
                elif mutated is None and directory.endswith("/KernelSU"):
                    mutated = config.sukisu_commit
                elif mutated is None:
                    mutated = {"susfs4ksu": config.susfs_commit,
                               "SukiSU_patch": config.sukisu_patch_commit,
                               "AnyKernel3": config.anykernel_commit}.get(
                                   directory.rsplit("/", 1)[-1])
                return subprocess.CompletedProcess(command, 0,
                                                   stdout=mutated if mutated else "0" * 40)
            return head

        with tempfile.TemporaryDirectory() as temp:
            def prepared():
                builder = KernelBuilder(config, temp)
                for _, path, revision in projects:
                    builder._verified_checkouts[builder.work_dir / path] = revision
                builder._verified_checkouts.update({
                    builder.work_dir / ".repo/repo": config.repo_commit,
                    builder.work_dir / "KernelSU": config.sukisu_commit,
                    builder.susfs_dir: config.susfs_commit,
                    builder.sukisu_patch_dir: config.sukisu_patch_commit,
                    builder.anykernel_dir: config.anykernel_commit,
                })
                return builder

            # A tree moved after the checkout pass must fail the final audit.
            for moved in ("common", "KernelSU", "SukiSU_patch", "repo"):
                with self.subTest(moved=moved), patch("kernel_builder.subprocess.run",
                                                       side_effect=heads(**{moved: "0" * 40})):
                    with self.assertRaisesRegex(RuntimeError, "Source checkout mismatch"):
                        prepared().verify_checkouts(reuse_verified=False)

            # A clean final audit still queries every tree, cached record or not.
            with patch("kernel_builder.subprocess.run", side_effect=heads()) as run:
                actual = prepared().verify_checkouts(reuse_verified=False)
                self.assertGreaterEqual(run.call_count, trees)
                self.assertEqual(len(actual), trees)

            # The earlier audit after sync may still reuse the resolved record.
            with patch("kernel_builder.subprocess.run") as run:
                prepared().verify_checkouts()
                run.assert_not_called()

    def test_repo_launcher_comes_from_the_recorded_commit_even_with_a_cached_launcher(self):
        import base64
        config = BuildConfig(repo_commit="f" * 40)
        with tempfile.TemporaryDirectory() as temp:
            builder = KernelBuilder(config, temp)
            launcher = Path(temp) / "git-repo/repo"
            launcher.parent.mkdir()
            launcher.write_bytes(b"stale launcher")
            with patch("urllib.request.urlopen", return_value=io.BytesIO(base64.b64encode(b"new launcher"))) as download:
                builder.setup_repo_tool()
            self.assertIn(config.repo_commit, download.call_args.args[0])
            self.assertEqual(launcher.read_bytes(), b"new launcher")

    def test_helpers_resolve_only_the_supported_branches_and_fail_closed(self):
        for resolve, branch in ((resolve_susfs, "gki-android13-5.15"),
                                (resolve_sukisu_patch, "main"),
                                (resolve_anykernel, "gki-2.0")):
            ref = f"refs/heads/{branch}"
            with self.subTest(branch=branch), patch("target._git", return_value="a" * 40 + f"\t{ref}\n") as git:
                self.assertEqual(resolve(), "a" * 40)
                self.assertEqual(git.call_args.args[-1], ref)
            for output in ("", "bad\t" + ref, "a" * 40 + "\trefs/heads/wrong\n"):
                with self.subTest(branch=branch, output=output), patch("target._git", return_value=output):
                    with self.assertRaisesRegex(RuntimeError, "Cannot resolve branch"):
                        resolve()

    def test_each_build_records_the_once_resolved_helper_commits(self):
        gki = GKITarget("certified", "a" * 40, "5.15.230", "manifest", "b" * 40)
        with patch("build.resolve_gki", return_value=gki), \
             patch("build.resolve_sukisu", return_value=("", "c" * 40)), \
             patch("build.resolve_susfs", return_value="d" * 40) as susfs, \
             patch("build.resolve_sukisu_patch", return_value="e" * 40) as helper, \
             patch("build.resolve_anykernel", return_value="f" * 40) as anykernel, \
             patch("build.resolve_branch", return_value="4" * 40) as repo_tool, \
             patch("build.logging.basicConfig"), \
             patch("build.check_reviewed_upstreams"), patch("build.KernelBuilder") as builder:
            builder.return_value.build.return_value.success = True
            self.assertEqual(main([]), 0)
            config = builder.call_args.args[0]
            self.assertEqual((config.susfs_commit, config.sukisu_patch_commit, config.anykernel_commit),
                             ("d" * 40, "e" * 40, "f" * 40))
            self.assertEqual(config.repo_commit, "4" * 40)
            repo_tool.assert_called_once()
            for resolver in (susfs, helper, anykernel):
                resolver.assert_called_once_with()
            susfs.reset_mock()
            helper.reset_mock()
            anykernel.reset_mock()
            self.assertEqual(main(["--susfs-commit", "1" * 40,
                                   "--sukisu-patch-commit", "2" * 40,
                                   "--anykernel-commit", "3" * 40]), 0)
            for resolver in (susfs, helper, anykernel):
                resolver.assert_not_called()
            config = builder.call_args.args[0]
            self.assertEqual((config.susfs_commit, config.sukisu_patch_commit, config.anykernel_commit),
                             ("1" * 40, "2" * 40, "3" * 40))

    def test_reviewed_ports_must_match_current_stable_upstream(self):
        for commit, release, valid in (
            (BBR_REVISION, {"tag_name": LZ4_RELEASE}, True),
            ("a" * 40, {"tag_name": LZ4_RELEASE}, False),
            (BBR_REVISION, {"tag_name": "v99.0.0"}, False),
            (BBR_REVISION, {"tag_name": LZ4_RELEASE, "prerelease": True}, False),
            (BBR_REVISION, {}, False),
        ):
            with self.subTest(commit=commit, release=release), \
                 patch("target.resolve_branch", return_value=commit), \
                 patch("target.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(release).encode())):
                if valid:
                    check_reviewed_upstreams()
                else:
                    with self.assertRaises(RuntimeError):
                        check_reviewed_upstreams()

    def test_unavailable_latest_source_never_falls_back_or_starts_build(self):
        with patch("build.check_reviewed_upstreams", side_effect=RuntimeError("upstream unavailable")), \
             patch("build.logging.basicConfig"), \
             patch("build.KernelBuilder") as builder:
            with self.assertRaisesRegex(RuntimeError, "upstream unavailable"):
                main([])
            builder.assert_not_called()

    def test_helper_checkout_still_requires_the_recorded_exact_commit(self):
        with tempfile.TemporaryDirectory() as root:
            builder = KernelBuilder(BuildConfig(susfs_commit="a" * 40,
                                               sukisu_patch_commit="b" * 40,
                                               anykernel_commit="c" * 40), root)
            with patch.object(builder, "_clone_source") as checkout:
                builder.clone_repositories()
                self.assertEqual([call.args[3] for call in checkout.call_args_list],
                                 ["a" * 40, "b" * 40, "c" * 40])
            with patch.object(builder, "_chdir"), patch("kernel_builder.subprocess.run", side_effect=[
                subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout="d" * 40),
            ]):
                with self.assertRaisesRegex(RuntimeError, "checkout mismatch"):
                    builder._checkout_commit(Path(root), "a" * 40, "SUSFS")

    def test_config_failure_stops_before_kernel_compilation(self):
        with tempfile.TemporaryDirectory() as root:
            builder = KernelBuilder(BuildConfig(), root)
            methods = ("_preflight", "clone_repositories", "setup_repo_tool", "init_and_sync_kernel",
                       "configure_kernel_toolchain", "add_kernelsu", "apply_susfs_patches",
                       "apply_feature_patches", "apply_zram_patches", "configure_kernel",
                       "configure_kernel_name", "show_kernel_config", "configure_build_fragment",
                       "_verify_source_mode", "write_build_info", "build_kernel", "verify_checkouts")
            for method in methods:
                setattr(builder, method, Mock())
            builder.verify_checkouts.return_value = {}
            builder.generate_and_verify_config = Mock(side_effect=RuntimeError("bad final config"))
            with patch("kernel_builder.write_sources"), patch("kernel_builder.logger"):
                result = builder.build()
            self.assertFalse(result.success)
            self.assertIn("bad final config", result.message)
            builder.build_kernel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
