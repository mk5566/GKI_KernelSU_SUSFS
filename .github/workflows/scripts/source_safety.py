"""Compare protected kernel sources with a pinned Git commit, not mutable HEAD.

Only build-time work is performed. No runtime hooks, locks or SIMD wrappers
are added to the kernel. This is a regression guard, not device qualification.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from patch_policy import PROTECTED_SOURCE_PATHS, FORBIDDEN_SOURCE_PATHS


def _git(common: Path, *args: str) -> bytes:
    env = os.environ.copy()
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    result = subprocess.run(["git", "-C", str(common), *args],
                            capture_output=True, check=False, timeout=30, env=env)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def _regular_source(common: Path, relative: str) -> Path:
    path = common
    # A symlink to identical bytes is not the source contract being checked.
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise RuntimeError(f"Symlink is not permitted in protected path: {relative}")
    if not stat.S_ISREG(path.stat().st_mode):
        raise RuntimeError(f"Protected source is not a regular file: {relative}")
    return path


def verify_upstream_sources(common: Path, revision: str,
                            report_path: Path | None = None) -> dict:
    """Require pinned original bytes, including for staged/committed changes.

    The caller records revision before running any third-party setup scripts.
    A failed check writes a failed report rather than leaving a stale pass.
    The source worktree must remain exclusively owned by the build job.
    """
    common = Path(common).absolute()
    report = {"schema": 1, "status": "failed", "baseline_revision": revision,
              "files": {}, "errors": []}
    try:
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise RuntimeError("Source safety requires the initially synced full common SHA")
        if common.is_symlink() or not common.is_dir():
            raise RuntimeError("Kernel common directory is missing or a symlink")
        if _git(common, "cat-file", "-t", revision).strip() != b"commit":
            raise RuntimeError("Source safety baseline is not a Git commit")
        for relative in PROTECTED_SOURCE_PATHS:
            try:
                original = _git(common, "cat-file", "blob", f"{revision}:{relative}")
                path = _regular_source(common, relative)
                actual = path.read_bytes()
                report["files"][relative] = {
                    "expected_sha256": hashlib.sha256(original).hexdigest(),
                    "actual_sha256": hashlib.sha256(actual).hexdigest(),
                }
                if actual != original:
                    raise RuntimeError(f"Protected source differs from pinned common: {relative}")
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                report["errors"].append(f"{relative}: {error}")
        for relative in FORBIDDEN_SOURCE_PATHS:
            path = common / relative
            if path.exists() or path.is_symlink():
                report["errors"].append(f"Retired backport source is present: {relative}")
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        report["errors"].append(str(error))
    if not report["errors"]:
        report["status"] = "passed"
    if report_path is not None:
        Path(report_path).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                                    encoding="utf-8")
    if report["errors"]:
        raise RuntimeError("Kernel source safety check failed:\n" +
                           "\n".join(report["errors"]))
    return report
