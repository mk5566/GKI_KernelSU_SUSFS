import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
import struct
import warnings
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import BuildConfig, REPO_SYNC_PROJECTS
from evidence import collect_evidence, write_sources, load_sources, sha256
from config_checks import ConfigPolicy
from integrity import atomic_json
from patch_plan import expected_patch_records
from upstream_policy import make_upstream_status, expected_reviewed_ports, expected_actions
import test_build_modes as build_modes


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        # Provenance is a fixture here. Real Git behavior is exercised separately;
        # artifact tests must also run from a source ZIP without inventing a HEAD.
        revision = patch("source_lock.builder_revision", return_value="0" * 40)
        dirty = patch("source_lock.builder_dirty", return_value=False)
        revision.start()
        dirty.start()
        self.addCleanup(revision.stop)
        self.addCleanup(dirty.stop)

    def prepare(self, root):
        config = BuildConfig(gki_tag="android13-5.15-2027-01_r1", gki_commit="d" * 40,
                             manifest_branch="common-android13-5.15-2027-01", manifest_commit="e" * 40,
                             kernel_version="5.15.230", official_build_id="17000000",
                             source_projects=tuple((f"project/{path}", path, "d" * 40) for path in REPO_SYNC_PROJECTS),
                             sukisu_commit="f" * 40, susfs_commit="a" * 40, sukisu_patch_commit="b" * 40,
                             anykernel_commit="c" * 40, repo_commit="1" * 40)
        config.upstream_status = make_upstream_status(config, {
            "reviewed_ports": expected_reviewed_ports(), "actions": expected_actions()}, [])
        write_sources(config, root)
        files = ["BUILD_INFO.md", "boot.img", "AnyKernel3.zip", "Image", "GKI-reference.symvers",
                 "android13-5.15/out/android13-5.15/common/.config",
                 "android13-5.15/out/android13-5.15/dist/vmlinux.symvers"]
        for name in files:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        symbols = "0x12345678\talloc_pages\tvmlinux\tEXPORT_SYMBOL\n"
        (root / "GKI-reference.symvers").write_text(symbols, encoding="utf-8")
        (root / "android13-5.15/out/android13-5.15/dist/vmlinux.symvers").write_text(symbols, encoding="utf-8")
        with zipfile.ZipFile(root / "AnyKernel3.zip", "w") as archive:
            archive.write(root / "Image", "Image")
        image = (root / "Image").read_bytes()
        header = bytearray(4096)
        header[:8] = b"ANDROID!"
        struct.pack_into("<I", header, 8, len(image))
        struct.pack_into("<I", header, 20, 1584)
        struct.pack_into("<I", header, 40, 4)
        (root / "boot.img").write_bytes(header + image)
        (root / "android13-5.15/out/android13-5.15/dist/Image").write_bytes(image)
        final = root / "android13-5.15/out/android13-5.15/common/.config"
        final.write_text(build_modes.RuntimeConfigTests()._valid_config(), encoding="utf-8")
        (root / "precompile.config").write_bytes(final.read_bytes())
        atomic_json(root / "CONFIG_REPORT.json", {stage: ConfigPolicy.verify(final)
                    for stage in ("precompile", "final")})
        atomic_json(root / "CONFIG_DIFF.json", {})
        atomic_json(root / "BUILD_TIMINGS.json", {"fixture": {"status": "passed", "elapsed_seconds": 0.1}})
        upstream = root / "susfs4ksu/kernel_patches" / config.get_susfs_patch_filename()
        upstream.parent.mkdir(parents=True)
        upstream.write_text("synthetic upstream patch fixture\n", encoding="utf-8")
        atomic_json(root / "PATCH_REPORT.json", {"applied": expected_patch_records(config, root)})
        checkouts = {f"android13-5.15/{path}": revision for _, path, revision in config.source_projects}
        checkouts.update({"android13-5.15/.repo/repo": config.repo_commit,
                          "android13-5.15/KernelSU": config.sukisu_commit,
                          "susfs4ksu": config.susfs_commit, "SukiSU_patch": config.sukisu_patch_commit,
                          "AnyKernel3": config.anykernel_commit})
        (root / "checkouts.json").write_text(json.dumps(checkouts), encoding="utf-8")
        return config

    def test_success_evidence_contains_final_config_sources_crc_report_and_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.prepare(root)
            directory = collect_evidence(root, config.config_name, 1)
            sources = json.loads((directory / "sources.json").read_text())
            self.assertEqual(sources["anykernel_commit"], "c" * 40)
            self.assertEqual(sources["susfs_commit"], "a" * 40)
            self.assertEqual(len(sources["builder_commit"]), 40)
            self.assertIn("patches/bbr/0001-bbrv3-android-kabi.patch", sources["local_source_sha256"])
            report = json.loads((directory / "ABI_REPORT.json").read_text())
            self.assertEqual(report["matched_certified_exports"], 1)
            self.assertEqual(report["status"], "passed")
            self.assertEqual((directory / ".config").read_bytes(),
                             (root / "android13-5.15/out/android13-5.15/common/.config").read_bytes())
            for line in (directory / "SHA256SUMS").read_text().splitlines():
                digest, name = line.split("  ", 1)
                path = root / name if name in ("boot.img", "AnyKernel3.zip", "Image") else directory / name
                self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_missing_evidence_and_unverified_crc_are_fatal(self):
        for missing in ("boot.img", "sources.json", "GKI-reference.symvers",
                        "android13-5.15/out/android13-5.15/common/.config"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config = self.prepare(root)
                (root / missing).unlink()
                with self.assertRaisesRegex(RuntimeError, "Missing or empty build evidence"):
                    collect_evidence(root, config.config_name, 1)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.prepare(root)
            with self.assertRaisesRegex(RuntimeError, "successful certified CRC"):
                collect_evidence(root, config.config_name, 0)

    def test_resolved_sources_are_reusable_without_resolving_again_and_reject_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.prepare(root)
            path = root / "sources.json"
            self.assertEqual(load_sources(path).to_dict(), config.to_dict())
            record = json.loads(path.read_text(encoding="utf-8"))
            for field, bad in (("susfs_commit", "main"), ("official_build_id", "latest"),
                               ("manifest_branch", "wrong"), ("source_projects", []),
                               ("local_source_sha256", {})):
                with self.subTest(field=field):
                    path.write_text(json.dumps(dict(record, **{field: bad})), encoding="utf-8")
                    with self.assertRaises(RuntimeError):
                        load_sources(path)

    def test_stale_configuration_crc_checkout_or_archive_cannot_be_published(self):
        for name in ("config", "crc", "count", "checkouts", "zip", "zip_duplicate", "export_type", "namespace"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config = self.prepare(root)
                if name == "config":
                    (root / "android13-5.15/out/android13-5.15/common/.config").write_text("changed")
                elif name == "crc":
                    (root / "GKI-reference.symvers").write_text("0x99999999\talloc_pages\tvmlinux\tEXPORT_SYMBOL\n")
                elif name == "checkouts":
                    (root / "checkouts.json").write_text("{}")
                elif name == "zip":
                    with zipfile.ZipFile(root / "AnyKernel3.zip", "w") as archive:
                        archive.writestr("Image", b"wrong kernel")
                elif name == "zip_duplicate":
                    with warnings.catch_warnings(), zipfile.ZipFile(root / "AnyKernel3.zip", "a") as archive:
                        warnings.simplefilter("ignore", UserWarning)
                        archive.writestr("Image", (root / "Image").read_bytes())
                elif name in ("export_type", "namespace"):
                    table = root / "android13-5.15/out/android13-5.15/dist/vmlinux.symvers"
                    text = table.read_text()
                    table.write_text(text.replace("EXPORT_SYMBOL", "EXPORT_SYMBOL_GPL")
                                     if name == "export_type" else text.rstrip() + " new_namespace\n")
                with self.assertRaises(RuntimeError):
                    collect_evidence(root, config.config_name, 2 if name == "count" else 1)

    def test_packaged_image_pair_cannot_hide_a_different_compiled_kernel(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.prepare(root)
            (root / "android13-5.15/out/android13-5.15/dist/Image").write_bytes(b"different actual build")
            with self.assertRaisesRegex(RuntimeError, "compiled dist/Image"):
                collect_evidence(root, config.config_name, 1)
            self.assertFalse((root / "evidence").exists())

    def test_same_sized_wrong_boot_payload_is_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.prepare(root)
            boot = root / "boot.img"
            boot.write_bytes(boot.read_bytes()[:-1] + b"!")
            with self.assertRaisesRegex(RuntimeError, "Boot kernel payload"):
                collect_evidence(root, config.config_name, 1)

    def test_self_consistent_forged_config_report_cannot_replace_policy_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.prepare(root)
            path = root / "CONFIG_REPORT.json"
            report = json.loads(path.read_text())
            for stage in report.values():
                stage["expected"] = stage["actual"] = {"CONFIG_KSU": "y"}
            atomic_json(path, report)
            with self.assertRaisesRegex(RuntimeError, "configuration evidence"):
                collect_evidence(root, config.config_name, 1)

    def test_invalid_config_cannot_be_hidden_by_updating_its_report_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.prepare(root)
            final = root / "android13-5.15/out/android13-5.15/common/.config"
            final.write_text(final.read_text().replace("CONFIG_CFI_CLANG=y", "CONFIG_CFI_CLANG=n"))
            report = json.loads((root / "CONFIG_REPORT.json").read_text())
            report["final"]["sha256"] = sha256(final)
            atomic_json(root / "CONFIG_REPORT.json", report)
            with self.assertRaisesRegex(RuntimeError, "CONFIG_CFI_CLANG"):
                collect_evidence(root, config.config_name, 1)

    def test_patch_order_hash_and_target_evidence_cannot_drift(self):
        for kind in ("order", "hash", "target", "upstream_content"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config = self.prepare(root)
                path = root / "PATCH_REPORT.json"
                record = json.loads(path.read_text())
                if kind == "order":
                    record["applied"].reverse()
                elif kind == "hash":
                    record["applied"][0]["sha256"] = "0" * 64
                elif kind == "target":
                    record["applied"][0]["target"] = "wrong"
                else:
                    upstream = root / "susfs4ksu/kernel_patches" / config.get_susfs_patch_filename()
                    upstream.write_text("replaced after application")
                atomic_json(path, record)
                with self.assertRaisesRegex(RuntimeError, "Applied patch"):
                    collect_evidence(root, config.config_name, 1)

    def test_precompile_snapshot_and_delta_are_rechecked(self):
        for kind in ("snapshot", "delta"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                config = self.prepare(root)
                if kind == "snapshot":
                    with (root / "precompile.config").open("a") as stream:
                        stream.write("# changed snapshot\n")
                else:
                    atomic_json(root / "CONFIG_DIFF.json", {"CONFIG_CFI_CLANG": {"final": "n"}})
                with self.assertRaises(RuntimeError):
                    collect_evidence(root, config.config_name, 1)

    def test_copy_failure_cannot_publish_partial_success_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.prepare(root)
            with patch("evidence.shutil.copy2", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    collect_evidence(root, config.config_name, 1)
            self.assertFalse((root / "evidence").exists())
            self.assertEqual(list(root.glob(".evidence-*")), [])

    def test_existing_evidence_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.prepare(root)
            evidence = root / "evidence"
            evidence.mkdir()
            (evidence / "keep").write_bytes(b"preserve")
            with self.assertRaisesRegex(RuntimeError, "existing build evidence"):
                collect_evidence(root, config.config_name, 1)
            self.assertEqual((evidence / "keep").read_bytes(), b"preserve")


if __name__ == "__main__":
    unittest.main()
