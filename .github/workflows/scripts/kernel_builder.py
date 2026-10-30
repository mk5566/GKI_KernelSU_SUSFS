import os
import shutil
import subprocess
import logging
import re
import sys
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from config import (ANDROID_FAMILY, BuildConfig, KSU_REPO_CONFIG, SUSFS_REPO_CONFIG,
                   SUKISU_PATCH_REPO_CONFIG, ANYKERNEL_CONFIG, REPO_TOOL_CONFIG, REPO_ROOT,
                   REPO_SYNC_PROJECTS)
from susfs_integration import select_mount_patch
from patch_plan import make_patch_plan
from abi import verify_gki_abi
from repack_boot import repack_boot
from evidence import write_sources, collect_evidence, sha256

logger = logging.getLogger(__name__)


_RELEASE_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
                   "jul", "aug", "sep", "oct", "nov", "dec")
_DIRTY_RELEASE = "printf '%s' -dirty"
_DATED_RELEASE = re.compile(r"printf '%s' -([a-z]{3}\d{1,2})")


def release_date_suffix(moment):
    """UTC month and day with no leading zero, as in oct6."""
    if moment.tzinfo is None:
        raise ValueError("release date requires a timezone")
    moment = moment.astimezone(timezone.utc)
    return f"{_RELEASE_MONTHS[moment.month - 1]}{moment.day}"


def apply_release_date_suffix(script, suffix):
    """Replace setlocalversion's -dirty with the build date.

    The GKI commit hash stays ahead of this suffix. The edit is repeatable.
    """
    if not re.fullmatch(r"[a-z]{3}(?:[1-9]|[12]\d|3[01])", suffix):
        raise ValueError(f"invalid release suffix: {suffix}")
    replacement = f"printf '%s' -{suffix}"
    if _DIRTY_RELEASE in script:
        if script.count(_DIRTY_RELEASE) != 1:
            raise RuntimeError("setlocalversion has more than one -dirty release suffix")
        return script.replace(_DIRTY_RELEASE, replacement, 1)
    matches = list(_DATED_RELEASE.finditer(script))
    if len(matches) != 1:
        raise RuntimeError("setlocalversion has no -dirty release suffix to replace")
    match = matches[0]
    return script[:match.start()] + replacement + script[match.end():]


