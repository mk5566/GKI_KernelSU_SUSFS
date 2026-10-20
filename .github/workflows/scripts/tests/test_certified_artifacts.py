"""Exercise certified manifest and CRC downloads, including their consumers."""
import base64
import io
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from abi import artifact_url, read_symvers, verify_gki_abi
from kernel_builder import REPO_SYNC_PROJECTS
from target import GKI_RELEASES_URL, GKI_URL, MANIFEST_URL, resolve_gki
from evidence import load_sources


class CertifiedArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tag = "android13-5.15-2027-01_r1"
        self.commit = "a" * 40
        self.manifest_commit = "b" * 40
        self.build_id = "17000000"
        self.branch = "common-android13-5.15-2027-01"
        self.manifest_url = (f"https://ci.android.com/builds/submitted/{self.build_id}/"
                             f"kernel_aarch64/latest/raw/manifest_{self.build_id}.xml")
        self.symvers_url = (f"https://ci.android.com/builds/submitted/{self.build_id}/"
                            "kernel_aarch64/latest/raw/vmlinux.symvers")
        self.manifest = (
            f'<manifest><project name="kernel/common" path="common" revision="{self.commit}"/>'
            f'<project name="kernel/build" path="build" revision="{"c" * 40}"/></manifest>'
        ).encode()
        rows = []
        for tag, build_id in (("android13-5.15-2026-06_r5", "16000000"),
                              (self.tag, self.build_id)):
            rows.append(
                f'<tr><a href="https://ci.android.com/builds/submitted/{build_id}/kernel_aarch64/latest">kernel</a>'
                f'<a href="https://dl.google.com/android/gki/gki-certified-boot-{tag}.zip">boot</a></tr>')
        self.responses = {
            GKI_RELEASES_URL: "".join(rows).encode(),
            f"{GKI_URL}/+/{self.commit}/Makefile?format=TEXT":
                base64.b64encode(b"VERSION = 5\nPATCHLEVEL = 15\nSUBLEVEL = 230\n"),
            self.manifest_url: self.manifest,
        }

    def git(self, *args):
        if args == ("ls-remote", "--tags", GKI_URL, "android13-5.15-*_r*"):
            return f'{"d" * 40}\trefs/tags/{self.tag}\n{self.commit}\trefs/tags/{self.tag}^{{}}\n'
        if args == ("ls-remote", "--heads", MANIFEST_URL, f"refs/heads/{self.branch}"):
            return f"{self.manifest_commit}\trefs/heads/{self.branch}\n"
        self.fail(f"Unexpected git request: {args}")

    def urlopen(self, url, timeout):
        self.assertGreater(timeout, 0)
        # A changed URL, including a regression to the internal /url API,
        # must fail rather than being accepted by an unrestricted mock.
        self.assertIn(url, self.responses)
        return io.BytesIO(self.responses[url])

    def test_new_certified_release_downloads_its_exact_pinned_manifest(self):
        with patch("target._git", side_effect=self.git), \
             patch("urllib.request.urlopen", side_effect=self.urlopen) as request:
            target = resolve_gki()
        self.assertEqual(target.tag, self.tag)
        self.assertEqual(target.commit, self.commit)
        self.assertEqual(target.kernel_version, "5.15.230")
        self.assertEqual(target.manifest_branch, self.branch)
        self.assertEqual(target.manifest_commit, self.manifest_commit)
        self.assertEqual(target.official_build_id, self.build_id)
        self.assertEqual(target.source_projects, (
            ("kernel/common", "common", self.commit), ("kernel/build", "build", "c" * 40)))
        self.assertEqual(request.call_args_list[-1].args, (self.manifest_url,))

    def test_downloaded_manifest_still_requires_matching_kernel_and_pinned_tools(self):
        for manifest, message in (
            (self.manifest.replace(self.commit.encode(), b"d" * 40), "does not match"),
            (self.manifest.replace(b"c" * 40, b"main"), "unpinned"),
            (b"<html>Login required</html>", "does not match"),
        ):
            with self.subTest(message=message), \
                 patch("target._git", side_effect=self.git), \
                 patch("urllib.request.urlopen", side_effect=self.urlopen):
                self.responses[self.manifest_url] = manifest
                with self.assertRaisesRegex(RuntimeError, message):
                    resolve_gki()

    def test_crc_comparison_downloads_the_same_build_and_keeps_failures_fatal(self):
        reference_text = "0x12345678\talloc_pages\tvmlinux\tEXPORT_SYMBOL\n"
        self.responses[self.symvers_url] = reference_text.encode()
        for built_text, valid in (
            (reference_text + "0x87654321\troot_export\tvmlinux\tEXPORT_SYMBOL\n", True),
            (reference_text.replace("0x12345678", "0x11111111"), False),
            ("0x87654321\tother_export\tvmlinux\tEXPORT_SYMBOL\n", False),
        ):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as temp, \
                 patch("urllib.request.urlopen", side_effect=self.urlopen) as request:
                built = Path(temp) / "built.symvers"
                reference = Path(temp) / "reference.symvers"
                built.write_text(built_text, encoding="utf-8")
                if valid:
                    self.assertEqual(verify_gki_abi(self.build_id, built, reference), 1)
                else:
                    with self.assertRaisesRegex(RuntimeError, "GKI ABI mismatch"):
                        verify_gki_abi(self.build_id, built, reference)
                self.assertEqual(reference.read_text(encoding="utf-8"), reference_text)
                request.assert_called_once_with(self.symvers_url, timeout=60)

    def test_invalid_or_unavailable_reference_is_not_replaced_or_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            built = Path(temp) / "built.symvers"
            built.write_text("0x12345678\talloc_pages\tvmlinux\tEXPORT_SYMBOL\n", encoding="utf-8")
            reference = Path(temp) / "reference.symvers"
            with patch("urllib.request.urlopen", return_value=io.BytesIO(b"<html>Login</html>")):
                with self.assertRaisesRegex(RuntimeError, "Empty GKI symbol"):
                    verify_gki_abi(self.build_id, built, reference)
            error = urllib.error.HTTPError(self.symvers_url, 501, "Not implemented", {}, io.BytesIO())
            self.addCleanup(error.close)
            with patch("urllib.request.urlopen", side_effect=error) as request:
                with self.assertRaises(urllib.error.HTTPError):
                    verify_gki_abi(self.build_id, built, reference)
                self.assertEqual(request.call_count, 1)

    def test_artifact_names_cannot_switch_build_target_or_escape_the_path(self):
        for build_id, name in (("latest", "vmlinux.symvers"), ("123/other", "vmlinux.symvers"),
                               ("123", "manifest_456.xml"), ("123", "../vmlinux.symvers"),
                               ("123", "boot.img")):
            with self.subTest(build_id=build_id, name=name), self.assertRaises(ValueError):
                artifact_url(build_id, name)

    @unittest.skipUnless(os.environ.get("GKI_TEST_NETWORK") == "1",
                         "set GKI_TEST_NETWORK=1 to check live certified downloads")
    def test_live_certified_manifest_and_crc_reference_are_available(self):
        # This runs in CI before kernel sync/compilation, using production
        # resolution and both production download URLs. No build-ID pin.
        source_record = os.environ.get("GKI_TEST_SOURCES")
        target = load_sources(source_record) if source_record else resolve_gki()
        projects = {path: revision for _, path, revision in target.source_projects}
        self.assertTrue(set(REPO_SYNC_PROJECTS).issubset(projects))
        commit = target.gki_commit if source_record else target.commit
        tag = target.gki_tag if source_record else target.tag
        self.assertEqual(projects["common"], commit)
        with urllib.request.urlopen(artifact_url(target.official_build_id, "vmlinux.symvers"),
                                    timeout=60) as response:
            symbols = read_symvers(response.read().decode("utf-8"))
        self.assertGreater(len(symbols), 0)
        print(f"\nCertified downloads verified: {tag}, build {target.official_build_id}, "
              f"{len(target.source_projects)} pinned projects, {len(symbols)} reference exports")


if __name__ == "__main__":
    unittest.main()
