"""Offline protocol and integration tests; remote data below are fixtures."""
import copy
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import target
from config import (ACTION_PINS, BBR_REVISION, KSU_REPO_CONFIG, LZ4_RELEASE, LZ4_REVISION,
                    LZ4_REPO_URL, LZ4_UPSTREAM_SHA256, REPO_ROOT, SUSFS_REPO_CONFIG,
                    SUKISU_PATCH_REPO_CONFIG, ANYKERNEL_CONFIG)
from source_lock import write_sources, load_sources
from upstream_policy import (fetch_bytes, latest_release, verify_workflow_pins,
                             expected_reviewed_ports, expected_actions,
                             make_upstream_status, validate_upstream_status)
from test_hardening import valid_build_config


class ReviewedUpstreamTests(unittest.TestCase):
    def setUp(self):
        self.bytes = {"lib/lz4.c": b"fixture C source\n", "lib/lz4.h": b"fixture header\n"}
        self.tags = {"lz4/lz4": LZ4_RELEASE,
                     **{repo: tag for repo, (tag, _) in ACTION_PINS.items()}}
        self.commits = {LZ4_REPO_URL: LZ4_REVISION,
                        **{f"https://github.com/{repo}.git": sha for repo, (_, sha) in ACTION_PINS.items()}}
        mocks = [patch.dict(LZ4_UPSTREAM_SHA256, {p: hashlib.sha256(b).hexdigest()
                                                for p, b in self.bytes.items()}),
                 patch("target.resolve_branch", return_value=BBR_REVISION),
                 patch("target.latest_release", side_effect=lambda repo: self.tags[repo]),
                 patch("target._git", side_effect=self.git),
                 patch("target.fetch_bytes", side_effect=self.download)]
        self.mocks = [m.start() for m in mocks]
        for m in mocks:
            self.addCleanup(m.stop)

    def git(self, *args):
        self.assertEqual(args[:2], ("ls-remote", "--tags"))
        url, ref, peeled = args[2:]
        self.assertEqual(peeled, ref + "^{}")
        self.assertTrue(ref.startswith("refs/tags/v"))
        # Annotated tag object is not the code commit.
        return f'{"0" * 40}\t{ref}\n{self.commits[url]}\t{peeled}\n'

    def download(self, url):
        prefix = f"https://raw.githubusercontent.com/lz4/lz4/{LZ4_REVISION}/"
        self.assertTrue(url.startswith(prefix))
        return self.bytes[url.removeprefix(prefix)]

    def test_matching_latest_ports_and_actions_are_verified_at_exact_revisions(self):
        result = target.check_reviewed_upstreams()
        self.assertEqual(result, {"reviewed_ports": expected_reviewed_ports(), "actions": expected_actions()})
        self.assertEqual(self.mocks[3].call_count, 1 + len(ACTION_PINS))
        self.assertEqual(self.mocks[4].call_count, 2)

    def test_new_bbr_head_is_fatal_before_port_downloads(self):
        self.mocks[1].return_value = "a" * 40
        with self.assertRaisesRegex(RuntimeError, "BBR port is stale"):
            target.check_reviewed_upstreams()
        self.mocks[4].assert_not_called()

    def test_new_lz4_release_cannot_fall_back_to_the_vendored_version(self):
        self.tags["lz4/lz4"] = "v99.0.0"
        with self.assertRaisesRegex(RuntimeError, "LZ4 port is stale"):
            target.check_reviewed_upstreams()
        self.mocks[3].assert_not_called()

    def test_retagged_lz4_release_is_rejected_even_if_version_matches(self):
        self.commits[LZ4_REPO_URL] = "a" * 40
        with self.assertRaisesRegex(RuntimeError, "LZ4 release tag moved"):
            target.check_reviewed_upstreams()

    def test_upstream_lz4_content_must_match_the_reviewed_hash(self):
        self.bytes["lib/lz4.h"] += b"unexpected modification"
        with self.assertRaisesRegex(RuntimeError, "LZ4 upstream source hash mismatch"):
            target.check_reviewed_upstreams()

    def test_new_action_release_requires_review_not_automatic_execution(self):
        self.tags["actions/checkout"] = "v99.0.0"
        with self.assertRaisesRegex(RuntimeError, "Reviewed action actions/checkout is stale"):
            target.check_reviewed_upstreams()

    def test_retagged_action_cannot_reuse_the_approved_release_label(self):
        self.commits["https://github.com/actions/setup-python.git"] = "a" * 40
        with self.assertRaisesRegex(RuntimeError, "Action release tag moved"):
            target.check_reviewed_upstreams()

    def test_network_failure_cannot_produce_successful_upstream_evidence(self):
        self.mocks[4].side_effect = RuntimeError("unavailable")
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            target.check_reviewed_upstreams()


