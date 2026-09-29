"""One Android 13 / Linux 5.15 build family with three supported choices."""
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class KSUChannel(str, Enum):
    STABLE = "stable"
    DEV = "dev"


class BBRVersion(str, Enum):
    V1 = "v1"
    V3 = "v3"


ANDROID_FAMILY = "android13-5.15"
KSU_REPO_CONFIG = {"repo_url": "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git"}
# Upstream stable and development may expose different UAPI revisions.
# Record the resolved value and require a declared, compatible layout.
SUPPORTED_SUKISU_UAPI = frozenset({2, 4})
# The mount-only port is reviewed against this exact SUSFS source revision.
SUSFS_REVISION = "e565931d19256fd821ada01b35263506e7c7a364"
SUSFS_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/susfs4ksu.git"}
SUKISU_PATCH_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/SukiSU_patch.git"}
SUKISU_PATCH_REVISION = "547ae94bcaec53d030398f857950c64662043a5d"
ANYKERNEL_CONFIG = {
    "repo_url": "https://github.com/WildPlusKernel/AnyKernel3.git", "branch": "gki-2.0"
}
# Source-of-truth reference for the maintained 5.15 backport, updated deliberately.
GOOGLE_BBR_V3_COMMIT = "90210de4b779d40496dee0b89081780eeddf2a60"
REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class BuildConfig:
    sukisu_channel: str = KSUChannel.STABLE.value
    bbr_version: str = BBRVersion.V3.value
    apply_tweaks: bool = True
    gki_tag: str = ""
    gki_commit: str = ""
    kernel_version: str = ""
    manifest_branch: str = ""
    sukisu_tag: str = ""
    sukisu_commit: str = ""

    def __post_init__(self):
        self.sukisu_channel = KSUChannel(self.sukisu_channel).value
        self.bbr_version = BBRVersion(self.bbr_version).value
        if not isinstance(self.apply_tweaks, bool):
            raise ValueError("apply_tweaks must be boolean")

    @property
    def config_name(self):
        return ANDROID_FAMILY

    @property
    def kernel_branch(self):
        return f"gki-{ANDROID_FAMILY}"

    @property
    def artifact_stem(self):
        version = self.kernel_version or "5.15-unresolved"
        tweaks = "tweaks" if self.apply_tweaks else "no-tweaks"
        return f"{ANDROID_FAMILY}.{version.rsplit('.', 1)[-1]}-sukisu-{self.sukisu_channel}-bbr{self.bbr_version}-{tweaks}"

    def get_susfs_patch_filename(self):
        return "50_add_susfs_in_gki-android13-5.15.patch"

    def to_dict(self):
        return dict(self.__dict__, artifact_stem=self.artifact_stem)
