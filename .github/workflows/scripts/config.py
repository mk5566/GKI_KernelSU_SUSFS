"""One kernel: the newest official android13-5.15 GKI stable."""
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re


class KSUChannel(str, Enum):
    STABLE = "stable"
    DEV = "dev"


ANDROID_FAMILY = "android13-5.15"
KSU_REPO_CONFIG = {"repo_url": "https://github.com/SukiSU-Nova/SukiSU-Nova.git",
                   # GKI integration lives on the upstream `builtin` branch, which
                   # carries the KernelSU-side SUSFS support natively. The `main`
                   # branch has no SUSFS and would need a hand-rolled shim.
                   "branch": "builtin"}
SUSFS_REPO_CONFIG = {
    "repo_url": "https://gitlab.com/simonpunk/susfs4ksu.git", "branch": "gki-android13-5.15",
}
SUKISU_PATCH_REPO_CONFIG = {
    "repo_url": "https://github.com/SukiSU-Nova/SukiSU_patch.git", "branch": "main",
}
ANYKERNEL_CONFIG = {
    "repo_url": "https://github.com/WildKernels/AnyKernel3.git", "branch": "gki-2.0",
}
REPO_TOOL_CONFIG = {"repo_url": "https://gerrit.googlesource.com/git-repo", "branch": "stable"}
# Reviewed code embedded in patches/. New upstream code needs a reviewed port,
# not a blind replacement of the frozen Android interfaces.
LZ4_RELEASE = "v1.11.1"
LZ4_REVISION = "aa4647fad575d01a874aaaadcdbe8e6a40e072f5"
LZ4_REPO_URL = "https://github.com/lz4/lz4.git"
# Original upstream bytes, before the documented private-kernel adaptations.
LZ4_UPSTREAM_SHA256 = {
    "lib/lz4.c": "2e58f124f1032f15aecc99772b7de624c4289245729fa5403506f00b06a54937",
    "lib/lz4.h": "c204caad0638a12c9ce80831094ec8fffb34aa57e74307067b08fc9452fc2611",
}
BBR_REPO_CONFIG = {"repo_url": "https://github.com/google/bbr.git", "branch": "v3"}
# Updating a workflow action requires updating this reviewed version AND its
# full commit pin. Resolution checks both against the latest stable release.
ACTION_PINS = {
    "actions/checkout": ("v7.0.1", "3d3c42e5aac5ba805825da76410c181273ba90b1"),
    "actions/setup-python": ("v7.1.0", "6d4fd22b20465ccc0f7354476588e8df7c9ca0e0"),
    "actions/upload-artifact": ("v7.0.2", "cf430e030ddbb5b0abf93d22962f4752f3646cd9"),
}
BBR_REVISION = "90210de4b779d40496dee0b89081780eeddf2a60"
REPO_ROOT = Path(__file__).resolve().parents[3]
# Only projects used by build.sh and boot packaging; omit Bazel/JDK/device trees.
REPO_SYNC_PROJECTS = (
    "common", "build/kernel", "kernel/configs",
    "prebuilts/clang/host/linux-x86", "prebuilts/build-tools",
    "prebuilts/kernel-build-tools",
    "prebuilts/gcc/linux-x86/host/x86_64-linux-glibc2.17-4.8",
    "prebuilts/ndk-r23", "tools/mkbootimg",
)


@dataclass
class BuildConfig:
    sukisu_channel: str = KSUChannel.STABLE.value
    sukisu_tag: str = ""
    sukisu_commit: str = ""
    gki_tag: str = ""
    gki_commit: str = ""
    kernel_version: str = ""
    manifest_branch: str = ""
    manifest_commit: str = ""
    official_build_id: str = ""
    source_projects: tuple[tuple[str, str, str], ...] = ()
    susfs_commit: str = ""
    sukisu_patch_commit: str = ""
    anykernel_commit: str = ""
    repo_commit: str = ""
    base_boot: str = ""
    upstream_status: dict = field(default_factory=dict)

    def __post_init__(self):
        self.sukisu_channel = KSUChannel(self.sukisu_channel).value
        if not isinstance(self.source_projects, (tuple, list)) or any(
                not isinstance(project, (tuple, list)) or len(project) != 3
                or not all(isinstance(value, str) for value in project)
                for project in self.source_projects):
            raise RuntimeError("Certified projects must be (name, path, revision) triples")
        self.source_projects = tuple(tuple(project) for project in self.source_projects)

    def validate(self):
        for name in ("gki_commit", "manifest_commit", "sukisu_commit", "susfs_commit",
                     "sukisu_patch_commit", "anykernel_commit", "repo_commit"):
            if not isinstance(getattr(self, name), str) or not re.fullmatch(r"[0-9a-f]{40}", getattr(self, name)):
                raise RuntimeError(f"Resolve {name} to an exact commit before building")
        match = re.fullmatch(r"android13-5\.15-(\d{4}-(?:0[1-9]|1[0-2]))_r[1-9]\d*", self.gki_tag)
        if not match or self.manifest_branch != f"common-android13-5.15-{match[1]}":
            raise RuntimeError("GKI tag and monthly manifest branch must match")
        if not re.fullmatch(r"5\.15\.\d+", self.kernel_version) or not re.fullmatch(r"[0-9]+", self.official_build_id):
            raise RuntimeError("Invalid certified kernel version or build ID")
        projects = {}
        for name, path, revision in self.source_projects:
            if not name or not path or path in projects or not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise RuntimeError("Invalid, duplicate or unpinned certified project")
            if path.startswith("/") or "\\" in path or any(part in ("", ".", "..") for part in path.split("/")):
                raise RuntimeError("Unsafe certified project path")
            projects[path] = revision
        if not set(REPO_SYNC_PROJECTS).issubset(projects) or projects.get("common") != self.gki_commit:
            raise RuntimeError("Certified manifest missing required projects or matching common commit")

    @property
    def config_name(self):
        return ANDROID_FAMILY

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
            "manifest_commit": self.manifest_commit,
            "official_build_id": self.official_build_id,
            "source_projects": self.source_projects,
            "susfs_commit": self.susfs_commit,
            "sukisu_patch_commit": self.sukisu_patch_commit,
            "anykernel_commit": self.anykernel_commit,
            "repo_commit": self.repo_commit,
            "upstream_status": self.upstream_status,
            "lz4_release": LZ4_RELEASE,
            "lz4_commit": LZ4_REVISION,
            "bbr_commit": BBR_REVISION,
            "artifact_stem": self.artifact_stem,
        }
