"""Resolve the newest official android13-5.15 stable and the SukiSU channel."""
import json
import re
import subprocess
import urllib.request
import base64
import xml.etree.ElementTree as ET
from abi import artifact_url
from config import SUKISU_STABLE_REVISION
from dataclasses import dataclass


GKI_URL = "https://android.googlesource.com/kernel/common"
MANIFEST_URL = "https://android.googlesource.com/kernel/manifest"
KSU_URL = "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git"
GKI_RE = re.compile(r"^android13-5\.15-(\d{4})-(\d{2})_r(\d+)$")
MANIFEST_RE = re.compile(r"^common-android13-5\.15-\d{4}-\d{2}$")
GKI_RELEASES_URL = "https://source.android.com/docs/core/architecture/kernel/gki-android13-5_15-release-builds"


def _git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True, timeout=120).stdout


def parse_refs(output):
    refs = {}
    for line in output.splitlines():
        sha, ref = line.split("\t", 1)
        refs[ref.removeprefix("refs/tags/")] = sha
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


SUSFS_URL = "https://github.com/ShirkNeko/susfs4ksu.git"
SUKISU_PATCH_URL = "https://github.com/ShirkNeko/SukiSU_patch.git"


def resolve_susfs(branch="gki-android13-5.15"):
    output = _git("ls-remote", "--heads", SUSFS_URL, f"refs/heads/{branch}")
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].strip() == f"refs/heads/{branch}":
            sha = parts[0].strip()
            if re.fullmatch(r"[0-9a-f]{40}", sha):
                return sha
    raise RuntimeError(f"Cannot resolve SUSFS branch HEAD: {branch}")


def resolve_sukisu_patch(branch="main"):
    output = _git("ls-remote", "--heads", SUKISU_PATCH_URL, f"refs/heads/{branch}")
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].strip() == f"refs/heads/{branch}":
            sha = parts[0].strip()
            if re.fullmatch(r"[0-9a-f]{40}", sha):
                return sha
    raise RuntimeError(f"Cannot resolve SukiSU_patch branch HEAD: {branch}")


def resolve_sukisu(channel):
    if channel == "dev":
        output = _git("ls-remote", "--symref", KSU_URL, "HEAD")
        match = re.search(r"^ref: refs/heads/(\S+)\s+HEAD$", output, re.M)
        sha = re.search(r"^([0-9a-f]{40})\s+HEAD$", output, re.M)
        if not match or not sha:
            raise RuntimeError("Cannot resolve SukiSU default branch")
        return "", sha[1]
    if channel != "stable":
        raise ValueError(f"Unknown SukiSU channel: {channel}")
    # Keep the actual UAPI-4 source that booted in run 35612891639, rather
    # than silently reverting to the older UAPI-2 formal v4.2.0 release.
    return "v4.2.0-reviewed-uapi4", SUKISU_STABLE_REVISION
