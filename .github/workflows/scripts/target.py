"""Resolve the newest official android13-5.15 stable and the SukiSU channel."""
import hashlib
import os
import re
import subprocess
import urllib.request
import base64
import xml.etree.ElementTree as ET
from abi import artifact_url
from dataclasses import dataclass
from config import (KSU_REPO_CONFIG, SUSFS_REPO_CONFIG, SUKISU_PATCH_REPO_CONFIG, ANYKERNEL_CONFIG,
                    LZ4_RELEASE, LZ4_REVISION, LZ4_REPO_URL, LZ4_UPSTREAM_SHA256,
                    BBR_REVISION, BBR_REPO_CONFIG, ACTION_PINS)
from upstream_policy import (fetch_bytes, latest_release, expected_reviewed_ports,
                             expected_actions, verify_workflow_pins)


GKI_URL = "https://android.googlesource.com/kernel/common"
MANIFEST_URL = "https://android.googlesource.com/kernel/manifest"
KSU_URL = KSU_REPO_CONFIG["repo_url"]
GKI_RE = re.compile(r"^android13-5\.15-(\d{4})-(\d{2})_r(\d+)$")
MANIFEST_RE = re.compile(r"^common-android13-5\.15-\d{4}-\d{2}$")
GKI_RELEASES_URL = "https://source.android.com/docs/core/architecture/kernel/gki-android13-5_15-release-builds"


def _git(*args):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True, timeout=120, env=env).stdout


def parse_refs(output):
    refs = {}
    for line in output.splitlines():
        sha, ref = line.split("\t", 1)
        key = ref.removeprefix("refs/tags/")
        if key in refs and refs[key] != sha:
            raise RuntimeError(f"Conflicting remote ref: {ref}")
        refs[key] = sha
    return refs


@dataclass(frozen=True)
class GKITarget:
    tag: str
    commit: str
    kernel_version: str
    manifest_branch: str
    manifest_commit: str = ""
    official_build_id: str = ""
    source_projects: tuple = ()


def choose_gki_tag(refs):
    candidates = []
    for name in refs:
        match = GKI_RE.fullmatch(name)
        if match:
            candidates.append((int(match[1]), int(match[2]), int(match[3]), name))
    if not candidates:
        raise RuntimeError("No certified android13-5.15 monthly release tag")
    return max(candidates)[3]


def peeled_commit(refs, tag):
    # Annotated tags need the peeled commit; never record the tag object.
    commit = refs.get(tag + "^{}", refs.get(tag))
    if not commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError(f"Invalid commit for {tag}: {commit}")
    return commit


def choose_manifest_branch(output):
    branches = [ref.removeprefix("refs/heads/")
                for _, ref in (line.split("\t", 1) for line in output.splitlines())]
    candidates = [name for name in branches if MANIFEST_RE.fullmatch(name)]
    if not candidates:
        raise RuntimeError("No official android13-5.15 monthly manifest branch")
    return max(candidates)


def certified_releases(html):
    """Use rows containing certified downloads, excluding debug/point builds."""
    releases = {}
    for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, re.S):
        tag = re.search(r"gki-certified-boot-(android13-5\.15-\d{4}-\d{2}_r\d+)\.zip", row)
        build = re.search(r"ci\.android\.com/builds/submitted/(\d+)/kernel_aarch64/latest", row)
        if tag and build:
            if tag[1] in releases and releases[tag[1]] != build[1]:
                raise RuntimeError(f"Conflicting certified build IDs for {tag[1]}")
            releases[tag[1]] = build[1]
    if not releases:
        raise RuntimeError("AOSP release page contains no certified android13-5.15 releases")
    return releases


