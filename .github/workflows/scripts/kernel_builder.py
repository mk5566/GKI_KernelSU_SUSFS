import os
import shutil
import subprocess
import logging
import re
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass, field
from config import (BuildConfig, KSU_REPO_CONFIG, SUSFS_REPO_CONFIG, SUKISU_PATCH_REPO_CONFIG,
                   ANYKERNEL_CONFIG, BBG_CONFIG, TOOLCHAIN_CONFIG,
                   LEGACY_FIXES, OP8E_PATCH_URL, KPM_PATCH_URL)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
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

    def run_with_callback(self, cmd: str, callback: Optional[Callable] = None) -> str:
        logger.info(f"Running: {cmd}")
        process = subprocess.Popen(cmd, shell=True, cwd=self.cwd, env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        output_lines = []
        for line in process.stdout:
            line = line.rstrip()
            output_lines.append(line)
            if callback:
                callback(line)
        process.wait()
        if process.returncode != 0:
            raise RuntimeError(f"Command failed (exit {process.returncode}): {cmd}")
        return "\n".join(output_lines)


class KernelBuilder:
    KERNEL_CONFIG_UPDATES = {
        "CONFIG_KSU": "y",
        "CONFIG_KSU_DEBUG": "n",
        "CONFIG_KSU_SUSFS": "y",
        "CONFIG_KSU_SUSFS_SUS_MAP": "y",
        "CONFIG_KSU_SUSFS_SUS_MOUNT": "y",
        "CONFIG_KSU_SUSFS_SUS_KSTAT": "y",
        "CONFIG_KSU_SUSFS_SPOOF_UNAME": "y",
        "CONFIG_KSU_SUSFS_ENABLE_LOG": "n",
        "CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS": "y",
        "CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG": "y",
        "CONFIG_KSU_SUSFS_OPEN_REDIRECT": "y",
        "CONFIG_TMPFS_XATTR": "y",
        "CONFIG_TMPFS_POSIX_ACL": "y",
        "CONFIG_IP_NF_TARGET_TTL": "y",
        "CONFIG_IP6_NF_TARGET_HL": "y",
        "CONFIG_IP6_NF_MATCH_HL": "y",
        "CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE": "y",
        "CONFIG_CC_OPTIMIZE_FOR_SIZE": None,
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
        self.env["CCACHE_COMPILERCHECK"] = "%compiler% -dumpmachine; %compiler% -dumpversion"
        self.env["CCACHE_NOHASHDIR"] = "true"
        self.env["CCACHE_HARDLINK"] = "true"
        self.env.setdefault("CCACHE_DIR", os.path.expanduser("~/.ccache"))
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
        cmd = f"git clone --filter=blob:none {url} {dest}"
        if branch:
            cmd = f"git clone --filter=blob:none -b {branch} {url} {dest}"
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

    def _apply_patch_file(self, patch_path: Path, required: bool = False) -> bool:
        def _run_patch(fuzz: int) -> subprocess.CompletedProcess:
            fuzz_arg = f"-F {fuzz} " if fuzz else ""
            return self._run_cmd(
                f"patch -p1 --forward --no-backup-if-mismatch -l {fuzz_arg}< '{patch_path}'",
                check=False,
                capture_output=True,
            )

        logger.info(f"Applying patch: {patch_path.name}")
        result = _run_patch(0)
        output = (result.stdout or "") + (result.stderr or "")
        if output.strip():
            logger.info(output.strip())
        if result.returncode == 0:
            return True
        if "Reversed (or previously applied)" in output:
            logger.info(f"Patch already applied: {patch_path.name}")
            return True

        result = _run_patch(3)
        output = (result.stdout or "") + (result.stderr or "")
        if output.strip():
            logger.info(output.strip())
        if result.returncode == 0 or "Reversed (or previously applied)" in output:
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
        if self.config.android_version in ["android12", "android13"]:
            return self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist/Image"
        return self.work_dir / "bazel-bin/common/kernel_aarch64/Image"

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
            self._run_cmd("git fetch origin", check=True)
            self._run_cmd(f"git reset --hard {self.config.susfs_commit}", check=True)
        else:
            self._run_cmd("git fetch origin", check=True)
            self._run_cmd(f"git checkout {self.config.susfs_commit}", check=True)
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
                f"-b {manifest_branch} --repo-rev=v2.16",
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
        self._run_cmd("$REPO --trace sync -c -j$(nproc --all) --no-tags --fail-fast", check=True)

        self._require_path(self.work_dir / "common", "kernel common/ directory after repo sync")
        kernel_ver = self._read_kernel_version()
        logger.info(f"Synced kernel version: {kernel_ver}")
        expected = f"{self.config.kernel_version}.{self.config.sub_level}"
        if self.config.sub_level != "X" and kernel_ver != expected:
            raise RuntimeError(
                f"Synced kernel {kernel_ver} does not match requested {expected} "
                f"(branch {formatted_branch})"
            )
        self._apply_legacy_fixes(remote)
        logger.info("=== Kernel source sync complete ===")

    def _apply_legacy_fixes(self, remote_branch: str = ""):
        av, kv = self.config.android_version, self.config.kernel_version
        sub = self.config.get_sub_level_int()
        is_deprecated = "deprecated" in remote_branch

        if is_deprecated and av == "android13" and kv == "5.15" and sub and sub < 123:
            common_dir = self.work_dir / "common"
            self._chdir(common_dir)
            self._run_cmd(f"curl -LSs {LEGACY_FIXES['android13-5.15-below-123']['url']} -o fix.patch && patch -p1 < fix.patch", check=False)
            self._chdir(self.work_dir)

        if av == "android12" and kv == "5.10" and sub and sub < 136:
            common_dir = self.work_dir / "common"
            self._chdir(common_dir)
            self._run_cmd(f"curl -LSs {LEGACY_FIXES['android12-5.10-below-136']['url']} | patch -p1", check=False)
            self._chdir(self.work_dir)

    def add_kernel_supatch(self):
        if not self.config.support_op8e:
            return
        logger.info("=== 添加 OnePlus 8E 支持补丁 ===")
        drivers_dir = self.work_dir / "common/drivers"
        if not drivers_dir.exists():
            return
        self._chdir(drivers_dir)
        self._run_cmd(f"curl -LSs {OP8E_PATCH_URL} -o hmbird_patch.c", check=False)
        if (drivers_dir / "hmbird_patch.c").exists():
            with open(drivers_dir / "Makefile", "a") as f:
                f.write("obj-y += hmbird_patch.o\n")

    def add_kernelsu(self):
        logger.info("=== Adding KernelSU ===")
        self._chdir(self.work_dir)
        setup_ref = self.config.kernelsu_commit or self.config.ksu_setup_ref
        setup_url = (
            f"https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/{setup_ref}/kernel/setup.sh"
            if self.config.kernelsu_commit else KSU_REPO_CONFIG["setup_script"]
        )
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

    def add_bbg(self):
        if not self.config.use_bbg:
            return
        logger.info("=== 添加 Baseband-guard ===")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            return
        self._chdir(common_dir)
        self._run_cmd(f"wget -O- {BBG_CONFIG['setup_script']} | bash", check=False)
        config_file = common_dir / "arch/arm64/configs/gki_defconfig"
        if config_file.exists():
            with open(config_file, "a") as f:
                f.write("CONFIG_BBG=y\n")
        kconfig_file = common_dir / "security/Kconfig"
        if kconfig_file.exists():
            with open(kconfig_file, "r") as f:
                content = f.read()
            content = re.sub(r'(config LSM.*?)(default .*)(\n.*?help)',
                           lambda m: m.group(1) + ('lockdown,baseband_guard' if 'lockdown' in m.group(2) and 'baseband_guard' not in m.group(2) else m.group(2)) + m.group(3),
                           content, flags=re.DOTALL)
            with open(kconfig_file, "w") as f:
                f.write(content)

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
        hooks_patch = self.sukisu_patch_dir / "69_hide_stuff.patch"
        if hooks_patch.exists():
            self._apply_patch_file(hooks_patch, required=False)
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

        self._run_cmd(
            f"patch -p1 -F 3 < {lz4kd_patch}",
            check=True,
        )

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

    def apply_task_mmu_fixes(self):
        logger.info("=== 应用 task_mmu.c 修复 ===")
        self._chdir(self.work_dir / "common")
        task_mmu = Path("fs/proc/task_mmu.c")
        if not task_mmu.exists():
            return

        fb = f"{self.config.android_version}-{self.config.kernel_version}"

        with open(task_mmu, "r") as f:
            content = f.read()

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
            with open(task_mmu, "w") as f:
                f.write(content)

        # Fix uninitialized dentry introduced by 69_hide_stuff.patch.
        if fb == "android13-5.15":
            old_declaration = "struct dentry *dentry;"
            new_declaration = "struct dentry *dentry = NULL;"

            if old_declaration in content:
                content = content.replace(
                    old_declaration,
                    new_declaration,
                    1,
                )
                with open(task_mmu, "w") as f:
                    f.write(content)

        if fb == "android15-6.6" and "unsigned int nr_subpages" not in content:
            self._fix_base_c_header()
        elif fb == "android14-6.1" and "if (!vma_pages(vma))" not in content:
            self._fix_base_c_header()
            if "goto show_pad;" in content:
                content = content.replace("goto show_pad;", "return 0;")
                with open(task_mmu, "w") as f:
                    f.write(content)
        elif fb in ["android12-5.10", "android13-5.10", "android13-5.15"] and "if (!vma_pages(vma))" not in content:
            if "goto show_pad;" in content:
                content = content.replace("goto show_pad;", "return 0;")
                with open(task_mmu, "w") as f:
                    f.write(content)

    def _fix_base_c_header(self):
        base_c = self.work_dir / "common/fs/proc/base.c"
        if not base_c.exists():
            return
        with open(base_c, "r") as f:
            content = f.read()
        if "#include <linux/dma-buf.h>" not in content:
            content = content.replace("#include <linux/cpufreq_times.h>",
                                    "#include <linux/cpufreq_times.h>\n#include <linux/dma-buf.h>")
            with open(base_c, "w") as f:
                f.write(content)

    def apply_vendor_patches(self):
        logger.info("=== Applying Vendor / Performance Patches ===")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            return

        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        candidate_dirs = [
            repo_root / "patches" / f"{self.config.kernel_version}.{self.config.sub_level}",
            repo_root / "patches" / self.config.sub_level,
            repo_root / "patches" / self.config.kernel_version,
            repo_root / "patches",
        ]

        patch_dir = None
        for cand in candidate_dirs:
            if cand.exists() and cand.is_dir():
                patch_dir = cand
                break

        if not patch_dir:
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
            if self._apply_patch_file(patch_path, required=required):
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
        updates["CONFIG_KPM"] = "y" if self.config.use_kpm else "n"
        updates["CONFIG_KSU_SUSFS_SUS_PATH"] = "n" if self.config.kernel_version == "6.6" else "y"
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
            self._configure_bazel()

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

    def _configure_bazel(self):
        modules_bzl = self.work_dir / "common/modules.bzl"
        if modules_bzl.exists():
            content = modules_bzl.read_text(encoding="utf-8")
            modified = False
            for old in ['"drivers/block/zram/zram.ko",\n', '"drivers/block/zram/zram.ko",',
                       '"mm/zsmalloc.ko",\n', '"mm/zsmalloc.ko",']:
                if old in content:
                    content = content.replace(old, '')
                    modified = True
            if modified:
                modules_bzl.write_text(content, encoding="utf-8")
        self._upsert_defconfig({"CONFIG_MODULE_SIG_FORCE": "n"})

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

        if self.config.kernel_version in ["6.1", "6.6"]:
            init_makefile = self.work_dir / "common/init/Makefile"
            if init_makefile.exists():
                with open(init_makefile, "r") as f:
                    content = f.read()
                content = content.replace('$(preempt-flag-y) "$(build-timestamp)"', f'$(preempt-flag-y) "{current_time}"')
                with open(init_makefile, "w") as f:
                    f.write(content)

        if not (self.work_dir / "build/build.sh").exists():
            bazel_build = self.work_dir / "common/BUILD.bazel"
            if bazel_build.exists():
                with open(bazel_build, "r") as f:
                    content = f.read()
                lines = [l for l in content.split('\n') if '"protected_exports_list"' not in l or 'android/abi_gki_protected_exports_aarch64' not in l]
                with open(bazel_build, "w") as f:
                    f.write('\n'.join(lines))

            abi_path = self.work_dir / "common/android/abi_gki_protected_exports_aarch64"
            if abi_path.exists():
                import shutil
                try:
                    if abi_path.is_dir():
                        shutil.rmtree(abi_path)
                    else:
                        abi_path.unlink()
                except Exception:
                    pass

            stamp_bzl = self.work_dir / "build/kernel/kleaf/impl/stamp.bzl"
            if stamp_bzl.exists():
                with open(stamp_bzl, "r") as f:
                    content = f.read()
                content = content.replace("-maybe-dirty", "")
                with open(stamp_bzl, "w") as f:
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
            "CONFIG_KPM": "KPM",
            "CONFIG_KSU_SUSFS": "SUSFS",
            "CONFIG_BBG": "Baseband-guard",
            "CONFIG_TCP_CONG_BBR3": "BBRv3",
            "CONFIG_DEFAULT_TCP_CONG": "Default TCP cong",
            "CONFIG_ZRAM": "ZRAM",
            "CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE": "Optimize for performance",
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
            if (self.work_dir / "build/build.sh").exists():
                logger.info("使用旧版构建方式...")
                build_cmd = (
                    "LTO=thin "
                    "BUILD_SYSTEM_DLKM=0 "
                    "BUILD_GKI_ARTIFACTS=0 "
                    "BUILD_GKI_CERTIFICATION_TOOLS=0 "
                    "TRIM_NONLISTED_KMI=0 "
                    "KMI_ENFORCED=0 "
                    "POST_DEFCONFIG_CMDS=\"\" "
                    "BUILD_CONFIG=common/build.config.gki.aarch64 "
                    "build/build.sh CC=\"/usr/bin/ccache clang\""
                )
                result = self._run_cmd(build_cmd, check=False)
            else:
                logger.info("使用 Bazel 构建方式...")
                result = self._run_cmd("tools/bazel build --disk_cache=/home/runner/.cache/bazel --config=fast --lto=thin //common:kernel_aarch64_dist", check=False)

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

    def patch_kpm_image(self):
        if not self.config.use_kpm or self.config.kernel_version == "6.6":
            return
        logger.info("=== 修补 Image 文件 (KPM) ===")
        self._chdir(self.work_dir)
        if self.config.android_version in ["android12", "android13"]:
            image_dir = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist"
        else:
            image_dir = self.work_dir / "bazel-bin/common/kernel_aarch64"
        if not image_dir.exists():
            return
        self._chdir(image_dir)
        self._run_cmd(f"curl -LSs {KPM_PATCH_URL} -o patch && chmod 777 patch && ./patch", check=False)
        if (image_dir / "oImage").exists():
            self._run_cmd("mv oImage Image", check=False)

    def prepare_boot_images(self) -> list:
        logger.info("=== 准备启动镜像 ===")
        self._chdir(self.work_dir)
        bootimgs_dir = self.work_dir / "bootimgs"
        bootimgs_dir.mkdir(exist_ok=True)
        artifacts = []

        if self.config.android_version in ["android12", "android13"]:
            image_source = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist"
        else:
            image_source = self.work_dir / "bazel-bin/common/kernel_aarch64"

        for image_name in ["Image"]:
            src = image_source / image_name
            if src.exists():
                self._run_cmd(f"cp {src} {bootimgs_dir}/ && cp {src} {self.work_dir}/", check=False)

        if self.config.android_version == "android12":
            self._prepare_android12_boot_images(bootimgs_dir, artifacts)
        else:
            self._prepare_boot_images_generic(bootimgs_dir, artifacts)
        return artifacts

    def _prepare_android12_boot_images(self, bootimgs_dir: Path, artifacts: list):
        self._chdir(bootimgs_dir)
        gki_url = f"https://dl.google.com/android/gki/gki-certified-boot-android12-5.10-{self.config.os_patch_level}_{self.config.revision}.zip"
        fallback_url = "https://dl.google.com/android/gki/gki-certified-boot-android12-5.10-2023-01_r1.zip"
        result = subprocess.run(f"curl -sL -w '%{{http_code}}' {gki_url} -o /dev/null", shell=True, capture_output=True, text=True)
        url = gki_url if "200" in result.stdout else fallback_url
        self._run_cmd(f"curl -Lo gki-kernel.zip {url} && unzip -o gki-kernel.zip && rm gki-kernel.zip", check=False)
        boot_img_path = bootimgs_dir / "boot-5.10.img"
        if boot_img_path.exists():
            self._run_cmd(f"$UNPACK_BOOTIMG --boot_img={boot_img_path}", check=False)
        self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=True)

    def _prepare_boot_images_generic(self, bootimgs_dir: Path, artifacts: list):
        self._chdir(bootimgs_dir)
        self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=False)

    def _create_boot_image_variants(self, bootimgs_dir: Path, artifacts: list, has_ramdisk: bool = False):
        self._chdir(bootimgs_dir)

        for kernel_file, output_file in [("Image", "boot.img")]:
            kernel_path = bootimgs_dir / kernel_file
            if not kernel_path.exists():
                continue
            cmd = f"$MKBOOTIMG --header_version 4 --kernel {kernel_file} --output {output_file}"
            if has_ramdisk:
                cmd += f" --ramdisk out/ramdisk --os_version 12.0.0 --os_patch_level {self.config.os_patch_level}"
            self._run_cmd(cmd, check=False)
            self._run_cmd(f"$AVBTOOL add_hash_footer --partition_name boot --partition_size $((64 * 1024 * 1024)) --image {output_file} --algorithm SHA256_RSA2048 --key $BOOT_SIGN_KEY_PATH", check=False)
            dest = self.work_dir / f"{self.config.android_version}-{self.config.kernel_version}.{self.config.sub_level}-{self.config.os_patch_level}-{output_file}"
            self._run_cmd(f"cp {output_file} {dest}", check=False)
            artifacts.append(str(dest))

    def create_anykernel_zips(self) -> list:
        logger.info("=== 创建 AnyKernel3 ZIP 文件 ===")
        self._chdir(self.work_dir)
        artifacts = []
        ak3_dir = self.anykernel_dir

        image_src = self._kernel_image_path()
        self._require_path(image_src, "compiled kernel Image")
        self._run_cmd(f"cp {image_src} {self.work_dir}/Image", check=True)

        for suffix in [""]:
            image_file = f"Image{suffix}"
            image_path = self.work_dir / image_file
            if not image_path.exists():
                continue
            zip_name = f"{self.config.android_version}-{self.config.kernel_version}.{self.config.sub_level}-{self.config.os_patch_level}-AnyKernel3{suffix}.zip"
            self._run_cmd(f"cp {image_path} {ak3_dir}/", check=True)
            self._chdir(ak3_dir)
            zip_path = self.work_dir / zip_name
            self._run_cmd(f"zip -r '{zip_path}' ./*", check=True)
            self._run_cmd(f"rm -f {ak3_dir}/{image_file}", check=False)
            if not zip_path.exists():
                raise RuntimeError(f"AnyKernel3 zip was not created: {zip_path}")
            artifacts.append(str(zip_path))
            self._chdir(self.work_dir)
        if not artifacts:
            raise RuntimeError("No AnyKernel3 zip was produced")
        return artifacts

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
            self.add_kernel_supatch()
            self.add_kernelsu()
            self.add_bbg()
            self.apply_susfs_patches()
            self.apply_sukisu_patches()
            self.apply_zram_patches()
            self.apply_task_mmu_fixes()
            self.apply_vendor_patches()
            self.configure_kernel()
            self.configure_kernel_name()
            self.show_kernel_config()
            if not self.build_kernel():
                return BuildResult(success=False, config=self.config, message="Kernel compile failed", build_time=time.time() - start_time)
            self.patch_kpm_image()
            artifacts = []
            artifacts.extend(self.create_anykernel_zips())
            try:
                artifacts.extend(self.prepare_boot_images())
            except Exception as boot_err:
                logger.warning(f"boot.img packaging failed (AnyKernel3 zip is still valid): {boot_err}")
            build_time = time.time() - start_time
            logger.info(f"Build succeeded in {build_time:.2f}s, {len(artifacts)} artifact(s)")
            return BuildResult(success=True, config=self.config, message="Build succeeded", artifacts=artifacts, build_time=build_time)
        except Exception as e:
            logger.exception(f"Build failed: {e}")
            return BuildResult(success=False, config=self.config, message=str(e), build_time=time.time() - start_time)
