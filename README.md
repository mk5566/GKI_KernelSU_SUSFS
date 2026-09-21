# GKI SukiSU-Ultra + SUSFS Build System (Slim 5.15)

Automated Generic Kernel Image (GKI) build system defaulting to **android13-5.15.211** (with **5.15.180** retained) with integrated **SukiSU-Ultra**, **SUSFS**, **BBRv3**, **ZRAM (LZ4KD)**, and performance optimizations.

> [!NOTE]
> Designed for modern GKI 2.0 devices running Android 13+ with Linux Kernel 5.15.x (e.g. Snapdragon 8 Gen 2 platforms).

---

## Features

| Feature | Description | Status |
|---|---|---|
| [SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra) | Advanced kernel-based root solution | Included |
| [SUSFS](https://gitlab.com/simonpunk/susfs4ksu) | Kernel-level root and filesystem stealth hiding | Included |
| **BBRv3 + TCP PLB** | Google BBRv3 congestion control with KABI compliance, set as the default TCP algorithm | Enabled by default |
| **LZ4KD ZRAM** | High-performance LZ4KD memory compression algorithm | Enabled by default |
| **Performance Patches** | Scheduler, F2FS/ext4, memcpy/memcmp, idle/wakeup, and memory-pressure tweaks for Snapdragon 8 Gen 2 GKI | Included in `patches/5.15.211` and `patches/5.15.180` |

---

## Quick Start

### 1. GitHub Actions (Cloud Build)

1. Navigate to the **Actions** tab in your repository.
2. Select **Kernel Build**.
3. Click **Run workflow**.
4. Configure options:
   * **Android Version**: `android13`
   * **Kernel Version**: `5.15`
   * **Sub Level**: `211` (or `180`)
   * **OS Patch Level**: `auto` (`211` → `2026-09`, `180` → `2025-05`)
   * **SukiSU Version**: `Stable(standard)` or `Dev(development)`
   * **ZRAM (LZ4KD)**: `true`
   * **BBR**: `true`
5. Download output artifacts (`*-AnyKernel3.zip` and optional `*-boot.img`) upon completion. Filenames include the variant (`lz4kd`/`nozram`, `bbr3`/`nobbr`, `sukisu-stable`/`sukisu-dev`).

### 2. Local CLI Build

```bash
# Navigate to scripts directory
cd .github/workflows/scripts

# Build android13-5.15.211
python build.py --android android13 --kernel 5.15 --sub-level 211 --os-patch 2026-09

# Dry-run validation (target + vendor patches)
python build.py --dry-run
```

---

## Flashing Instructions

### AnyKernel3 (Recommended)
Flash the generated `android13-5.15.211-2026-09-lz4kd-bbr3-sukisu-stable-AnyKernel3.zip` via custom recovery or a kernel manager such as [HorizonKernelFlasher](https://github.com/libxzr/HorizonKernelFlasher/releases).

### Fastboot `boot.img`
The `*-boot.img` artifact is a **ramdisk-less** GKI header v4 image with a test-key AVB footer. It is not a full boot image for devices that need a ramdisk. Prefer AnyKernel3 unless you know you only need to replace the kernel.

```bash
fastboot flash boot android13-5.15.211-2026-09-lz4kd-bbr3-sukisu-stable-boot.img
```

---

## Applied Performance Patches (`patches/5.15.211` and `patches/5.15.180`)

Required:

1. `0001-net-tcp-backport-BBRv3-to-android13-5.15.patch` — BBRv3 + TCP PLB backport with Android KABI guards. The build fails if this does not apply.

Optional (applied cleanly or skipped with a warning):

1. `avoid_extra_s2idle_wake_attempts.patch` — Avoid redundant s2idle wakeups.
2. `minimise_wakeup_time.patch` — Dynamic alarmtimer wakeup timeout reduction.
3. `reduce_freeze_timeout.patch` — Generous 1-second process freeze timeout.
4. `clear_page_16bytes_align.patch` — 16-byte cache alignment for ARM64 page zeroing.
5. `f2fs_reduce_congestion.patch` — F2FS congestion wait timeout reduced from 20ms to 6ms.
6. `silence_irq_cpu_logspam.patch` — Cut noisy IRQ CPU affinity warnings.
7. `f2fs_enlarge_min_fsync_blocks.patch` — Enlarge `min_fsync_blocks` to 20.
8. `adjust_cpu_scan_order.patch` — Scheduler idle capacity scanning optimization.
9. `optimise_memcmp.patch` — ARM-optimized NEON SIMD memcmp routine.

---

## Repository Structure

```
.
├── .github/
│   └── workflows/
│       ├── kernel-build.yml       # android13-5.15 workflow (211 / 180)
│       └── scripts/               # Python build engine & config
├── patches/
│   ├── 5.15.211/                  # Rebased patches for the default target
│   ├── susfs/                    # SUSFS patch context adaptation
│   └── 5.15.180/                  # Vendor performance & BBRv3 patches
│       └── APPLY_ORDER.txt        # Sequence of patch application
└── README.md
```

## Source pins and minimal SUSFS profile

The 5.15.211 target uses AOSP's `common-android13-5.15-2026-09` manifest
with kernel/common pinned to `dc9467e8f9bfdec0d012f9345ac5f12f63dc7eba`.
5.15.211 is the kernel version; the KMI family remains **android13-5.15**.
This does not certify vendor-module ABI compatibility or bootability.

Stable selects SukiSU main revision
`cf87e3f4ddd3f6e5464d85acf56aaa6950e70841` (latest checked 2026-09-21),
paired with SUSFS 2.3.0 at `e565931d19256fd821ada01b35263506e7c7a364`.
Dev tracks `main`. Both compile SukiSU into the kernel (`CONFIG_KSU=y`)
and apply this repository's mount-only SUSFS integration. The branch name
`builtin` is not required to compile SukiSU into a kernel.

### Manager compatibility (checked 2026-09-21)

This build requires **UAPI 4**, matching Manager `40922-4`. It retains
upstream's scoped su-session driver descriptors and version-matching behavior;
the UAPI constant is not modified. Old kernels built from `builtin` expose
UAPI 2 and still need a UAPI-2 manager until the new kernel is flashed.

The build verifies the requested checkout, rejects other UAPI versions,
applies all integration patches with zero fuzz, and records the actual source
and UAPI in `BUILD_INFO.md`. Dev may fail when upstream changes require a port
refresh; use Stable for a reproducible source revision. After flashing, reboot
and check manager root access, profiles, modules, and `ksud susfs status` /
`ksud susfs version`. Matching UAPI alone does not verify bootability.

### Integration design

The local SukiSU patch adds SUSFS initialization, the post-fs-data storage
monitor, SELinux-domain helpers, app-profile mount visibility, and a root-only
SUSFS command dispatcher. Root escalation clears inherited SUSFS task flags.
The Linux patch selection includes only the filesystem changes needed for
mount hiding. It excludes the legacy exec, setuid, input, read, and SELinux
hooks, preserving current SukiSU's root, safe-mode, and daemon startup hooks.
SUSFS commands execute in sleepable reboot syscall context; KernelSU's normal
driver-fd requests retain the upstream path. Unsupported SUSFS commands return
an error rather than reporting a feature that is disabled.

Only `CONFIG_KSU_SUSFS` and `CONFIG_KSU_SUSFS_SUS_MOUNT` are enabled.
Path, kstat, map, uname, cmdline/bootconfig, open-redirect, symbol hiding,
and SUSFS logging are disabled. Legacy TRY_UMOUNT and SUS_SU are off;
SUSFS 2.3 has removed them. KernelSU's normal module-unmount mechanism is retained.

Read-only device audit (2026-09-20):

- Root ADB worked after the Shell profile was corrected; running kernel was 5.15.180.
- `ksud susfs status` returned false and version returned unsupported.
- Persistent SUSFS config contained only an empty `sus_paths` entry.
- Zygisk Next had unmount mode enabled; KernelSU kernel_umount was enabled.
- Bindhosts was forced to mode 1 (legacy SUSFS bind + try_umount), but its
  `/data/adb/ksu/bin/ksu_susfs` helper was absent. This is a broken configured
  dependency, not evidence that legacy SUSFS unmount works.

Mount-only SUSFS is a conservative new baseline, not a claim that SUSFS is
currently active or that every installed app will accept the new kernel.
No phone module settings or app data were changed. Bindhosts' legacy override
needs migration to a supported unmount mode separately. No per-app inventory,
credentials, or integrity attestation data is committed.

Validation: both CLI targets and mismatched-pair rejection; sequential patch
application against pinned 5.15.211 sources with zero fuzz (including the SUSFS
context adaptation and required BBRv3 patch). A full Linux kernel build and
post-flash app checks are still required.
