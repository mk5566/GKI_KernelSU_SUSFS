"""Select SUSFS filesystem changes without replacing SukiSU main's root hooks."""
import re


MOUNT_PATCH_FILES = frozenset({
    "fs/Makefile",
    "fs/namespace.c",
    "fs/notify/fdinfo.c",
    "fs/proc/fd.c",
    "fs/proc_namespace.c",
    "fs/statfs.c",
    "fs/super.c",
})


def select_mount_patch(patch_text: str) -> str:
    selected = []
    seen = set()
    for section in re.split(r"(?=^diff --git )", patch_text, flags=re.M):
        match = re.match(r"diff --git a/(\S+) b/(\S+)\n", section)
        if not match or match[2] not in MOUNT_PATCH_FILES:
            continue
        path = match[2]
        if match[1] != path or path in seen:
            raise ValueError(f"Unexpected SUSFS patch section: {path}")
        # Old SUSFS root hooks have different signatures and duplicate main's
        # syscall hooks. They must never enter the filesystem-only patch.
        if re.search(r"^\+.*\b(?:ksu_handle_|ksu_su_compat_|__ksu_is_allow_uid)", section, re.M):
            raise ValueError(f"Legacy KernelSU hook in mount patch: {path}")
        seen.add(path)
        selected.append(section)
    if seen != MOUNT_PATCH_FILES:
        raise ValueError(f"Missing SUSFS mount patch files: {sorted(MOUNT_PATCH_FILES - seen)}")
    return "".join(selected)
