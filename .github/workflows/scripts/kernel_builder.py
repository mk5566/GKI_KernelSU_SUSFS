import os
import shutil
import subprocess
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from dataclasses import dataclass, field
from config import (ANDROID_FAMILY, BuildConfig, KSU_REPO_CONFIG, SUSFS_REPO_CONFIG,
                   SUKISU_PATCH_REPO_CONFIG, ANYKERNEL_CONFIG, REPO_TOOL_CONFIG, REPO_ROOT,
                   REPO_SYNC_PROJECTS)
from patch_plan import make_patch_plan
from kernel_profile import KernelProfile
from abi import verify_gki_abi
from repack_boot import repack_boot
from evidence import collect_evidence
from source_lock import write_sources
from upstream_policy import validate_upstream_status
from integrity import sha256, atomic_json
from build_runtime import ShellCommand, log_excerpt, validate_build_environment
from config_checks import ConfigPolicy, config_delta, unquote_kconfig_string

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
    artifacts: list[str] = field(default_factory=list)
    build_time: float | None = None


class KernelBuilder(KernelProfile):
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
        self.patch_plan = make_patch_plan()
        self.patch_records = []
        self.phase_timings = {}
        self.applied_bbr_patches = []
        self.applied_tweak_patches = []
        self.applied_integration_patches = []
        self.abi_symbol_count = 0
        self.release_suffix = ""
        self.config_checks = {}
        self.compiler_version = ""
        self.build_error_excerpt = ""
        self._verified_checkouts = {}
        self._setup_env()

    def _setup_env(self):
        self.env["CONFIG"] = self.config.config_name
        self.env.setdefault("GIT_TERMINAL_PROMPT", "0")
        self.shell.env = self.env

    def _run_cmd(self, cmd: str | Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
        return self.shell.run(cmd, **kwargs)

    def _chdir(self, path: Path):
        self.shell.cwd = str(path)

    def _preflight(self):
        self.config.validate()
        validate_upstream_status(self.config)
        validate_build_environment(self.env)
        if self.workspace == REPO_ROOT or REPO_ROOT in self.workspace.parents:
            raise RuntimeError("Build workspace must be outside the source checkout")
        stale = [name for name in (
            "boot.img", "Image", "AnyKernel3.zip", "BUILD_INFO.md", "build.log",
            "preflight.log", "precompile.config", "CONFIG_REPORT.json", "CONFIG_DIFF.json",
            "PATCH_REPORT.json", "BUILD_TIMINGS.json", "checkouts.json", "GKI-reference.symvers",
            "susfs4ksu", "SukiSU_patch", "AnyKernel3", "git-repo",
        ) if (self.workspace / name).exists() or (self.workspace / name).is_symlink()]
        if stale:
            raise RuntimeError("Use a fresh workspace; preserving existing files: " + ", ".join(stale))
        if (self.workspace / "evidence").exists():
            raise RuntimeError("Use a fresh workspace; previous successful evidence must be preserved")
        if any(self.work_dir.iterdir()):
            raise RuntimeError("Use a fresh workspace; existing kernel sources and outputs are preserved")
        missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
        if missing:
            raise RuntimeError(f"Missing required tools: {', '.join(missing)}")

    def _ensure_git_identity(self):
        # repo init needs an identity, not a permanent global Git mutation or a
        # safe.directory='*' exemption. Append process-scoped config entries.
        count = int(self.env.get("GIT_CONFIG_COUNT", "0"))
        for key, value in (("user.name", "GKI Builder"), ("user.email", "gki-builder@localhost")):
            self.env[f"GIT_CONFIG_KEY_{count}"] = key
            self.env[f"GIT_CONFIG_VALUE_{count}"] = value
            count += 1
        self.env["GIT_CONFIG_COUNT"] = str(count)
        self.shell.env = self.env

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
            cwd=self.shell.cwd, env=self.env, capture_output=True, text=True)
        if result.returncode:
            context = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"Selected patch failed: {patch_path.name}\n{context}")
        self.applied_integration_patches.append(patch_path.name)
        resolved = patch_path.resolve()
        anchor = REPO_ROOT if resolved.is_relative_to(REPO_ROOT) else self.workspace
        self.patch_records.append({
            "sequence": len(self.patch_records) + 1,
            "path": resolved.relative_to(anchor).as_posix(),
            "origin": "builder" if anchor == REPO_ROOT else "workspace",
            "sha256": sha256(resolved),
            "target": Path(self.shell.cwd).relative_to(self.workspace).as_posix(),
        })
        atomic_json(self.workspace / "PATCH_REPORT.json", {"applied": self.patch_records})
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
        logger.info("=== Boot tools ready ===")

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
            [self.env["REPO"], "init", "--depth=1", "-u",
             "https://android.googlesource.com/kernel/manifest", "-b", self.config.manifest_commit,
             f"--repo-rev={self.config.repo_commit}", "--no-clone-bundle"], check=True)
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
        self._run_cmd([self.env["REPO"], "sync", "-c", "-j4", "--no-tags", "--fail-fast",
                       "--no-clone-bundle", *REPO_SYNC_PROJECTS], check=True)

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

    def verify_checkouts(self, include_helpers=True, reuse_verified=True):
        """Compare every required tree with the revision this run resolved.

        `reuse_verified` skips re-reading revisions this same run already
        checked out. The final audit before compilation must be
        authoritative, so it passes reuse_verified=False: a later checkout
        change must not hide behind an in-memory record that was taken
        before the source stack was integrated.
        """
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
            head = self._verified_checkouts.get(directory) if reuse_verified else None
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

    def _wire_kernelsu_driver(self, ksu_dir: Path):
        """Link KernelSU/kernel into common/drivers and register it in Kbuild/Kconfig."""
        ksu_kernel = ksu_dir / "kernel"
        if not ksu_kernel.is_dir():
            raise RuntimeError(f"KernelSU kernel directory not found: {ksu_kernel}")
        drivers_dir = self.work_dir / "common/drivers"
        drivers_makefile = drivers_dir / "Makefile"
        drivers_kconfig = drivers_dir / "Kconfig"
        self._require_path(drivers_dir, "kernel drivers directory")
        self._require_path(drivers_makefile, "kernel drivers/Makefile")
        self._require_path(drivers_kconfig, "kernel drivers/Kconfig")

        symlink_path = drivers_dir / "kernelsu"
        if symlink_path.is_symlink():
            symlink_path.unlink()
        rel_target = os.path.relpath(ksu_kernel, drivers_dir).replace("\\", "/")
        os.symlink(rel_target, symlink_path, target_is_directory=True)

        makefile_text = drivers_makefile.read_text(encoding="utf-8")
        if "kernelsu" not in makefile_text:
            suffix = "\nobj-$(CONFIG_KSU) += kernelsu/\n"
            drivers_makefile.write_text(makefile_text.rstrip("\n") + "\n" + suffix,
                                        encoding="utf-8", newline="\n")

        kconfig_text = drivers_kconfig.read_text(encoding="utf-8")
        kconfig_entry = 'source "drivers/kernelsu/Kconfig"'
        if kconfig_entry not in kconfig_text:
            lines = kconfig_text.splitlines()
            end_indices = [idx for idx, line in enumerate(lines) if line.strip() == "endmenu"]
            if not end_indices:
                raise RuntimeError(f"Cannot insert KernelSU Kconfig: missing endmenu in {drivers_kconfig}")
            lines.insert(end_indices[-1], kconfig_entry)
            drivers_kconfig.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def add_kernelsu(self):
        logger.info("=== Adding KernelSU ===")
        self._chdir(self.work_dir)
        ksu_dir = self.work_dir / "KernelSU"
        # Clone directly from KSU_REPO_CONFIG at the resolved commit rather than
        # running upstream setup.sh, which performs a mutable git pull and
        # runs `git pull` on detached checkouts.
        self._clone_source("SukiSU", ksu_dir, KSU_REPO_CONFIG["repo_url"], self.config.sukisu_commit)
        self._wire_kernelsu_driver(ksu_dir)
        self._require_path(self.work_dir / "common/drivers/kernelsu", "KernelSU driver symlink")
        self._require_path(ksu_dir, "KernelSU checkout")
        uapi = self._read_ksu_uapi_version()
        logger.info("SukiSU kernel UAPI: %s; the manager must use the same UAPI",
                    uapi if uapi is not None else "unset")

        # The `builtin` branch already integrates SUSFS on the KernelSU side,
        # so only production glue is applied here: the quiet log patch.
        # Upstream natively carries the priority uid filter Kconfig option and
        # guards (issue #1045, commit de9bc7e).
        # Filesystem support comes from the full upstream SUSFS kernel patch
        # applied in apply_susfs_patches.
        self._chdir(ksu_dir)
        self._apply_patch_file(REPO_ROOT / "patches/susfs/0001-sukisu-quiet-production.patch")
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
        # Apply the complete upstream filesystem patch. Slicing it down to a
        # file subset breaks cross-file consistency with the KernelSU-side
        # integration and with future SUSFS revisions; mount-only remains a
        # Kconfig selection (CONFIG_KSU_SUSFS_SUS_MOUNT=y, others off).
        # The trailing glue patches keep the mount-only build warning-free:
        # 1. Provide empty fallback macros for SUSFS_LOGI_RL/SUSFS_LOGE_RL when logging is off
        # 2. Mark helpers maybe-unused when priority uid filter is off
        self._chdir(common_dir)
        self._apply_patch_file(susfs_patch)
        self._apply_patch_file(REPO_ROOT / "patches/susfs/0002-susfs-ratelimit-log-fallback.patch")
        self._apply_patch_file(REPO_ROOT / "patches/susfs/0003-susfs-no-priority-uid-filter.patch")
        self._chdir(self.work_dir)

    def apply_zram_patches(self):
        logger.info("=== Applying ZRAM (LZ4KD) patches ===")
        common = self.work_dir / "common"
        self._chdir(common)

        # Ensure the original kernel Kconfig has not been corrupted.
        lib_kconfig = (common / "lib/Kconfig")
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
                (common / "include/linux/lz4kd.h"),
            ),
            (
                self.sukisu_patch_dir / "other/zram/lz4k/lib/lz4kd",
                (common / "lib/lz4kd"),
            ),
            (
                self.sukisu_patch_dir / "other/zram/lz4k/crypto/lz4kd.c",
                (common / "crypto/lz4kd.c"),
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
        for patch in self.patch_plan.zram:
            self._apply_patch_file(patch)
        self._verify_zram_backends()

        # Verify that the patch was applied and the kernel Kconfig survived.
        required_markers = {
            (common / "lib/Kconfig"): [
                "config ASSOCIATIVE_ARRAY",
                "config LZ4KD_COMPRESS",
            ],
            (common / "lib/Makefile"): [
                "CONFIG_LZ4KD_COMPRESS",
            ],
            (common / "crypto/Kconfig"): [
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
        plan = self.patch_plan
        shutil.copytree(REPO_ROOT / "patches/lz4/source", common_dir / "crypto/lz4_111",
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
            "CONFIG_ZRAM_DEF_COMP_LZ4": "y",
            "CONFIG_ZRAM_DEF_COMP_LZ4KD": "n",
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
                filtered = [line for line in lines if not (
                    line.strip() and not line.lstrip().startswith("#")
                    and Path(line.strip()).name in {"zram.ko", "zsmalloc.ko"})]
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

        key_configs = self.CONFIG_SUMMARY_LABELS

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
            raise RuntimeError("BBRv3 changed the frozen 104-byte icsk_ca_priv layout")

    # Compatibility wrappers keep callers/tests independent of policy location.
    _parse_config = staticmethod(ConfigPolicy.parse)
    _verify_runtime_config = staticmethod(ConfigPolicy._verify_runtime_config)
    _verify_susfs_config = staticmethod(ConfigPolicy._verify_susfs_config)

    def _verify_generated_config(self, path: Path, stage: str | None = None):
        report = ConfigPolicy.verify(path)
        if stage:
            if stage not in ("precompile", "final"):
                raise ValueError(f"Unknown config verification stage: {stage}")
            snapshot = self.workspace / "precompile.config"
            if stage == "precompile":
                shutil.copy2(path, snapshot)
            elif snapshot.is_file():
                atomic_json(self.workspace / "CONFIG_DIFF.json", config_delta(snapshot, path))
            self.config_checks[stage] = report
            atomic_json(self.workspace / "CONFIG_REPORT.json", self.config_checks)
        logger.info("Generated .config verified: %s", stage or "unrecorded check")

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

    BUILD_LOG_NAME = "build.log"

    def _build_log_path(self) -> Path:
        return self.workspace / self.BUILD_LOG_NAME

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
                                 "O=\"${OUT_DIR}\" \"${TOOL_ARGS[@]}\" olddefconfig "
                                 "|| exit $?; exit 0'\n",
                                 encoding="utf-8", newline="\n")
            env["GKI_BUILD_CONFIG_FRAGMENT"] = str(preflight)
        # Stream build output line-by-line to build.log and the logger so the
        # compile log is never buffered in RAM during full LTO and progress is
        # visible in real time.
        cmd = ["bash", "build/build.sh"]
        log_path = self.workspace / "preflight.log" if config_only else self._build_log_path()
        with log_path.open("w", encoding="utf-8", buffering=1) as log_file, \
                subprocess.Popen(
                    cmd, cwd=self.work_dir, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                ) as proc:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\r\n")
                log_file.write(line + "\n")
                logger.info("[build.sh] %s", line)
            returncode = proc.wait()
        if config_only and returncode != 0:
            raise subprocess.CalledProcessError(returncode, cmd, output=log_excerpt(log_path), stderr="")
        return subprocess.CompletedProcess(cmd, returncode, "", "")

    def build_kernel(self) -> bool:
        logger.info("=== Starting kernel compile ===")
        self._chdir(self.work_dir)
        self.build_error_excerpt = ""
        try:
            logger.info("Starting kernel compilation with build.sh...")
            result = self._run_kernel_build()

            if result.returncode != 0:
                self.build_error_excerpt = log_excerpt(self._build_log_path())
                logger.error("Kernel compile failed: exit %d\n%s",
                             result.returncode, self.build_error_excerpt)
                return False
            image_path = self._kernel_image_path()
            if not image_path.exists():
                raise RuntimeError(f"Compile reported success but Image is missing: {image_path}")
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
            self.build_error_excerpt = str(e)[:6000]
            logger.exception("Compile or post-compile verification failed: %s", e)
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

    def prepare_boot_images(self) -> list:
        logger.info("=== Preparing boot image ===")
        self.configure_boot_tools()
        self._chdir(self.work_dir)

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

    def create_anykernel_zips(self) -> list[str]:
        logger.info("=== Creating AnyKernel3 zip ===")
        image_src = self._kernel_image_path()
        self._require_path(image_src, "compiled kernel Image")
        zip_path = self.workspace / "AnyKernel3.zip"
        staged = self.anykernel_dir / "Image"
        if zip_path.exists() or zip_path.is_symlink() or staged.exists() or staged.is_symlink():
            raise RuntimeError("Refusing to merge a stale AnyKernel3 archive or overwrite its Image")
        shutil.copy2(image_src, staged)
        try:
            subprocess.run(["zip", "-qr", str(zip_path), ".", "-x", ".git", "-x", ".git/*",
                            "-x", "*/.git/*"], cwd=self.anykernel_dir, env=self.env, check=True)
        finally:
            staged.unlink(missing_ok=True)
        if not zip_path.is_file() or not zip_path.stat().st_size:
            raise RuntimeError(f"AnyKernel3 zip was not created: {zip_path}")
        return [str(zip_path)]

    def _read_ksu_uapi_version(self) -> int | None:
        # Older layouts declare the version with DECLARE() in kernel/include
        # or as a C constant in uapi/. Nova builtin currently declares UAPI 6;
        # historical unversioned trees are reported as "unset", never spoofed.
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
            image_sha = sha256(image)
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
            f"- SukiSU tag: {self.config.sukisu_tag or 'none (exact source commit)'}",
            f"- Source selection policy: {self.config.upstream_status.get('policy', 'not resolved')}",
            f"- Upstream resolution completed (UTC): {self.config.upstream_status.get('checked_at_utc', 'not resolved')}",
            f"- Explicit commit overrides: {', '.join(self.config.upstream_status.get('explicit_commit_overrides', [])) or 'none'}",
            f"- SukiSU exact commit: `{self.config.sukisu_commit}`",
            f"- SukiSU checkout: `{self._git_head(self.work_dir / 'KernelSU')}`",
            f"- SukiSU kernel UAPI: {self._read_ksu_uapi_version()}",
            f"- SUSFS exact commit: `{self.config.susfs_commit}`",
            f"- LZ4KD helper source commit: `{self.config.sukisu_patch_commit}`",
            f"- AnyKernel3 exact commit: `{self.config.anykernel_commit}`",
            "- TCP: BBRv3 default, stock BBRv1 built in, Reno retained; optional alternatives off",
            "- Multi-gen LRU: compiled default-on; statistics off (runtime policy not tested)",
            "- RCU: certified lazy-off default; no CPU is callback-offloaded",
            "- CPU idle: TEO configured default, menu built in (runtime selection not tested)",
            "- FQ qdisc: built in; runtime interface qdisc selection is not enforced",
            f"- Applied BBR patches: {', '.join(self.applied_bbr_patches) or 'none'}",
            f"- Applied tweak patches: {', '.join(self.applied_tweak_patches) or 'none'}",
            f"- Other integration patches: {', '.join(self.applied_integration_patches)}",
            "- ZRAM: lz4 default, lz4kd available, other backends removed",
            "- ZRAM crypto LZ4: official 1.11.1, private freestanding implementation",
            "- Exported GKI LZ4 library/header: unchanged",
            "- KPM: disabled",
            "- cpufreq governors and I/O schedulers: certified GKI defaults preserved",
            f"- Certified GKI symbol CRCs matched: {self.abi_symbol_count}",
            "- CRC scope: frozen export presence, CRCs, type and namespace; not complete binary KMI or device validation",
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

    def _phase(self, name, operation):
        start = time.monotonic()
        status = "failed"
        try:
            result = operation()
            status = "failed" if result is False else "passed"
            return result
        finally:
            self.phase_timings[name] = {
                "status": status, "elapsed_seconds": round(time.monotonic() - start, 6)}
            atomic_json(self.workspace / "BUILD_TIMINGS.json", self.phase_timings)

    def build(self, preflight_only: bool = False) -> BuildResult:
        start_time = time.monotonic()
        admitted = False
        lock_owned = False
        lock = self.workspace / ".build.lock"
        logger.info("=" * 50)
        logger.info(f"Starting GKI kernel build - {self.config.config_name}")
        logger.info("=" * 50)
        try:
            # Exclusive creation also protects two local processes using one workspace.
            with lock.open("x", encoding="utf-8") as stream:
                lock_owned = True
                stream.write(f"pid={os.getpid()}\n")
            self._preflight()
            admitted = True
            write_sources(self.config, self.workspace)
            (self.workspace / "artifact_stem.txt").write_text(self.config.artifact_stem + "\n", encoding="utf-8")
            self._phase("clone_repositories", self.clone_repositories)
            self._phase("setup_repo_tool", self.setup_repo_tool)
            self._phase("init_and_sync_kernel", self.init_and_sync_kernel)
            self._phase("configure_kernel_toolchain", self.configure_kernel_toolchain)
            self._phase("add_kernelsu", self.add_kernelsu)
            self._phase("apply_susfs_patches", self.apply_susfs_patches)
            self._phase("apply_feature_patches", self.apply_feature_patches)
            self._phase("apply_zram_patches", self.apply_zram_patches)
            self._phase("configure_kernel", self.configure_kernel)
            self._phase("configure_kernel_name", self.configure_kernel_name)
            self.show_kernel_config()
            self._phase("configure_build_fragment", self.configure_build_fragment)
            self._phase("verify_source_mode", self._verify_source_mode)
            atomic_json(self.workspace / "checkouts.json", self.verify_checkouts(reuse_verified=False))
            logger.info("Patch report: GKI %s %s; SukiSU %s; SUSFS %s; BBR %s; tweaks %s; integration count=%d",
                        self.config.kernel_version, self.config.gki_commit, self.config.sukisu_commit,
                        self.config.susfs_commit, self.applied_bbr_patches, self.applied_tweak_patches,
                        len(self.applied_integration_patches))
            self._phase("generate_and_verify_config", self.generate_and_verify_config)
            if preflight_only:
                info = self.write_build_info(success=True, message="Source and generated config preflight passed")
                return BuildResult(success=True, config=self.config, message="Preflight passed",
                                   artifacts=[str(info)], build_time=time.monotonic() - start_time)
            if not self._phase("compile_and_crc", self.build_kernel):
                build_time = time.monotonic() - start_time
                message = "Kernel compile failed"
                if self.build_error_excerpt:
                    message += "\n" + self.build_error_excerpt
                self.write_build_info(build_time=build_time, success=False, message=message)
                return BuildResult(success=False, config=self.config, message=message, build_time=build_time)
            artifacts = []
            artifacts.extend(self._phase("package_anykernel", self.create_anykernel_zips))
            artifacts.extend(self._phase("package_boot", self.prepare_boot_images))
            build_time = time.monotonic() - start_time
            self.write_build_info(artifacts=artifacts, build_time=build_time, success=True)
            evidence = collect_evidence(self.workspace, self.config.config_name, self.abi_symbol_count)
            artifacts.append(str(evidence))
            logger.info(f"Build succeeded in {build_time:.2f}s, {len(artifacts)} artifact(s)")
            return BuildResult(success=True, config=self.config, message="Build succeeded", artifacts=artifacts, build_time=build_time)
        except Exception as e:
            logger.exception(f"Build failed: {e}")
            try:
                if admitted:
                    self.write_build_info(build_time=time.monotonic() - start_time, success=False, message=str(e))
            except Exception:
                pass
            return BuildResult(success=False, config=self.config, message=str(e), build_time=time.monotonic() - start_time)
        finally:
            if lock_owned:
                lock.unlink(missing_ok=True)
