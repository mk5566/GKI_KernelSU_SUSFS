# Xiaomi 13 Ultra kernel: latest android13-5.15, SukiSU, SUSFS

One GitHub Actions build. It produces a GKI-derived arm64 kernel for the
Xiaomi 13 Ultra (ishtar) on HyperOS 3 and uploads two files: `boot.img` and
`AnyKernel3.zip`.

Each run resolves Google's newest official `android13-5.15.<sublevel>_rNN`
tag, peels it to a commit, and syncs the newest
`common-android13-5.15-YYYY-MM` manifest branch for the toolchain. As of
this tree, that tag is `android13-5.15.216_r00`
(`5bfe2b8c1439354d25dc5b1779cd0bb75bb90a7f`). The resolver asks again at
build time, so a newer point release is picked up without editing the
workflow.

The kernel release string includes that sublevel
(`5.15.<sublevel>-android13-8-g<commit>`). Vendor modules built for a
different sublevel, including the 5.15.211 modules on a phone that last
booted `5.15.211-android13-8-gdc9467e8f9bf`, do not load. Flash this only
when the installed vendor modules match the sublevel the build resolved.

## What the kernel contains

- SukiSU-Ultra is built in. The workflow choice is `stable` (latest formal
  release tag, peeled to a commit) or `dev` (default branch HEAD, peeled to
  a commit). The builder checks that the checkout matches that commit.
  Supported kernel UAPI revisions are 2 and 4.
- SUSFS is mount-only: `CONFIG_KSU_SUSFS` and `CONFIG_KSU_SUSFS_SUS_MOUNT`.
  The SUSFS revision is pinned in the builder.
- KPM is forced off.
- ZRAM is built in. The only backends are `lz4kd` (default) and `lz4`.
- LZ4 in the kernel is 1.10.0, with the arm64 NEON decompressor and a 1KB
  hash table for 4KB zram pages.
- TCP congestion is BBRv3 by default, with stock BBRv1 still built in.
  Reno stays in the core stack. Cubic, Westwood, and the other optional
  algorithms are turned off. The default qdisc is `fq`, which is what
  BBRv3 needs in order to pace.
- The BBRv3 patch is `patches/bbr/0001-bbrv3-android-kabi.patch`. It keeps
  `icsk_ca_priv` at 104 bytes and hides the extra fields from genksyms.
  A newer GKI sublevel still changes the release string. It does not, by
  itself, grow that array. Replacing the patch with a backport that grows
  the array, or that drops the `__GENKSYMS__` guards, breaks vendor-module
  CRCs.

## Patches that stay

Every patch is applied with `git apply --check` and then `git apply`. A
reject fails the build. Nothing is applied with fuzz. The LZ4, ZRAM, BBRv3,
and tweak patches all apply on android13-5.15.216.

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

## Performance and efficiency

- Clang thin LTO. `gki_defconfig` selects full LTO; this build switches it
  to thin. The compiler flag stays `-O2`
  (`CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE`). The LZ4 objects themselves are
  built with `-O3`.
- `HZ` 250 and `PREEMPT` stay at the GKI values.
- CPU governors: `schedutil` and `performance`. Powersave, conservative,
  ondemand, and userspace are off. `ENERGY_MODEL` and `SCHED_MC` stay on.
- Workqueues that are marked power-efficient use that mode by default.
- I/O: `none` and `mq-deadline`. BFQ and kyber are off. Slab caches are
  allowed to merge.
- ZRAM uses lz4kd. The LZ4 fallback uses the 1KB hash table above.
- These GKI debug options are off: KASAN (including hardware tags), UBSAN,
  KFENCE, page owner, init-on-alloc, SLUB debug, schedstats, scheduler
  debug, and BTF. BPF, CFI, kprobes, and the shadow call stack stay on.
  KSU needs kprobes. Android needs BPF.

KMI symbol enforcement is turned off so this defconfig can compile. The
build checks that `icsk_ca_priv` is still the 104-byte array after the
patches.

## GitHub Actions

Run **Kernel Build** from the Actions tab. The only input is
`sukisu_channel` (`stable` or `dev`).

The Ubuntu 24.04 job syncs the AOSP projects this one kernel needs, runs the
unit tests, compiles with the Clang version named in that manifest, and
uploads `boot.img` and `AnyKernel3.zip`. The artifact name follows the
resolved sublevel, for example `android13-5.15.216-sukisu-stable`.
`BUILD_INFO.md` is written in the workspace for the log. It is not a
success artifact. A failed run uploads that note, `.config`, and any
`.rej` files.

`boot.img` is mkbootimg header version 4, kernel only (the generic ramdisk
stays in `init_boot`), then an AVB hash footer for a 64MiB boot partition
signed with a generated test key.

This repository is maintained for that GitHub job. The Windows checkout is
not a kernel build host.