class FetchProtocolTests(unittest.TestCase):
    def test_latest_release_rejects_missing_flags_drafts_prereleases_and_bad_json(self):
        valid = {"tag_name": LZ4_RELEASE, "draft": False, "prerelease": False}
        for changes in ({"draft": True}, {"prerelease": True}, {"draft": 0},
                        {"tag_name": "main"}, {"tag_name": None}):
            with self.subTest(changes=changes), patch("upstream_policy.fetch_bytes", return_value=json.dumps(valid | changes).encode()):
                with self.assertRaises(RuntimeError):
                    latest_release("lz4/lz4")
        for raw in (b"[]", b"{}", b'{"tag_name":"v1.0.0","tag_name":"v2.0.0"}', b'{"draft":NaN}'):
            with self.subTest(raw=raw), patch("upstream_policy.fetch_bytes", return_value=raw):
                with self.assertRaises(RuntimeError):
                    latest_release("lz4/lz4")
        with patch("upstream_policy.fetch_bytes", return_value=json.dumps(valid).encode()):
            self.assertEqual(latest_release("lz4/lz4"), LZ4_RELEASE)

    def test_latest_release_falls_back_to_web_redirect_on_api_rate_limit(self):
        class FakeOpener:
            def __init__(self, location, code=302):
                self.location = location
                self.code = code
                self.calls = 0

            def open(self, req, timeout=30):
                self.calls += 1
                raise urllib.error.HTTPError(
                    req.full_url, self.code, "Found", {"Location": self.location}, io.BytesIO()
                )

        opener = FakeOpener(f"https://github.com/lz4/lz4/releases/tag/{LZ4_RELEASE}")
        with patch("upstream_policy.fetch_bytes",
                   side_effect=RuntimeError("Cannot verify upstream https://api.github.com/repos/lz4/lz4/releases/latest: HTTP 403; no stale fallback")), \
             patch("upstream_policy.urllib.request.build_opener", return_value=opener):
            self.assertEqual(latest_release("lz4/lz4"), LZ4_RELEASE)
        self.assertEqual(opener.calls, 1)

        bad_opener = FakeOpener("https://github.com/other/repo/releases/tag/v1.0.0")
        with patch("upstream_policy.fetch_bytes",
                   side_effect=RuntimeError("Cannot verify upstream https://api.github.com/repos/lz4/lz4/releases/latest: HTTP 429; no stale fallback")), \
             patch("upstream_policy.urllib.request.build_opener", return_value=bad_opener):
            with self.assertRaisesRegex(RuntimeError, "Unexpected release redirect"):
                latest_release("lz4/lz4")

        non_rate_limit = RuntimeError("Cannot verify upstream https://api.github.com/repos/lz4/lz4/releases/latest: HTTP 404; no stale fallback")
        with patch("upstream_policy.fetch_bytes", side_effect=non_rate_limit), \
             patch("upstream_policy.urllib.request.build_opener") as build_opener:
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                latest_release("lz4/lz4")
            build_opener.assert_not_called()

    def test_response_size_is_bounded(self):
        with patch("upstream_policy.MAX_RESPONSE_BYTES", 16), \
             patch("upstream_policy.urllib.request.urlopen", return_value=io.BytesIO(b"x" * 17)):
            with self.assertRaisesRegex(RuntimeError, "size limit"):
                fetch_bytes("https://example.test/source")

    def test_transient_error_can_retry_but_never_returns_cached_data(self):
        with patch("upstream_policy.time.sleep") as sleep, \
             patch("upstream_policy.urllib.request.urlopen", side_effect=[
                 urllib.error.URLError("temporary"), io.BytesIO(b"fresh")]) as request:
            self.assertEqual(fetch_bytes("https://example.test/source"), b"fresh")
            self.assertEqual(request.call_count, 2)
            sleep.assert_called_once_with(1)

    def test_persistent_network_error_is_fatal_after_bounded_retries(self):
        with patch("upstream_policy.time.sleep"), \
             patch("upstream_policy.urllib.request.urlopen", side_effect=urllib.error.URLError("offline")) as request:
            with self.assertRaisesRegex(RuntimeError, "no stale fallback"):
                fetch_bytes("https://example.test/source")
            self.assertEqual(request.call_count, 3)

    def test_404_is_not_retried_and_503_is_retried(self):
        for code, count in ((404, 1), (503, 3)):
            def error(*args, **kwargs):
                raise urllib.error.HTTPError("https://example.test", code, "test", {}, io.BytesIO())
            with self.subTest(code=code), patch("upstream_policy.time.sleep"), \
                 patch("upstream_policy.urllib.request.urlopen", side_effect=error) as request:
                with self.assertRaisesRegex(RuntimeError, f"HTTP {code}"):
                    fetch_bytes("https://example.test/source")
                self.assertEqual(request.call_count, count)

    def test_public_download_does_not_forward_environment_credentials(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "sensitive", "GH_TOKEN": "sensitive"}), \
             patch("upstream_policy.urllib.request.urlopen", return_value=io.BytesIO(b"ok")) as request:
            fetch_bytes("https://raw.githubusercontent.com/example/repo/hash/file")
            headers = request.call_args.args[0].header_items()
            self.assertNotIn("sensitive", repr(headers))
            self.assertNotIn("authorization", {k.lower() for k, _ in headers})

    def test_non_https_and_invalid_attempt_counts_are_rejected(self):
        for url, attempts in (("http://example.test", 1), ("https://example.test", 0),
                              ("https://example.test", 4)):
            with self.subTest(url=url, attempts=attempts), self.assertRaises(ValueError):
                fetch_bytes(url, attempts=attempts)


