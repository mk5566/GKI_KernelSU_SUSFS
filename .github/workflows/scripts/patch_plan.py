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
        return self.lz4 + self.zram + self.bbr + self.tweaks


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
    return tuple(directory / name for name in names)


def make_patch_plan():
    groups = {name: _read_group(name) for name in GROUPS}
    return PatchPlan(**groups)
