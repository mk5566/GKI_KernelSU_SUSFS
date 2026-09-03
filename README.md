# GKI SukiSU-Ultra + SUSFS Build System (Slim 5.15.180)

Automated Generic Kernel Image (GKI) build system locked to **android13-5.15.180** with integrated **SukiSU-Ultra**, **SUSFS**, **BBRv3**, **ZRAM (LZ4KD)**, and performance optimizations.

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
| **Performance Patches** | Scheduler, F2FS/ext4, memcpy/memcmp, idle/wakeup, and memory-pressure tweaks for Snapdragon 8 Gen 2 GKI | Included in `patches/5.15.180` |

---

## Quick Start

### 1. GitHub Actions (Cloud Build)

1. Navigate to the **Actions** tab in your repository.
2. Select **Kernel Build**.
3. Click **Run workflow**.
4. Configure options:
   * **Android Version**: `android13`
   * **Kernel Version**: `5.15`
   * **Sub Level**: `180`
   * **OS Patch Level**: `2025-05`
   * **SukiSU Version**: `Stable(standard)` or `Dev(development)`
   * **ZRAM (LZ4KD)**: `true`
   * **BBR**: `true`
5. Download output artifacts (`boot.img` and `AnyKernel3.zip`) upon completion.

### 2. Local CLI Build

```bash
# Navigate to scripts directory
cd .github/workflows/scripts

# Install dependencies
pip install PyYAML

# Build android13-5.15.180
python build.py --android android13 --kernel 5.15 --sub-level 180 --os-patch 2025-05

# Dry-run validation
python build.py --dry-run
```

---

## Flashing Instructions

### AnyKernel3 (Recommended)
Flash the generated `android13-5.15.180-2025-05-AnyKernel3.zip` via custom recovery or a kernel manager application such as [HorizonKernelFlasher](https://github.com/libxzr/HorizonKernelFlasher/releases).

### Fastboot `boot.img`
Reboot into Fastboot mode and flash directly:
```bash
fastboot flash boot android13-5.15.180-2025-05-boot.img
```

---

## Applied Performance Patches (`patches/5.15.180`)

Required:

1. `0001-net-tcp-backport-BBRv3-to-android13-5.15.patch` — BBRv3 + TCP PLB backport with Android KABI guards. The build fails if this does not apply.

Optional (applied cleanly or skipped with a warning):

1. `avoid_extra_s2idle_wake_attempts.patch` — Avoid redundant s2idle wakeups.
2. `minimise_wakeup_time.patch` — Dynamic alarmtimer wakeup timeout reduction.
3. `reduce_freeze_timeout.patch` — Generous 1-second process freeze timeout.
4. `clear_page_16bytes_align.patch` — 16-byte cache alignment for ARM64 page zeroing.
5. `f2fs_reduce_congestion.patch` — F2FS congestion wait timeout reduced from 20ms to 6ms.
6. `disable_cache_hot_buddy.patch` — Leverage DynamIQ Shared Unit (DSU) on modern cores.
7. `silence_irq_cpu_logspam.patch` — Cut noisy IRQ CPU affinity warnings.
8. `f2fs_enlarge_min_fsync_blocks.patch` — Enlarge `min_fsync_blocks` to 20.
9. `adjust_cpu_scan_order.patch` — Scheduler idle capacity scanning optimization.
10. `optimise_memcmp.patch` — ARM-optimized NEON SIMD memcmp routine.

---

## Repository Structure

```
.
├── .github/
│   └── workflows/
│       ├── kernel-build.yml       # Optimized single-target build workflow
│       ├── config/matrix.json     # Locked build matrix entry
│       └── scripts/               # Python build engine & config
├── patches/
│   └── 5.15.180/                  # Vendor performance & BBRv3 patches
│       └── APPLY_ORDER.txt        # Sequence of patch application
└── README.md
```
