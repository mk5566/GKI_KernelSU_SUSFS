"""Build-time policy for the audited ishtar patch series.

These checks deliberately retain upstream code, rather than supplying new
kernel fast paths. They are not an ABI checker or a general source-code audit.
"""
from pathlib import Path
import re
from types import MappingProxyType


RETIRED_PATCHES = MappingProxyType({
    'bbrv3': ('0001-net-tcp-backport-BBRv3-to-android13-5.15.patch', 'Unchecked allocation callbacks and incompatible shared TCP metadata; use upstream TCP or explicitly select upstream BBRv1.'),
    'memcmp': ('optimise_memcmp.patch', 'Generic memcmp must remain usable without acquiring a SIMD context; retain the upstream integer-register implementation.'),
    'freeze-timeout': ('reduce_freeze_timeout.patch', 'Drops sysfs writes and forces a one-second timeout; retain the upstream writable timeout and default.'),
    's2idle-wake': ('avoid_extra_s2idle_wake_attempts.patch', 'Wake/abort coalescing is not qualified against this device; retain upstream synchronization.'),
    'alarm-wakeup': ('minimise_wakeup_time.patch', 'Shortens suspend-abort wake protection and can convert a negative deadline to an unsigned timeout; retain upstream handling.'),
    'f2fs-congestion': ('f2fs_reduce_congestion.patch', 'A shorter retry timeout has no demonstrated storage-pressure benefit on this device; retain upstream backoff.'),
    'f2fs-fsync': ('f2fs_enlarge_min_fsync_blocks.patch', 'This is an in-place-update policy threshold, not a proven fsync batching optimization; retain upstream default and sysfs control.'),
    'irq-log': ('silence_irq_cpu_logspam.patch', 'Actual affinity failures must retain the existing rate-limited warning.'),
})

# Preserve these files byte-for-byte against the initially synced common
# commit, even if a helper stages/commits its changes or renames its patch.
PROTECTED_SOURCE_PATHS = (
    "arch/arm64/lib/memcmp.S",
    "arch/arm64/lib/Makefile",
    "drivers/base/power/wakeup.c",
    "kernel/power/main.c",
    "kernel/power/process.c",
    "kernel/power/suspend.c",
    "kernel/time/alarmtimer.c",
    "kernel/irq/cpuhotplug.c",
    "kernel/module.c",
    "fs/f2fs/f2fs.h",
    "fs/f2fs/segment.h",
    "include/linux/netdevice.h",
    "include/linux/tcp.h",
    "include/net/inet_connection_sock.h",
    "include/net/tcp.h",
    "include/net/netns/ipv4.h",
    "include/uapi/linux/inet_diag.h",
    "include/uapi/linux/rtnetlink.h",
    "net/ipv4/Kconfig",
    "net/ipv4/Makefile",
    "net/ipv4/sysctl_net_ipv4.c",
    "net/ipv4/tcp.c",
    "net/ipv4/tcp_bbr.c",
    "net/ipv4/tcp_cong.c",
    "net/ipv4/tcp_input.c",
    "net/ipv4/tcp_ipv4.c",
    "net/ipv4/tcp_minisocks.c",
    "net/ipv4/tcp_output.c",
    "net/ipv4/tcp_rate.c",
)
# Introducing these is a port refresh, not a silently accepted monthly update.
FORBIDDEN_SOURCE_PATHS = ("net/ipv4/tcp_bbr3.c", "net/ipv4/tcp_plb.c")


def reject_retired_aliases(aliases) -> None:
    errors = [f"{alias}: {RETIRED_PATCHES[alias][1]}"
              for alias in sorted(set(aliases) & RETIRED_PATCHES.keys())]
    if errors:
        raise ValueError("Retired optional patch(es) cannot be selected: " +
                         "; ".join(errors))


def check_patch_policy(path: Path) -> None:
    """Reject retired names and protected targets, including renamed patches.

    Inspect git-style and traditional unified-diff file headers. The final
    source-byte check is authoritative; this is an early diagnostic, not a
    security parser for arbitrary patch formats.
    """
    path = Path(path)
    if path.name in {item[0] for item in RETIRED_PATCHES.values()}:
        raise ValueError(f"Retired patch file cannot be restored: {path.name}")
    protected = set(PROTECTED_SOURCE_PATHS + FORBIDDEN_SOURCE_PATHS)
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith(("--- ", "+++ ")):
            target = line[4:].split("\t", 1)[0].strip('"')
            if target.startswith(("a/", "b/")):
                target = target[2:]
            if target in protected:
                raise ValueError(
                    f"Patch {path.name} changes protected upstream source {target}; "
                    "a reviewed policy update is required")


def verify_no_retired_config(path: Path) -> None:
    """Do not silently turn an unsupported BBR3 selection into another CCA."""
    text = Path(path).read_text(encoding="utf-8")
    forbidden = re.findall(
        r'^(?:CONFIG_(?:TCP_CONG_BBR3|DEFAULT_BBR3)=[ym]|'
        r'CONFIG_DEFAULT_TCP_CONG="bbr3")$', text, re.M)
    if forbidden:
        raise RuntimeError("Retired BBRv3 configuration present: " +
                           ", ".join(forbidden))
