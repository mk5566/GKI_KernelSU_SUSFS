"""Resolve-once provenance for builder inputs; no artifact publication here."""
import os
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

from config import BuildConfig, REPO_ROOT, LZ4_RELEASE, LZ4_REVISION, BBR_REVISION
from integrity import sha256, atomic_json, read_json
from upstream_policy import validate_upstream_status


def local_sources() -> dict[str, str]:
    paths = list((REPO_ROOT / "patches").rglob("*"))
    # Include shell helpers and native regression fixtures, not only Python.
    paths += [p for p in (REPO_ROOT / ".github/workflows").rglob("*")
              if p.suffix in {".py", ".sh", ".c", ".h", ".yml", ".yaml"}]
    paths += [REPO_ROOT / name for name in ("README.md", "AGENTS.md")]
    result = {}
    for path in sorted(set(paths)):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(REPO_ROOT):
            raise RuntimeError(f"Builder inputs must not be external symlinks: {path}")
        if not path.is_file():
            continue
        result[path.relative_to(REPO_ROOT).as_posix()] = sha256(path)
    return result


def builder_revision() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("Build provenance requires a Git checkout with a commit; ZIP metadata is not a revision") from exc


def builder_dirty() -> bool:
    return bool(subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                              check=True, capture_output=True, text=True).stdout.strip())


def write_sources(config: BuildConfig, workspace: str | Path) -> Path:
    config.validate()
    validate_upstream_status(config)
    revision = builder_revision()
    record = dict(config.to_dict(), schema_version=2, builder_commit=revision)
    record["builder_dirty"] = builder_dirty()
    record["local_source_sha256"] = local_sources()
    record["environment"] = {key: os.environ[key] for key in (
        "GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
        "GITHUB_SERVER_URL", "RUNNER_OS", "ImageOS", "ImageVersion") if key in os.environ}
    record["environment"]["python"] = sys.version
    path = Path(workspace) / "sources.json"
    atomic_json(path, record)
    return path


def load_sources(path: str | Path) -> BuildConfig:
    record = read_json(path)
    revision = builder_revision()
    if (record.get("schema_version") != 2 or record.get("lz4_release") != LZ4_RELEASE
            or record.get("lz4_commit") != LZ4_REVISION
            or record.get("bbr_commit") != BBR_REVISION
            or record.get("builder_commit") != revision
            or record.get("local_source_sha256") != local_sources()):
        raise RuntimeError("Resolved sources do not match this builder and reviewed ports")
    config = BuildConfig(**{field.name: record[field.name]
                           for field in fields(BuildConfig) if field.name in record})
    config.validate()
    validate_upstream_status(config)
    return config
