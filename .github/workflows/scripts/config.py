"""One kernel: the newest official android13-5.15 GKI stable."""
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class KSUChannel(str, Enum):
    STABLE = "stable"
    DEV = "dev"


ANDROID_FAMILY = "android13-5.15"
KSU_REPO_CONFIG = {"repo_url": "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git"}
# Upstream stable and development may expose different UAPI revisions.
SUPPORTED_SUKISU_UAPI = frozenset({2, 4})
# The mount-only port is reviewed against this exact SUSFS source revision.
SUSFS_REVISION = "687d2d18d94cb2e3e72d1074778d58384d58e379"
SUSFS_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/susfs4ksu.git"}
SUKISU_PATCH_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/SukiSU_patch.git"}
SUKISU_PATCH_REVISION = "547ae94bcaec53d030398f857950c64662043a5d"
ANYKERNEL_CONFIG = {
    "repo_url": "https://github.com/WildPlusKernel/AnyKernel3.git", "branch": "gki-2.0"
}
REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class BuildConfig:
    sukisu_channel: str = KSUChannel.STABLE.value
    sukisu_tag: str = ""
    sukisu_commit: str = ""
    gki_tag: str = ""
    gki_commit: str = ""
    kernel_version: str = ""
    manifest_branch: str = ""
    susfs_commit: str = ""
    sukisu_patch_commit: str = ""

    def __post_init__(self):
        self.sukisu_channel = KSUChannel(self.sukisu_channel).value
        if not self.susfs_commit:
            self.susfs_commit = SUSFS_REVISION
        if not self.sukisu_patch_commit:
            self.sukisu_patch_commit = SUKISU_PATCH_REVISION

    @property
    def config_name(self):
        return ANDROID_FAMILY

    @property
    def kernel_branch(self):
        return f"gki-{ANDROID_FAMILY}"

    @property
    def artifact_stem(self):
        sublevel = self.kernel_version.rsplit(".", 1)[-1]
        return f"{ANDROID_FAMILY}.{sublevel}-sukisu-{self.sukisu_channel}"

    def get_susfs_patch_filename(self):
        return "50_add_susfs_in_gki-android13-5.15.patch"

    def to_dict(self):
        return {
            "sukisu_channel": self.sukisu_channel,
            "sukisu_tag": self.sukisu_tag,
            "sukisu_commit": self.sukisu_commit,
            "gki_tag": self.gki_tag,
            "gki_commit": self.gki_commit,
            "kernel_version": self.kernel_version,
            "manifest_branch": self.manifest_branch,
            "susfs_commit": self.susfs_commit,
            "sukisu_patch_commit": self.sukisu_patch_commit,
            "artifact_stem": self.artifact_stem,
        }
