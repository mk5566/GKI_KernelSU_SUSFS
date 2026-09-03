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


# Single canonical lock for this slim tree. Workflow choices and BuildConfig
# both enforce these values.
LOCKED_TARGET = {
    "android": AndroidVersion.ANDROID13.value,
    "kernel": KernelVersion.KERNEL_5_15.value,
    "sub_level": "180",
    "os_patch_level": "2025-05",
}

ANDROID_KERNEL_MAP = {
    AndroidVersion.ANDROID13: [KernelVersion.KERNEL_5_15],
}

KSU_REPO_CONFIG = {
    "repo_url": "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git",
    "branch": "main",
}

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
    android_version: str = LOCKED_TARGET["android"]
    kernel_version: str = LOCKED_TARGET["kernel"]
    sub_level: str = LOCKED_TARGET["sub_level"]
    os_patch_level: str = LOCKED_TARGET["os_patch_level"]
    kernelsu_version: str = KSUVersion.STABLE.value
    kernelsu_commit: Optional[str] = None
    susfs_commit: Optional[str] = None
    use_zram: bool = True
    set_default_bbr: bool = True
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
        self._validate_locked_target()
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

    def _validate_locked_target(self):
        expected = (
            f"{LOCKED_TARGET['android']}-{LOCKED_TARGET['kernel']}."
            f"{LOCKED_TARGET['sub_level']} (OS patch {LOCKED_TARGET['os_patch_level']})"
        )
        if (
            self.android_version != LOCKED_TARGET["android"]
            or self.kernel_version != LOCKED_TARGET["kernel"]
            or self.sub_level != LOCKED_TARGET["sub_level"]
            or self.os_patch_level != LOCKED_TARGET["os_patch_level"]
        ):
            raise ValueError(
                f"This tree is locked to {expected}; got "
                f"{self.android_version}-{self.kernel_version}.{self.sub_level} "
                f"(OS patch {self.os_patch_level})"
            )

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
            return "builtin"
        # Stable: setup.sh with no args checks out the latest tagged release.
        return None

    @property
    def variant_suffix(self) -> str:
        parts = [
            "lz4kd" if self.use_zram else "nozram",
            "bbr3" if self.set_default_bbr else "nobbr",
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
