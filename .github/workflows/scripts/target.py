"""Resolve the newest official android13-5.15 stable and the SukiSU channel."""
import json
import re
import subprocess
import urllib.request
from dataclasses import dataclass


GKI_URL = "https://android.googlesource.com/kernel/common"
MANIFEST_URL = "https://android.googlesource.com/kernel/manifest"
KSU_URL = "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git"
GKI_RE = re.compile(r"^android13-5\.15\.(\d+)_r(\d+)$")
MANIFEST_RE = re.compile(r"^common-android13-5\.15-\d{4}-\d{2}$")


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


def choose_gki_tag(refs):
    candidates = []
    for name in refs:
        match = GKI_RE.fullmatch(name)
        if match:
            candidates.append((int(match[1]), int(match[2]), name))
    if not candidates:
        raise RuntimeError("No official android13-5.15.x_rNN point-release tag")
    return max(candidates)[2]


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


def resolve_gki():
    refs = parse_refs(_git("ls-remote", "--tags", GKI_URL,
                           "android13-5.15.*_r*"))
    tag = choose_gki_tag(refs)
    commit = peeled_commit(refs, tag)
    sublevel = GKI_RE.fullmatch(tag)[1]
    branch = choose_manifest_branch(_git("ls-remote", "--heads", MANIFEST_URL,
                                         "common-android13-5.15-*"))
    return GKITarget(tag, commit, f"5.15.{sublevel}", branch)


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
    request = urllib.request.Request(
        "https://api.github.com/repos/SukiSU-Ultra/SukiSU-Ultra/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "gki-builder"})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    tag = release["tag_name"]
    if release.get("draft") or release.get("prerelease") or not re.fullmatch(r"v?\d+(?:\.\d+)+", tag):
        raise RuntimeError(f"SukiSU latest release is not a formal version: {tag}")
    refs = parse_refs(_git("ls-remote", "--tags", KSU_URL, tag))
    commit = refs.get(tag + "^{}", refs.get(tag))
    if not commit or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError(f"Cannot resolve SukiSU release tag {tag}")
    return tag, commit
