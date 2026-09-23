"""Resolve the newest published Android 13 GKI 5.15 monthly branch."""

import base64
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen


MANIFEST_URL = "https://android.googlesource.com/kernel/manifest"
COMMON_URL = "https://android.googlesource.com/kernel/common"
BRANCH_RE = re.compile(r"refs/heads/common-android13-5\.15-(20\d\d-(?:0[1-9]|1[0-2]))$")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")


@dataclass(frozen=True)
class TargetSelection:
    sublevel: str
    month: str
    common_revision: str

    def __post_init__(self):
        if not re.fullmatch(r"[1-9]\d*", self.sublevel):
            raise ValueError("Invalid GKI sublevel in target selection")
        if not re.fullmatch(r"20\d\d-(?:0[1-9]|1[0-2])", self.month):
            raise ValueError("Invalid GKI month in target selection")
        if not SHA_RE.fullmatch(self.common_revision):
            raise ValueError("Invalid common revision in target selection")

    def to_dict(self) -> dict:
        return {"schema": 1, "sublevel": self.sublevel, "month": self.month,
                "common_revision": self.common_revision}

    @classmethod
    def from_file(cls, path: str | Path) -> "TargetSelection":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "schema", "sublevel", "month", "common_revision"
        } or payload["schema"] != 1:
            raise ValueError("Invalid target selection file")
        return cls(payload["sublevel"], payload["month"], payload["common_revision"])

    def write(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")


def parse_makefile_version(content: str) -> str:
    values = dict(re.findall(r"^(VERSION|PATCHLEVEL|SUBLEVEL)\s*=\s*(\d+)\s*$", content, re.M))
    if set(values) != {"VERSION", "PATCHLEVEL", "SUBLEVEL"}:
        raise RuntimeError("GKI Makefile has no complete kernel version")
    if (values["VERSION"], values["PATCHLEVEL"]) != ("5", "15"):
        raise RuntimeError(f"Expected a 5.15 kernel, got {values['VERSION']}.{values['PATCHLEVEL']}")
    return values["SUBLEVEL"]


def resolve_latest_target_selection() -> TargetSelection:
    result = subprocess.run(
        ["git", "ls-remote", "--heads", MANIFEST_URL,
         "refs/heads/common-android13-5.15-20*"],
        capture_output=True, text=True, check=True, timeout=60,
    )
    months = [match.group(1) for line in result.stdout.splitlines()
              if (match := BRANCH_RE.search(line))]
    if not months:
        raise RuntimeError("No dated android13-5.15 manifest branch was found")
    month = max(months)
    common_branch = f"refs/heads/android13-5.15-{month}"
    common = subprocess.run(
        ["git", "ls-remote", "--heads", COMMON_URL, common_branch],
        capture_output=True, text=True, check=True, timeout=60,
    )
    refs = [line.split() for line in common.stdout.splitlines()]
    if len(refs) != 1 or len(refs[0]) != 2 or refs[0][1] != common_branch \
            or not SHA_RE.fullmatch(refs[0][0]):
        raise RuntimeError(f"No exact common revision for {common_branch}")
    common_revision = refs[0][0]
    makefile_url = f"{COMMON_URL}/+/{common_revision}/Makefile?format=TEXT"
    with urlopen(makefile_url, timeout=30) as response:
        makefile = base64.b64decode(response.read(), validate=True).decode("utf-8")
    return TargetSelection(parse_makefile_version(makefile), month, common_revision)


def resolve_latest_target() -> tuple[str, str]:
    selection = resolve_latest_target_selection()
    return selection.sublevel, selection.month
