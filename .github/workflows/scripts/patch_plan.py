"""Deterministic all-or-nothing feature patch plan."""
from dataclasses import dataclass
from pathlib import Path
from config import REPO_ROOT, BuildConfig


@dataclass(frozen=True)
class PatchPlan:
    bbr: tuple[Path, ...]
    tweaks: tuple[Path, ...]

    @property
    def all(self):
        return self.bbr + self.tweaks


def _read_group(group):
    directory = REPO_ROOT / "patches" / group
    order = directory / "APPLY_ORDER.txt"
    names = [line.strip() for line in order.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if not names or len(set(names)) != len(names):
        raise RuntimeError(f"Empty or duplicate selected patch list: {order}")
    if any("/" in name or "\\" in name or not name.endswith(".patch") for name in names):
        raise RuntimeError(f"Invalid patch name in {order}")
    return tuple(directory / name for name in names)


def make_patch_plan(config: BuildConfig):
    bbr = _read_group("bbrv3") if config.bbr_version == "v3" else ()
    tweaks = _read_group("tweaks") if config.apply_tweaks else ()
    return PatchPlan(bbr, tweaks)
