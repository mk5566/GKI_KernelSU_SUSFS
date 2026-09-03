import os
import shutil
import subprocess
import logging
import re
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from config import (BuildConfig, KSU_REPO_CONFIG, SUSFS_REPO_CONFIG, SUKISU_PATCH_REPO_CONFIG,
                   ANYKERNEL_CONFIG, TOOLCHAIN_CONFIG)

logger = logging.getLogger(__name__)

REQUIRED_TOOLS = ("git", "curl", "patch", "python3", "zip", "openssl")


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
        "CONFIG_KSU_DEBUG": "n",
        "CONFIG_KSU_SUSFS": "y",
        "CONFIG_KSU_SUSFS_SUS_PATH": "y",
        "CONFIG_KSU_SUSFS_SUS_MOUNT": "y",
        "CONFIG_KSU_SUSFS_SUS_KSTAT": "y",
        "CONFIG_KSU_SUSFS_SUS_MAP": "y",
        "CONFIG_KSU_SUSFS_SPOOF_UNAME": "y",
        "CONFIG_KSU_SUSFS_ENABLE_LOG": "n",
        "CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS": "y",
        "CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG": "y",
        "CONFIG_KSU_SUSFS_OPEN_REDIRECT": "y",
        # TRY_UMOUNT is a separate hiding path from SUS_SU. SUSFS Kconfig
        # defaults it to y and gates susfs_try_umount_all() on the symbol.
        "CONFIG_KSU_SUSFS_TRY_UMOUNT": "y",
        "CONFIG_KSU_SUSFS_SUS_SU": "y",
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
        "CONFIG_TCP_CONG_BBR": "y",
        "CONFIG_TCP_CONG_BBR3": "y",
        "CONFIG_DEFAULT_BBR3": "y",
        "CONFIG_DEFAULT_BBR": None,
        "CONFIG_DEFAULT_CUBIC": None,
        "CONFIG_DEFAULT_TCP_CONG": '"bbr3"',
        "CONFIG_TCP_CONG_WESTWOOD": "y",
        "CONFIG_NET_SCH_FQ": "y",
        "CONFIG_TCP_CONG_BIC": "n",
        "CONFIG_TCP_CONG_HTCP": "n",
    }

    def __init__(self, config: BuildConfig, workspace: str):
        self.config = config
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
        self.env["USE_CCACHE"] = "1"
        self.env["CCACHE_EXEC"] = "/usr/bin/ccache"
        self.env["CCACHE_COMPILERCHECK"] = "%compiler% -dumpmachine; %compiler% -dumpversion"
        self.env["CCACHE_NOHASHDIR"] = "true"
        self.env["CCACHE_HARDLINK"] = "true"
        self.env.setdefault("CCACHE_DIR", os.path.expanduser("~/.ccache"))
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

    def _clone_if_needed(self, name: str, dest: Path, url: str, branch: Optional[str] = None):
        if dest.exists():
            logger.info(f"{name} already present at {dest}")
            return
        cmd = f"git clone --depth 1 {url} {dest}"
        if branch:
            cmd = f"git clone --depth 1 -b {branch} {url} {dest}"
        logger.info(f"Cloning {name}...")
        self._run_cmd(cmd, check=True)
        if not dest.exists():
            raise RuntimeError(f"Failed to clone {name} from {url}")

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
        def _run_patch(fuzz: int = 0, dry: bool = False) -> subprocess.CompletedProcess:
            fuzz_arg = f"-F {fuzz} " if fuzz else ""
            dry_arg = "--dry-run " if dry else ""
            return self._run_cmd(
                f"patch -p1 --forward --no-backup-if-mismatch -l {dry_arg}{fuzz_arg}< '{patch_path}'",
                check=False,
                capture_output=True,
            )

        def _log_output(result: subprocess.CompletedProcess):
            output = ((result.stdout or "") + (result.stderr or "")).strip()
            if output:
                logger.info(output)
            return output

        logger.info(f"Applying patch: {patch_path.name}")
        dry = _run_patch(dry=True)
        dry_out = _log_output(dry)
        if dry.returncode == 0:
            result = _run_patch(dry=False)
            _log_output(result)
            if result.returncode == 0:
                return True
        elif "Reversed (or previously applied)" in dry_out:
            logger.info(f"Patch already applied: {patch_path.name}")
            return True

        # Never fuzz-retry after a partial apply. Only fuzz when a dry-run of
        # the whole patch succeeds, so hunks cannot be inserted twice.
        if allow_fuzz:
            dry_fuzz = _run_patch(fuzz=3, dry=True)
            fuzz_out = _log_output(dry_fuzz)
            if dry_fuzz.returncode == 0:
                result = _run_patch(fuzz=3, dry=False)
                _log_output(result)
                if result.returncode == 0:
                    return True
            elif "Reversed (or previously applied)" in fuzz_out:
                logger.info(f"Patch already applied: {patch_path.name}")
                return True

        message = f"Patch failed: {patch_path.name}"
        if required:
            rej = list(Path(".").rglob("*.rej"))
            for rej_file in rej[:8]:
                try:
                    logger.error(f"Reject {rej_file}:\n{rej_file.read_text(encoding='utf-8', errors='replace')[:2000]}")
                except OSError:
                    pass
            raise RuntimeError(message)
        logger.warning(message)
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

    def _apply_susfs_commit(self):
        if not self.config.susfs_commit or not self.susfs_dir.exists():
            return
        self._chdir(self.susfs_dir)
        if self.config.susfs_commit.startswith("HEAD~"):
            self._run_cmd("git fetch --depth 10 origin", check=True)
            self._run_cmd(f"git reset --hard {self.config.susfs_commit}", check=True)
        else:
            self._run_cmd(f"git fetch --depth 1 origin {self.config.susfs_commit}", check=True)
            self._run_cmd("git checkout FETCH_HEAD", check=True)
        self._chdir(self.workspace)

    def clone_repositories(self):
        logger.info("=== Cloning helper repositories ===")
        self._clone_if_needed("SUSFS", self.susfs_dir, SUSFS_REPO_CONFIG["repo_url"], self.config.kernel_branch)
        self._clone_if_needed("SukiSU Patch", self.sukisu_patch_dir, SUKISU_PATCH_REPO_CONFIG["repo_url"])
        self._clone_if_needed("AnyKernel3", self.anykernel_dir, ANYKERNEL_CONFIG["repo_url"], ANYKERNEL_CONFIG["branch"])
        self._apply_susfs_commit()
        logger.info("=== Helper repositories ready ===")

    def clone_toolchain(self):
        logger.info("=== Cloning toolchain ===")
        if not self.toolchain_dir.exists():
            self._run_cmd(
                f"git clone --depth 1 -b {TOOLCHAIN_CONFIG['build_tools_branch']} "
                f"{TOOLCHAIN_CONFIG['aosp_mirror']}/kernel/prebuilts/build-tools {self.toolchain_dir}",
                check=True,
            )
        if not self.mkbootimg_dir.exists():
            self._run_cmd(
                f"git clone --depth 1 -b {TOOLCHAIN_CONFIG['mkbootimg_branch']} "
                f"{TOOLCHAIN_CONFIG['aosp_mirror']}/platform/system/tools/mkbootimg {self.mkbootimg_dir}",
                check=True,
            )
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
            self._run_cmd(f"openssl genrsa -out '{key_path}' 2048", check=True)
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

        remote = subprocess.run(
            f"git ls-remote https://android.googlesource.com/kernel/common {formatted_branch}",
            shell=True, capture_output=True, text=True,
        ).stdout.strip()
        manifest_path = self.work_dir / ".repo/manifests/default.xml"
        if "deprecated/" in remote and manifest_path.exists():
            content = manifest_path.read_text(encoding="utf-8")
            if f'deprecated/{formatted_branch}' not in content:
                content = content.replace(f'"{formatted_branch}"', f'"deprecated/{formatted_branch}"')
                manifest_path.write_text(content, encoding="utf-8")
                logger.info(f"Rewrote manifest revision to deprecated/{formatted_branch}")

        self.env["REMOTE_BRANCH"] = remote
        logger.info("Syncing kernel sources...")
        self._run_cmd("$REPO sync -c -j$(nproc --all) --no-tags --fail-fast --no-clone-bundle", check=True)

        self._require_path(self.work_dir / "common", "kernel common/ directory after repo sync")
        kernel_ver = self._read_kernel_version()
        logger.info(f"Synced kernel version: {kernel_ver}")
        expected = f"{self.config.kernel_version}.{self.config.sub_level}"
        if kernel_ver != expected:
            raise RuntimeError(
                f"Synced kernel {kernel_ver} does not match requested {expected} "
                f"(branch {formatted_branch})"
            )
        logger.info("=== Kernel source sync complete ===")

    def add_kernelsu(self):
        logger.info("=== Adding KernelSU ===")
        self._chdir(self.work_dir)
        setup_ref = self.config.ksu_setup_ref
        raw_base = KSU_REPO_CONFIG["repo_url"].rstrip("/").removesuffix(".git").replace(
            "https://github.com/", "https://raw.githubusercontent.com/", 1
        )
        setup_url = f"{raw_base}/{setup_ref}/kernel/setup.sh"
        setup_script = self.work_dir / "sukisu_setup.sh"
        self._run_cmd(f"curl -fLSs {setup_url} -o {setup_script}", check=True)
        self._run_cmd(f"bash {setup_script} {setup_ref}", check=True)
        self._require_path(self.work_dir / "common/drivers/kernelsu", "KernelSU driver symlink")
        self._require_path(self.work_dir / "KernelSU", "KernelSU checkout")

        # Fix missing kernel_umount_feature_set in SukiSU-Ultra v4.2.0 if present
        for umount_path in [
            self.work_dir / "common/drivers/kernelsu/feature/kernel_umount.c",
            self.work_dir / "KernelSU/kernel/feature/kernel_umount.c",
        ]:
            if umount_path.exists():
                content = umount_path.read_text(encoding="utf-8")
                if "kernel_umount_feature_set" in content and "static int kernel_umount_feature_set" not in content:
                    logger.info("Applying kernel_umount_feature_set compatibility fix...")
                    fix = (
                        "static int kernel_umount_feature_set(u64 value)\n"
                        "{\n"
                        "    bool enable = value != 0;\n"
                        "    ksu_kernel_umount_enabled = enable;\n"
                        "    pr_info(\"kernel_umount: set to %d\\n\", enable);\n"
                        "    return 0;\n"
                        "}\n\n"
                    )
                    content = content.replace("static const struct ksu_feature_handler kernel_umount_handler",
                                              fix + "static const struct ksu_feature_handler kernel_umount_handler")
                    umount_path.write_text(content, encoding="utf-8")

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
        self._apply_patch_file(patch_file, required=True)
        self._chdir(self.work_dir)

    def apply_sukisu_patches(self):
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
        if not self.config.use_zram:
            return

        logger.info("=== 应用 ZRAM (LZ4KD) 补丁 ===")
        self._chdir(self.work_dir / "common")

        # Ensure the original kernel Kconfig has not been corrupted.
        lib_kconfig = Path("lib/Kconfig")
        if (not lib_kconfig.exists()
                or "config ASSOCIATIVE_ARRAY" not in lib_kconfig.read_text()):
            raise RuntimeError(
                "ZRAM patch preflight failed: "
                "lib/Kconfig is missing ASSOCIATIVE_ARRAY"
            )

        # Copy only the standard LZ4K/LZ4KD source files.
        # Do not copy lz4k_oplus into lib/.
        for src, dst in [
            (
                self.sukisu_patch_dir / "other/zram/lz4k/include/linux",
                "include/linux/",
            ),
            (
                self.sukisu_patch_dir / "other/zram/lz4k/lib",
                "lib/",
            ),
            (
                self.sukisu_patch_dir / "other/zram/lz4k/crypto",
                "crypto/",
            ),
        ]:
            if not src.exists():
                raise RuntimeError(f"Required LZ4KD source directory not found: {src}")

            self._run_cmd(
                f"cp -r {src}/* {dst}",
                check=True,
            )

        # Apply only LZ4KD. Do not apply lz4k_oplus.patch.
        zram_patch_dir = (
            self.sukisu_patch_dir
            / f"other/zram/zram_patch/{self.config.kernel_version}"
        )
        lz4kd_patch = zram_patch_dir / "lz4kd.patch"

        if not lz4kd_patch.exists():
            raise RuntimeError(f"Required ZRAM patch not found: {lz4kd_patch}")

        self._apply_patch_file(lz4kd_patch, required=True, allow_fuzz=True)

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
        logger.info("=== Applying task_mmu.c compatibility fixes ===")
        self._chdir(self.work_dir / "common")
        task_mmu = Path("fs/proc/task_mmu.c")
        if not task_mmu.exists():
            return

        content = task_mmu.read_text(encoding="utf-8")
        original = content

        # SUSFS patches can reference this macro on kernels that do not define it.
        if "VMA_PAD_START" in content and "#define VMA_PAD_START" not in content:
            include = "#include <linux/pkeys.h>"
            definition = (
                f"{include}\n\n"
                "// VMA_PAD_START compatibility fix for SUSFS\n"
                "#ifndef VMA_PAD_START\n"
                "#define VMA_PAD_START(vma) ((vma)->vm_end)\n"
                "#endif"
            )
            if include not in content:
                raise RuntimeError(
                    "VMA_PAD_START fix failed: linux/pkeys.h include not found"
                )
            content = content.replace(include, definition, 1)

        content = self._strip_duplicate_c_function(content, "static void show_vma_header_prefix_fake")

        content = content.replace("struct dentry *dentry;", "struct dentry *dentry = NULL;")
        content = re.sub(
            r"(struct dentry \*dentry = NULL;\s*){2,}",
            "struct dentry *dentry = NULL;\n",
            content,
        )

        if re.search(r"^\s*bypass:\s*$", content, re.MULTILINE) and "goto bypass" not in content:
            content = re.sub(r"\n[ \t]*bypass:[ \t]*\n", "\n", content)
            logger.info("Removed unused bypass label from task_mmu.c")

        if "if (!vma_pages(vma))" not in content:
            content = content.replace("goto show_pad;", "return 0;")

        if content != original:
            task_mmu.write_text(content, encoding="utf-8")

    def apply_vendor_patches(self):
        logger.info("=== Applying Vendor / Performance Patches ===")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            return

        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        patch_dir = repo_root / "patches" / f"{self.config.kernel_version}.{self.config.sub_level}"
        if not patch_dir.is_dir():
            logger.info("No vendor patches directory found, skipping.")
            return

        logger.info(f"Loading vendor patches from: {patch_dir}")
        self._chdir(common_dir)

        order_file = patch_dir / "APPLY_ORDER.txt"
        patch_list = []
        if order_file.exists():
            for raw in order_file.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                required = line.startswith("!")
                name = line[1:].strip() if required else line
                patch_list.append((name, required))
        else:
            patch_list = [(p.name, False) for p in sorted(patch_dir.glob("*.patch"))]

        applied, failed_optional = [], []
        for patch_name, required in patch_list:
            patch_path = patch_dir / patch_name
            if not patch_path.exists():
                if required:
                    raise RuntimeError(f"Required vendor patch missing: {patch_path}")
                logger.warning(f"Vendor patch file not found: {patch_path}")
                continue
            if self._apply_patch_file(patch_path, required=required, allow_fuzz=False):
                applied.append(patch_name)
            else:
                failed_optional.append(patch_name)

        logger.info(f"Vendor patches applied: {len(applied)}")
        if failed_optional:
            logger.warning(f"Optional vendor patches skipped: {failed_optional}")
        self._chdir(self.work_dir)

    def configure_kernel(self):
        logger.info("=== Configuring kernel ===")
        self._chdir(self.work_dir)
        self._require_path(self._defconfig_path(), "gki_defconfig")

        updates = dict(self.KERNEL_CONFIG_UPDATES)
        if self.config.set_default_bbr:
            updates.update(self.BBR3_CONFIG_UPDATES)
        else:
            updates.update({
                "CONFIG_TCP_CONG_ADVANCED": "y",
                "CONFIG_TCP_CONG_BBR": "y",
                "CONFIG_TCP_CONG_BBR3": "y",
                "CONFIG_TCP_CONG_WESTWOOD": "y",
                "CONFIG_NET_SCH_FQ": "y",
            })
        self._upsert_defconfig(updates)

        if self.config.use_zram:
            self._configure_zram()

        build_config = self.work_dir / "common/build.config.gki"
        if build_config.exists():
            content = build_config.read_text(encoding="utf-8")
            content = content.replace('POST_DEFCONFIG_CMDS="check_defconfig"', 'POST_DEFCONFIG_CMDS=""')
            content = content.replace("check_defconfig", "")
            build_config.write_text(content, encoding="utf-8")

    def _configure_zram(self):
        self._upsert_defconfig({
            "CONFIG_ZRAM": "y",
            "CONFIG_ZSMALLOC": "y",
            "CONFIG_CRYPTO_LZ4": "y",
            "CONFIG_CRYPTO_LZ4KD": "y",
            "CONFIG_ZRAM_WRITEBACK": "y",
            "CONFIG_ZRAM_DEF_COMP_LZ4KD": "y",
            "CONFIG_ZRAM_DEF_COMP_LZ4": "n",
            "CONFIG_ZRAM_DEF_COMP_DEFLATE": "n",
            "CONFIG_ZRAM_DEF_COMP_ZSTD": "n",
            "CONFIG_ZRAM_DEF_COMP_LZO": "n",
            "CONFIG_ZRAM_DEF_COMP_LZORLE": "n",
            "CONFIG_ZRAM_DEF_COMP_LZ4HC": "n",
            "CONFIG_ZRAM_DEF_COMP_842": "n",
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
        logger.info("=== 配置内核名称 ===")
        self._chdir(self.work_dir)
        MAX_CUSTOM_LEN = 48
        safe_custom_version = ""
        if self.config.custom_version:
            safe_custom_version = self.config.custom_version.rstrip('-')[:MAX_CUSTOM_LEN]

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

        import datetime
        current_time = datetime.datetime.utcnow().strftime("%a %b %d %H:%M:%S UTC %Y")
        mkcompile_h = self.work_dir / "common/scripts/mkcompile_h"
        if mkcompile_h.exists():
            with open(mkcompile_h, "r") as f:
                content = f.read()
            content = content.replace('UTS_VERSION="$(echo $UTS_VERSION $CONFIG_FLAGS $TIMESTAMP | cut -b -$UTS_LEN)"',
                                    f'UTS_VERSION="#1 SMP PREEMPT {current_time}"')
            with open(mkcompile_h, "w") as f:
                f.write(content)

        if self.config.custom_version:
            config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
            if config_file.exists():
                with open(config_file, "r") as f:
                    content = f.read()
                content = re.sub(r'^CONFIG_LOCALVERSION=".*"$', f'CONFIG_LOCALVERSION="{self.config.custom_version}"', content, flags=re.MULTILINE)
                with open(config_file, "w") as f:
                    f.write(content)
            else:
                logger.warning(f"配置文件不存在，跳过 custom_version 设置: {config_file}")

    def show_kernel_config(self):
        logger.info("=== 显示内核配置列表 ===")
        self._chdir(self.work_dir)
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"

        if not config_file.exists():
            logger.warning(f"配置文件不存在: {config_file}")
            return

        with open(config_file, "r") as f:
            lines = f.readlines()

        config_lines = [line.strip() for line in lines if line.strip().startswith("CONFIG_")]

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
        for prefix, name in key_configs.items():
            found = [c for c in config_lines if c.startswith(prefix)]
            if found:
                status = "enabled"
            else:
                status = "missing"
            logger.info(f" [{status}] {name}")
            if found:
                for f in sorted(found):
                    logger.info(f" -> {f}")

        if self.config.use_zram:
            zram_configs = [c for c in config_lines if any(x in c for x in ["ZRAM", "ZSMALLOC", "LZ4", "LZ4KD", "CRYPTO_LZ4", "MODULE_SIG"])]
            if zram_configs:
                logger.info("ZRAM 相关配置:")
                for zc in sorted(zram_configs):
                    logger.info(f" -> {zc}")

        logger.info("-" * 60)

    def build_kernel(self) -> bool:
        logger.info("=== 开始编译内核 ===")
        self._chdir(self.work_dir)

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

        try:
            logger.info("Starting kernel compilation with build.sh...")
            build_cmd = (
                "USE_CCACHE=1 "
                "LTO=thin "
                "BUILD_SYSTEM_DLKM=0 "
                "BUILD_GKI_ARTIFACTS=0 "
                "BUILD_GKI_CERTIFICATION_TOOLS=0 "
                "TRIM_NONLISTED_KMI=0 "
                "KMI_ENFORCED=0 "
                "INSTALL_MOD_STRIP=1 "
                "POST_DEFCONFIG_CMDS=\"\" "
                "BUILD_CONFIG=common/build.config.gki.aarch64 "
                "build/build.sh CC=\"/usr/bin/ccache clang\""
            )
            result = self._run_cmd(build_cmd, check=False)

            if result.returncode != 0:
                logger.error(f"Kernel compile failed: {result.stderr if result.stderr else f'exit {result.returncode}'}")
                return False
            image_path = self._kernel_image_path()
            if not image_path.exists():
                logger.error(f"Compile reported success but Image is missing: {image_path}")
                return False
            logger.info(f"=== Kernel compile succeeded: {image_path} ===")
            return True
        except Exception as e:
            logger.error(f"Compile error: {e}")
            return False

    def prepare_boot_images(self) -> list:
        logger.info("=== Preparing boot image ===")
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
        dest = self.work_dir / (
            f"{self.config.android_version}-{self.config.kernel_version}."
            f"{self.config.sub_level}-{self.config.os_patch_level}-boot.img"
        )
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

        zip_name = (
            f"{self.config.android_version}-{self.config.kernel_version}."
            f"{self.config.sub_level}-{self.config.os_patch_level}-AnyKernel3.zip"
        )
        zip_path = self.work_dir / zip_name
        self._chdir(ak3_dir)
        self._run_cmd(f"zip -qr '{zip_path}' ./*", check=True)
        self._run_cmd(f"rm -f {ak3_dir}/Image", check=False)
        self._chdir(self.work_dir)
        if not zip_path.exists():
            raise RuntimeError(f"AnyKernel3 zip was not created: {zip_path}")
        return [str(zip_path)]

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
            f"- Makefile version: {self._read_kernel_version()}",
            f"- SukiSU version: {self.config.kernelsu_version}",
            f"- SukiSU setup ref: {self.config.ksu_setup_ref}",
            f"- SukiSU-Ultra: `{self._git_head(self.work_dir / 'KernelSU')}`",
            f"- SUSFS: `{self._git_head(self.susfs_dir)}`",
            f"- SukiSU_patch: `{self._git_head(self.sukisu_patch_dir)}`",
            f"- AnyKernel3: `{self._git_head(self.anykernel_dir)}`",
            f"- ZRAM (LZ4KD): {'enabled' if self.config.use_zram else 'disabled'}",
            f"- BBRv3 default: {'enabled' if self.config.set_default_bbr else 'not default'}",
            f"- CONFIG_KSU_SUSFS_TRY_UMOUNT: {self.KERNEL_CONFIG_UPDATES.get('CONFIG_KSU_SUSFS_TRY_UMOUNT')}",
            f"- CONFIG_KSU_SUSFS_SUS_SU: {self.KERNEL_CONFIG_UPDATES.get('CONFIG_KSU_SUSFS_SUS_SU')}",
        ]
        if self.config.custom_version:
            lines.append(f"- Custom version: {self.config.custom_version}")
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

    def build(self) -> BuildResult:
        import time
        start_time = time.time()
        logger.info("=" * 50)
        logger.info(f"Starting GKI kernel build - {self.config.config_name}")
        logger.info("=" * 50)
        try:
            self._preflight()
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
            self.configure_kernel()
            self.configure_kernel_name()
            self.show_kernel_config()
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
