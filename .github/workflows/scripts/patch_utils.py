"""Deterministic, fail-closed patch operations. No shell or fuzzy matching."""
from pathlib import Path
import re
import subprocess
from typing import List, Tuple


def read_patch_order(directory: Path) -> List[Tuple[Path, bool]]:
    directory = Path(directory).resolve(strict=True)
    order = directory / "APPLY_ORDER.txt"
    if not order.is_file():
        raise ValueError(f"Missing patch manifest: {order}")
    entries = []
    seen = set()
    for number, raw in enumerate(order.read_text(encoding="utf-8-sig").splitlines(), 1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        required = text.startswith("!")
        name = text[1:].strip() if required else text
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.patch", name):
            raise ValueError(f"Invalid patch basename at {order}:{number}: {name!r}")
        if name in seen:
            raise ValueError(f"Duplicate patch at {order}:{number}: {name}")
        seen.add(name)
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Patch is missing, not regular, or a symlink: {path}")
        entries.append((path, required))
    return entries


def apply_patch_exact(directory: Path, patch: Path) -> str:
    """Return 'applied' or 'already-applied'; otherwise raise without fuzz.

    git apply checks every hunk before changing files. Reverse checking must
    also succeed for EVERY hunk: one previously applied file is not success.
    Caller must give this function exclusive access to its source worktree.
    """
    directory = Path(directory).resolve(strict=True)
    patch = Path(patch).resolve(strict=True)
    def run(*options):
        return subprocess.run(
            ["git", "-c", "apply.ignoreWhitespace=no", "-c", "apply.whitespace=nowarn",
             "apply", *options, str(patch)], cwd=directory,
            capture_output=True, text=True, check=False, timeout=120)
    forward = run("--check")
    if forward.returncode == 0:
        applied = run()
        if applied.returncode:
            raise RuntimeError(f"Patch apply failed: {patch.name}\n{applied.stderr}")
        return "applied"
    reverse = run("--reverse", "--check")
    if reverse.returncode == 0:
        return "already-applied"
    raise RuntimeError(
        f"Patch is neither wholly applicable nor wholly applied: {patch.name}\n"
        f"Forward check:\n{forward.stderr}Reverse check:\n{reverse.stderr}")