class UpstreamRecordTests(unittest.TestCase):
    def test_record_contains_compatible_family_urls_and_certified_tool_revisions(self):
        config = valid_build_config()
        validate_upstream_status(config)
        entries = config.upstream_status["components"]
        self.assertEqual(entries["sukisu"]["repo_url"], KSU_REPO_CONFIG["repo_url"])
        self.assertEqual(entries["susfs"]["ref"], "refs/heads/gki-android13-5.15")
        self.assertEqual(entries["certified_projects"]["projects"], [list(x) for x in config.source_projects])
        self.assertEqual(config.upstream_status["kmi_status"], "not-established-by-source-resolution")

    def test_explicit_commit_overrides_are_never_mislabeled_as_latest(self):
        config = valid_build_config()
        config.upstream_status = make_upstream_status(config, {
            "reviewed_ports": expected_reviewed_ports(), "actions": expected_actions()}, ["susfs"])
        validate_upstream_status(config)
        self.assertEqual(config.upstream_status["components"]["susfs"]["selection"], "explicit-commit")
        config.upstream_status["components"]["susfs"]["selection"] = "latest-branch-at-resolution"
        with self.assertRaisesRegex(RuntimeError, "differs"):
            validate_upstream_status(config)

    def test_missing_evidence_wrong_urls_revisions_or_certification_claims_are_fatal(self):
        for kind in ("absent", "url", "ref", "commit", "ports", "actions", "kmi", "schema", "timestamp", "extra"):
            config = valid_build_config()
            record = config.upstream_status
            if kind == "absent":
                config.upstream_status = {}
            elif kind in ("url", "ref", "commit"):
                key = "repo_url" if kind == "url" else kind
                record["components"]["susfs"][key] = "wrong"
            elif kind in ("ports", "actions"):
                record["reviewed_ports" if kind == "ports" else kind] = {}
            elif kind == "kmi":
                record["kmi_status"] = "certified"
            elif kind == "schema":
                record["schema_version"] = True
            elif kind == "timestamp":
                record["checked_at_utc"] = "2026-99-99T00:00:00Z"
            else:
                record["unexpected"] = True
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                validate_upstream_status(config)

    def test_lock_schema_2_roundtrip_and_missing_or_old_freshness_evidence_fail(self):
        config = valid_build_config()
        with tempfile.TemporaryDirectory() as temp, \
             patch("source_lock.builder_revision", return_value="0" * 40), \
             patch("source_lock.builder_dirty", return_value=False):
            path = write_sources(config, temp)
            original = json.loads(path.read_text())
            self.assertEqual(original["schema_version"], 2)
            loaded = load_sources(path)
            self.assertEqual(loaded.upstream_status, config.upstream_status)
            for kind in ("old", "missing", "port"):
                data = copy.deepcopy(original)
                if kind == "old":
                    data["schema_version"] = 1
                elif kind == "missing":
                    del data["upstream_status"]
                else:
                    data["lz4_commit"] = "0" * 40
                path.write_text(json.dumps(data))
                with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                    load_sources(path)

    def test_resolve_only_writes_complete_current_source_selection_without_building(self):
        from build import main
        config = valid_build_config()
        gki = target.GKITarget(config.gki_tag, config.gki_commit, config.kernel_version,
                               config.manifest_branch, config.manifest_commit,
                               config.official_build_id, config.source_projects)
        with tempfile.TemporaryDirectory() as temp, \
             patch("build.check_reviewed_upstreams", return_value={"reviewed_ports": expected_reviewed_ports(), "actions": expected_actions()}), \
             patch("build.resolve_gki", return_value=gki), \
             patch("build.resolve_sukisu", return_value=("", config.sukisu_commit)), \
             patch("build.resolve_susfs", return_value=config.susfs_commit), \
             patch("build.resolve_sukisu_patch", return_value=config.sukisu_patch_commit), \
             patch("build.resolve_anykernel", return_value=config.anykernel_commit), \
             patch("build.resolve_branch", return_value=config.repo_commit), \
             patch("build.KernelBuilder") as builder, \
             patch("source_lock.builder_revision", return_value="0" * 40), \
             patch("source_lock.builder_dirty", return_value=False):
            self.assertEqual(main(["--resolve-only", "--workspace", temp]), 0)
            builder.assert_not_called()
            replay = load_sources(Path(temp) / "sources.json")
            self.assertEqual(replay.sukisu_commit, config.sukisu_commit)
            self.assertEqual(replay.upstream_status["explicit_commit_overrides"], [])
            self.assertEqual(replay.upstream_status["components"]["susfs"]["selection"],
                             "latest-branch-at-resolution")

    def test_unverified_config_cannot_be_saved_as_a_source_lock(self):
        config = valid_build_config()
        config.upstream_status = {}
        with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(RuntimeError, "freshness evidence"):
            write_sources(config, temp)


