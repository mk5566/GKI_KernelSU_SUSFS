"""Latest-compatible source policy and replayable, non-certifying observations.

Freshness is checked when resolving, not again between preflight and compilation.
A validated record is provenance, not a signature, ABI approval, or proof that
remote refs have not advanced since resolution. No credentials are serialized.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import (ACTION_PINS, ANDROID_FAMILY, ANYKERNEL_CONFIG, BBR_REPO_CONFIG,
                    BBR_REVISION, BuildConfig, KSU_REPO_CONFIG, LZ4_RELEASE,
                    LZ4_REVISION, LZ4_REPO_URL, LZ4_UPSTREAM_SHA256, REPO_ROOT,
                    REPO_TOOL_CONFIG, SUKISU_PATCH_REPO_CONFIG, SUSFS_REPO_CONFIG)
from integrity import _unique_object, _reject_constant

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SOURCE_POLICY = "latest-compatible-at-resolution"
KMI_STATUS = "not-established-by-source-resolution"


def fetch_bytes(url: str, *, attempts: int = 3) -> bytes:
    """Bounded HTTPS reads; retry only transient failures, never stale results.

    Public source endpoints need no credentials. Deliberately do not forward
    GITHUB_TOKEN, cookies, git credentials, or arbitrary environment headers.
    """
    if not url.startswith("https://") or attempts not in range(1, 4):
        raise ValueError("Expected HTTPS and one to three attempts")
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json" if url.startswith("https://api.github.com/")
                  else "application/octet-stream",
        "User-Agent": "gki-certified-source-resolver",
    })
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = response.read(MAX_RESPONSE_BYTES + 1)
            if len(result) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f"Upstream response exceeds size limit: {url}")
            return result
        except urllib.error.HTTPError as exc:
            retry = exc.code in (429, 500, 502, 503, 504)
            exc.close()
            if not retry or attempt + 1 == attempts:
                raise RuntimeError(f"Cannot verify upstream {url}: HTTP {exc.code}; no stale fallback") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt + 1 == attempts:
                raise RuntimeError(f"Cannot verify upstream {url}; no stale fallback: {exc}") from exc
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def latest_release(repository: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository identifier")
    data = json.loads(fetch_bytes(f"https://api.github.com/repos/{repository}/releases/latest"),
                      object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    if (not isinstance(data, dict) or data.get("draft") is not False
            or data.get("prerelease") is not False
            or not isinstance(data.get("tag_name"), str)
            or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", data["tag_name"])):
        raise RuntimeError(f"Invalid upstream stable release payload: {repository}")
    return data["tag_name"]


def expected_reviewed_ports() -> dict[str, Any]:
    return {
        "bbr": {"repo_url": BBR_REPO_CONFIG["repo_url"],
                "ref": "refs/heads/" + BBR_REPO_CONFIG["branch"], "commit": BBR_REVISION},
        "lz4": {"repo_url": LZ4_REPO_URL, "tag": LZ4_RELEASE, "commit": LZ4_REVISION,
                "upstream_sha256": dict(LZ4_UPSTREAM_SHA256)},
    }


def expected_actions() -> dict[str, dict[str, str]]:
    return {repo: {"tag": tag, "commit": commit} for repo, (tag, commit) in ACTION_PINS.items()}


def verify_workflow_pins(root: Path = REPO_ROOT) -> None:
    """Every remote `uses` entry must have an approved full-SHA action pin.

    Reject forms this deliberately small checker cannot understand. A future
    workflow syntax change needs a reviewed parser/test change, not a bypass.
    """
    observed: set[str] = set()
    for path in sorted((root / ".github/workflows").glob("*")):
        if path.suffix not in (".yml", ".yaml"):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*(?:-\s*)?uses\s*:\s*(.*?)\s*(?:#.*)?$", line)
            if not match:
                continue
            value = match[1].strip().strip("\"'")
            if value.startswith("./"):
                raise RuntimeError("Local workflow actions need an explicitly reviewed provenance policy")
            try:
                repo, commit = value.split("@")
                expected = ACTION_PINS[repo][1]
            except (ValueError, KeyError) as exc:
                raise RuntimeError(f"Unreviewed workflow action: {value}") from exc
            if commit != expected:
                raise RuntimeError(f"Workflow action pin disagrees with policy: {repo}@{commit}")
            observed.add(repo)
    if observed != set(ACTION_PINS):
        raise RuntimeError("Workflow action inventory does not match ACTION_PINS")


def selected_sources(config: BuildConfig, explicit: list[str]) -> dict[str, Any]:
    components: dict[str, Any] = {
        "gki": {"selection": "latest-published-certified", "repo_url":
                "https://android.googlesource.com/kernel/common", "tag": config.gki_tag,
                "commit": config.gki_commit, "build_id": config.official_build_id},
        "manifest": {"selection": "matching-month", "repo_url":
                     "https://android.googlesource.com/kernel/manifest",
                     "ref": "refs/heads/" + config.manifest_branch, "commit": config.manifest_commit},
        "certified_projects": {"selection": "certified-build-manifest",
                               "projects": [list(project) for project in config.source_projects]},
    }
    for name, repo in (("sukisu", KSU_REPO_CONFIG), ("susfs", SUSFS_REPO_CONFIG),
                       ("sukisu_patch", SUKISU_PATCH_REPO_CONFIG), ("anykernel", ANYKERNEL_CONFIG),
                       ("repo", REPO_TOOL_CONFIG)):
        components[name] = {
            "selection": "explicit-commit" if name in explicit else "latest-branch-at-resolution",
            "repo_url": repo["repo_url"], "ref": "refs/heads/" + repo["branch"],
            "commit": getattr(config, name + "_commit"),
        }
    return components


def make_upstream_status(config: BuildConfig, checked: dict, explicit: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "policy": SOURCE_POLICY,
        "family": ANDROID_FAMILY,
        "explicit_commit_overrides": sorted(explicit),
        "components": selected_sources(config, explicit),
        "reviewed_ports": checked["reviewed_ports"],
        "actions": checked["actions"],
        "kmi_status": KMI_STATUS,
    }


def validate_upstream_status(config: BuildConfig) -> None:
    record = config.upstream_status
    keys = {"schema_version", "checked_at_utc", "policy", "family", "explicit_commit_overrides",
            "components", "reviewed_ports", "actions", "kmi_status"}
    if not isinstance(record, dict) or set(record) != keys:
        raise RuntimeError("Missing/invalid upstream freshness evidence; resolve a new source lock")
    explicit = record["explicit_commit_overrides"]
    if (not isinstance(explicit, list) or any(not isinstance(x, str) for x in explicit)
            or any(x not in {"sukisu", "susfs", "sukisu_patch", "anykernel"} for x in explicit)
            or explicit != sorted(set(explicit))):
        raise RuntimeError("Invalid explicit-commit source selection record")
    if (type(record["schema_version"]) is not int or record["schema_version"] != 1
            or record["policy"] != SOURCE_POLICY or record["family"] != ANDROID_FAMILY
            or record["kmi_status"] != KMI_STATUS
            or record["components"] != selected_sources(config, explicit)
            or record["reviewed_ports"] != expected_reviewed_ports()
            or record["actions"] != expected_actions()):
        raise RuntimeError("Upstream evidence differs from the selected sources or reviewed policy")
    stamp = record["checked_at_utc"]
    if not isinstance(stamp, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp):
        raise RuntimeError("Invalid upstream resolution timestamp")
    try:
        datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("Invalid upstream resolution timestamp") from exc
