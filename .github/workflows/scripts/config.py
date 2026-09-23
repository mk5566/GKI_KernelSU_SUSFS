from dataclasses import dataclass
from typing import Optional
from enum import Enum
import re


class AndroidVersion(Enum):
    ANDROID13 = "android13"


class KernelVersion(Enum):
    KERNEL_5_15 = "5.15"


class KSUVersion(Enum):
    STABLE = "Stable(standard)"
    DEV = "Dev(development)"


ANDROID_KERNEL_MAP = {
    AndroidVersion.ANDROID13: [KernelVersion.KERNEL_5_15],
}

KSU_REPO_CONFIG = {
    "repo_url": "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git",
    "branch": "main",
}

# Recorded SukiSU revision for the optional reproducible Stable build.
SUKISU_MAIN_REVISION = "cf87e3f4ddd3f6e5464d85acf56aaa6950e70841"
SUKISU_UAPI_VERSION = 4
SUSFS_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/susfs4ksu.git"}

SUKISU_PATCH_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/SukiSU_patch.git"}

ANYKERNEL_CONFIG = {
    "repo_url": "https://github.com/WildPlusKernel/AnyKernel3.git",
    "branch": "gki-2.0",
}

TOOLCHAIN_CONFIG = {
    "aosp_mirror": "https://android.googlesource.com",
    "build_tools_branch": "main-kernel-build-2024",
    "mkbootimg_branch": "main-kernel-build-2024",
}

# Explicit commit/tag/builtin only. Short SHAs are allowed but fetch may need
# the full 40-char SHA (enforced at checkout time).
_REF_RE = re.compile(
    r"^(?:[0-9a-f]{7,40}|HEAD~\d+|builtin|main|v[0-9][\w.\-]*)$",
    re.IGNORECASE,
)


def validate_git_ref(value: str, name: str) -> str:
    ref = (value or "").strip()
    if not ref:
        return ""
    if not _REF_RE.match(ref):
        raise ValueError(
            f"Invalid {name} {value!r}. Use a 7-40 char hex SHA, HEAD~N, "
            f"builtin, main, or a v-prefixed tag."
        )
    return ref


def sanitize_custom_version(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")[:48]
    return cleaned or None


@dataclass
class BuildConfig:
    android_version: str = AndroidVersion.ANDROID13.value
    kernel_version: str = KernelVersion.KERNEL_5_15.value
    sub_level: str = ""
    os_patch_level: Optional[str] = None
    kernelsu_version: str = KSUVersion.DEV.value
    kernelsu_commit: Optional[str] = None
    susfs_commit: Optional[str] = None
    use_zram: bool = False
    set_default_bbr: bool = False
    make_release: bool = False
    custom_version: Optional[str] = None
    build_id: Optional[str] = None

    def __post_init__(self):
        self._normalize_ksu_version()
        self.kernelsu_commit = validate_git_ref(self.kernelsu_commit or "", "kernelsu_commit") or None
        self.susfs_commit = validate_git_ref(self.susfs_commit or "", "susfs_commit") or None
        self.custom_version = sanitize_custom_version(self.custom_version)
        self._validate_android_version()
        self._validate_kernel_version()
        self._validate_kernel_android_compat()
        self._validate_target()
        self._set_build_id()

    def _normalize_ksu_version(self):
        if self.kernelsu_version in ["Stable(标准)", "Stable(standard)", "Stable", "stable"]:
            self.kernelsu_version = KSUVersion.STABLE.value
        elif self.kernelsu_version in ["Dev(开发)", "Dev(development)", "Dev", "dev"]:
            self.kernelsu_version = KSUVersion.DEV.value

    def _validate_android_version(self):
        valid = [v.value for v in AndroidVersion]
        if self.android_version not in valid:
            raise ValueError(f"Invalid Android version: {self.android_version}. Supported: {', '.join(valid)}")

    def _validate_kernel_version(self):
        valid = [v.value for v in KernelVersion]
        if self.kernel_version not in valid:
            raise ValueError(f"Invalid Kernel version: {self.kernel_version}. Supported: {', '.join(valid)}")

    def _validate_kernel_android_compat(self):
        av = AndroidVersion(self.android_version)
        kv = KernelVersion(self.kernel_version)
        if kv not in ANDROID_KERNEL_MAP.get(av, []):
            raise ValueError(f"Android {self.android_version} does not support Kernel {self.kernel_version}")

    def _validate_target(self):
        if not re.fullmatch(r"[1-9]\d*", self.sub_level):
            raise ValueError(f"Invalid kernel sublevel: {self.sub_level!r}")
        if not self.os_patch_level or not re.fullmatch(r"20\d\d-(?:0[1-9]|1[0-2])", self.os_patch_level):
            raise ValueError(f"Invalid OS patch level: {self.os_patch_level!r}")

    def _set_build_id(self):
        if self.build_id is None:
            self.build_id = (
                f"{self.android_version}-{self.kernel_version}-"
                f"{self.sub_level}-{self.os_patch_level}"
            )

    @property
    def config_name(self) -> str:
        return f"{self.android_version}-{self.kernel_version}-{self.sub_level}"

    @property
    def formatted_branch(self) -> str:
        return f"{self.android_version}-{self.kernel_version}-{self.os_patch_level}"

    @property
    def kernel_branch(self) -> str:
        return f"gki-{self.android_version}-{self.kernel_version}"

    @property
    def ksu_setup_ref(self) -> Optional[str]:
        if self.kernelsu_commit:
            return self.kernelsu_commit
        if self.kernelsu_version == KSUVersion.DEV.value:
            return "main"
        # Stable: reproducible main revision; SUSFS is added by this builder.
        return SUKISU_MAIN_REVISION

    @property
    def variant_suffix(self) -> str:
        parts = [
            "zstd-requested" if self.use_zram else "lz4kd-builtin",
            "bbr1-default" if self.set_default_bbr else "rom-tcp",
            "sukisu-dev" if self.kernelsu_version == KSUVersion.DEV.value else "sukisu-stable",
        ]
        if self.custom_version:
            parts.append(self.custom_version)
        return "-".join(parts)

    @property
    def artifact_stem(self) -> str:
        return (
            f"{self.android_version}-{self.kernel_version}."
            f"{self.sub_level}-{self.os_patch_level}-{self.variant_suffix}"
        )

    def get_susfs_patch_filename(self) -> str:
        return f"50_add_susfs_in_gki-{self.android_version}-{self.kernel_version}.patch"

    def to_dict(self) -> dict:
        return {
            "android_version": self.android_version,
            "kernel_version": self.kernel_version,
            "sub_level": self.sub_level,
            "os_patch_level": self.os_patch_level,
            "kernelsu_version": self.kernelsu_version,
            "kernelsu_commit": self.kernelsu_commit,
            "susfs_commit": self.susfs_commit,
            "ksu_setup_ref": self.ksu_setup_ref,
            "use_zram": self.use_zram,
            "set_default_bbr": self.set_default_bbr,
            "make_release": self.make_release,
            "custom_version": self.custom_version,
            "build_id": self.build_id,
            "artifact_stem": self.artifact_stem,
        }
