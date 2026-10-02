# Xiaomi 13 Ultra kernel: latest android13-5.15, SukiSU, SUSFS

One GitHub Actions build. It produces a GKI-derived arm64 kernel for the
Xiaomi 13 Ultra (ishtar) on HyperOS 3 and uploads two files: `boot.img` and
`AnyKernel3.zip`.

Each run resolves Google's newest official `android13-5.15.<sublevel>_rNN`
tag, peels it to a commit, and syncs the newest
`common-android13-5.15-YYYY-MM` manifest branch for the toolchain. As of
this tree, that tag is `android13-5.15.216_r00`
(`5bfe2b8c1439354d25dc5b1779cd0bb75bb90a7f`). The resolver checks at build
time so new point releases are picked up automatically.

The build strictly preserves the GKI ABI so pre-compiled vendor modules
continue to load without symbol CRC or structure layout mismatches.

## What the kernel contains

- SukiSU-Ultra is built in. The workflow choice is `stable` (latest formal
  release tag, peeled to a commit) or `dev` (default branch HEAD, peeled to
  a commit). The builder checks that the checkout matches that commit.
  Supported kernel UAPI revisions are 2 and 4.
- SUSFS is mount-only: `CONFIG_KSU_SUSFS` and `CONFIG_KSU_SUSFS_SUS_MOUNT`.
  The SUSFS revision is pinned in the builder.
- KPM is forced off.
- ZRAM is built in. The backends are `lz4kd` (default) and `lz4`.
- LZ4 in the kernel is 1.9.4 with a 1KB hash table for 4KB zram pages.
- TCP congestion is BBRv3 by default, with stock BBRv1 still built in.
  Reno stays in the core stack. Cubic, Westwood, and the other optional
  algorithms are turned off. FQ pacing stays on.
- The BBRv3 patch is `patches/bbr/0001-bbrv3-android-kabi.patch`. It keeps
  `icsk_ca_priv` at 104 bytes and hides the extra fields from genksyms
  with `__GENKSYMS__` guards.

## Patches that stay

Every patch is applied with `git apply --check` and then `git apply`. A
reject fails the build. Nothing is applied with fuzz.

| Patch | Why it stays |
|---|---|
| `avoid_extra_s2idle_wake_attempts` | This phone uses s2idle. |
| `minimise_wakeup_time` | Shortens alarmtimer wake slack. |
| `reduce_freeze_timeout` | Shortens the suspend freeze wait. |
| `clear_page_16bytes_align` | Faster aligned page clears on arm64. |
| `f2fs_reduce_congestion` | `/data` is f2fs. |
| `f2fs_enlarge_min_fsync_blocks` | Fewer tiny fsync flushes. |
| `adjust_cpu_scan_order` | Scheduler scan starts at the next CPU. |
| `optimise_memcmp` | arm64 memcmp fast path. |

`silence_irq_cpu_logspam` only changed a ratelimited warning into a debug
line. It is not in the tree.

## Performance and GKI ABI Preservation

- `gki_defconfig` memory management and allocator settings are strictly
  preserved. Debug and sanitizer options (`SLUB_DEBUG`, `PAGE_OWNER`,
  `PAGE_PINNER`, `KFENCE`) are left at their GKI defaults to maintain exact
  structure layouts (`struct page`, `struct kmem_cache`) for vendor modules.
- Clang thin LTO, `CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE`, debug info stripped.
- `HZ` 250 and `PREEMPT` stay at the GKI values.
- CPU governors: `schedutil` and `performance`. Powersave, conservative,
  ondemand, and userspace are off.
- I/O: `none` and `mq-deadline`. BFQ and kyber are off.
- ZRAM uses lz4kd.
- The build checks that `icsk_ca_priv` remains the 104-byte array after patches.

## GitHub Actions

Run **Kernel Build** from the Actions tab. The only input is
`sukisu_channel` (`stable` or `dev`).

The Ubuntu 24.04 job syncs the AOSP projects this kernel needs, runs the
unit tests, compiles with the manifest's Clang release, and uploads
`boot.img` and `AnyKernel3.zip`. Artifact names follow the resolved sublevel
(e.g., `android13-5.15.216-sukisu-stable`). `BUILD_INFO.md` is written in the
workspace for the log.

`boot.img` is mkbootimg header version 4, kernel only (the generic ramdisk
stays in `init_boot`), then an AVB hash footer for a 64MiB boot partition
signed with a generated test key.
