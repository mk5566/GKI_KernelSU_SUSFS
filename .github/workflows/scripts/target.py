"""Resolve the SukiSU channel. The GKI revision is pinned in config.py."""
import json
import re
import subprocess
import urllib.request


KSU_URL = "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git"


def _git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True, timeout=120).stdout


def parse_refs(output):
    refs = {}
    for line in output.splitlines():
        sha, ref = line.split("\t", 1)
        refs[ref.removeprefix("refs/tags/")] = sha
    return refs


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
