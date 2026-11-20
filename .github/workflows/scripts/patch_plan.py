"""The one patch set this kernel always builds."""
from dataclasses import dataclass
from pathlib import Path
from config import REPO_ROOT


GROUPS = ("lz4", "zram", "bbr", "tweaks")


@dataclass(frozen=True)
class PatchPlan:
    lz4: tuple
    zram: tuple
    bbr: tuple
    tweaks: tuple

    @property
    def all(self):
        return self.lz4 + self.bbr + self.tweaks + self.zram


def _read_group(group):
    directory = REPO_ROOT / "patches" / group
    order = directory / "APPLY_ORDER.txt"
    names = [line.strip() for line in order.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if not names or len(set(names)) != len(names):
        raise RuntimeError(f"Empty or duplicate selected patch list: {order}")
    if any("/" in name or "\\" in name or not name.endswith(".patch") for name in names):
        raise RuntimeError(f"Invalid patch name in {order}")
    missing = [name for name in names if not (directory / name).is_file()]
    if missing:
        raise RuntimeError(f"Patch list names missing files: {missing}")
    selected = tuple(directory / name for name in names)
    if any(path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()) for path in selected):
        raise RuntimeError(f"Selected patches must be regular files inside {directory}")
    return selected


def make_patch_plan():
    groups = {name: _read_group(name) for name in GROUPS}
    return PatchPlan(**groups)


def integration_sequence(config, workspace):
    """Expected complete sequence, including root/SUSFS glue outside APPLY_ORDER."""
    workspace = Path(workspace)
    plan = make_patch_plan()
    common = f"{config.config_name}/common"
    entries = [(REPO_ROOT / "patches/susfs/0001-sukisu-quiet-production.patch",
                f"{config.config_name}/KernelSU"),
               (workspace / "susfs4ksu/kernel_patches" / config.get_susfs_patch_filename(), common),
               (REPO_ROOT / "patches/susfs/0002-susfs-ratelimit-log-fallback.patch", common),
               (REPO_ROOT / "patches/susfs/0003-susfs-no-priority-uid-filter.patch", common)]
    return tuple(entries + [(path, common) for path in plan.all])


def expected_patch_records(config, workspace):
    from integrity import sha256
    workspace = Path(workspace)
    records = []
    for number, (path, target) in enumerate(integration_sequence(config, workspace), 1):
        anchor = REPO_ROOT if path.is_relative_to(REPO_ROOT) else workspace
        records.append({"sequence": number, "path": path.relative_to(anchor).as_posix(),
                        "origin": "builder" if anchor == REPO_ROOT else "workspace",
                        "sha256": sha256(path), "target": target})
    return records
