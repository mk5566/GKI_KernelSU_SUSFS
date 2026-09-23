"""Deterministic, fail-closed patch operations. No shell or fuzzy matching."""
from pathlib import Path
import re
import subprocess
from typing import List, Tuple
from patch_policy import reject_retired_aliases, check_patch_policy


def parse_optional_patches(value: str) -> tuple[str, ...]:
    """Parse workflow aliases without allowing paths or empty CSV entries."""
    if not value.strip():
        return ()
    aliases = tuple(part.strip() for part in value.split(","))
    if any(not re.fullmatch(r"[a-z][a-z0-9-]*", alias) for alias in aliases):
        raise ValueError("Optional patches must be comma-separated aliases from APPLY_ORDER.txt")
    if len(set(aliases)) != len(aliases):
        raise ValueError("Duplicate optional patch alias")
    reject_retired_aliases(aliases)
    return tuple(sorted(aliases))


def read_patch_order(directory: Path, selected_optional=()) -> List[Tuple[Path, bool]]:
    directory = Path(directory).resolve(strict=True)
    order = directory / "APPLY_ORDER.txt"
    if not order.is_file():
        raise ValueError(f"Missing patch manifest: {order}")
    selected = tuple(selected_optional)
    reject_retired_aliases(selected)
    if len(set(selected)) != len(selected):
        raise ValueError("Duplicate optional patch alias")
    entries = []
    seen_files = set()
    seen_aliases = set()
    for number, raw in enumerate(order.read_text(encoding="utf-8-sig").splitlines(), 1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        selectable = text.startswith("?")
        required = text.startswith("!")
        if not selectable and not required:
            raise ValueError(f"Patch entry needs ! or ?alias: at {order}:{number}")
        if selectable:
            alias, separator, name = text[1:].partition(":")
            if not separator or not re.fullmatch(r"[a-z][a-z0-9-]*", alias):
                raise ValueError(f"Invalid optional patch alias at {order}:{number}")
            if alias in seen_aliases:
                raise ValueError(f"Duplicate optional patch alias at {order}:{number}: {alias}")
            reject_retired_aliases((alias,))
            seen_aliases.add(alias)
        else:
            name = text[1:].strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.patch", name):
            raise ValueError(f"Invalid patch basename at {order}:{number}: {name!r}")
        if name in seen_files:
            raise ValueError(f"Duplicate patch at {order}:{number}: {name}")
        seen_files.add(name)
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Patch is missing, not regular, or a symlink: {path}")
        check_patch_policy(path)
        if required or (selectable and alias in selected):
            # An explicitly selected patch is required for this build.
            entries.append((path, True))
    # Also reject unlisted retired/renamed patches left in the active directory.
    for candidate in directory.glob("*.patch"):
        if candidate.name not in seen_files:
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError(f"Unlisted patch is not regular or is a symlink: {candidate}")
            check_patch_policy(candidate)
    unknown = set(selected) - seen_aliases
    if unknown:
        raise ValueError(f"Unknown optional patch alias(es): {', '.join(sorted(unknown))}")
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
