# Xiaomi 13 Ultra kernel: android13-5.15.211, SukiSU, SUSFS

One GitHub Actions build. It produces a GKI-derived arm64 kernel for the
Xiaomi 13 Ultra (ishtar) on HyperOS 3 and uploads two files: `boot.img` and
`AnyKernel3.zip`.

The source pin is the commit that already booted this phone:

`5.15.211-android13-8-gdc9467e8f9bf`

`dc9467e8f9bfdec0d012f9345ac5f12f63dc7eba`

A newer android13-5.15 point release, including `android13-5.15.216_r00`,
changes that release string. Vendor modules are built against the string
above. They refuse to load, and the phone stays on the Xiaomi logo. This
tree builds that one revision and no other kernel version.

## What the kernel contains

- SukiSU-Ultra is built in. The workflow choice is `stable` (latest formal
  release tag, peeled to a commit) or `dev` (default branch HEAD, peeled to
  a commit). The builder checks that the checkout matches that commit.
  Supported kernel UAPI revisions are 2 and 4.
- SUSFS is mount-only: `CONFIG_KSU_SUSFS` and `CONFIG_KSU_SUSFS_SUS_MOUNT`.
  The SUSFS revision is pinned in the builder.
- KPM is forced off.
- ZRAM is built in. The only backends are `lz4kd` (default) and `lz4`.
- LZ4 in the kernel is updated to 1.9.4, with a 1KB hash table for 4KB zram
  pages. Public 1.10.0 backports for this tree delete the in-tree sources and
  apply with fuzz. They are not used.
- TCP congestion is BBRv3 by default, with stock BBRv1 still built in.
  Reno stays in the core stack. Cubic, Westwood, and the other optional
  algorithms are turned off. FQ pacing stays on.
- The BBRv3 patch is `patches/bbr/0001-bbrv3-android-kabi.patch`. It keeps
  `icsk_ca_priv` at 104 bytes and hides the extra fields from genksyms.
  Replacing it with a backport that grows that array, or that drops the
  `__GENKSYMS__` guards, breaks vendor-module CRCs and reproduces the logo hang.

## Patches that stay

Every patch is applied with `git apply --check` and then `git apply`. A
reject fails the build. Nothing is applied with fuzz.

The tweak set is the one that was on the booting 5.15.211 build, minus one:

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

The build keeps the settings the booting kernel used, and drops options this
phone does not need:

- Clang thin LTO, `CONFIG_CC_OPTIMIZE_FOR_PERFORMANCE`, no debug info.
- `HZ` 250 and `PREEMPT` stay at the GKI values. Changing them changes
  timing that vendor drivers assume.
- CPU governors: `schedutil` and `performance`. Powersave, conservative,
  ondemand, and userspace are off.
- I/O: `none` and `mq-deadline`. BFQ and kyber are off. The device also has
  its own vendor I/O module.
- ZRAM uses lz4kd. The LZ4 fallback uses the smaller hash table above.

KMI symbol enforcement is turned off so this defconfig can compile. That is
not a claim that vendor modules will load. The build checks that
`icsk_ca_priv` is still the 104-byte array after the patches.

## GitHub Actions

Run **Kernel Build** from the Actions tab. The only input is
`sukisu_channel` (`stable` or `dev`).

The Ubuntu 24.04 job syncs the AOSP projects this one kernel needs, runs the
unit tests, compiles with the Clang version named in that manifest, and
uploads `boot.img` and `AnyKernel3.zip`. The booting kernel was built with
Clang `r450784e`. `BUILD_INFO.md` is written in
the workspace for the log. It is not a success artifact. A failed run
uploads that note, `.config`, and any `.rej` files.

`boot.img` is the same layout that booted before: mkbootimg header version
4, kernel only (the generic ramdisk stays in `init_boot`), then an AVB hash
footer for a 64MiB boot partition signed with a generated test key.

This repository is maintained for that GitHub job. The Windows checkout is
not a kernel build host. Do not add a second kernel version, a second
workflow input, or another compression or congestion algorithm without a
boot test on this phone.