def unquote_kconfig_string(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def ensure_cmdline_tokens(cmdline, tokens):
    """Keep existing parameters and make each requested token appear once.

    A different value of the same parameter is replaced. Repeating this on
    its own output does not add another copy.
    """
    parts = [part for part in cmdline.split() if part]
    desired = set(tokens)
    owned = {token.split("=", 1)[0] for token in tokens}
    kept = []
    seen = set()
    for part in parts:
        key = part.split("=", 1)[0]
        if key in owned and part not in desired:
            continue
        if part in seen:
            continue
        kept.append(part)
        seen.add(part)
    for token in tokens:
        if token not in seen:
            kept.append(token)
            seen.add(token)
    return " ".join(kept)


REQUIRED_TOOLS = ("git", "curl", "python3", "make", "bash", "zip")

@dataclass
class BuildResult:
    success: bool
    config: BuildConfig
    message: str = ""
    artifacts: list = field(default_factory=list)
    build_time: Optional[float] = None


class ShellCommand:
    def __init__(self, cwd: Optional[str] = None, env: Optional[dict] = None):
        self.cwd = cwd
        self.env = env or os.environ.copy()

    def run(self, cmd: str, check: bool = True, capture_output: bool = False,
            shell: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        logger.info(f"Running: {cmd}")
        try:
            return subprocess.run(cmd, shell=shell, cwd=self.cwd, env=self.env,
                                capture_output=capture_output, text=True, timeout=timeout, check=check)
        except subprocess.CalledProcessError as e:
            output = e.stderr or e.stdout or str(e)
            logger.error(f"Command failed (exit {e.returncode}): {output}")
            raise
        except subprocess.TimeoutExpired:
            logger.error(f"Command timed out: {cmd}")
            raise


class KernelBuilder:
    KERNEL_CONFIG_UPDATES = {
        "CONFIG_KSU": "y",
        "CONFIG_KPROBES": "y",
        "CONFIG_KRETPROBES": "y",
        "CONFIG_HAVE_SYSCALL_TRACEPOINTS": "y",
        "CONFIG_KSU_DEBUG": "n",
        # Device audit: no path/stat/map/spoof rules; retain mount hiding only.
        "CONFIG_KSU_SUSFS": "y",
        "CONFIG_KSU_SUSFS_SUS_PATH": "n",
        "CONFIG_KSU_SUSFS_SUS_MOUNT": "y",
        "CONFIG_KSU_SUSFS_SUS_KSTAT": "n",
        "CONFIG_KSU_SUSFS_SUS_MAP": "n",
        "CONFIG_KSU_SUSFS_SPOOF_UNAME": "n",
        "CONFIG_KSU_SUSFS_ENABLE_LOG": "n",
        "CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS": "n",
        "CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG": "n",
        "CONFIG_KSU_SUSFS_OPEN_REDIRECT": "n",
        # Legacy options are explicitly off; SUSFS 2.3 uses KernelSU umount.
        "CONFIG_KSU_SUSFS_TRY_UMOUNT": "n",
        "CONFIG_KSU_SUSFS_SUS_SU": "n",
        "CONFIG_KPM": "n",
        "CONFIG_TMPFS_XATTR": "y",
        "CONFIG_TMPFS_POSIX_ACL": "y",
        "CONFIG_IP_NF_TARGET_TTL": "y",
        "CONFIG_IP6_NF_TARGET_HL": "y",
        "CONFIG_IP6_NF_MATCH_HL": "y",
        "CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE": "y",
        "CONFIG_CC_OPTIMIZE_FOR_SIZE": None,
        # Preserve certified DWARF/BTF defaults: BTF module metadata changes
        # struct module and its transitive vendor symbol CRCs. It is not logging.
    }

    # BBRv3 is the default. Stock BBRv1 stays built in as the fallback.
    # The KABI patch keeps icsk_ca_priv at 104 bytes so vendor modules still load.
    NETWORK_CONFIG = {
        "CONFIG_TCP_CONG_ADVANCED": "y",
        "CONFIG_TCP_CONG_BBR": "y",
        "CONFIG_TCP_CONG_BBR3": "y",
        "CONFIG_DEFAULT_BBR3": "y",
        "CONFIG_DEFAULT_BBR": "n",
        "CONFIG_DEFAULT_CUBIC": "n",
        "CONFIG_DEFAULT_TCP_CONG": '"bbr3"',
        "CONFIG_NET_SCH_FQ": "y",
    }

    # android13-5.15 already builds MGLRU, lazy RCU, menu, and TEO. It has
    # no cpuidle default-governor choice, and menu is rated above TEO, so
    # the built-in command line selects TEO. Bootloader arguments stay
    # appended through CONFIG_CMDLINE_EXTEND.
    #
    # Lazy RCU stays at the certified default. Turning it on requires
    # rcu_nocbs=all, which offloads every CPU, including the boot CPU, while
    # the offload threads are not created until early_initcall. That pair
    # stalled fastboot boot and the bootloader returned to the flashed kernel.
    RUNTIME_CONFIG = {
        "CONFIG_LRU_GEN": "y",
        "CONFIG_LRU_GEN_ENABLED": "y",
        "CONFIG_RCU_EXPERT": "y",
        "CONFIG_RCU_NOCB_CPU": "y",
        "CONFIG_RCU_LAZY": "y",
        "CONFIG_RCU_LAZY_DEFAULT_OFF": "y",
        "CONFIG_CPU_IDLE": "y",
        "CONFIG_CPU_IDLE_GOV_MENU": "y",
        "CONFIG_CPU_IDLE_GOV_TEO": "y",
        "CONFIG_CMDLINE_EXTEND": "y",
        "CONFIG_CMDLINE_FROM_BOOTLOADER": "n",
        "CONFIG_CMDLINE_FORCE": "n",
    }
    CMDLINE_TOKENS = (
        "cpuidle.governor=teo",
    )

    def __init__(self, config: BuildConfig, workspace: str):
        self.config = config
        self.workspace = Path(workspace).resolve()
        self.shell = ShellCommand(cwd=str(self.workspace))
        self.env = os.environ.copy()
        self.work_dir = self.workspace / config.config_name
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.susfs_dir = self.workspace / "susfs4ksu"
        self.sukisu_patch_dir = self.workspace / "SukiSU_patch"
        self.anykernel_dir = self.workspace / "AnyKernel3"
        self.mkbootimg_dir = self.work_dir / "tools/mkbootimg"
        self.applied_bbr_patches = []
        self.applied_tweak_patches = []
        self.applied_integration_patches = []
        self.abi_symbol_count = 0
        self.release_suffix = ""
        self.config_checks = {}
        self.compiler_version = ""
        self._verified_checkouts = {}
        self._setup_env()

    def _setup_env(self):
        self.env["CONFIG"] = self.config.config_name
        self.env.setdefault("GIT_TERMINAL_PROMPT", "0")
        self.shell.env = self.env

    def _run_cmd(self, cmd: str, **kwargs) -> subprocess.CompletedProcess:
        return self.shell.run(cmd, **kwargs)

    def _chdir(self, path: Path):
        os.chdir(path)
        self.shell.cwd = str(path)

    def _preflight(self):
        self.config.validate()
        if (self.workspace / "evidence").exists():
            raise RuntimeError("Use a fresh workspace; previous successful evidence must be preserved")
        if any(self.work_dir.iterdir()):
            raise RuntimeError("Use a fresh workspace; existing kernel sources and outputs are preserved")
        missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
        if missing:
            raise RuntimeError(f"Missing required tools: {', '.join(missing)}")

    def _ensure_git_identity(self):
        def _has(key: str) -> bool:
            result = subprocess.run(["git", "config", "--global", key],
                                    capture_output=True, text=True)
            return result.returncode == 0 and bool(result.stdout.strip())

        if not _has("user.email"):
            self._run_cmd('git config --global user.email "gki-builder@localhost"', check=True)
        if not _has("user.name"):
            self._run_cmd('git config --global user.name "GKI Builder"', check=True)
        self._run_cmd("git config --global --add safe.directory '*'", check=False)

    def _clone_source(self, name, dest, url, commit):
        if dest.exists():
            raise RuntimeError(f"Use a fresh workspace; preserving existing {name}: {dest}")
        subprocess.run(["git", "init", "-q", str(dest)], check=True, env=self.env)
        subprocess.run(["git", "remote", "add", "origin", url], cwd=dest, check=True, env=self.env)
        self._checkout_commit(dest, commit, name)

    def _require_path(self, path: Path, what: str):
        if not path.exists():
            raise RuntimeError(f"{what} not found: {path}")

    def _defconfig_path(self) -> Path:
        return self.work_dir / "common/arch/arm64/configs/gki_defconfig"

    def _defconfig_assignment(self, key):
        prefix = key + "="
        for line in self._defconfig_path().read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(prefix):
                return stripped[len(prefix):]
        return None

    def _upsert_defconfig(self, updates: dict):
        config_file = self._defconfig_path()
        if not config_file.exists():
            raise RuntimeError(f"gki_defconfig not found: {config_file}")

        lines = config_file.read_text(encoding="utf-8").splitlines()
        seen = set()
        new_lines = []
        for line in lines:
            key = None
            stripped = line.strip()
            if stripped.startswith("CONFIG_"):
                key = stripped.split("=", 1)[0]
            elif stripped.startswith("# CONFIG_") and stripped.endswith(" is not set"):
                key = stripped[2:].split(" ", 1)[0]
            if key and key in updates:
                if key in seen:
                    continue
                val = updates[key]
                new_lines.append(f"# {key} is not set" if val is None else f"{key}={val}")
                seen.add(key)
            else:
                new_lines.append(line)
        for key, val in updates.items():
            if key not in seen:
                new_lines.append(f"# {key} is not set" if val is None else f"{key}={val}")
        config_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    def _apply_patch_file(self, patch_path: Path) -> bool:
        """Apply once atomically; missing files or rejected hunks are fatal."""
        if not patch_path.is_file():
            raise RuntimeError(f"Selected patch missing: {patch_path}")
        # git apply validates all hunks before changing any file. A separate
        # --check pass duplicates that work and adds no atomicity guarantee.
        result = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_path.resolve())],
            cwd=self.shell.cwd, capture_output=True, text=True)
        if result.returncode:
            context = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Selected patch failed: {patch_path.name}\n{context}")
        self.applied_integration_patches.append(patch_path.name)
        return True

    def _kernel_image_path(self) -> Path:
        return self.work_dir / "out/android13-5.15/dist/Image"

    def _read_kernel_version(self) -> str:
        makefile = self.work_dir / "common/Makefile"
        if not makefile.exists():
            return "unknown"
        version = patchlevel = sublevel = "?"
        for line in makefile.read_text(encoding="utf-8").splitlines()[:20]:
            if line.startswith("VERSION ="):
                version = line.split("=", 1)[1].strip()
            elif line.startswith("PATCHLEVEL ="):
                patchlevel = line.split("=", 1)[1].strip()
            elif line.startswith("SUBLEVEL ="):
                sublevel = line.split("=", 1)[1].strip()
        return f"{version}.{patchlevel}.{sublevel}"

    def _checkout_commit(self, repo: Path, commit: str, name: str):
        self._chdir(repo)
        fetch = subprocess.run(["git", "fetch", "--depth", "1", "origin", commit],
                               cwd=repo, env=self.env, capture_output=True, text=True)
        if fetch.returncode != 0:
            output = ((fetch.stderr or "") + (fetch.stdout or "")).strip()
            raise RuntimeError(f"Failed to fetch {name} ref {commit}: {output}")
        subprocess.run(["git", "checkout", "FETCH_HEAD"], cwd=repo,
                       env=self.env, check=True)
        actual = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True).stdout.strip()
        if actual != commit:
            raise RuntimeError(f"{name} checkout mismatch: {actual} != {commit}")
        logger.info(f"{name} checked out {actual}")
        self._verified_checkouts[repo] = actual
        self._chdir(self.workspace)

    def clone_repositories(self):
        logger.info("=== Cloning helper repositories ===")
        for name, directory, repo, commit in (
            ("SUSFS", self.susfs_dir, SUSFS_REPO_CONFIG, self.config.susfs_commit),
            ("SukiSU Patch", self.sukisu_patch_dir, SUKISU_PATCH_REPO_CONFIG, self.config.sukisu_patch_commit),
            ("AnyKernel3", self.anykernel_dir, ANYKERNEL_CONFIG, self.config.anykernel_commit),
        ):
            self._clone_source(name, directory, repo["repo_url"], commit)
        logger.info("=== Helper repositories ready ===")

    def configure_boot_tools(self):
        logger.info("=== Configuring manifest-pinned boot packaging tools ===")
        mkbootimg = self.mkbootimg_dir / "mkbootimg.py"
        unpack = self.mkbootimg_dir / "unpack_bootimg.py"
        self._require_path(mkbootimg, "mkbootimg.py")
        self._require_path(unpack, "unpack_bootimg.py")
        self.env["MKBOOTIMG"] = str(mkbootimg)
        self.env["UNPACK_BOOTIMG"] = str(unpack)

        self.shell.env = self.env
        logger.info("=== Toolchain ready ===")

    def setup_repo_tool(self):
        logger.info("=== Installing repo tool ===")
        repo_dir = self.workspace / "git-repo"
        repo_dir.mkdir(exist_ok=True)
        repo_path = repo_dir / "repo"
        # Bootstrap from the same resolved revision that repo init will use.
        # A cached launcher must not silently outlive the per-run source lock.
        launcher_url = (f"{REPO_TOOL_CONFIG['repo_url']}/+/{self.config.repo_commit}/repo?format=TEXT")
        import base64
        import urllib.request
        with urllib.request.urlopen(launcher_url, timeout=30) as response:
            repo_path.write_bytes(base64.b64decode(b"".join(response.read().split()), validate=True))
        repo_path.chmod(repo_path.stat().st_mode | 0o111)
        self.env["REPO"] = str(repo_path)
        self.shell.env = self.env

    def init_and_sync_kernel(self):
        logger.info("=== Initializing and syncing kernel sources ===")
        self._ensure_git_identity()
        self._chdir(self.work_dir)
        branch = self.config.manifest_branch
        if not re.fullmatch(r"common-android13-5\.15-\d{4}-\d{2}", branch):
            raise RuntimeError(f"Unsafe manifest branch: {branch}")
        if not re.fullmatch(r"[0-9a-f]{40}", self.config.manifest_commit):
            raise RuntimeError("Matching manifest must be pinned to a commit")
        self._run_cmd(
            "$REPO init --depth=1 -u https://android.googlesource.com/kernel/manifest "
            f"-b {self.config.manifest_commit} --repo-rev={self.config.repo_commit} --no-clone-bundle", check=True)
        # Freeze the source AND every required tool to the certified build.
        local_manifests = self.work_dir / ".repo/local_manifests"
        local_manifests.mkdir(parents=True, exist_ok=True)
        manifest = ET.Element("manifest")
        projects = {path: (name, revision) for name, path, revision in self.config.source_projects}
        for path in REPO_SYNC_PROJECTS:
            if path not in projects:
                raise RuntimeError(f"Certified manifest missing required project: {path}")
            name, revision = projects[path]
            ET.SubElement(manifest, "extend-project", name=name, path=path, revision=revision)
        (local_manifests / "kernel-revision.xml").write_text(
            ET.tostring(manifest, encoding="unicode") + "\n", encoding="utf-8")
        logger.info("Syncing kernel sources...")
        # One kernel, shallow, and only the projects build.sh actually needs.
        self._run_cmd("$REPO sync -c -j4 --no-tags --fail-fast --no-clone-bundle " +
                      " ".join(REPO_SYNC_PROJECTS), check=True)

        self._require_path(self.work_dir / "common", "kernel common/ directory after repo sync")
        kernel_ver = self._read_kernel_version()
        logger.info(f"Synced kernel version: {kernel_ver}")
        expected = self.config.kernel_version
        if kernel_ver != expected:
            raise RuntimeError(
                f"Synced kernel {kernel_ver} does not match requested {expected} "
                f"(tag {self.config.gki_tag})"
            )
        actual = subprocess.run(["git", "-C", str(self.work_dir / "common"), "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True).stdout.strip()
        if actual != self.config.gki_commit:
            raise RuntimeError(f"GKI checkout mismatch: {actual} != {self.config.gki_commit}")
        self._verified_checkouts[self.work_dir / "common"] = actual
        logger.info("=== Kernel source sync complete ===")
        self.verify_checkouts(include_helpers=False)

    def verify_checkouts(self, include_helpers=True):
        projects = {path: revision for _, path, revision in self.config.source_projects}
        expected = {self.work_dir / path: projects[path] for path in REPO_SYNC_PROJECTS}
        expected[self.work_dir / ".repo/repo"] = self.config.repo_commit
        if include_helpers:
            expected.update({
                self.work_dir / "KernelSU": self.config.sukisu_commit,
                self.susfs_dir: self.config.susfs_commit,
                self.sukisu_patch_dir: self.config.sukisu_patch_commit,
                self.anykernel_dir: self.config.anykernel_commit,
            })
        actual = {}
        for directory, revision in expected.items():
            head = self._verified_checkouts.get(directory)
            if head is None:
                head = subprocess.run(["git", "-C", str(directory), "rev-parse", "HEAD"],
                                      check=True, capture_output=True, text=True).stdout.strip()
                self._verified_checkouts[directory] = head
            if head != revision:
                raise RuntimeError(f"Source checkout mismatch: {directory.name}: {head} != {revision}")
            actual[directory.relative_to(self.workspace).as_posix()] = head
        return actual

    def configure_kernel_toolchain(self):
        """Use the exact AOSP compiler selected by this kernel's build config."""
        constants = self.work_dir / "common/build.config.constants"
        self._require_path(constants, "GKI build.config.constants")
        match = re.search(r"^CLANG_VERSION=(r[0-9a-z]+)$",
                          constants.read_text(encoding="utf-8"), re.M)
        if not match:
            raise RuntimeError("Cannot determine AOSP CLANG_VERSION")
        clang_bin = (self.work_dir / "prebuilts/clang/host/linux-x86" /
                     f"clang-{match[1]}/bin")
        clang = clang_bin / "clang"
        lld = clang_bin / "ld.lld"
        self._require_path(clang, "manifest-selected AOSP Clang")
        self._require_path(lld, "manifest-selected AOSP LLD")
        host_sysroot = self.work_dir / "build/kernel/build-tools/sysroot"
        if not host_sysroot.is_dir():
            raise RuntimeError(f"AOSP host sysroot not found: {host_sysroot}")
        self.env["PATH"] = str(clang_bin) + os.pathsep + self.env.get("PATH", "")
        self.shell.env = self.env
        compiler = subprocess.run([str(clang), "--version"], capture_output=True,
                                  text=True, check=True, env=self.env)
        lines = compiler.stdout.splitlines()
        self.compiler_version = lines[0] if lines else "unavailable"
        logger.info("Preflight compiler: %s", self.compiler_version)

    def add_kernelsu(self):
        logger.info("=== Adding KernelSU ===")
        self._chdir(self.work_dir)
        setup_ref = self.config.sukisu_commit
        script_ref = setup_ref
        raw_base = KSU_REPO_CONFIG["repo_url"].rstrip("/").removesuffix(".git").replace(
            "https://github.com/", "https://raw.githubusercontent.com/", 1
        )
        setup_url = f"{raw_base}/{script_ref}/kernel/setup.sh"
        setup_script = self.work_dir / "sukisu_setup.sh"
        logger.info(f"SukiSU setup.sh from exact commit {script_ref}")
        subprocess.run(["curl", "-fLSs", setup_url, "-o", str(setup_script)],
                       cwd=self.work_dir, env=self.env, check=True)
        subprocess.run(["bash", str(setup_script), setup_ref], cwd=self.work_dir,
                       env=self.env, check=True)
        self._require_path(self.work_dir / "common/drivers/kernelsu", "KernelSU driver symlink")
        self._require_path(self.work_dir / "KernelSU", "KernelSU checkout")
        # Upstream setup.sh reports success even when its requested checkout fails.
        # Verify the actual revision instead of silently building the default branch.
        ksu_dir = self.work_dir / "KernelSU"
        requested = subprocess.run(
            ["git", "-C", str(ksu_dir), "rev-parse", "--verify", f"{setup_ref}^{{commit}}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        actual = subprocess.run(
            ["git", "-C", str(ksu_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if actual != requested:
            raise RuntimeError(f"SukiSU checkout mismatch: requested {requested}, got {actual}")
        self._verified_checkouts[ksu_dir] = actual
        uapi = self._read_ksu_uapi_version()
        logger.info("SukiSU kernel UAPI: %s; the manager must use the same UAPI",
                    uapi if uapi is not None else "unset")

        self._chdir(ksu_dir)
        integration_patch = (Path(__file__).resolve().parents[3] / "patches/susfs/"
                             "0001-sukisu-main-mount-support.patch")
        self._apply_patch_file(integration_patch)
        self._apply_patch_file(REPO_ROOT / "patches/susfs/0003-sukisu-quiet-production.patch")
        self._chdir(self.work_dir)

    def apply_susfs_patches(self):
        logger.info("=== Applying SUSFS patches ===")
        self._chdir(self.work_dir)
        common_dir = self.work_dir / "common"
        susfs_patch = self.susfs_dir / "kernel_patches" / self.config.get_susfs_patch_filename()
        self._require_path(susfs_patch, "SUSFS patch")
        for src, dst in [
            (self.susfs_dir / "kernel_patches/fs", common_dir / "fs/"),
            (self.susfs_dir / "kernel_patches/include/linux", common_dir / "include/linux/"),
        ]:
            self._require_path(src, f"SUSFS source {src}")
            shutil.copytree(src, dst, dirs_exist_ok=True)
        self._chdir(common_dir)
        mount_patch = common_dir / "susfs-mount-only.patch"
        mount_patch.write_text(select_mount_patch(susfs_patch.read_text(encoding="utf-8")),
                               encoding="utf-8", newline="\n")
        self._apply_patch_file(mount_patch)
        reboot_patch = (Path(__file__).resolve().parents[3] / "patches/susfs/"
                        "0002-common-susfs-reboot-dispatch.patch")
        self._apply_patch_file(reboot_patch)
        self._chdir(self.work_dir)

    def apply_zram_patches(self):
        logger.info("=== Applying ZRAM (LZ4KD) patches ===")
        self._chdir(self.work_dir / "common")

        # Ensure the original kernel Kconfig has not been corrupted.
        lib_kconfig = Path("lib/Kconfig")
        if (not lib_kconfig.exists()
                or "config ASSOCIATIVE_ARRAY" not in lib_kconfig.read_text()):
            raise RuntimeError(
                "ZRAM patch preflight failed: "
                "lib/Kconfig is missing ASSOCIATIVE_ARRAY"
            )

        # Copy only the three LZ4KD source components. No LZ4K or module
        # version-check bypass is included in this standard build.
        for src, dst in [
            (
                self.sukisu_patch_dir / "other/zram/lz4k/include/linux/lz4kd.h",
                Path("include/linux/lz4kd.h"),
            ),
            (
                self.sukisu_patch_dir / "other/zram/lz4k/lib/lz4kd",
                Path("lib/lz4kd"),
            ),
            (
                self.sukisu_patch_dir / "other/zram/lz4k/crypto/lz4kd.c",
                Path("crypto/lz4kd.c"),
            ),
        ]:
            if not src.exists():
                raise RuntimeError(f"Required LZ4KD source not found: {src}")
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

        # The reviewed ZRAM slice omits LZ4K and the upstream module CRC
        # bypass. Builtin provider handling only returns the normal EEXIST
        # result for OEM attempts to load zram/zsmalloc a second time.
        for patch in make_patch_plan().zram:
            self._apply_patch_file(patch)
        self._verify_zram_backends()

        # Verify that the patch was applied and the kernel Kconfig survived.
        required_markers = {
            Path("lib/Kconfig"): [
                "config ASSOCIATIVE_ARRAY",
                "config LZ4KD_COMPRESS",
            ],
            Path("lib/Makefile"): [
                "CONFIG_LZ4KD_COMPRESS",
            ],
            Path("crypto/Kconfig"): [
                "config CRYPTO_LZ4KD",
            ],
        }

        for path, markers in required_markers.items():
            content = path.read_text()
            missing = [
                marker
                for marker in markers
                if marker not in content
            ]
            if missing:
                raise RuntimeError(
                    f"LZ4KD patch validation failed for {path}: "
                    f"missing {missing}"
                )

    def apply_feature_patches(self):
        """LZ4, then the KABI-safe BBRv3 series, then the reviewed tweaks."""
        logger.info("=== Applying LZ4, BBRv3 and tweak patches ===")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            raise RuntimeError(f"kernel common/ directory missing: {common_dir}")
        self._chdir(common_dir)
        plan = make_patch_plan()
        shutil.copytree(REPO_ROOT / "patches/lz4/source", common_dir / "crypto/lz4_110",
                        dirs_exist_ok=True)
        for patch in plan.lz4:
            self._apply_patch_file(patch)
        for patch in plan.bbr:
            self._apply_patch_file(patch)
            self.applied_bbr_patches.append(patch.name)
        for patch in plan.tweaks:
            self._apply_patch_file(patch)
            self.applied_tweak_patches.append(patch.name)
        self._chdir(self.work_dir)

    def _verify_zram_backends(self):
        text = (self.work_dir / "common/drivers/block/zram/zcomp.c").read_text(encoding="utf-8")
        listed = re.findall(r'^\t"([a-z0-9-]+)",$', text, re.M)
        if listed != ["lz4", "lz4kd"]:
            raise RuntimeError(f"ZRAM backends must be lz4 then lz4kd, got {listed}")

    def configure_kernel(self):
        logger.info("=== Configuring kernel ===")
        self._chdir(self.work_dir)
        self._require_path(self._defconfig_path(), "gki_defconfig")

        kconfig = self.work_dir / "KernelSU/kernel/Kconfig"
        declared = set(re.findall(r"^config\s+(\w+)", kconfig.read_text(encoding="utf-8"), re.M))
        for key, value in self.KERNEL_CONFIG_UPDATES.items():
            if key.startswith("CONFIG_KSU_SUSFS") and value == "y" and key[7:] not in declared:
                raise RuntimeError(f"Selected SukiSU source does not support {key}")
        updates = dict(self.KERNEL_CONFIG_UPDATES)
        # New upstream SUSFS options default off unless explicitly audited here.
        for symbol in declared:
            if symbol.startswith("KSU_SUSFS_"):
                updates.setdefault(f"CONFIG_{symbol}", "n")
        updates.update(self.NETWORK_CONFIG)
        # FQ paces BBRv3. Keep Reno in the core stack and drop the optional
        # congestion algorithms this phone does not use.
        for symbol in ("BIC", "HTCP", "WESTWOOD", "VEGAS", "VENO",
                       "HYBLA", "ILLINOIS", "DCTCP", "CDG", "NV", "CUBIC"):
            updates[f"CONFIG_TCP_CONG_{symbol.upper()}"] = "n"
        # Preserve upstream cpufreq governors and I/O schedulers. The
        # conservative governor selects common code with frozen
        # cpufreq_dbs_* exports; removing it would break the certified KMI.
        updates.update(self.RUNTIME_CONFIG)
        current = unquote_kconfig_string(self._defconfig_assignment("CONFIG_CMDLINE") or "")
        updates["CONFIG_CMDLINE"] = '"' + ensure_cmdline_tokens(current, self.CMDLINE_TOKENS) + '"'
        self._upsert_defconfig(updates)
        self._configure_zram()

    def _configure_zram(self):
        self._upsert_defconfig({
            "CONFIG_ZRAM": "y",
            "CONFIG_ZSMALLOC": "y",
            "CONFIG_CRYPTO_LZ4": "y",
            "CONFIG_MODVERSIONS": "y",
            "CONFIG_CFI_CLANG": "y",
            "CONFIG_LTO_CLANG_FULL": "y",
            "CONFIG_CRYPTO_LZ4KD": "y",
            "CONFIG_LZ4_COMPRESS": "y",
            "CONFIG_LZ4_DECOMPRESS": "y",
            "CONFIG_LZ4KD_COMPRESS": "y",
            "CONFIG_LZ4KD_DECOMPRESS": "y",
            "CONFIG_ZRAM_WRITEBACK": "y",
            "CONFIG_ZRAM_DEF_COMP_LZ4KD": "y",
            "CONFIG_ZRAM_DEF_COMP_LZ4": "n",
            # Not offered to ZRAM. Leave them unset even if another option
            # later selects the crypto library for a filesystem.
            "CONFIG_CRYPTO_LZO": "n",
            "CONFIG_CRYPTO_LZ4HC": "n",
            "CONFIG_CRYPTO_LZ4K": "n",
            "CONFIG_CRYPTO_ZSTD": "n",
            "CONFIG_CRYPTO_DEFLATE": "n",
            "CONFIG_CRYPTO_842": "n",
            "CONFIG_MODULE_SIG_FORCE": "n",
        })

        # Remove zram and zsmalloc from module lists since they are built into vmlinux
        android_dir = self.work_dir / "common/android"
        if android_dir.exists():
            for mod_list_file in android_dir.glob("*modules*"):
                if not mod_list_file.is_file():
                    continue
                lines = mod_list_file.read_text(encoding="utf-8").splitlines()
                filtered = [l for l in lines if not any(x in l for x in ["zram", "zsmalloc"])]
                if filtered != lines:
                    content = "".join(line + "\n" for line in filtered)
                    mod_list_file.write_text(content, encoding="utf-8", newline="\n")
                    logger.info(f"Removed built-in zram/zsmalloc from {mod_list_file.name}")

    def configure_kernel_name(self):
        # Patches leave the tree dirty, so setlocalversion appends -dirty
        # after -g<commit>. Keep that commit and use the UTC build date.
        self.env["KBUILD_BUILD_USER"] = "gki-builder"
        self.env["KBUILD_BUILD_HOST"] = "github-actions"
        suffix = release_date_suffix(datetime.now(timezone.utc))
        script = self.work_dir / "common/scripts/setlocalversion"
        self._require_path(script, "kernel setlocalversion")
        script.write_text(apply_release_date_suffix(
            script.read_text(encoding="utf-8"), suffix), encoding="utf-8", newline="\n")
        self.release_suffix = suffix
        logger.info("Kernel release suffix: %s", suffix)

    def show_kernel_config(self):
        logger.info("=== Kernel config summary ===")
        self._chdir(self.work_dir)
        config_file = self._defconfig_path()

        if not config_file.exists():
            logger.warning(f"gki_defconfig not found: {config_file}")
            return

        config_lines = [
            line.strip()
            for line in config_file.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("CONFIG_") or line.strip().startswith("# CONFIG_")
        ]

        key_configs = {
            "CONFIG_KSU": "KernelSU",
            "CONFIG_KSU_SUSFS": "SUSFS",
            "CONFIG_KSU_SUSFS_TRY_UMOUNT": "SUSFS try_umount",
            "CONFIG_KSU_SUSFS_SUS_SU": "SUSFS sus_su",
            "CONFIG_TCP_CONG_BBR3": "BBRv3",
            "CONFIG_DEFAULT_TCP_CONG": "Default TCP cong",
            "CONFIG_ZRAM": "ZRAM",
            "CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE": "Optimize for performance",
            "CONFIG_DEBUG_INFO_BTF": "GKI BTF metadata",
            "CONFIG_LRU_GEN_ENABLED": "Multi-gen LRU",
            "CONFIG_RCU_LAZY": "Lazy RCU",
            "CONFIG_RCU_LAZY_DEFAULT_OFF": "Lazy RCU default off",
            "CONFIG_CPU_IDLE_GOV_TEO": "TEO cpuidle governor",
            "CONFIG_CPU_IDLE_GOV_MENU": "Menu cpuidle governor",
        }

        logger.info("Key config status:")
        for key, name in key_configs.items():
            assigned = [c for c in config_lines if c.startswith(key + "=")]
            if assigned:
                logger.info(f" [{assigned[0]}] {name}")
            elif f"# {key} is not set" in config_lines:
                logger.info(f" [not set] {name}")
            else:
                logger.info(f" [missing] {name}")

        if any("ZRAM" in c for c in config_lines):
            zram_configs = [
                c for c in config_lines
                if any(x in c for x in ["ZRAM", "ZSMALLOC", "LZ4KD", "CRYPTO_LZ4"])
            ]
            if zram_configs:
                logger.info("ZRAM-related config:")
                for zc in sorted(set(zram_configs)):
                    logger.info(f" -> {zc}")

        logger.info("-" * 60)

    def generate_and_verify_config(self):
        """Resolve Kconfig in the same certified environment as compilation."""
        self._run_kernel_build(config_only=True)
        out = self.work_dir / "out/android13-5.15/common"
        self._verify_generated_config(out / ".config", stage="precompile")

    def _verify_source_mode(self):
        ipv4 = self.work_dir / "common/net/ipv4"
        algorithm = ipv4 / "tcp_bbr3.c"
        stock = ipv4 / "tcp_bbr.c"
        makefile_path = ipv4 / "Makefile"
        kconfig_path = ipv4 / "Kconfig"
        header_path = self.work_dir / "common/include/net/inet_connection_sock.h"
        required = (algorithm, stock, makefile_path, kconfig_path, header_path)
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise RuntimeError("BBRv3 source check missing: " + ", ".join(missing))
        makefile = makefile_path.read_text(encoding="utf-8")
        kconfig = kconfig_path.read_text(encoding="utf-8")
        header = header_path.read_text(encoding="utf-8")
        bbr3_name = re.search(r'^\s*\.name\s*=\s*"bbr3"\s*,',
                              algorithm.read_text(encoding="utf-8"), re.M)
        bbr1_name = re.search(r'^\s*\.name\s*=\s*"bbr"\s*,',
                              stock.read_text(encoding="utf-8"), re.M)
        if not bbr3_name or not bbr1_name or \
           "CONFIG_TCP_CONG_BBR3" not in makefile or "config TCP_CONG_BBR3" not in kconfig:
            raise RuntimeError("BBRv3 must be added beside stock BBRv1")
        if "icsk_ca_priv[160" in header or "icsk_ca_priv[104" not in header:
            raise RuntimeError("BBRv3 changed icsk_ca_priv; vendor modules would fail to load")

    def _parse_config(self, path: Path, what: str = "generated .config") -> dict:
        self._require_path(path, what)
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("CONFIG_"):
                key, value = line.split("=", 1)
                if key in values:
                    raise RuntimeError(f"Final .config has duplicate assignment: {key}")
                values[key] = value
            elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
                key = line[2:-11]
                if key in values:
                    raise RuntimeError(f"Final .config has duplicate assignment: {key}")
                values[key] = "n"
        return values

    def _verify_generated_config(self, path, stage=None):
        values = self._parse_config(path)
        expected = {
            "CONFIG_NET_SCH_FQ": "y",
            "CONFIG_ZRAM": "y", "CONFIG_ZSMALLOC": "y",
            "CONFIG_CRYPTO_LZ4KD": "y", "CONFIG_LZ4KD_COMPRESS": "y",
            "CONFIG_LZ4KD_DECOMPRESS": "y", "CONFIG_ZRAM_DEF_COMP_LZ4KD": "y",
            "CONFIG_ZRAM_DEF_COMP": '"lz4kd"', "CONFIG_ZRAM_WRITEBACK": "y",
            "CONFIG_TCP_CONG_BBR": "y",
            "CONFIG_TCP_CONG_BBR3": "y",
            "CONFIG_DEFAULT_TCP_CONG": '"bbr3"',
            "CONFIG_KPM": "n",
            "CONFIG_CRYPTO_LZ4": "y",
            "CONFIG_MODVERSIONS": "y", "CONFIG_CFI_CLANG": "y",
            "CONFIG_LTO_CLANG_FULL": "y",
            "CONFIG_DEBUG_INFO": "y", "CONFIG_DEBUG_INFO_BTF": "y",
            "CONFIG_DEBUG_INFO_BTF_MODULES": "y",
        }
        # Assert the requested root/logging/performance profile after Kconfig
        # resolves dependencies, rather than trusting defconfig text alone.
        expected.update({key: value or "n" for key, value in self.KERNEL_CONFIG_UPDATES.items()})
        expected.update(self.NETWORK_CONFIG)
        expected.update(self.RUNTIME_CONFIG)
        expected.update({"CONFIG_LRU_GEN_STATS": "n", "CONFIG_LTO_CLANG_THIN": "n",
                         "CONFIG_CFI_PERMISSIVE": "n", "CONFIG_SECURITY_SELINUX": "y",
                         "CONFIG_DEBUG_INFO_DWARF4": "y", "CONFIG_LZ4_COMPRESS": "y",
                         "CONFIG_LZ4_DECOMPRESS": "y", "CONFIG_MODULE_SIG_FORCE": "n"})
        for symbol in ("BIC", "HTCP", "WESTWOOD", "VEGAS", "VENO", "HYBLA",
                       "ILLINOIS", "DCTCP", "CDG", "NV", "CUBIC"):
            expected[f"CONFIG_TCP_CONG_{symbol}"] = "n"
        for symbol in ("LZO", "LZORLE", "LZ4", "LZ4HC", "ZSTD", "DEFLATE", "842"):
            expected[f"CONFIG_ZRAM_DEF_COMP_{symbol}"] = "n"
        for key, want in expected.items():
            got = values.get(key, "n")
            if got != want:
                raise RuntimeError(f"Final .config mismatch: {key}={got}, expected {want}")
        self._verify_runtime_config(values)
        self._verify_susfs_config(values)
        if stage:
            self.config_checks[stage] = {
                "status": "passed", "sha256": sha256(path), "expected": expected,
                "actual": {key: values.get(key, "n") for key in expected},
                "cmdline": unquote_kconfig_string(values.get("CONFIG_CMDLINE", "")),
            }
            (self.workspace / "CONFIG_REPORT.json").write_text(
                json.dumps(self.config_checks, indent=2) + "\n", encoding="utf-8")
        logger.info("Generated .config verified: BBRv3 default, BBRv1 built in, "
                    "ZRAM lz4kd, KPM off, MGLRU on, lazy RCU off, TEO default")

    def configure_build_fragment(self):
        # Extra root symbols preclude an exact export-set check. Frozen GKI
        # exports are checked against Google's certified symbol CRCs below.
        fragment = self.work_dir / "build.config.custom"
        fragment.write_text("\n".join((
            "POST_DEFCONFIG_CMDS=", "KMI_SYMBOL_LIST_STRICT_MODE=",
            "BUILD_SYSTEM_DLKM=", "BUILD_GKI_ARTIFACTS=",
            "BUILD_GKI_CERTIFICATION_TOOLS=", "TRIM_NONLISTED_KMI=1",
        )) + "\n", encoding="utf-8")
        self.env["GKI_BUILD_CONFIG_FRAGMENT"] = str(fragment)

    def _run_kernel_build(self, config_only=False):
        """Let AOSP select all tools, flags and output paths for both stages."""
        env = self.env.copy()
        env.update(BUILD_CONFIG="common/build.config.gki.aarch64", INSTALL_MOD_STRIP="1")
        if config_only:
            fragment = Path(env["GKI_BUILD_CONFIG_FRAGMENT"])
            self._require_path(fragment, "kernel build config fragment")
            preflight = self.work_dir / "build.config.preflight"
            # build.sh initializes its hermetic toolchain (including pahole),
            # generates DEFCONFIG, then evaluates this supported hook. Exit
            # before compilation without exporting any setup state to Python.
            preflight.write_text(fragment.read_text(encoding="utf-8") +
                                 "\nPOST_DEFCONFIG_CMDS='make -C \"${KERNEL_DIR}\" "
                                 "O=\"${OUT_DIR}\" ${TOOL_ARGS} olddefconfig "
                                 "|| exit $?; exit 0'\n",
                                 encoding="utf-8", newline="\n")
            env["GKI_BUILD_CONFIG_FRAGMENT"] = str(preflight)
        return subprocess.run(["bash", "build/build.sh"], cwd=self.work_dir,
                              env=env, check=config_only)

    def build_kernel(self) -> bool:
        logger.info("=== Starting kernel compile ===")
        self._chdir(self.work_dir)
        try:
            logger.info("Starting kernel compilation with build.sh...")
            result = self._run_kernel_build()

            if result.returncode != 0:
                logger.error(f"Kernel compile failed: {result.stderr if result.stderr else f'exit {result.returncode}'}")
                return False
            image_path = self._kernel_image_path()
            if not image_path.exists():
                logger.error(f"Compile reported success but Image is missing: {image_path}")
                return False
            final_config = (self.work_dir / "out" /
                            "android13-5.15" /
                            "common/.config")
            self._verify_generated_config(final_config, stage="final")
            self._verify_kernel_release()
            dist = image_path.parent
            symvers = dist / "vmlinux.symvers"
            self._require_path(symvers, "built GKI symbol CRCs")
            self.abi_symbol_count = verify_gki_abi(
                self.config.official_build_id, symvers, self.workspace / "GKI-reference.symvers")
            logger.info("Certified GKI ABI verified: %d frozen symbol CRCs", self.abi_symbol_count)
            logger.info(f"=== Kernel compile succeeded: {image_path} ===")
            return True
        except Exception as e:
            logger.error(f"Compile error: {e}")
            return False

    def _verify_kernel_release(self):
        path = (self.work_dir / "out/android13-5.15/common/include/config/kernel.release")
        self._require_path(path, "kernel release string")
        release = path.read_text(encoding="utf-8").strip()
        suffix = f"-{self.release_suffix}"
        short = (self.config.gki_commit or "")[:12]
        if (not self.release_suffix or "-dirty" in release or not release.endswith(suffix)
                or not short or f"-g{short}" not in release):
            raise RuntimeError(
                f"Kernel release must keep GKI commit {short} and end with {suffix}, got {release}")
        logger.info("Kernel release: %s", release)

    def _verify_runtime_config(self, values):
        cmdline = unquote_kconfig_string(values.get("CONFIG_CMDLINE", ""))
        tokens = cmdline.split()
        for token in self.CMDLINE_TOKENS:
            prefix = token.split("=", 1)[0] + "="
            if tokens.count(token) != 1 or any(part.startswith(prefix) and part != token for part in tokens):
                raise RuntimeError(
                    f"Final .config command line must contain {token} once, got: {cmdline}")
        if any(part.startswith(("rcu_nocbs=", "rcutree.enable_rcu_lazy="))
               for part in tokens):
            raise RuntimeError(
                "Final .config mismatch: lazy RCU and callback offload stay "
                "at the certified default")

    def _verify_susfs_config(self, config_or_values):
        if isinstance(config_or_values, dict):
            values = config_or_values
        else:
            values = self._parse_config(config_or_values, "compiled kernel .config")
        for symbol in ("CONFIG_KSU", "CONFIG_KPROBES", "CONFIG_KRETPROBES",
                       "CONFIG_HAVE_SYSCALL_TRACEPOINTS", "CONFIG_KSU_SUSFS", "CONFIG_KSU_SUSFS_SUS_MOUNT"):
            if values.get(symbol) != "y":
                raise RuntimeError(f"Built-in SukiSU requires {symbol}=y in the compiled config")
        enabled = {key for key, val in values.items()
                   if key.startswith("CONFIG_KSU_SUSFS") and val in ("y", "m")}
        expected = {key for key, value in self.KERNEL_CONFIG_UPDATES.items()
                    if key.startswith("CONFIG_KSU_SUSFS") and value == "y"}
        if enabled != expected:
            raise RuntimeError(f"SUSFS profile mismatch: expected {sorted(expected)}, got {sorted(enabled)}")

    def prepare_boot_images(self) -> list:
        logger.info("=== Preparing boot image ===")
        self.configure_boot_tools()
        self._chdir(self.work_dir)
        bootimgs_dir = self.work_dir / "bootimgs"
        bootimgs_dir.mkdir(exist_ok=True)

        image_src = self._kernel_image_path()
        self._require_path(image_src, "compiled kernel Image")
        dest = self.workspace / "boot.img"
        if self.config.base_boot:
            repack_boot(self.config.base_boot, image_src, dest, self.mkbootimg_dir)
        else:
            # Generic GKI v4 container. No invented partition size, AVB key,
            # OS patch level, cmdline, or device ramdisk.
            subprocess.run([sys.executable, str(self.mkbootimg_dir / "mkbootimg.py"),
                            "--header_version", "4", "--kernel", str(image_src),
                            "--output", str(dest)], check=True, env=self.env)
        shutil.copy2(image_src, self.workspace / "Image")
        return [str(dest)]

    def create_anykernel_zips(self) -> list:
        logger.info("=== Creating AnyKernel3 zip ===")
        self._chdir(self.work_dir)
        ak3_dir = self.anykernel_dir

        image_src = self._kernel_image_path()
        self._require_path(image_src, "compiled kernel Image")
        self._run_cmd(f"cp {image_src} {ak3_dir}/Image", check=True)

        zip_path = self.workspace / "AnyKernel3.zip"
        self._chdir(ak3_dir)
        self._run_cmd(f"zip -qr '{zip_path}' . -x '.git' -x '.git/*' -x '*/.git/*'", check=True)
        self._run_cmd(f"rm -f {ak3_dir}/Image", check=False)
        self._chdir(self.work_dir)
        if not zip_path.exists():
            raise RuntimeError(f"AnyKernel3 zip was not created: {zip_path}")
        return [str(zip_path)]

    def _read_ksu_uapi_version(self) -> Optional[int]:
        # builtin uses DECLARE() in kernel/include; main uses a C constant in uapi/.
        for relative in ("kernel/include/uapi/supercall.h", "uapi/supercall.h"):
            header = self.work_dir / "KernelSU" / relative
            if not header.is_file():
                continue
            match = re.search(
                r"\bKERNEL_SU_UAPI_VERSION\s*(?:,|=)\s*(\d+)\b",
                header.read_text(encoding="utf-8"),
            )
            if match:
                return int(match.group(1))
        return None

    def _git_head(self, repo: Path) -> str:
        if not repo.exists():
            return "unknown"
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        if sha.returncode != 0 or not sha.stdout.strip():
            return "unknown"
        branch = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        ref = (branch.stdout or "").strip()
        commit = sha.stdout.strip()
        if ref and ref != "HEAD":
            return f"{commit} ({ref})"
        return commit

    def write_build_info(self, artifacts: list = None, build_time: float = None,
                         success: bool = True, message: str = "") -> Path:
        image = self._kernel_image_path()
        image_sha = "unavailable"
        if image.is_file():
            import hashlib
            image_sha = hashlib.sha256(image.read_bytes()).hexdigest()
        compiler_version = self.compiler_version
        if not compiler_version:
            try:
                compiler = subprocess.run(["clang", "--version"], capture_output=True,
                                          text=True, env=self.env)
                compiler_lines = compiler.stdout.splitlines()
                compiler_version = compiler_lines[0] if compiler.returncode == 0 and compiler_lines else "unavailable"
            except OSError:
                compiler_version = "unavailable"
        lines = [
            f"## GKI-derived kernel {self.config.kernel_version}",
            "",
            f"- Status: {'success' if success else 'failed'}",
            f"- Message: {message or ('Build succeeded' if success else 'Build failed')}",
            f"- Build timestamp (UTC): {datetime.now(timezone.utc).isoformat()}",
            f"- Android family: {ANDROID_FAMILY}",
            f"- GKI tag: `{self.config.gki_tag}`",
            f"- GKI exact commit: `{self.config.gki_commit}`",
            f"- Certified GKI build ID: `{self.config.official_build_id}`",
            f"- Kernel version: {self.config.kernel_version}",
            f"- Kernel release suffix: {self.release_suffix or 'not set'}",
            f"- Manifest branch: `{self.config.manifest_branch}`",
            f"- Manifest exact commit: `{self.config.manifest_commit}`",
            f"- Repo tool exact commit: `{self.config.repo_commit}`",
            f"- Kernel source: `{self._git_head(self.work_dir / 'common')}`",
            f"- SukiSU channel: {self.config.sukisu_channel}",
            f"- SukiSU tag: {self.config.sukisu_tag or 'none (upstream HEAD)'}",
            f"- SukiSU exact commit: `{self.config.sukisu_commit}`",
            f"- SukiSU checkout: `{self._git_head(self.work_dir / 'KernelSU')}`",
            f"- SukiSU kernel UAPI: {self._read_ksu_uapi_version()}",
            f"- SUSFS exact commit: `{self.config.susfs_commit}`",
            f"- LZ4KD helper source commit: `{self.config.sukisu_patch_commit}`",
            f"- AnyKernel3 exact commit: `{self.config.anykernel_commit}`",
            "- TCP: BBRv3 default, stock BBRv1 built in, other congestion algorithms off",
            "- Multi-gen LRU: enabled by default; statistics off",
            "- RCU: certified lazy-off default; no CPU is callback-offloaded",
            "- CPU idle: TEO default, menu governor built in",
            f"- Applied BBR patches: {', '.join(self.applied_bbr_patches) or 'none'}",
            f"- Applied tweak patches: {', '.join(self.applied_tweak_patches) or 'none'}",
            f"- Other integration patches: {', '.join(self.applied_integration_patches)}",
            "- ZRAM: lz4kd default, lz4 available, other backends removed",
            "- ZRAM crypto LZ4: official 1.10.0, private freestanding implementation",
            "- Exported GKI LZ4 library/header: unchanged",
            "- KPM: disabled",
            "- cpufreq governors and I/O schedulers: certified GKI defaults preserved",
            f"- Certified GKI symbol CRCs matched: {self.abi_symbol_count}",
            f"- Boot packaging: {'device base image repacked' if self.config.base_boot else 'generic ramdisk-less GKI header v4; device ramdisk must be in init_boot'}",
            f"- Compiler: {compiler_version}",
            f"- Configuration checks: {', '.join(self.config_checks) or 'not completed'}",
            f"- Kernel Image SHA256: `{image_sha}`",
            "- SUSFS profile: mount-only (core + SUS_MOUNT)",
        ]
        if build_time is not None:
            lines.append(f"- Build time: {build_time:.2f}s")
        if artifacts:
            lines.append("")
            lines.append("### Artifacts")
            for artifact in artifacts:
                lines.append(f"- `{Path(artifact).name}`")
        content = "\n".join(lines) + "\n"
        info_path = self.workspace / "BUILD_INFO.md"
        info_path.write_text(content, encoding="utf-8")
        logger.info(f"Wrote build info: {info_path}")
        return info_path

    def build(self, preflight_only: bool = False) -> BuildResult:
        import time
        start_time = time.time()
        logger.info("=" * 50)
        logger.info(f"Starting GKI kernel build - {self.config.config_name}")
        logger.info("=" * 50)
        try:
            self._preflight()
            write_sources(self.config, self.workspace)
            (self.workspace / "artifact_stem.txt").write_text(self.config.artifact_stem + "\n", encoding="utf-8")
            self.clone_repositories()
            self.setup_repo_tool()
            self.init_and_sync_kernel()
            self.configure_kernel_toolchain()
            self.add_kernelsu()
            self.apply_susfs_patches()
            self.apply_feature_patches()
            self.apply_zram_patches()
            self.configure_kernel()
            self.configure_kernel_name()
            self.show_kernel_config()
            self.configure_build_fragment()
            self._verify_source_mode()
            (self.workspace / "checkouts.json").write_text(
                json.dumps(self.verify_checkouts(), indent=2) + "\n", encoding="utf-8")
            logger.info("Patch report: GKI %s %s; SukiSU %s; SUSFS %s; BBR %s; tweaks %s; integration count=%d",
                        self.config.kernel_version, self.config.gki_commit, self.config.sukisu_commit,
                        self.config.susfs_commit, self.applied_bbr_patches, self.applied_tweak_patches,
                        len(self.applied_integration_patches))
            self.generate_and_verify_config()
            if preflight_only:
                info = self.write_build_info(success=True, message="Source and generated config preflight passed")
                return BuildResult(success=True, config=self.config, message="Preflight passed",
                                   artifacts=[str(info)], build_time=time.time() - start_time)
            if not self.build_kernel():
                build_time = time.time() - start_time
                self.write_build_info(build_time=build_time, success=False, message="Kernel compile failed")
                return BuildResult(success=False, config=self.config, message="Kernel compile failed", build_time=build_time)
            artifacts = []
            artifacts.extend(self.create_anykernel_zips())
            artifacts.extend(self.prepare_boot_images())
            build_time = time.time() - start_time
            self.write_build_info(artifacts=artifacts, build_time=build_time, success=True)
            evidence = collect_evidence(self.workspace, self.config.config_name, self.abi_symbol_count)
            artifacts.append(str(evidence))
            logger.info(f"Build succeeded in {build_time:.2f}s, {len(artifacts)} artifact(s)")
            return BuildResult(success=True, config=self.config, message="Build succeeded", artifacts=artifacts, build_time=build_time)
        except Exception as e:
            logger.exception(f"Build failed: {e}")
            try:
                self.write_build_info(build_time=time.time() - start_time, success=False, message=str(e))
            except Exception:
                pass
            return BuildResult(success=False, config=self.config, message=str(e), build_time=time.time() - start_time)
