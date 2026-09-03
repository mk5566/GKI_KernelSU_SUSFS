from dataclasses import dataclass
from typing import Optional
from enum import Enum
import re
import urllib.request
import ssl


_SUSFS_VERSION_CACHE = None


def get_susfs_version() -> str:
    """Fetch SUSFS version from the gki-android13-5.15 branch, with a local default."""
    global _SUSFS_VERSION_CACHE
    if _SUSFS_VERSION_CACHE:
        return _SUSFS_VERSION_CACHE

    ssl_ctx = ssl.create_default_context()
    branches = ["gki-android13-5.15", "main"]
    version_pattern = re.compile(r'#define\s+SUSFS_VERSION\s+"([^"]+)"')

    for branch in branches:
        try:
            url = f"https://raw.githubusercontent.com/ShirkNeko/susfs4ksu/{branch}/kernel_patches/include/linux/susfs.h"
            req = urllib.request.Request(url, headers={"User-Agent": "GKI-Builder"})
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=10) as response:
                content = response.read().decode("utf-8")
                match = version_pattern.search(content)
                if match:
                    _SUSFS_VERSION_CACHE = match.group(1)
                    return _SUSFS_VERSION_CACHE
        except Exception:
            continue

    _SUSFS_VERSION_CACHE = "v2.3.0"
    return _SUSFS_VERSION_CACHE


KERNEL_VERSION = "v2.3.0"


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

# Repository configurations
KSU_REPO_CONFIG = {
    "repo_url": "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git",
    "branch": "main",
    "setup_script": "https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/main/kernel/setup.sh",
}

# SUSFS repository configuration
SUSFS_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/susfs4ksu.git"}

# SukiSU Patch repository configuration
SUKISU_PATCH_REPO_CONFIG = {"repo_url": "https://github.com/ShirkNeko/SukiSU_patch.git"}

# AnyKernel3 repository configuration
ANYKERNEL_CONFIG = {"repo_url": "https://github.com/WildPlusKernel/AnyKernel3.git", "branch": "gki-2.0"}

# Toolchain configuration
TOOLCHAIN_CONFIG = {
    "aosp_mirror": "https://android.googlesource.com",
    "build_tools_branch": "main-kernel-build-2024",
    "mkbootimg_branch": "main-kernel-build-2024",
}



@dataclass
class BuildConfig:
    android_version: str = "android13"
    kernel_version: str = "5.15"
    sub_level: str = "180"
    os_patch_level: str = "2025-05"
    kernelsu_version: str = "Stable(standard)"
    kernelsu_commit: Optional[str] = None
    susfs_commit: Optional[str] = None
    use_zram: bool = True
    set_default_bbr: bool = True
    make_release: bool = False
    custom_version: Optional[str] = None
    build_id: Optional[str] = None

    def __post_init__(self):
        self._normalize_ksu_version()
        self._validate_android_version()
        self._validate_kernel_version()
        self._validate_kernel_android_compat()
        self._validate_sub_level()
        self._set_build_id()

    def _normalize_ksu_version(self):
        if self.kernelsu_version in ["Stable(标准)", "Stable(standard)", "Stable", "stable"]:
            self.kernelsu_version = "Stable(standard)"
        elif self.kernelsu_version in ["Dev(开发)", "Dev(development)", "Dev", "dev"]:
            self.kernelsu_version = "Dev(development)"

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

    def _validate_sub_level(self):
        if self.sub_level != "X" and not self.sub_level.isdigit():
            raise ValueError(f"Invalid sub_level: {self.sub_level}")

    def _set_build_id(self):
        if self.build_id is None:
            self.build_id = f"{self.android_version}-{self.kernel_version}-{self.sub_level}-{self.os_patch_level}"

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
    def ksu_setup_ref(self) -> str:
        if self.kernelsu_commit:
            return self.kernelsu_commit
        # GKI built-in integration always uses the builtin kernel tree.
        # Stable and Dev both consume builtin; Dev tracks branch HEAD.
        return "builtin"

    def get_susfs_patch_filename(self) -> str:
        return f"50_add_susfs_in_gki-{self.android_version}-{self.kernel_version}.patch"

    def is_lts(self) -> bool:
        return self.sub_level == "X"

    def get_sub_level_int(self) -> Optional[int]:
        return None if self.sub_level == "X" else int(self.sub_level)

    def to_dict(self) -> dict:
        return {
            "android_version": self.android_version,
            "kernel_version": self.kernel_version,
            "sub_level": self.sub_level,
            "os_patch_level": self.os_patch_level,
            "kernelsu_version": self.kernelsu_version,
            "kernelsu_commit": self.kernelsu_commit,
            "use_zram": self.use_zram,
            "set_default_bbr": self.set_default_bbr,
            "make_release": self.make_release,
            "custom_version": self.custom_version,
            "build_id": self.build_id,
        }


def validate_commit_hash(commit_hash: str) -> bool:
    return bool(re.match(r'^[0-9a-f]{7,40}$', commit_hash, re.IGNORECASE))