class SelectionAndWorkflowTests(unittest.TestCase):
    def test_current_workflow_pins_match_policy(self):
        verify_workflow_pins()

    def test_mutable_or_unreviewed_action_pin_cannot_enter_workflow(self):
        text = (REPO_ROOT / ".github/workflows/kernel-build.yml").read_text()
        for replacement in ("main", "v7", "0" * 40):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / ".github/workflows").mkdir(parents=True)
                (root / ".github/workflows/build.yml").write_text(text.replace(ACTION_PINS["actions/checkout"][1], replacement))
                with self.subTest(replacement=replacement), self.assertRaises(RuntimeError):
                    verify_workflow_pins(root)

    def test_local_actions_cannot_bypass_the_reviewed_inventory(self):
        text = (REPO_ROOT / ".github/workflows/kernel-build.yml").read_text()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".github/workflows").mkdir(parents=True)
            (root / ".github/workflows/build.yml").write_text(text + "\n      - uses: ./.github/actions/unreviewed\n")
            with self.assertRaisesRegex(RuntimeError, "Local workflow actions"):
                verify_workflow_pins(root)

    def test_source_resolution_precedes_expensive_runner_provisioning(self):
        text = (REPO_ROOT / ".github/workflows/kernel-build.yml").read_text()
        self.assertLess(text.index("- name: Resolve current dependencies once"),
                        text.index("- name: Install kernel build dependencies"))
        self.assertIn('from config import KSU_REPO_CONFIG', text)
        self.assertIn("check-latest: true", text)
        self.assertIn("python-version: '3.12'", text)

    def test_supported_sources_use_canonical_urls_without_changing_family(self):
        self.assertEqual(KSU_REPO_CONFIG, {"repo_url": "https://git.zako.shirkneko.top:20004/shirkneko/SukiSU.git", "branch": "builtin"})
        self.assertEqual(SUKISU_PATCH_REPO_CONFIG["repo_url"], "https://github.com/SukiSU-Nova/SukiSU_patch.git")
        self.assertEqual(ANYKERNEL_CONFIG, {"repo_url": "https://github.com/WildKernels/AnyKernel3.git", "branch": "gki-2.0"})
        self.assertEqual(SUSFS_REPO_CONFIG["branch"], "gki-android13-5.15")

    def test_conflicting_remote_ref_cannot_silently_replace_the_first_sha(self):
        with self.assertRaisesRegex(RuntimeError, "Conflicting remote ref"):
            target.parse_refs(f'{"a" * 40}\trefs/heads/v3\n{"b" * 40}\trefs/heads/v3\n')

    def test_conflicting_certified_build_rows_are_fatal(self):
        def row(build):
            return (f'<tr><a href="https://ci.android.com/builds/submitted/{build}/kernel_aarch64/latest">kernel</a>'
                    '<a href="https://dl.google.com/android/gki/gki-certified-boot-android13-5.15-2026-09_r5.zip">boot</a></tr>')
        with self.assertRaisesRegex(RuntimeError, "Conflicting certified build IDs"):
            target.certified_releases(row(16941857) + row(16941858))
        self.assertEqual(target.certified_releases(row(16941857) * 2), {"android13-5.15-2026-09_r5": "16941857"})

    def test_respin_selection_does_not_follow_uncertified_point_tags(self):
        self.assertEqual(target.choose_gki_tag({"android13-5.15-2026-09_r4": "old",
                                               "android13-5.15-2026-09_r5": "current",
                                               "android13-5.15.999_r1": "not a monthly release"}),
                         "android13-5.15-2026-09_r5")


if __name__ == "__main__":
    unittest.main()
