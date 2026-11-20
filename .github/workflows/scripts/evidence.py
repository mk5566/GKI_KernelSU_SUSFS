"""Validate and atomically publish a small evidence bundle, never source trees."""
import hashlib
import shutil
import tempfile
import zipfile
from pathlib import Path

from abi import compare_exports, read_exports
from boot_validation import verify_boot_image
from config import REPO_SYNC_PROJECTS
from config_checks import ConfigPolicy, config_delta
from integrity import sha256, atomic_json, read_json
from patch_plan import expected_patch_records
# Kept as public aliases for existing callers; provenance lives in one module.
from source_lock import local_sources, write_sources, load_sources


def collect_evidence(workspace: str | Path, config_name: str, abi_count: int) -> Path:
    workspace = Path(workspace)
    out = workspace / config_name / "out" / config_name
    files = {
        "BUILD_INFO.md": workspace / "BUILD_INFO.md",
        "BUILD_TIMINGS.json": workspace / "BUILD_TIMINGS.json",
        "sources.json": workspace / "sources.json",
        ".config": out / "common/.config",
        "precompile.config": workspace / "precompile.config",
        "vmlinux.symvers": out / "dist/vmlinux.symvers",
        "GKI-reference.symvers": workspace / "GKI-reference.symvers",
        "CONFIG_REPORT.json": workspace / "CONFIG_REPORT.json",
        "CONFIG_DIFF.json": workspace / "CONFIG_DIFF.json",
        "PATCH_REPORT.json": workspace / "PATCH_REPORT.json",
        "checkouts.json": workspace / "checkouts.json",
    }
    artifacts = {name: workspace / name for name in ("boot.img", "AnyKernel3.zip", "Image")}
    compiled = out / "dist/Image"
    for path in (*files.values(), *artifacts.values(), compiled):
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError(f"Missing or empty build evidence: {path}")
    if abi_count <= 0:
        raise RuntimeError("Cannot publish evidence without a successful certified CRC check")
    matched = compare_exports(read_exports(files["GKI-reference.symvers"].read_text(encoding="utf-8")),
                              read_exports(files["vmlinux.symvers"].read_text(encoding="utf-8")))
    if matched != abi_count:
        raise RuntimeError("Certified CRC evidence count changed after verification")

    # Independently re-run policy; a self-consistent JSON report is not proof.
    config_report = read_json(files["CONFIG_REPORT.json"])
    verified = {"precompile": ConfigPolicy.verify(files["precompile.config"]),
                "final": ConfigPolicy.verify(files[".config"])}
    if config_report != verified:
        raise RuntimeError("Final configuration evidence is missing or stale")
    if read_json(files["CONFIG_DIFF.json"]) != config_delta(files["precompile.config"], files[".config"]):
        raise RuntimeError("Configuration delta evidence is stale")
    config = load_sources(files["sources.json"])
    if config.config_name != config_name:
        raise RuntimeError("Evidence configuration does not match the source lock")
    projects = {path: revision for _, path, revision in config.source_projects}
    expected_checkouts = {f"{config_name}/{path}": projects[path] for path in REPO_SYNC_PROJECTS}
    expected_checkouts.update({f"{config_name}/.repo/repo": config.repo_commit,
                              f"{config_name}/KernelSU": config.sukisu_commit,
                              "susfs4ksu": config.susfs_commit,
                              "SukiSU_patch": config.sukisu_patch_commit,
                              "AnyKernel3": config.anykernel_commit})
    if read_json(files["checkouts.json"]) != expected_checkouts:
        raise RuntimeError("Actual source checkouts do not match resolved sources")
    if read_json(files["PATCH_REPORT.json"]) != {"applied": expected_patch_records(config, workspace)}:
        raise RuntimeError("Applied patch order, target, or contents differ from the selected patch stack")

    compiled_hash = sha256(compiled)
    if sha256(artifacts["Image"]) != compiled_hash:
        raise RuntimeError("Packaged Image differs from the compiled dist/Image")
    boot_report = verify_boot_image(artifacts["boot.img"], compiled)
    with zipfile.ZipFile(artifacts["AnyKernel3.zip"]) as archive:
        if archive.namelist().count("Image") != 1:
            raise RuntimeError("AnyKernel3 must contain exactly one Image")
        with archive.open("Image") as image:
            if hashlib.file_digest(image, "sha256").hexdigest() != compiled_hash:
                raise RuntimeError("AnyKernel3 Image differs from the compiled kernel")

    directory = workspace / "evidence"
    if directory.exists() or directory.is_symlink():
        raise RuntimeError("Refusing to replace existing build evidence")
    staging = Path(tempfile.mkdtemp(prefix=".evidence-", dir=workspace))
    try:
        for name, path in files.items():
            shutil.copy2(path, staging / name)
        atomic_json(staging / "ABI_REPORT.json", {
            "status": "passed", "matched_certified_exports": abi_count,
            "export_type_and_namespace_checked": True,
            "reference_sha256": sha256(files["GKI-reference.symvers"]),
            "built_symvers_sha256": sha256(files["vmlinux.symvers"]),
            "scope": "Frozen export presence, CRCs, export types and namespaces; not full binary ABI, boot or runtime stability",
        })
        atomic_json(staging / "ARTIFACT_REPORT.json", {
            "status": "passed", "compiled_image_sha256": compiled_hash,
            "boot": boot_report, "anykernel_image_sha256": compiled_hash,
            "scope": "Payload identity only; not AVB, signing or bootability",
        })
        checksums = dict(artifacts)
        checksums.update({path.name: path for path in staging.iterdir()})
        (staging / "SHA256SUMS").write_text("".join(
            f"{sha256(path)}  {name}\n" for name, path in sorted(checksums.items())), encoding="utf-8")
        staging.rename(directory)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return directory
