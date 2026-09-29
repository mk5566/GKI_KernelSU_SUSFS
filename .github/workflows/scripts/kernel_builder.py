import os
import shutil
import subprocess
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from config import (BuildConfig, KSU_REPO_CONFIG, SUSFS_REPO_CONFIG, SUKISU_PATCH_REPO_CONFIG,
                   ANYKERNEL_CONFIG, SUPPORTED_SUKISU_UAPI,
                   SUSFS_REVISION, SUKISU_PATCH_REVISION,
                   GOOGLE_BBR_V3_COMMIT, REPO_ROOT)
from susfs_integration import select_mount_patch
from patch_plan import make_patch_plan

logger = logging.getLogger(__name__)

REQUIRED_TOOLS = ("git", "curl", "python3", "make", "bash", "zip", "openssl")

# Keep the GitHub-hosted runner's checkout within its available disk. These
# projects provide build.sh, its hermetic tools, the pinned compiler/NDK, and
# optional boot-image packaging. Bazel, JDK and virtual-device trees are unused.
REPO_SYNC_PROJECTS = (
    "common", "build/kernel", "kernel/configs",
    "prebuilts/clang/host/linux-x86", "prebuilts/build-tools",
    "prebuilts/kernel-build-tools", "prebuilts/ndk-r23", "tools/mkbootimg",
)


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
        # Strip debug symbols to speed up build/link time and avoid disk bloat
        "CONFIG_DEBUG_INFO": "n",
        "CONFIG_DEBUG_INFO_NONE": "y",
        "CONFIG_DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT": "n",
        "CONFIG_DEBUG_INFO_DWARF4": "n",
        "CONFIG_DEBUG_INFO_DWARF5": "n",
    }

    BBR3_CONFIG_UPDATES = {
        "CONFIG_TCP_CONG_ADVANCED": "y",
        "CONFIG_TCP_CONG_BBR": "n",
        "CONFIG_TCP_CONG_BBR3": "y",
        "CONFIG_DEFAULT_BBR3": "y",
        "CONFIG_DEFAULT_BBR": "n",
        "CONFIG_DEFAULT_CUBIC": "n",
        "CONFIG_DEFAULT_TCP_CONG": '"bbr3"',
        "CONFIG_NET_SCH_FQ": "y",
    }

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
        self.toolchain_dir = self.work_dir / "prebuilts/kernel-build-tools"
        self.mkbootimg_dir = self.work_dir / "tools/mkbootimg"
        self.applied_bbr_patches = []
        self.applied_tweak_patches = []
        self.applied_integration_patches = []
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

    def _clone_or_update(self, name: str, dest: Path, url: str, branch: Optional[str] = None):
        git_dir = dest / ".git"
        if dest.exists() and not git_dir.exists() and not git_dir.is_file():
            raise RuntimeError(f"{name} path exists but is not a Git checkout: {dest}")
        if not dest.exists():
            cmd = ["git", "clone", "--depth", "1"]
            if branch:
                cmd.extend(["-b", branch])
            cmd.extend([url, str(dest)])
            logger.info(f"Cloning {name}...")
            subprocess.run(cmd, cwd=self.workspace, env=self.env, check=True)
            if not dest.exists():
                raise RuntimeError(f"Failed to clone {name} from {url}")
            return
        logger.info(f"{name} already present at {dest} (HEAD {self._git_head(dest)})")
        fetch_ref = branch or "HEAD"
        fetch = subprocess.run(["git", "-C", str(dest), "fetch", "--depth", "1",
                                "origin", fetch_ref], cwd=self.workspace, env=self.env,
                               capture_output=True, text=True)
        if fetch.returncode != 0:
            output = ((fetch.stderr or "") + (fetch.stdout or "")).strip()
            raise RuntimeError(f"{name} fetch of {fetch_ref} failed: {output}")
        subprocess.run(["git", "-C", str(dest), "checkout", "-f", "FETCH_HEAD"],
                       cwd=self.workspace, env=self.env, check=True)
        logger.info(f"{name} updated to {self._git_head(dest)}")

    def _require_path(self, path: Path, what: str):
        if not path.exists():
            raise RuntimeError(f"{what} not found: {path}")

    def _defconfig_path(self) -> Path:
        return self.work_dir / "common/arch/arm64/configs/gki_defconfig"

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
        """Validate and apply once, exactly, in the current Git checkout."""
        if not patch_path.is_file():
            raise RuntimeError(f"Selected patch missing: {patch_path}")
        for extra in (("--check",), ()):
            result = subprocess.run(["git", "apply", *extra, str(patch_path.resolve())],
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
        self._chdir(self.workspace)

    def _apply_susfs_commit(self):
        self._checkout_commit(self.susfs_dir, SUSFS_REVISION, "SUSFS")

    def clone_repositories(self):
        logger.info("=== Cloning helper repositories ===")
        self._clone_or_update("SUSFS", self.susfs_dir, SUSFS_REPO_CONFIG["repo_url"], self.config.kernel_branch)
        self._clone_or_update("SukiSU Patch", self.sukisu_patch_dir, SUKISU_PATCH_REPO_CONFIG["repo_url"])
        self._checkout_commit(self.sukisu_patch_dir, SUKISU_PATCH_REVISION, "SukiSU Patch")
        self._clone_or_update("AnyKernel3", self.anykernel_dir, ANYKERNEL_CONFIG["repo_url"], ANYKERNEL_CONFIG["branch"])
        self._apply_susfs_commit()
        logger.info("=== Helper repositories ready ===")

    def configure_boot_tools(self):
        logger.info("=== Configuring manifest-pinned boot packaging tools ===")
        avbtool = self.toolchain_dir / "linux-x86/bin/avbtool"
        mkbootimg = self.mkbootimg_dir / "mkbootimg.py"
        unpack = self.mkbootimg_dir / "unpack_bootimg.py"
        self._require_path(avbtool, "avbtool")
        self._require_path(mkbootimg, "mkbootimg.py")
        self.env["AVBTOOL"] = str(avbtool)
        self.env["MKBOOTIMG"] = str(mkbootimg)
        self.env["UNPACK_BOOTIMG"] = str(unpack)

        key_path = Path(os.environ.get("BOOT_SIGN_KEY_PATH", self.workspace / "boot_avb_testkey.pem"))
        if not key_path.exists():
            subprocess.run(["openssl", "genrsa", "-out", str(key_path), "2048"],
                           cwd=self.workspace, env=self.env, check=True)
        self.env["BOOT_SIGN_KEY_PATH"] = str(key_path)
        self.shell.env = self.env
        logger.info("=== Toolchain ready ===")

    def setup_repo_tool(self):
        logger.info("=== Installing repo tool ===")
        repo_dir = self.workspace / "git-repo"
        repo_dir.mkdir(exist_ok=True)
        repo_path = repo_dir / "repo"
        if not repo_path.exists():
            subprocess.run(["curl", "-fLSs", "https://storage.googleapis.com/git-repo-downloads/repo",
                            "-o", str(repo_path)], cwd=self.workspace, env=self.env, check=True)
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
        self._run_cmd(
            f"$REPO init --depth=1 -u https://android.googlesource.com/kernel/manifest "
            f"-b {branch} --repo-rev=v2.16 --no-clone-bundle", check=True)
        # Use the manifest's tools, but freeze kernel/common to the peeled tag.
        local_manifests = self.work_dir / ".repo/local_manifests"
        local_manifests.mkdir(parents=True, exist_ok=True)
        (local_manifests / "kernel-revision.xml").write_text(
            '<manifest><extend-project name="kernel/common" path="common" '
            f'revision="{self.config.gki_commit}" /></manifest>\n', encoding="utf-8")
        logger.info("Syncing kernel sources...")
        self._run_cmd("$REPO sync -j2 --no-tags --fail-fast --no-clone-bundle " +
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
        logger.info("=== Kernel source sync complete ===")

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
        self.env["PATH"] = str(clang_bin) + os.pathsep + self.env.get("PATH", "")
        self.shell.env = self.env
        compiler = subprocess.run([str(clang), "--version"], capture_output=True,
                                  text=True, check=True, env=self.env)
        logger.info("Preflight compiler: %s", compiler.stdout.splitlines()[0])

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
        uapi = self._read_ksu_uapi_version()
        if uapi not in SUPPORTED_SUKISU_UAPI:
            raise RuntimeError(
                f"SukiSU UAPI {uapi} is not audited for this mount-only integration; "
                f"supported: {sorted(SUPPORTED_SUKISU_UAPI)}"
            )
        logger.info(f"SukiSU kernel UAPI: {uapi}; the manager must use the same UAPI")

        self._chdir(ksu_dir)
        integration_patch = (Path(__file__).resolve().parents[3] / "patches/susfs/"
                             "0001-sukisu-main-uapi4-mount-support.patch")
        self._apply_patch_file(integration_patch)
        self._chdir(self.work_dir)

    def apply_susfs_patches(self):
        logger.info("=== Applying SUSFS patches ===")
        self._chdir(self.work_dir)
        common_dir = self.work_dir / "common"
        susfs_patch = self.susfs_dir / "kernel_patches" / self.config.get_susfs_patch_filename()
        self._require_path(susfs_patch, "SUSFS patch")
        shutil.copy2(susfs_patch, common_dir / susfs_patch.name)
        for src, dst in [
            (self.susfs_dir / "kernel_patches/fs", common_dir / "fs/"),
            (self.susfs_dir / "kernel_patches/include/linux", common_dir / "include/linux/"),
        ]:
            self._require_path(src, f"SUSFS source {src}")
            shutil.copytree(src, dst, dirs_exist_ok=True)
        patch_file = common_dir / self.config.get_susfs_patch_filename()
        self._chdir(common_dir)
        context_patch = REPO_ROOT / "patches/susfs/5.15-context.patch"
        self._apply_patch_file(context_patch)
        mount_patch = common_dir / "susfs-mount-only.patch"
        mount_patch.write_text(select_mount_patch(patch_file.read_text(encoding="utf-8")),
                               encoding="utf-8")
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
        # compatibility bypass is included in this standard build.
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

        # Apply only LZ4KD. Do not apply lz4k_oplus.patch.
        # The upstream helper's combined patch also changes kernel/module.c.
        # This reviewed local slice contains only LZ4KD integration.
        lz4kd_patch = REPO_ROOT / "patches/zram/lz4kd-integration.patch"

        if not lz4kd_patch.exists():
            raise RuntimeError(f"Required ZRAM patch not found: {lz4kd_patch}")

        self._apply_patch_file(lz4kd_patch)

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

    def apply_vendor_patches(self):
        logger.info("=== Applying selected BBR and tweak patches ===")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            raise RuntimeError(f"kernel common/ directory missing: {common_dir}")
        self._chdir(common_dir)
        plan = make_patch_plan(self.config)
        for patch in plan.bbr:
            self._apply_patch_file(patch)
            self.applied_bbr_patches.append(patch.name)
        for patch in plan.tweaks:
            self._apply_patch_file(patch)
            self.applied_tweak_patches.append(patch.name)
        self._chdir(self.work_dir)

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
        if self.config.bbr_version == "v3":
            updates.update(self.BBR3_CONFIG_UPDATES)
        else:
            updates.update({
                "CONFIG_TCP_CONG_ADVANCED": "y",
                "CONFIG_TCP_CONG_BBR": "y",
                "CONFIG_TCP_CONG_BBR3": "n",
                "CONFIG_DEFAULT_BBR": "y",
                "CONFIG_DEFAULT_BBR3": "n",
                "CONFIG_DEFAULT_CUBIC": "n",
                "CONFIG_DEFAULT_TCP_CONG": '"bbr"',
                "CONFIG_NET_SCH_FQ": "y",
            })
        # The selected BBR uses FQ pacing. Keep the core TCP and Android
        # networking stack; remove only optional congestion algorithms.
        for symbol in ("BIC", "HTCP", "WESTWOOD", "VEGAS", "VENO", "Hybla",
                       "HYBLA", "ILLINOIS", "DCTCP", "CDG", "NV", "CUBIC"):
            updates[f"CONFIG_TCP_CONG_{symbol.upper()}"] = "n"
        # Generic arm64 GKI: retain none and mq-deadline, schedutil and
        # performance. These optional alternatives are not selected here.
        updates.update({
            "CONFIG_MQ_IOSCHED_DEADLINE": "y",
            "CONFIG_IOSCHED_BFQ": "n",
            "CONFIG_MQ_IOSCHED_KYBER": "n",
            "CONFIG_CPU_FREQ_GOV_SCHEDUTIL": "y",
            "CONFIG_CPU_FREQ_GOV_PERFORMANCE": "y",
            "CONFIG_CPU_FREQ_GOV_POWERSAVE": "n",
            "CONFIG_CPU_FREQ_GOV_CONSERVATIVE": "n",
            "CONFIG_CPU_FREQ_GOV_ONDEMAND": "n",
            "CONFIG_CPU_FREQ_GOV_USERSPACE": "n",
        })
        self._upsert_defconfig(updates)
        self._configure_zram()

    def _configure_zram(self):
        self._upsert_defconfig({
            "CONFIG_ZRAM": "y",
            "CONFIG_ZSMALLOC": "y",
            "CONFIG_CRYPTO_LZ4": "y",
            "CONFIG_CRYPTO_LZ4KD": "y",
            "CONFIG_LZ4KD_COMPRESS": "y",
            "CONFIG_LZ4KD_DECOMPRESS": "y",
            "CONFIG_ZRAM_WRITEBACK": "y",
            "CONFIG_ZRAM_DEF_COMP_LZ4KD": "y",
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
                    mod_list_file.write_text("\n".join(filtered) + "\n", encoding="utf-8")
                    logger.info(f"Removed built-in zram/zsmalloc from {mod_list_file.name}")

    def configure_kernel_name(self):
        logger.info("=== Configuring kernel name ===")
        self._chdir(self.work_dir)
        safe_custom_version = ""

        setlocalversion = self.work_dir / "common/scripts/setlocalversion"
        if setlocalversion.exists():
            content = setlocalversion.read_text(encoding="utf-8")
            content = content.replace("-dirty", "")
            if safe_custom_version:
                lines = content.split('\n')
                for i, line in enumerate(lines):
                    if 'echo "$res"' in line and not line.strip().startswith('#'):
                        lines[i] = f'\techo "{safe_custom_version}$res"'
                        break
                content = '\n'.join(lines)
            setlocalversion.write_text(content, encoding="utf-8")

        current_time = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S UTC %Y")
        mkcompile_h = self.work_dir / "common/scripts/mkcompile_h"
        if mkcompile_h.exists():
            content = mkcompile_h.read_text(encoding="utf-8")
            content = content.replace(
                'UTS_VERSION="$(echo $UTS_VERSION $CONFIG_FLAGS $TIMESTAMP | cut -b -$UTS_LEN)"',
                f'UTS_VERSION="#1 SMP PREEMPT {current_time}"',
            )
            mkcompile_h.write_text(content, encoding="utf-8")

        if safe_custom_version:
            self._upsert_defconfig({"CONFIG_LOCALVERSION": f'"{safe_custom_version}"'})

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
            "CONFIG_DEBUG_INFO_NONE": "Debug info disabled",
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
        """Kconfig resolves dependencies; verify its output before compilation."""
        common = self.work_dir / "common"
        out = self.work_dir / "out/android13-5.15/common"
        out.mkdir(parents=True, exist_ok=True)
        subprocess.run(["make", "-C", str(common), f"O={out}", "ARCH=arm64",
                        "LLVM=1", "gki_defconfig"], check=True, env=self.env)
        subprocess.run(["make", "-C", str(common), f"O={out}", "ARCH=arm64",
                        "LLVM=1", "olddefconfig"], check=True, env=self.env)
        self._verify_source_mode()
        self._verify_generated_config(out / ".config")

    def _verify_source_mode(self):
        ipv4 = self.work_dir / "common/net/ipv4"
        algorithm = ipv4 / "tcp_bbr3.c"
        plb = ipv4 / "tcp_plb.c"
        if self.config.bbr_version == "v1":
            if self.applied_bbr_patches or algorithm.exists():
                raise RuntimeError("BBRv1 source contains the BBRv3 backport")
            return
        makefile = (ipv4 / "Makefile").read_text(encoding="utf-8")
        kconfig = (ipv4 / "Kconfig").read_text(encoding="utf-8")
        if not algorithm.is_file() or not plb.is_file() or \
           "tcp_plb.o" not in makefile or "CONFIG_TCP_CONG_BBR3" not in makefile or \
           "config TCP_CONG_BBR3" not in kconfig or \
           '.name\t\t= "bbr3"' not in algorithm.read_text(encoding="utf-8"):
            raise RuntimeError("Incomplete BBRv3 source/PLB integration")

    def _verify_generated_config(self, path):
        self._require_path(path, "generated .config")
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("CONFIG_"):
                key, value = line.split("=", 1)
                values[key] = value
            elif line.startswith("# CONFIG_") and line.endswith(" is not set"):
                values[line[2:-11]] = "n"
        expected = {
            "CONFIG_KSU": "y", "CONFIG_KSU_SUSFS": "y",
            "CONFIG_KSU_SUSFS_SUS_MOUNT": "y", "CONFIG_NET_SCH_FQ": "y",
            "CONFIG_ZRAM": "y", "CONFIG_ZSMALLOC": "y",
            "CONFIG_CRYPTO_LZ4KD": "y", "CONFIG_LZ4KD_COMPRESS": "y",
            "CONFIG_LZ4KD_DECOMPRESS": "y", "CONFIG_ZRAM_DEF_COMP_LZ4KD": "y",
            "CONFIG_ZRAM_DEF_COMP": '"lz4kd"', "CONFIG_ZRAM_WRITEBACK": "y",
            "CONFIG_MQ_IOSCHED_DEADLINE": "y", "CONFIG_IOSCHED_BFQ": "n",
            "CONFIG_MQ_IOSCHED_KYBER": "n",
            "CONFIG_CPU_FREQ_GOV_SCHEDUTIL": "y",
            "CONFIG_CPU_FREQ_GOV_PERFORMANCE": "y",
            "CONFIG_CPU_FREQ_GOV_POWERSAVE": "n",
            "CONFIG_CPU_FREQ_GOV_CONSERVATIVE": "n",
            "CONFIG_CPU_FREQ_GOV_ONDEMAND": "n",
            "CONFIG_CPU_FREQ_GOV_USERSPACE": "n",
            "CONFIG_TCP_CONG_BBR": "y" if self.config.bbr_version == "v1" else "n",
            "CONFIG_TCP_CONG_BBR3": "y" if self.config.bbr_version == "v3" else "n",
            "CONFIG_DEFAULT_TCP_CONG": '"bbr"' if self.config.bbr_version == "v1" else '"bbr3"',
        }
        for symbol in ("BIC", "HTCP", "WESTWOOD", "VEGAS", "VENO", "HYBLA",
                       "ILLINOIS", "DCTCP", "CDG", "NV", "CUBIC"):
            expected[f"CONFIG_TCP_CONG_{symbol}"] = "n"
        for symbol in ("LZO", "LZORLE", "LZ4", "LZ4HC", "ZSTD", "DEFLATE", "842"):
            expected[f"CONFIG_ZRAM_DEF_COMP_{symbol}"] = "n"
        for key, want in expected.items():
            got = values.get(key, "n")
            if got != want:
                raise RuntimeError(f"Final .config mismatch: {key}={got}, expected {want}")
        self._verify_susfs_config(path)
        logger.info("Generated .config verified for BBR%s, ZRAM/LZ4KD, TCP, I/O and CPU choices",
                    self.config.bbr_version)

    def _disable_kmi_enforcement(self):
        # Intentional custom GKI-derived build: no frozen KMI/ABI enforcement.
        # This is not a claim of vendor module ABI compatibility.
        # 1. Neutralize build.config.gki
        build_config_gki = self.work_dir / "common/build.config.gki"
        if build_config_gki.exists():
            content = build_config_gki.read_text(encoding="utf-8")
            content = content.replace('POST_DEFCONFIG_CMDS="check_defconfig"', 'POST_DEFCONFIG_CMDS=""')
            content = content.replace("check_defconfig", "")
            build_config_gki.write_text(content, encoding="utf-8")

        # 2. Neutralize build.config.aarch64
        build_config_aarch64 = self.work_dir / "common/build.config.aarch64"
        if build_config_aarch64.exists():
            content = build_config_aarch64.read_text(encoding="utf-8")
            content = content.replace("GKI_MODULES_LIST=android/gki_aarch64_modules", "GKI_MODULES_LIST=")
            build_config_aarch64.write_text(content, encoding="utf-8")

        # 3. Neutralize build.config.gki.aarch64
        build_config = self.work_dir / "common/build.config.gki.aarch64"
        if build_config.exists():
            content = build_config.read_text(encoding="utf-8")
            content = content.replace("BUILD_SYSTEM_DLKM=1", "BUILD_SYSTEM_DLKM=0")
            content = content.replace("BUILD_GKI_ARTIFACTS=1", "BUILD_GKI_ARTIFACTS=0")
            content = content.replace("BUILD_GKI_CERTIFICATION_TOOLS=1", "BUILD_GKI_CERTIFICATION_TOOLS=0")
            lines = [l for l in content.split('\n') if not any(k in l for k in [
                'MODULES_ORDER=', 'MODULES_LIST=', 'KMI_SYMBOL_LIST_STRICT_MODE'
            ])]
            extra_flags = [
                "TRIM_NONLISTED_KMI=0",
                "KMI_SYMBOL_LIST_STRICT_MODE=0",
                "KMI_SYMBOL_LIST_ADD_ONLY=0",
                "KMI_ENFORCED=0",
                "BUILD_SYSTEM_DLKM=0",
                "BUILD_GKI_ARTIFACTS=0",
                "BUILD_GKI_CERTIFICATION_TOOLS=0",
                "MODULES_LIST=",
                "MODULES_ORDER=",
                "GKI_MODULES_LIST=",
                "ABI_DEFINITION=",
                "KMI_SYMBOL_LIST=",
                "ADDITIONAL_KMI_SYMBOL_LISTS=",
                "POST_DEFCONFIG_CMDS=",
            ]
            content = '\n'.join(lines) + '\n' + '\n'.join(extra_flags) + '\n'
            build_config.write_text(content, encoding="utf-8")

    def build_kernel(self) -> bool:
        logger.info("=== Starting kernel compile ===")
        self._chdir(self.work_dir)
        try:
            logger.info("Starting kernel compilation with build.sh...")
            build_cmd = (
                "LTO=thin "
                "BUILD_SYSTEM_DLKM=0 "
                "BUILD_GKI_ARTIFACTS=0 "
                "BUILD_GKI_CERTIFICATION_TOOLS=0 "
                "TRIM_NONLISTED_KMI=0 "
                "KMI_ENFORCED=0 "
                "INSTALL_MOD_STRIP=1 "
                "POST_DEFCONFIG_CMDS=\"\" "
                "BUILD_CONFIG=common/build.config.gki.aarch64 "
                "build/build.sh"
            )
            result = self._run_cmd(build_cmd, check=False)

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
            self._verify_generated_config(final_config)
            logger.info(f"=== Kernel compile succeeded: {image_path} ===")
            return True
        except Exception as e:
            logger.error(f"Compile error: {e}")
            return False

    def _verify_susfs_config(self, config_path: Path):
        self._require_path(config_path, "compiled kernel .config")
        config_text = config_path.read_text(encoding="utf-8")
        for symbol in ("CONFIG_KSU", "CONFIG_KPROBES", "CONFIG_KRETPROBES",
                       "CONFIG_HAVE_SYSCALL_TRACEPOINTS"):
            if not re.search(rf"^{symbol}=y$", config_text, re.M):
                raise RuntimeError(f"Built-in SukiSU requires {symbol}=y in the compiled config")
        enabled = set(re.findall(r"^(CONFIG_KSU_SUSFS\w*)=y$",
                                 config_text, re.M))
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
        self._run_cmd(f"cp {image_src} {bootimgs_dir}/Image && cp {image_src} {self.work_dir}/Image", check=True)

        self._chdir(bootimgs_dir)
        self._run_cmd("$MKBOOTIMG --header_version 4 --kernel Image --output boot.img", check=True)
        self._run_cmd(
            "$AVBTOOL add_hash_footer --partition_name boot --partition_size $((64 * 1024 * 1024)) "
            "--image boot.img --algorithm SHA256_RSA2048 --key $BOOT_SIGN_KEY_PATH",
            check=True,
        )
        dest = self.work_dir / f"{self.config.artifact_stem}-boot.img"
        self._run_cmd(f"cp boot.img '{dest}'", check=True)
        return [str(dest)]

    def create_anykernel_zips(self) -> list:
        logger.info("=== Creating AnyKernel3 zip ===")
        self._chdir(self.work_dir)
        ak3_dir = self.anykernel_dir

        image_src = self._kernel_image_path()
        self._require_path(image_src, "compiled kernel Image")
        self._run_cmd(f"cp {image_src} {self.work_dir}/Image", check=True)
        self._run_cmd(f"cp {self.work_dir}/Image {ak3_dir}/", check=True)

        zip_name = f"{self.config.artifact_stem}-AnyKernel3.zip"
        zip_path = self.work_dir / zip_name
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
            "- Android family: android13-5.15",
            f"- GKI tag: `{self.config.gki_tag}`",
            f"- GKI exact commit: `{self.config.gki_commit}`",
            f"- Kernel version: {self.config.kernel_version}",
            f"- Manifest branch: `{self.config.manifest_branch}`",
            f"- Kernel source: `{self._git_head(self.work_dir / 'common')}`",
            f"- SukiSU channel: {self.config.sukisu_channel}",
            f"- SukiSU tag: {self.config.sukisu_tag or 'none (dev)'}",
            f"- SukiSU exact commit: `{self.config.sukisu_commit}`",
            f"- SukiSU checkout: `{self._git_head(self.work_dir / 'KernelSU')}`",
            f"- Artifact stem: `{self.config.artifact_stem}`",
            f"- SukiSU kernel UAPI: {self._read_ksu_uapi_version()}",
            f"- SUSFS exact commit: `{SUSFS_REVISION}`",
            f"- LZ4KD helper source commit: `{SUKISU_PATCH_REVISION}`",
            f"- BBR version: {self.config.bbr_version}",
            f"- Google BBR v3 reference: `{GOOGLE_BBR_V3_COMMIT}`",
            f"- Applied BBR patches: {', '.join(self.applied_bbr_patches) or 'none'}",
            f"- Tweaks enabled: {'yes' if self.config.apply_tweaks else 'no'}",
            f"- Applied tweak patches: {', '.join(self.applied_tweak_patches) or 'none'}",
            f"- Other integration patches: {', '.join(self.applied_integration_patches)}",
            f"- Applied patch count: {len(self.applied_integration_patches)}",
            "- ZRAM compressor: LZ4KD",
            "- Disabled optional algorithms: TCP BIC/HTCP/Westwood/Vegas/Veno/Hybla/Illinois/DCTCP/CDG/NV/CUBIC; ZRAM alternatives; BFQ/Kyber; powersave/conservative/ondemand/userspace governors",
            "- KMI/ABI enforcement: intentionally disabled; no frozen GKI KMI compatibility claim",
            f"- Compiler: {compiler_version}",
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
            (self.workspace / "artifact_stem.txt").write_text(self.config.artifact_stem + "\n", encoding="utf-8")
            self.clone_repositories()
            self.setup_repo_tool()
            self.init_and_sync_kernel()
            self.configure_kernel_toolchain()
            self.add_kernelsu()
            self.apply_susfs_patches()
            self.apply_zram_patches()
            self.apply_vendor_patches()
            self.configure_kernel()
            self.configure_kernel_name()
            self.show_kernel_config()
            self._disable_kmi_enforcement()
            self.generate_and_verify_config()
            logger.info("Patch report: GKI %s %s; SukiSU %s; SUSFS %s; BBR %s %s; tweaks=%s %s; integration count=%d",
                        self.config.gki_tag, self.config.gki_commit, self.config.sukisu_commit,
                        SUSFS_REVISION, self.config.bbr_version, self.applied_bbr_patches,
                        self.config.apply_tweaks, self.applied_tweak_patches,
                        len(self.applied_integration_patches))
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
            try:
                artifacts.extend(self.prepare_boot_images())
            except Exception as boot_err:
                logger.warning(f"boot.img packaging failed (AnyKernel3 zip is still valid): {boot_err}")
            build_time = time.time() - start_time
            info_path = self.write_build_info(artifacts=artifacts, build_time=build_time, success=True)
            artifacts.append(str(info_path))
            logger.info(f"Build succeeded in {build_time:.2f}s, {len(artifacts)} artifact(s)")
            return BuildResult(success=True, config=self.config, message="Build succeeded", artifacts=artifacts, build_time=build_time)
        except Exception as e:
            logger.exception(f"Build failed: {e}")
            try:
                self.write_build_info(build_time=time.time() - start_time, success=False, message=str(e))
            except Exception:
                pass
            return BuildResult(success=False, config=self.config, message=str(e), build_time=time.time() - start_time)
