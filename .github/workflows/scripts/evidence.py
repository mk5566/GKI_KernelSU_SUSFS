"""Small, self-contained records for each build; never copy source trees."""
import hashlib
import json
import shutil
import subprocess
import os
import sys
import zipfile
from dataclasses import fields
from pathlib import Path

from config import BuildConfig, REPO_ROOT, REPO_SYNC_PROJECTS, LZ4_RELEASE, BBR_REVISION
from abi import compare_symvers, read_symvers


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_sources():
    paths = list((REPO_ROOT / "patches").rglob("*"))
    paths += list((REPO_ROOT / ".github/workflows").rglob("*.py"))
    paths += list((REPO_ROOT / ".github/workflows").glob("*.yml"))
    return {path.relative_to(REPO_ROOT).as_posix(): sha256(path)
            for path in sorted(paths) if path.is_file() and "__pycache__" not in path.parts}


def write_sources(config, workspace):
    revision = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
    record = dict(config.to_dict(), schema_version=1, builder_commit=revision)
    record["builder_dirty"] = bool(subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
        check=True, capture_output=True, text=True).stdout.strip())
    record["local_source_sha256"] = local_sources()
    record["environment"] = {key: os.environ[key] for key in (
        "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
        "GITHUB_SERVER_URL", "RUNNER_OS", "ImageOS", "ImageVersion") if key in os.environ}
    record["environment"]["python"] = sys.version
    path = Path(workspace) / "sources.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def load_sources(path):
    record = json.loads(Path(path).read_text(encoding="utf-8"))
    revision = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
    if (record.get("schema_version") != 1 or record.get("lz4_release") != LZ4_RELEASE
            or record.get("bbr_commit") != BBR_REVISION
            or record.get("builder_commit") != revision
            or record.get("local_source_sha256") != local_sources()):
        raise RuntimeError("Resolved sources do not match this builder and reviewed ports")
    config = BuildConfig(**{field.name: record[field.name]
                           for field in fields(BuildConfig) if field.name in record})
    config.validate()
    return config


def collect_evidence(workspace, config_name, abi_count):
    workspace = Path(workspace)
    out = workspace / config_name / "out" / config_name
    files = {
        "BUILD_INFO.md": workspace / "BUILD_INFO.md",
        "sources.json": workspace / "sources.json",
        ".config": out / "common/.config",
        "vmlinux.symvers": out / "dist/vmlinux.symvers",
        "GKI-reference.symvers": workspace / "GKI-reference.symvers",
        "CONFIG_REPORT.json": workspace / "CONFIG_REPORT.json",
        "checkouts.json": workspace / "checkouts.json",
    }
    artifacts = {name: workspace / name for name in ("boot.img", "AnyKernel3.zip", "Image")}
    for path in (*files.values(), *artifacts.values()):
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError(f"Missing or empty build evidence: {path}")
    if abi_count <= 0:
        raise RuntimeError("Cannot publish evidence without a successful certified CRC check")
    # Recompute the result from the exact tables being published. A remembered
    # count alone must never certify stale or replaced files.
    matched = compare_symvers(read_symvers(files["GKI-reference.symvers"].read_text(encoding="utf-8")),
                              read_symvers(files["vmlinux.symvers"].read_text(encoding="utf-8")))
    if matched != abi_count:
        raise RuntimeError("Certified CRC evidence count changed after verification")
    config_report = json.loads(files["CONFIG_REPORT.json"].read_text(encoding="utf-8"))
    if (set(config_report) != {"precompile", "final"}
            or config_report["final"].get("sha256") != sha256(files[".config"])
            or any(stage.get("status") != "passed" or not stage.get("expected")
                   or stage.get("actual") != stage["expected"] for stage in config_report.values())
            or config_report["precompile"]["expected"] != config_report["final"]["expected"]):
        raise RuntimeError("Final configuration evidence is missing or stale")
    config = load_sources(files["sources.json"])
    projects = {path: revision for _, path, revision in config.source_projects}
    expected_checkouts = {f"{config_name}/{path}": projects[path] for path in REPO_SYNC_PROJECTS}
    expected_checkouts.update({f"{config_name}/.repo/repo": config.repo_commit,
                              f"{config_name}/KernelSU": config.sukisu_commit,
                              "susfs4ksu": config.susfs_commit,
                              "SukiSU_patch": config.sukisu_patch_commit,
                              "AnyKernel3": config.anykernel_commit})
    if json.loads(files["checkouts.json"].read_text(encoding="utf-8")) != expected_checkouts:
        raise RuntimeError("Actual source checkouts do not match resolved sources")
    with zipfile.ZipFile(artifacts["AnyKernel3.zip"]) as archive:
        if archive.namelist().count("Image") != 1:
            raise RuntimeError("AnyKernel3 must contain exactly one Image")
        with archive.open("Image") as image:
            if hashlib.file_digest(image, "sha256").hexdigest() != sha256(artifacts["Image"]):
                raise RuntimeError("AnyKernel3 Image differs from the packaged kernel")
    directory = workspace / "evidence"
    directory.mkdir(exist_ok=False)
    for name, path in files.items():
        shutil.copy2(path, directory / name)
    report = {
        "status": "passed",
        "matched_certified_exports": abi_count,
        "reference_sha256": sha256(files["GKI-reference.symvers"]),
        "built_symvers_sha256": sha256(files["vmlinux.symvers"]),
        "scope": "Frozen export presence and CRCs; not device boot or runtime stability",
    }
    (directory / "ABI_REPORT.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    # The image checksums refer to the separate boot artifact and Image inside
    # AnyKernel3.zip. Evidence-file checksums can be checked in this directory.
    checksums = dict(artifacts)
    checksums.update({name: directory / name for name in (*files, "ABI_REPORT.json")})
    (directory / "SHA256SUMS").write_text("".join(
        f"{sha256(path)}  {name}\n" for name, path in sorted(checksums.items())),
        encoding="utf-8")
    return directory
