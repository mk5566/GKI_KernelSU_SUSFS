"""Small process and diagnostic helpers independent of kernel integration."""
import logging
import os
import subprocess
from collections import deque
from pathlib import Path
from typing import Mapping, Sequence

logger = logging.getLogger(__name__)
ERROR_PATTERNS = ("error:", "Error ", "ERROR", "undefined", "FAILED", "failed",
                  "ld.lld", "No such file", "*** ")


class ShellCommand:
    def __init__(self, cwd: str | None = None, env: Mapping[str, str] | None = None):
        self.cwd = cwd
        self.env = dict(os.environ if env is None else env)

    def run(self, cmd: str | Sequence[str], check: bool = True,
            capture_output: bool = False, shell: bool | None = None,
            timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        # Shell evaluation is reserved for explicitly supplied shell programs.
        # All paths and external values should use argv sequences.
        if shell is None:
            shell = isinstance(cmd, str)
        logger.info("Running: %s", cmd)
        try:
            return subprocess.run(cmd, shell=shell, cwd=self.cwd, env=self.env,
                                  capture_output=capture_output, text=True,
                                  timeout=timeout, check=check)
        except subprocess.CalledProcessError as exc:
            logger.error("Command failed (exit %s): %s", exc.returncode,
                         exc.stderr or exc.stdout or str(exc))
            raise
        except subprocess.TimeoutExpired:
            logger.error("Command timed out: %s", cmd)
            raise


def log_excerpt(path: Path, limit: int = 60, max_chars: int = 6000) -> str:
    """Memory stays bounded even when a failed full-LTO build emitted huge logs."""
    flagged: deque[str] = deque(maxlen=limit)
    tail: deque[str] = deque(maxlen=limit)
    with path.open(encoding="utf-8", errors="replace") as stream:
        while line := stream.readline(max_chars + 1):
            # A corrupted compiler diagnostic can also be one enormous line.
            # Discard its remainder in bounded chunks, retaining the prefix.
            if not line.endswith("\n"):
                while remainder := stream.readline(max_chars + 1):
                    if remainder.endswith("\n"):
                        break
            line = line.rstrip("\r\n")[:max_chars]
            tail.append(line)
            if any(token in line for token in ERROR_PATTERNS):
                flagged.append(line)
    # Reserve room for actual errors, not just make's last status lines.
    selected = list(dict.fromkeys([*flagged, *tail]))
    return "\n".join(selected[:limit])[:max_chars]


# These knobs can bypass defconfig, redirect outputs, or replace certified tools.
# Refuse them instead of silently building a different profile. AOSP itself
# remains responsible for setting its normal tool arguments after this boundary.
UNSUPPORTED_BUILD_ENV = (
    "SKIP_DEFCONFIG", "SKIP_MRPROPER", "SKIP_IF_VERSION_MATCHES", "TAGS_CONFIG",
    "OUT_DIR", "DIST_DIR", "KBUILD_OUTPUT", "KCONFIG_CONFIG", "KCONFIG_ALLCONFIG",
    "BUILD_CONFIG", "BUILD_CONFIG_FRAGMENTS", "GKI_BUILD_CONFIG", "GKI_BUILD_CONFIG_FRAGMENT",
    "CC", "LD", "HOSTCC", "HOSTCXX", "LLVM", "LLVM_IAS", "CROSS_COMPILE",
    "CROSS_COMPILE_COMPAT", "CROSS_COMPILE_ARM32", "ARCH", "SUBARCH", "LTO",
    "KCFLAGS", "KAFLAGS", "KCPPFLAGS", "KBUILD_CFLAGS", "KBUILD_AFLAGS",
    "HOSTCFLAGS", "HOSTLDFLAGS", "MAKEFLAGS", "MFLAGS",
    "HERMETIC_TOOLCHAIN", "ADDITIONAL_HOST_TOOLS", "CLANG_PREBUILT_BIN",
    "KERNEL_DIR", "DEFCONFIG", "MAKE_GOALS", "EXT_MODULES",
    "PRE_DEFCONFIG_CMDS", "POST_DEFCONFIG_CMDS", "POST_KERNEL_BUILD_CMDS",
    "EXTRA_CMDS", "BASH_ENV",
)


def validate_build_environment(env: Mapping[str, str]) -> None:
    unsafe = [name for name in UNSUPPORTED_BUILD_ENV if env.get(name)]
    if unsafe:
        raise RuntimeError("Unsupported ambient build overrides: " + ", ".join(unsafe))