def resolve_gki():
    with urllib.request.urlopen(GKI_RELEASES_URL, timeout=30) as response:
        published = certified_releases(response.read().decode("utf-8"))
    refs = parse_refs(_git("ls-remote", "--tags", GKI_URL,
                           "android13-5.15-*_r*"))
    tag = choose_gki_tag(published)
    commit = peeled_commit(refs, tag)
    match = GKI_RE.fullmatch(tag)
    branch = f"common-android13-5.15-{match[1]}-{match[2]}"
    manifest_refs = _git("ls-remote", "--heads", MANIFEST_URL, f"refs/heads/{branch}")
    manifest_commit = parse_refs(manifest_refs).get(f"refs/heads/{branch}")
    if not manifest_commit or not re.fullmatch(r"[0-9a-f]{40}", manifest_commit):
        raise RuntimeError(f"Cannot resolve matching manifest: {branch}")
    with urllib.request.urlopen(f"{GKI_URL}/+/{commit}/Makefile?format=TEXT", timeout=30) as response:
        makefile = base64.b64decode(response.read()).decode("utf-8")
    version = [re.search(rf"^{key}\s*=\s*(\d+)$", makefile, re.M)
               for key in ("VERSION", "PATCHLEVEL", "SUBLEVEL")]
    if not all(version) or [item[1] for item in version[:2]] != ["5", "15"]:
        raise RuntimeError(f"Invalid GKI version for {tag}")
    build_id = published[tag]
    with urllib.request.urlopen(artifact_url(build_id, f"manifest_{build_id}.xml"), timeout=60) as response:
        manifest = ET.fromstring(response.read())
    projects = tuple((project.get("name"), project.get("path"), project.get("revision"))
                     for project in manifest.findall("project"))
    if not any(path == "common" and revision == commit for _, path, revision in projects):
        raise RuntimeError("Certified manifest does not match the selected GKI tag")
    if any(not re.fullmatch(r"[0-9a-f]{40}", revision or "") for _, _, revision in projects):
        raise RuntimeError("Certified manifest contains unpinned project revisions")
    return GKITarget(tag, commit, ".".join(item[1] for item in version), branch,
                     manifest_commit, build_id, projects)


def resolve_branch(repo):
    ref = f"refs/heads/{repo['branch']}"
    output = _git("ls-remote", "--heads", repo["repo_url"], ref)
    sha = parse_refs(output).get(ref, "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError(f"Cannot resolve branch {ref} from {repo['repo_url']}")
    return sha


def resolve_susfs():
    return resolve_branch(SUSFS_REPO_CONFIG)


def resolve_sukisu_patch():
    return resolve_branch(SUKISU_PATCH_REPO_CONFIG)


def resolve_anykernel():
    return resolve_branch(ANYKERNEL_CONFIG)


def check_reviewed_upstreams(strict=True):
    """Fail closed on unreviewed upstream drift; never import a newer port blindly.

    Each successful fresh resolution proves freshness at observation time. An
    existing source lock replays the same revisions without chasing moving refs.
    The old warning-only API is deliberately no longer supported.
    """
    if strict is not True:
        raise RuntimeError("Warning-only upstream checks are disabled; strict=True is mandatory")
    verify_workflow_pins()
    bbr = resolve_branch(BBR_REPO_CONFIG)
    if bbr != BBR_REVISION:
        raise RuntimeError(f"Reviewed BBR port is stale: {BBR_REVISION} -> {bbr}. "
                           "Review/rebase the android13-5.15 port and revalidate KMI; no fallback.")
    tag = latest_release("lz4/lz4")
    if tag != LZ4_RELEASE:
        raise RuntimeError(f"Reviewed LZ4 port is stale: {LZ4_RELEASE} -> {tag}. "
                           "Review the private crypto port before building; no fallback.")
    refs = parse_refs(_git("ls-remote", "--tags", LZ4_REPO_URL,
                           f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"))
    commit = peeled_commit(refs, tag)
    if commit != LZ4_REVISION:
        raise RuntimeError(f"LZ4 release tag moved or differs from reviewed commit: {commit}")
    for path, expected in LZ4_UPSTREAM_SHA256.items():
        url = f"https://raw.githubusercontent.com/lz4/lz4/{commit}/{path}"
        actual = hashlib.sha256(fetch_bytes(url)).hexdigest()
        if actual != expected:
            raise RuntimeError(f"LZ4 upstream source hash mismatch: {path}: {actual} != {expected}")
    for repo, (version, expected) in ACTION_PINS.items():
        current = latest_release(repo)
        if current != version:
            raise RuntimeError(f"Reviewed action {repo} is stale: {version} -> {current}. "
                               "Review the release and update the full SHA pin and ACTION_PINS together.")
        refs = parse_refs(_git("ls-remote", "--tags", f"https://github.com/{repo}.git",
                               f"refs/tags/{version}", f"refs/tags/{version}^{{}}"))
        if peeled_commit(refs, version) != expected:
            raise RuntimeError(f"Action release tag moved or pin mismatches upstream: {repo}@{version}")
    return {"reviewed_ports": expected_reviewed_ports(), "actions": expected_actions()}


def resolve_sukisu(channel):
    if channel not in ("stable", "dev"):
        raise ValueError(f"Unknown SukiSU channel: {channel}")
    # Both existing workflow choices follow the upstream `builtin` branch,
    # which carries the KernelSU-side SUSFS integration natively. The default
    # `main` branch has no SUSFS support. Resolve once so setup.sh and the
    # kernel checkout use the same exact commit throughout this build.
    ref = f"refs/heads/{KSU_REPO_CONFIG['branch']}"
    output = _git("ls-remote", "--heads", KSU_URL, ref)
    sha = parse_refs(output).get(ref, "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("Cannot resolve SukiSU builtin branch")
    return "", sha
