import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import BuildConfig, REPO_SYNC_PROJECTS
from evidence import collect_evidence, write_sources, load_sources, sha256


class EvidenceTests(unittest.TestCase):
    def prepare(self, root):
        config = BuildConfig(gki_tag="android13-5.15-2027-01_r1", gki_commit="d" * 40,
                             manifest_branch="common-android13-5.15-2027-01", manifest_commit="e" * 40,
                             kernel_version="5.15.230", official_build_id="17000000",
                             source_projects=tuple((f"project/{path}", path, "d" * 40) for path in REPO_SYNC_PROJECTS),
                             sukisu_commit="f" * 40, susfs_commit="a" * 40, sukisu_patch_commit="b" * 40,
                             anykernel_commit="c" * 40, repo_commit="1" * 40)
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
        values = {"CONFIG_KSU": "y"}
        config_hash = sha256(root / "android13-5.15/out/android13-5.15/common/.config")
        (root / "CONFIG_REPORT.json").write_text(json.dumps({stage: {
            "status": "passed", "sha256": config_hash, "expected": values, "actual": values,
        } for stage in ("precompile", "final")}), encoding="utf-8")
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
        for name in ("config", "crc", "count", "checkouts", "zip"):
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
                with self.assertRaises(RuntimeError):
                    collect_evidence(root, config.config_name, 2 if name == "count" else 1)


if __name__ == "__main__":
    unittest.main()
