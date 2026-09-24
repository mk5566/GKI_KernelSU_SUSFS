import os
import shutil
import subprocess
import logging
import re
import hashlib
import struct
import sys
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from config import (BuildConfig, KSU_REPO_CONFIG, SUSFS_REPO_CONFIG, SUKISU_PATCH_REPO_CONFIG,
                   ANYKERNEL_CONFIG, SUKISU_UAPI_VERSION, TOOLCHAIN_CONFIG)
from susfs_integration import select_mount_patch
from patch_utils import apply_patch_exact, read_patch_order
from patch_policy import reject_retired_aliases, verify_no_retired_config
from source_safety import verify_upstream_sources

logger = logging.getLogger(__name__)

REQUIRED_TOOLS = ("git", "curl", "python3", "zip")
BOOT_IMAGE_SIZE = 64 * 1024 * 1024  # Temporary fastboot boot comparison image.


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
        "CONFIG_KSU_SUSFS": "y",
        "CONFIG_KSU_SUSFS_SUS_MOUNT": "y",
    }

    def __init__(self, config: BuildConfig, workspace: str,
                 expected_common_revision: Optional[str] = None):
        self.config = config
        self.expected_common_revision = expected_common_revision
        self._safety_common_revision = expected_common_revision
        self.workspace = Path(workspace)
        self.shell = ShellCommand(cwd=workspace)
        self.env = os.environ.copy()
        self.work_dir = self.workspace / config.config_name
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.susfs_dir = self.workspace / "susfs4ksu"
        self.sukisu_patch_dir = self.workspace / "SukiSU_patch"
        self.anykernel_dir = self.workspace / "AnyKernel3"
        self.toolchain_dir = self.workspace / "toolchain"
        self.mkbootimg_dir = self.workspace / "mkbootimg"
        self._setup_env()

    def _setup_env(self):
        self.env["CONFIG"] = self.config.config_name
        ccache = shutil.which("ccache")
        if ccache:
            self.env["USE_CCACHE"] = "1"
            self.env["CCACHE_EXEC"] = ccache
        self.env["CCACHE_COMPILERCHECK"] = "%compiler% -dumpmachine; %compiler% -dumpversion"
        self.env["CCACHE_NOHASHDIR"] = "true"
        self.env.pop("CCACHE_HARDLINK", None)
        self.env["CCACHE_NOHARDLINK"] = "true"
        self.env.setdefault("CCACHE_DIR", os.path.expanduser("~/.ccache"))
        self.env.setdefault("GIT_TERMINAL_PROMPT", "0")
        self.shell.env = self.env

    def _run_cmd(self, cmd: str, **kwargs) -> subprocess.CompletedProcess:
        return self.shell.run(cmd, **kwargs)

    def _chdir(self, path: Path):
        os.chdir(path)
        self.shell.cwd = str(path)

    def _preflight(self):
        reject_retired_aliases(self.config.optional_patches)
        patch_dir = Path(__file__).resolve().parents[3] / "patches" / self.config.kernel_version
        read_patch_order(patch_dir, self.config.optional_patches)
        missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
        if missing:
            raise RuntimeError(f"Missing required tools: {', '.join(missing)}")
        if any(self.work_dir.iterdir()):
            raise RuntimeError(
                f"Build work directory is not empty: {self.work_dir}. "
                "Use a fresh workspace; existing source and artifacts are preserved."
            )

    def _ensure_git_identity(self):
        self.env.setdefault("GIT_AUTHOR_NAME", "GKI Builder")
        self.env.setdefault("GIT_AUTHOR_EMAIL", "gki-builder@localhost")
        self.env.setdefault("GIT_COMMITTER_NAME", "GKI Builder")
        self.env.setdefault("GIT_COMMITTER_EMAIL", "gki-builder@localhost")
        self.shell.env = self.env

    @staticmethod
    def _assert_clean_repo(name: str, dest: Path):
        status = subprocess.run(
            ["git", "-C", str(dest), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True, text=True,
        )
        if status.returncode != 0:
            raise RuntimeError(f"{name} is not a usable Git checkout: {dest}")
        if status.stdout.strip():
            raise RuntimeError(
                f"{name} has local changes at {dest}; use a fresh build workspace "
                "instead of overwriting them"
            )

    def _clone_or_update(self, name: str, dest: Path, url: str, branch: Optional[str] = None):
        git_dir = dest / ".git"
        if dest.exists() and not git_dir.exists() and not git_dir.is_file():
            raise RuntimeError(
                f"{name} path exists but is not a Git checkout: {dest}; "
                "refusing to replace it"
            )
        if not dest.exists():
            cmd = f"git clone --depth 1 {url} {dest}"
            if branch:
                cmd = f"git clone --depth 1 -b {branch} {url} {dest}"
            logger.info(f"Cloning {name}...")
            self._run_cmd(cmd, check=True)
            if not dest.exists():
                raise RuntimeError(f"Failed to clone {name} from {url}")
            return
        logger.info(f"{name} already present at {dest} (HEAD {self._git_head(dest)})")
        self._assert_clean_repo(name, dest)
        fetch_ref = branch or "HEAD"
        fetch = self._run_cmd(
            f"git -C '{dest}' fetch --depth 1 origin {fetch_ref}",
            check=False,
            capture_output=True,
        )
        if fetch.returncode != 0:
            output = ((fetch.stderr or "") + (fetch.stdout or "")).strip()
            raise RuntimeError(f"{name} fetch of {fetch_ref} failed; refusing stale source: {output}")
        self._run_cmd(f"git -C '{dest}' checkout --detach FETCH_HEAD", check=True)
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

    def _apply_patch_file(self, patch_path: Path, required: bool = False,
                          allow_fuzz: bool = False) -> bool:
        if allow_fuzz:
            logger.warning("Fuzzy matching is disabled; using exact patch validation")
        try:
            status = apply_patch_exact(Path(self.shell.cwd), patch_path)
            logger.info("Patch %s: %s", patch_path.name, status)
            return True
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            if required:
                raise RuntimeError(f"Required patch failed: {patch_path.name}") from error
            logger.warning("Optional patch skipped: %s", error)
            return False

    def _kernel_image_path(self) -> Path:
        return self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist/Image"

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
        self._assert_clean_repo(name, repo)
        if commit.startswith("HEAD~"):
            self._run_cmd("git fetch --depth 50 origin", check=True)
            self._run_cmd(f"git checkout --detach {commit}", check=True)
            self._chdir(self.workspace)
            return
        fetch = self._run_cmd(
            f"git fetch --depth 1 origin {commit}",
            check=False,
            capture_output=True,
        )
        if fetch.returncode != 0:
            output = ((fetch.stderr or "") + (fetch.stdout or "")).strip()
            if 7 <= len(commit) < 40 and re.fullmatch(r"[0-9a-f]+", commit, re.I):
                raise RuntimeError(
                    f"{name} commit {commit} could not be fetched with --depth 1. "
                    f"Use the full 40-character SHA. git: {output}"
                )
            raise RuntimeError(f"Failed to fetch {name} ref {commit}: {output}")
        self._run_cmd("git checkout --detach FETCH_HEAD", check=True)
        logger.info(f"{name} checked out {self._git_head(repo)}")
        self._chdir(self.workspace)

    def _apply_susfs_commit(self):
        if not self.config.susfs_commit or not self.susfs_dir.exists():
            return
        self._checkout_commit(self.susfs_dir, self.config.susfs_commit, "SUSFS")

    def clone_repositories(self):
        logger.info("=== Cloning helper repositories ===")
        self._clone_or_update("SUSFS", self.susfs_dir, SUSFS_REPO_CONFIG["repo_url"], self.config.kernel_branch)
        self._clone_or_update("SukiSU Patch", self.sukisu_patch_dir, SUKISU_PATCH_REPO_CONFIG["repo_url"])
        self._clone_or_update("AnyKernel3", self.anykernel_dir, ANYKERNEL_CONFIG["repo_url"], ANYKERNEL_CONFIG["branch"])
        self._apply_susfs_commit()
        logger.info("=== Helper repositories ready ===")

    def clone_toolchain(self):
        logger.info("=== Setting up toolchain & tools ===")
        try:
            self._clone_or_update(
                "build-tools",
                self.toolchain_dir,
                f"{TOOLCHAIN_CONFIG['aosp_mirror']}/kernel/prebuilts/build-tools",
                TOOLCHAIN_CONFIG["build_tools_branch"],
            )
            self._clone_or_update(
                "mkbootimg",
                self.mkbootimg_dir,
                f"{TOOLCHAIN_CONFIG['aosp_mirror']}/platform/system/tools/mkbootimg",
                TOOLCHAIN_CONFIG["mkbootimg_branch"],
            )
            avbtool = self.toolchain_dir / "linux-x86/bin/avbtool"
            mkbootimg = self.mkbootimg_dir / "mkbootimg.py"
            unpack = self.mkbootimg_dir / "unpack_bootimg.py"
            if avbtool.is_file():
                self.env["AVBTOOL"] = str(avbtool)
            if mkbootimg.is_file():
                self.env["MKBOOTIMG"] = str(mkbootimg)
            if unpack.is_file():
                self.env["UNPACK_BOOTIMG"] = str(unpack)
        except Exception as e:
            logger.warning(f"Toolchain setup note: {e}")

        key_path = Path(os.environ.get("BOOT_SIGN_KEY_PATH", self.workspace / "boot_avb_testkey.pem"))
        if not key_path.exists():
            try:
                self._run_cmd(f"openssl genrsa -out '{key_path}' 2048", check=False)
            except Exception:
                pass
        self.env["BOOT_SIGN_KEY_PATH"] = str(key_path)
        self.shell.env = self.env
        logger.info("=== Toolchain ready ===")

    def setup_repo_tool(self):
        logger.info("=== Installing repo tool ===")
        repo_dir = self.workspace / "git-repo"
        repo_dir.mkdir(exist_ok=True)
        repo_path = repo_dir / "repo"
        if not repo_path.exists():
            self._run_cmd(
                f"curl -fLSs https://storage.googleapis.com/git-repo-downloads/repo -o {repo_path}",
                check=True,
            )
            self._run_cmd(f"chmod a+rx {repo_path}", check=True)
        self.env["REPO"] = str(repo_path)
        self.shell.env = self.env

    def init_and_sync_kernel(self):
        logger.info("=== Initializing and syncing kernel sources ===")
        self._ensure_git_identity()
        self._chdir(self.work_dir)
        formatted_branch = self.config.formatted_branch
        manifest_candidates = [
            f"common-{formatted_branch}",
            f"deprecated/common-{formatted_branch}",
            f"common-deprecated/{formatted_branch}",
        ]

        init_ok = False
        last_error = ""
        for manifest_branch in manifest_candidates:
            logger.info(f"repo init with manifest branch {manifest_branch}")
            result = self._run_cmd(
                f"$REPO init --depth=1 -u https://android.googlesource.com/kernel/manifest "
                f"-b {manifest_branch} --repo-rev=v2.16 --no-clone-bundle",
                check=False,
                capture_output=True,
            )
            output = (result.stdout or "") + (result.stderr or "")
            if output.strip():
                logger.info(output.strip())
            if result.returncode == 0:
                init_ok = True
                break
            last_error = output or f"exit {result.returncode}"

        if not init_ok:
            raise RuntimeError(f"repo init failed for {formatted_branch}: {last_error}")

        remote_proc = subprocess.run(
            [
                "git", "ls-remote",
                "https://android.googlesource.com/kernel/common",
                formatted_branch,
            ],
            capture_output=True, text=True,
        )
        if remote_proc.returncode != 0:
            logger.warning(
                f"git ls-remote failed for {formatted_branch}: "
                f"{(remote_proc.stderr or remote_proc.stdout or str(remote_proc.returncode)).strip()}"
            )
        remote = (remote_proc.stdout or "").strip()
        manifest_path = self.work_dir / ".repo/manifests/default.xml"
        if "deprecated/" in remote and manifest_path.exists():
            content = manifest_path.read_text(encoding="utf-8")
            if f'deprecated/{formatted_branch}' not in content:
                content = content.replace(f'"{formatted_branch}"', f'"deprecated/{formatted_branch}"')
                manifest_path.write_text(content, encoding="utf-8")
                logger.info(f"Rewrote manifest revision to deprecated/{formatted_branch}")

        # An old local pin must not silently override the selected monthly manifest.
        stale_pin = self.work_dir / ".repo/local_manifests/kernel-revision.xml"
        if stale_pin.exists():
            raise RuntimeError(
                f"Old kernel revision pin remains in the build workspace: {stale_pin}. "
                "Select a fresh workspace or review and remove that pin manually."
            )

        self.env["REMOTE_BRANCH"] = remote
        logger.info("Syncing kernel sources...")
        self._run_cmd("$REPO sync -c -j$(nproc --all) --no-tags --fail-fast --no-clone-bundle", check=True)
        self._run_cmd("$REPO manifest -r -o manifest.lock.xml", check=True)

        self._require_path(self.work_dir / "common", "kernel common/ directory after repo sync")
        synced_revision = self._git_head(self.work_dir / "common")
        if self.expected_common_revision:
            if synced_revision != self.expected_common_revision:
                raise RuntimeError(
                    f"Synced common revision {synced_revision} differs from the "
                    f"resolved revision {self.expected_common_revision}; rerun the workflow"
                )
        kernel_ver = self._read_kernel_version()
        logger.info(f"Synced kernel version: {kernel_ver}")
        expected = f"{self.config.kernel_version}.{self.config.sub_level}"
        if kernel_ver != expected:
            raise RuntimeError(
                f"Synced kernel {kernel_ver} does not match requested {expected} "
                f"(branch {formatted_branch})"
            )
        # Capture before any helper can change HEAD or source bytes.
        self._safety_common_revision = synced_revision
        self._verify_patch_safety()
        logger.info("=== Kernel source sync complete ===")

    def add_kernelsu(self):
        logger.info("=== Adding KernelSU ===")
        self._chdir(self.work_dir)
        setup_ref = self.config.ksu_setup_ref
        script_ref = setup_ref or "main"
        raw_base = KSU_REPO_CONFIG["repo_url"].rstrip("/").removesuffix(".git").replace(
            "https://github.com/", "https://raw.githubusercontent.com/", 1
        )
        setup_url = f"{raw_base}/{script_ref}/kernel/setup.sh"
        setup_script = self.work_dir / "sukisu_setup.sh"
        logger.info(f"SukiSU setup.sh from {script_ref}, checkout ref={setup_ref or 'latest-tag'}")
        self._run_cmd(f"curl -fLSs {setup_url} -o {setup_script}", check=True)
        if setup_ref:
            self._run_cmd(f"bash '{setup_script}' '{setup_ref}'", check=True)
        else:
            self._run_cmd(f"bash '{setup_script}'", check=True)
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
        if uapi != SUKISU_UAPI_VERSION:
            raise RuntimeError(
                f"This integration requires SukiSU UAPI {SUKISU_UAPI_VERSION}, got {uapi}. "
                "Use current main; the old builtin/UAPI-2 branch is incompatible."
            )
        logger.info(f"SukiSU kernel UAPI: {uapi}; the manager must use the same UAPI")

        self._chdir(ksu_dir)
        integration_patch = (Path(__file__).resolve().parents[3] / "patches/susfs/"
                             "0001-sukisu-main-uapi4-mount-support.patch")
        self._apply_patch_file(integration_patch, required=True, allow_fuzz=False)
        self._chdir(self.work_dir)

    def apply_susfs_patches(self):
        logger.info("=== Applying SUSFS patches ===")
        self._chdir(self.work_dir)
        common_dir = self.work_dir / "common"
        susfs_patch = self.susfs_dir / "kernel_patches" / self.config.get_susfs_patch_filename()
        self._require_path(susfs_patch, "SUSFS patch")
        self._run_cmd(f"cp {susfs_patch} {common_dir}/", check=True)
        for src, dst in [
            (self.susfs_dir / "kernel_patches/fs", common_dir / "fs/"),
            (self.susfs_dir / "kernel_patches/include/linux", common_dir / "include/linux/"),
        ]:
            self._require_path(src, f"SUSFS source {src}")
            self._run_cmd(f"cp -r {src}/* {dst}", check=True)
        patch_file = common_dir / self.config.get_susfs_patch_filename()
        self._chdir(common_dir)
        context_patch = (Path(__file__).resolve().parents[3] / "patches" /
                         "susfs/5.15-context.patch")
        self._apply_patch_file(context_patch, required=True, allow_fuzz=False)
        mount_patch = common_dir / "susfs-mount-only.patch"
        mount_patch.write_text(select_mount_patch(patch_file.read_text(encoding="utf-8")),
                               encoding="utf-8")
        self._apply_patch_file(mount_patch, required=True, allow_fuzz=False)
        reboot_patch = (Path(__file__).resolve().parents[3] / "patches/susfs/"
                        "0002-common-susfs-reboot-dispatch.patch")
        self._apply_patch_file(reboot_patch, required=True, allow_fuzz=False)
        self._chdir(self.work_dir)

    def apply_sukisu_patches(self):
        if self.KERNEL_CONFIG_UPDATES.get("CONFIG_KSU_SUSFS_SUS_MAP") != "y":
            logger.info("Map hiding disabled; skipping optional hide helpers")
            return
        logger.info("=== Applying SukiSU hide patches ===")
        self._chdir(self.work_dir / "common")
        task_mmu = Path("fs/proc/task_mmu.c")
        if task_mmu.exists() and "show_vma_header_prefix_fake" in task_mmu.read_text(encoding="utf-8"):
            logger.info("Hide helpers already present in task_mmu.c, skipping 69_hide_stuff.patch")
            return
        hooks_patch = self.sukisu_patch_dir / "69_hide_stuff.patch"
        if hooks_patch.exists():
            # Current SUSFS no longer has susfs_sus_ino_for_show_map_vma, so this
            # patch often mismatches. Never fuzz it: fuzz duplicates the fake
            # maps helper and breaks the android13-5.15 build.
            if not self._apply_patch_file(hooks_patch, required=False, allow_fuzz=False):
                logger.warning("69_hide_stuff.patch does not apply to this SUSFS tree, skipping")
        else:
            logger.warning("69_hide_stuff.patch not found, continuing")

    def apply_zram_patches(self):
        # Copy only the LZ4KD codec from the freshly cloned helper revision.
        # The helper's lz4kd.patch also changes kernel/module.c; never apply it.
        source = self.sukisu_patch_dir / "other/zram/lz4k"
        common = self.work_dir / "common"
        files = [
            "crypto/lz4kd.c",
            "include/linux/lz4kd.h",
            "lib/lz4kd/Makefile",
            "lib/lz4kd/lz4kd_private.h",
            "lib/lz4kd/lz4kd_encode_private.h",
            "lib/lz4kd/lz4kd_encode.c",
            "lib/lz4kd/lz4kd_encode_delta.c",
            "lib/lz4kd/lz4kd_decode.c",
            "lib/lz4kd/lz4kd_decode_delta.c",
        ]
        for relative in files:
            src, dst = source / relative, common / relative
            if not src.is_file() or src.is_symlink():
                raise RuntimeError(f"Required LZ4KD source missing or unsafe: {src}")
            if dst.exists() or dst.is_symlink():
                raise RuntimeError(f"LZ4KD destination already exists: {dst}")
        for relative in files:
            src, dst = source / relative, common / relative
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        logger.info("Copied %d LZ4KD source files from helper %s", len(files),
                    self._git_head(self.sukisu_patch_dir))

    def _strip_duplicate_c_function(self, content: str, signature: str) -> str:
        starts = []
        pos = 0
        while True:
            idx = content.find(signature, pos)
            if idx < 0:
                break
            starts.append(idx)
            pos = idx + len(signature)
        if len(starts) < 2:
            return content

        def function_end(src: str, start: int) -> int:
            brace = src.find("{", start)
            if brace < 0:
                return len(src)
            depth = 0
            for j in range(brace, len(src)):
                if src[j] == "{":
                    depth += 1
                elif src[j] == "}":
                    depth -= 1
                    if depth == 0:
                        end = j + 1
                        if end < len(src) and src[end] == "\n":
                            end += 1
                        return end
            return len(src)

        for start in reversed(starts[1:]):
            content = content[:start] + content[function_end(content, start):]
        logger.info(f"Removed {len(starts) - 1} duplicate definition(s) of {signature.split('(')[0].strip()}")
        return content

    def apply_task_mmu_fixes(self):
        # The audited mount-only profile has no task_mmu hunks to repair.
        # Never transform C semantics using regex as a patch-error recovery.
        if self.KERNEL_CONFIG_UPDATES.get("CONFIG_KSU_SUSFS_SUS_MAP") == "y":
            raise RuntimeError("Map hiding is outside this audited mount-only profile")

    def apply_vendor_patches(self):
        logger.info("=== Applying Vendor / Performance Patches ===")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            raise RuntimeError(f"kernel common/ directory missing; cannot apply vendor patches: {common_dir}")

        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        patch_dir = repo_root / "patches" / self.config.kernel_version
        if not patch_dir.is_dir():
            raise RuntimeError(
                f"Required vendor patch directory missing: {patch_dir}. "
                "Select a supported android13-5.15 target."
            )

        logger.info(f"Loading vendor patches from: {patch_dir}")
        self._chdir(common_dir)

        patch_list = read_patch_order(patch_dir, self.config.optional_patches)
        applied, failed_optional = [], []
        for patch_path, required in patch_list:
            if self._apply_patch_file(patch_path, required=required, allow_fuzz=False):
                applied.append(patch_path.name)
            else:
                failed_optional.append(patch_path.name)

        logger.info(f"Vendor patches applied: {len(applied)}")
        if failed_optional:
            logger.warning(f"Optional vendor patches skipped: {failed_optional}")
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
        self._configure_ksu_susfs()
        if self.config.set_default_bbr:
            self._configure_bbr()

        if self.config.use_zram:
            self._configure_zram()
        self._configure_device_features()
        self._verify_ishtar_zram_plan(self._defconfig_path())
        verify_no_retired_config(self._defconfig_path())

    def _configure_device_features(self):
        config_file = self._defconfig_path()
        content = config_file.read_text(encoding="utf-8")
        zram_anchor = ("CONFIG_ZRAM_DEF_COMP_ZSTD=y" if self.config.use_zram
                       else "CONFIG_ZRAM_DEF_COMP_LZ4KD=y")
        # Match the booted phone. POSIX_ACL selects TMPFS_XATTR.
        for anchor, setting in (
            ("CONFIG_TMPFS=y", "CONFIG_TMPFS_POSIX_ACL=y"),
            ("CONFIG_IP_NF_MANGLE=y", "CONFIG_IP_NF_TARGET_TTL=y"),
            ("CONFIG_IP6_NF_IPTABLES=y", "CONFIG_IP6_NF_MATCH_HL=y"),
            ("CONFIG_IP6_NF_MATCH_RPFILTER=y", "CONFIG_IP6_NF_TARGET_HL=y"),
            (zram_anchor, "CONFIG_ZRAM_WRITEBACK=y"),
        ):
            symbol = setting.split("=", 1)[0]
            if setting in content.splitlines():
                continue
            if re.search(rf"^(?:# )?{symbol}(?:=| is not set)", content, re.M):
                raise RuntimeError(f"Review changed upstream {symbol} before building")
            marker = anchor + "\n"
            if content.count(marker) != 1:
                raise RuntimeError(f"Cannot place {setting} after {anchor}")
            content = content.replace(marker, marker + setting + "\n", 1)
        config_file.write_text(content, encoding="utf-8")

    def _configure_ksu_susfs(self):
        config_file = self._defconfig_path()
        content = config_file.read_text(encoding="utf-8")
        # In SukiSU-Ultra, CONFIG_KSU defaults to y when KPROBES and EXT4_FS are set,
        # so savedefconfig omits CONFIG_KSU and outputs only CONFIG_KSU_SUSFS=y.
        # Ensure CONFIG_KSU=y is not in gki_defconfig to prevent savedefconfig mismatch.
        if "CONFIG_KSU=y\n" in content:
            content = content.replace("CONFIG_KSU=y\n", "")
        marker = "CONFIG_INTERCONNECT=y\n"
        ksu_lines = "CONFIG_KSU_SUSFS=y\n"
        if "CONFIG_KSU_SUSFS=y" not in content:
            if marker in content:
                content = content.replace(marker, marker + ksu_lines, 1)
                config_file.write_text(content, encoding="utf-8")
            else:
                self._upsert_defconfig({"CONFIG_KSU_SUSFS": "y"})
        else:
            config_file.write_text(content, encoding="utf-8")

    def _configure_bbr(self):
        config_file = self._defconfig_path()
        content = config_file.read_text(encoding="utf-8")
        marker = "CONFIG_INET_DIAG_DESTROY=y\n"
        bbr_lines = (
            "CONFIG_TCP_CONG_ADVANCED=y\n"
            "# CONFIG_TCP_CONG_BIC is not set\n"
            "# CONFIG_TCP_CONG_WESTWOOD is not set\n"
            "# CONFIG_TCP_CONG_HTCP is not set\n"
            "CONFIG_TCP_CONG_BBR=y\n"
            "CONFIG_DEFAULT_BBR=y\n"
        )
        if "CONFIG_TCP_CONG_BBR=y" not in content:
            if marker in content:
                content = content.replace(marker, marker + bbr_lines, 1)
                config_file.write_text(content, encoding="utf-8")
            else:
                raise RuntimeError("Cannot place BBRv1 in the selected GKI defconfig")

    def _configure_zram(self):
        fragment = (self.work_dir / "common/arch/arm64/configs/"
                    "ishtar_native_zstd.fragment")
        self._require_path(fragment, "native ZSTD configuration fragment")
        updates = {}
        for raw in fragment.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if re.fullmatch(r"CONFIG_\w+=(?:y|m|n)", line):
                symbol, value = line.split("=", 1)
                updates[symbol] = value
            elif re.fullmatch(r"# CONFIG_\w+ is not set", line):
                updates[line.split()[1]] = None
            elif line and not line.startswith("#"):
                raise RuntimeError(f"Unsupported fragment line: {line!r}")
        self._upsert_defconfig(updates)
        # One Kconfig choice can have only one default compressor.
        self._upsert_defconfig({"CONFIG_ZRAM_DEF_COMP_LZ4KD": None})

    def _verify_ishtar_zram_plan(self, defconfig_path: Path):
        """Fail before compilation if Image-only output would lose ishtar swap."""
        text = defconfig_path.read_text(encoding="utf-8")
        required = ["CONFIG_ZRAM=y", "CONFIG_ZSMALLOC=y"]
        if self.config.use_zram:
            required += ["CONFIG_CRYPTO_ZSTD=y", "CONFIG_ZRAM_DEF_COMP_ZSTD=y"]
        else:
            required += ["CONFIG_CRYPTO_LZ4KD=y", "CONFIG_ZRAM_DEF_COMP_LZ4KD=y"]
        missing = [line for line in required if line not in text.splitlines()]
        if missing:
            raise RuntimeError(
                "ishtar zRAM deployment is unresolved: the running phone has "
                "built-in zram/zsmalloc and uses lz4kd, while the upstream GKI "
                f"defconfig does not meet {missing}. Do not package an Image-only "
                "kernel until a reviewed compressor and module/KMI plan is implemented."
            )

    def configure_kernel_name(self):
        logger.info("=== Configuring kernel name ===")
        self._chdir(self.work_dir)
        if self.config.custom_version:
            self._upsert_defconfig({"CONFIG_LOCALVERSION": f'"{self.config.custom_version}"'})

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

        if self.config.use_zram:
            zram_configs = [
                c for c in config_lines
                if any(x in c for x in ["ZRAM", "ZSMALLOC", "LZ4KD", "CRYPTO_LZ4"])
            ]
            if zram_configs:
                logger.info("ZRAM-related config:")
                for zc in sorted(set(zram_configs)):
                    logger.info(f" -> {zc}")

        logger.info("-" * 60)

    def build_kernel(self) -> bool:
        logger.info("=== Starting kernel compile ===")
        self._chdir(self.work_dir)
        try:
            self._verify_patch_safety()
            verify_no_retired_config(self._defconfig_path())

            # The upstream config sources this fragment after its defaults.
            # Keep check_defconfig, module lists, and protected sources intact.
            fragment = self.work_dir / "ishtar.build.config"
            fragment.write_text(
                "TRIM_NONLISTED_KMI=0\n"
                "KMI_SYMBOL_LIST_STRICT_MODE=0\n"
                "KMI_ENFORCED=0\n"
                "BUILD_SYSTEM_DLKM=0\n"
                "BUILD_GKI_ARTIFACTS=0\n"
                "BUILD_GKI_CERTIFICATION_TOOLS=0\n",
                encoding="utf-8",
            )
            self.env["GKI_BUILD_CONFIG_FRAGMENT"] = str(fragment)

            logger.info("Starting kernel compilation with build.sh (TRIM_NONLISTED_KMI=0)...")
            build_cmd = (
                "USE_CCACHE=1 "
                "LTO=thin "
                "INSTALL_MOD_STRIP=1 "
                "BUILD_CONFIG=common/build.config.gki.aarch64 "
                "build/build.sh"
            )
            returncode = self._run_build_with_log(build_cmd)

            if returncode != 0:
                logger.error(f"Kernel compile failed: exit {returncode}; see {self.work_dir / 'build.log'}")
                return False
            image_path = self._kernel_image_path()
            if not image_path.exists():
                logger.error(f"Compile reported success but Image is missing: {image_path}")
                return False
            final_config = (self.work_dir / "out" /
                            f"{self.config.android_version}-{self.config.kernel_version}" /
                            "common/.config")
            self._verify_susfs_config(final_config)
            if self.config.use_zram:
                self._verify_zstd_config(final_config)
            else:
                self._verify_lz4kd_config(final_config)
            if self.config.set_default_bbr:
                self._verify_bbr_config(final_config)
            self._verify_device_config(final_config)
            verify_no_retired_config(final_config)
            shutil.copyfile(final_config, self.work_dir / "final.config")
            symvers = final_config.with_name("Module.symvers")
            self._require_path(symvers, "compiled Module.symvers")
            shutil.copyfile(symvers, self.work_dir / "Module.symvers")
            logger.info(f"=== Kernel compile succeeded: {image_path} ===")
            return True
        except Exception as e:
            logger.error(f"Compile error: {e}")
            return False

    def _run_build_with_log(self, command: str) -> int:
        log_path = self.work_dir / "build.log"
        with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
            with subprocess.Popen(
                command, shell=True, cwd=self.work_dir, env=self.env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace",
            ) as process:
                assert process.stdout is not None
                for line in process.stdout:
                    log_file.write(line)
                    log_file.flush()
                    print(line, end="", flush=True)
                return process.wait()

    @staticmethod
    def _sha256(path: Path) -> str:
        if not path.is_file():
            return "unavailable"
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

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

    @staticmethod
    def _verify_zstd_config(config_path: Path):
        text = config_path.read_text(encoding="utf-8")
        for symbol in ("CONFIG_ZRAM", "CONFIG_ZSMALLOC"):
            if not re.search(rf"^{symbol}=[ym]$", text, re.M):
                raise RuntimeError(
                    f"Native ZSTD needs {symbol}=y or m from the ROM's validated "
                    "deployment. Choose and package the matching module layout first.")
        for setting in ('CONFIG_ZRAM_DEF_COMP_ZSTD=y',
                        'CONFIG_ZRAM_DEF_COMP="zstd"', 'CONFIG_CRYPTO_ZSTD=y'):
            if setting not in text.splitlines():
                raise RuntimeError(f"Native ZSTD request not resolved in .config: {setting}")

    @staticmethod
    def _verify_lz4kd_config(config_path: Path):
        text = config_path.read_text(encoding="utf-8").splitlines()
        for setting in ('CONFIG_ZRAM=y', 'CONFIG_ZSMALLOC=y',
                        'CONFIG_CRYPTO_LZ4KD=y',
                        'CONFIG_ZRAM_DEF_COMP_LZ4KD=y',
                        'CONFIG_ZRAM_DEF_COMP="lz4kd"'):
            if setting not in text:
                raise RuntimeError(f"Ishtar LZ4KD deployment missing in .config: {setting}")

    @staticmethod
    def _verify_bbr_config(config_path: Path):
        text = config_path.read_text(encoding="utf-8").splitlines()
        for setting in ('CONFIG_TCP_CONG_BBR=y', 'CONFIG_DEFAULT_BBR=y',
                        'CONFIG_DEFAULT_TCP_CONG="bbr"',
                        'CONFIG_TCP_CONG_CUBIC=y'):
            if setting not in text:
                raise RuntimeError(f"Upstream BBRv1 deployment missing in .config: {setting}")
        for forbidden in ('CONFIG_TCP_CONG_BIC=y', 'CONFIG_TCP_CONG_BIC=m',
                          'CONFIG_TCP_CONG_WESTWOOD=y', 'CONFIG_TCP_CONG_WESTWOOD=m',
                          'CONFIG_TCP_CONG_HTCP=y', 'CONFIG_TCP_CONG_HTCP=m'):
            if forbidden in text:
                raise RuntimeError(f"Unused congestion algorithm present in .config: {forbidden}")

    @staticmethod
    def _verify_device_config(config_path: Path):
        resolved = set(config_path.read_text(encoding="utf-8").splitlines())
        required = {"CONFIG_MODVERSIONS=y", "CONFIG_TMPFS_POSIX_ACL=y", "CONFIG_TMPFS_XATTR=y",
                    "CONFIG_ZRAM_WRITEBACK=y", "CONFIG_IP_NF_TARGET_TTL=y",
                    "CONFIG_IP6_NF_TARGET_HL=y", "CONFIG_IP6_NF_MATCH_HL=y",
                    "# CONFIG_MODULE_SIG_FORCE is not set"}
        missing = required - resolved
        if missing:
            raise RuntimeError(f"Working ishtar config lost: {', '.join(sorted(missing))}")

    def _verify_patch_safety(self):
        reject_retired_aliases(self.config.optional_patches)
        return verify_upstream_sources(
            self.work_dir / "common", self._safety_common_revision,
            self.work_dir / "source-safety.json")

    def create_anykernel_zips(self) -> list:
        logger.info("=== Creating AnyKernel3 zip ===")
        self._verify_patch_safety()
        verify_no_retired_config(self.work_dir / "final.config")
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

    @staticmethod
    def _encode_os_version(os_version_str: Optional[str], os_patch_level_str: Optional[str]) -> int:
        val = 0
        if os_version_str:
            clean_ver = re.sub(r"^android", "", os_version_str, flags=re.I)
            m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", clean_ver)
            if m:
                a = int(m.group(1)) & 0x7F
                b = int(m.group(2) or 0) & 0x7F
                c = int(m.group(3) or 0) & 0x7F
                val |= (a << 25) | (b << 18) | (c << 11)
        if os_patch_level_str:
            m = re.match(r"^(\d{4})-(\d{2})", os_patch_level_str)
            if m:
                y = (int(m.group(1)) - 2000) & 0x7F
                month = int(m.group(2)) & 0xF
                val |= (y << 4) | month
        return val

    @classmethod
    def _pack_boot_image_v4(cls, kernel_path: Path, output_path: Path,
                            os_version_str: Optional[str] = None,
                            os_patch_level_str: Optional[str] = None,
                            cmdline: str = "") -> Path:
        """Create an official Android Boot Image Header Version 4 (GKI)."""
        kernel_bytes = kernel_path.read_bytes()
        kernel_size = len(kernel_bytes)
        ramdisk_size = 0
        os_version = cls._encode_os_version(os_version_str, os_patch_level_str)
        header_size = 1584
        reserved = (0, 0, 0, 0)
        header_version = 4
        cmdline_bytes = cmdline.encode("utf-8")[:1536]
        signature_size = 0

        header = struct.pack(
            "<8sIIII4II1536sI",
            b"ANDROID!",
            kernel_size,
            ramdisk_size,
            os_version,
            header_size,
            *reserved,
            header_version,
            cmdline_bytes,
            signature_size,
        )

        page_size = 4096
        header_pad_len = page_size - len(header)
        header_pad = b"\x00" * header_pad_len

        kernel_pad_len = (page_size - (kernel_size % page_size)) % page_size
        kernel_pad = b"\x00" * kernel_pad_len

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as f:
            f.write(header)
            f.write(header_pad)
            f.write(kernel_bytes)
            f.write(kernel_pad)

        return output_path

    def create_boot_image(self) -> list:
        logger.info("=== Creating GKI boot.img (Header v4) ===")
        self._verify_patch_safety()
        verify_no_retired_config(self.work_dir / "final.config")

        image_path = self.work_dir / "Image"
        if not image_path.is_file():
            image_src = self._kernel_image_path()
            self._require_path(image_src, "compiled kernel Image")
            shutil.copyfile(image_src, image_path)

        boot_img_name = f"{self.config.artifact_stem}-boot.img"
        boot_img_path = self.work_dir / boot_img_name
        standard_boot_path = self.work_dir / "boot.img"

        mkbootimg_script = self.mkbootimg_dir / "mkbootimg.py"
        if not mkbootimg_script.is_file():
            mkbootimg_script = self.work_dir / "tools" / "mkbootimg" / "mkbootimg.py"
        built_with_tool = False
        if mkbootimg_script.is_file():
            try:
                cmd = [
                    sys.executable,
                    str(mkbootimg_script),
                    "--header_version", "4",
                    "--kernel", str(image_path),
                    "--output", str(boot_img_path),
                ]
                env = self.env.copy()
                env["PYTHONPATH"] = str(mkbootimg_script.parent)
                subprocess.run(cmd, cwd=self.work_dir, env=env, check=True, capture_output=True, text=True)
                built_with_tool = True
                logger.info(f"Built boot.img using mkbootimg: {boot_img_path}")
            except Exception as e:
                logger.warning(f"mkbootimg invocation failed ({e}), falling back to built-in pack")

        if not built_with_tool:
            self._pack_boot_image_v4(
                kernel_path=image_path,
                output_path=boot_img_path,
            )
            logger.info(f"Built boot.img using built-in v4 pack: {boot_img_path}")

        avbtool = Path(self.env.get("AVBTOOL", ""))
        if not avbtool.is_file():
            candidates = [
                self.toolchain_dir / "linux-x86/bin/avbtool",
                self.work_dir / "prebuilts/kernel-build-tools/linux-x86/bin/avbtool",
            ]
            for c in candidates:
                if c.is_file():
                    avbtool = c
                    break
            if not avbtool.is_file():
                which_avb = shutil.which("avbtool")
                avbtool = Path(which_avb) if which_avb else None

        key_path = Path(self.env.get("BOOT_SIGN_KEY_PATH", self.workspace / "boot_avb_testkey.pem"))
        if not key_path.exists():
            self._run_cmd(f"openssl genrsa -out '{key_path}' 2048", check=True)
        if not avbtool or not avbtool.is_file() or not key_path.is_file():
            raise RuntimeError("boot.img requires avbtool and an RSA-2048 signing key")
        cmd = [
            str(avbtool), "add_hash_footer",
            "--partition_name", "boot",
            "--partition_size", str(BOOT_IMAGE_SIZE),
            "--image", str(boot_img_path),
            "--algorithm", "SHA256_RSA2048", "--key", str(key_path),
        ]
        subprocess.run(cmd, cwd=self.work_dir, check=True, capture_output=True, text=True)
        if boot_img_path.stat().st_size != BOOT_IMAGE_SIZE:
            raise RuntimeError("Signed boot.img does not match the configured temporary boot image size")
        logger.info(f"Added AVB hash footer to {boot_img_path} ({BOOT_IMAGE_SIZE} bytes)")

        shutil.copyfile(boot_img_path, standard_boot_path)

        if not boot_img_path.is_file() or boot_img_path.stat().st_size == 0:
            raise RuntimeError(f"Boot image was not created: {boot_img_path}")

        return [str(boot_img_path), str(standard_boot_path)]

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
        lines = [
            f"## GKI Kernel {self.config.build_id}",
            "",
            f"- Status: {'success' if success else 'failed'}",
            f"- Message: {message or ('Build succeeded' if success else 'Build failed')}",
            f"- Android / kernel: {self.config.android_version}-{self.config.kernel_version}.{self.config.sub_level}",
            f"- OS patch: {self.config.os_patch_level}",
            f"- GKI manifest branch: `common-{self.config.formatted_branch}`",
            f"- Locked manifest SHA-256: `{self._sha256(self.work_dir / 'manifest.lock.xml')}`",
            f"- Kernel source: `{self._git_head(self.work_dir / 'common')}`",
            f"- Makefile version: {self._read_kernel_version()}",
            f"- SukiSU version: {self.config.kernelsu_version}",
            f"- SukiSU setup ref: {self.config.ksu_setup_ref or 'latest-tag'}",
            f"- Artifact stem: `{self.config.artifact_stem}`",
            f"- SukiSU-Ultra: `{self._git_head(self.work_dir / 'KernelSU')}`",
            f"- SukiSU kernel UAPI: {self._read_ksu_uapi_version()}",
            "- Manager compatibility: manager and kernel UAPI must match; the v4.2.0 label alone is insufficient.",
            "- Integration: current SukiSU main, built-in CONFIG_KSU=y, local SUSFS 2.3 mount-only port",
            f"- SUSFS: `{self._git_head(self.susfs_dir)}`",
            f"- SukiSU_patch: `{self._git_head(self.sukisu_patch_dir)}`",
            f"- AnyKernel3: `{self._git_head(self.anykernel_dir)}`",
            f"- ZRAM: {'built-in ZSTD experiment requested' if self.config.use_zram else 'built-in LZ4KD default'}",
            f"- Upstream BBRv1 default: {'requested' if self.config.set_default_bbr else 'unchanged'}",
            "- Patch safety policy: eight upstream overrides retired; see PATCH_SAFETY_AUDIT.md",
            f"- Source safety baseline: `{self._safety_common_revision}`",
            f"- Source safety report SHA-256: `{self._sha256(self.work_dir / 'source-safety.json')}`",
            f"- Selected unverified patches: {', '.join(self.config.optional_patches) or 'none'}",
            "- Qualification: canonical defconfig and module-order checks retained; vendor ABI and device boot remain unverified",
            f"- Image SHA-256: `{self._sha256(self._kernel_image_path())}`",
            f"- Module.symvers SHA-256: `{self._sha256(self.work_dir / 'Module.symvers')}`",
            f"- Boot image SHA-256: `{self._sha256(self.work_dir / 'boot.img')}`",
            f"- Final config SHA-256: `{self._sha256(self.work_dir / 'final.config')}`",
            f"- Build log SHA-256: `{self._sha256(self.work_dir / 'build.log')}`",
            "- SUSFS profile: mount-only (core + SUS_MOUNT)",
            f"- CONFIG_KSU_SUSFS_TRY_UMOUNT: {self.KERNEL_CONFIG_UPDATES.get('CONFIG_KSU_SUSFS_TRY_UMOUNT', 'n')}",
            f"- CONFIG_KSU_SUSFS_SUS_SU: {self.KERNEL_CONFIG_UPDATES.get('CONFIG_KSU_SUSFS_SUS_SU', 'n')}",
        ]
        if self.config.custom_version:
            lines.append(f"- Custom version: {self.config.custom_version}")
        if build_time is not None:
            lines.append(f"- Build time: {build_time:.2f}s")
        lines.extend(["", "### Active kernel patches"])
        try:
            patch_dir = Path(__file__).resolve().parents[3] / "patches" / self.config.kernel_version
            for patch_path, required in read_patch_order(patch_dir, self.config.optional_patches):
                lines.append(
                    f"- `{patch_path.name}` ({'required' if required else 'optional'}): "
                    f"`{self._sha256(patch_path)}`"
                )
        except (OSError, ValueError) as error:
            lines.append(f"- Patch manifest unavailable: {error}")
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

    def build(self) -> BuildResult:
        import time
        start_time = time.time()
        logger.info("=" * 50)
        logger.info(f"Starting GKI kernel build - {self.config.config_name}")
        logger.info("=" * 50)
        try:
            self._preflight()
            (self.workspace / "artifact_stem.txt").write_text(self.config.artifact_stem + "\n", encoding="utf-8")
            self.clone_repositories()
            self.clone_toolchain()
            self.setup_repo_tool()
            self.init_and_sync_kernel()
            self.add_kernelsu()
            self.apply_susfs_patches()
            self.apply_sukisu_patches()
            self.apply_zram_patches()
            self.apply_task_mmu_fixes()
            self.apply_vendor_patches()
            self._verify_patch_safety()
            self.configure_kernel()
            self.configure_kernel_name()
            self.show_kernel_config()
            if not self.build_kernel():
                build_time = time.time() - start_time
                self.write_build_info(build_time=build_time, success=False, message="Kernel compile failed")
                return BuildResult(success=False, config=self.config, message="Kernel compile failed", build_time=build_time)
            artifacts = []
            artifacts.extend(self.create_anykernel_zips())
            artifacts.extend(self.create_boot_image())
            artifacts.append(str(self._kernel_image_path()))
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
