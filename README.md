# Xiaomi 13 Ultra: GKI 5.15 + SukiSU + Mount-Only SUSFS

Builds the latest **published, certified Android 13 GKI 5.15 monthly release** for Xiaomi 13 Ultra (*ishtar*, Snapdragon 8 Gen 2 / SM8550) with SukiSU, mount-only SUSFS, BBRv3, and optimized LZ4/LZ4KD ZRAM.

Each build automatically resolves the newest certified release tag, matching toolchains, and upstream dependencies without manual month-pinning.

---

## How to Build

Builds are triggered manually via GitHub Actions:

1. Open the [Kernel Build](https://github.com/mk5566/GKI_KernelSU_SUSFS/actions/workflows/kernel-build.yml) workflow.
2. Click **Run workflow** and select the branch **`main`**.
3. Choose the appropriate **`sukisu_channel`**:
   - **`stable` (Recommended / Default)**: Uses stable SukiSU releases for maximum day-to-day reliability.
   - **`dev`**: Uses bleeding-edge SukiSU development builds for early testing of new features.

   > [!IMPORTANT]
   > Make sure to install the matching SukiSU Manager APK (stable or dev) on your device to ensure UAPI compatibility with the selected kernel channel.

4. Once the workflow completes, download the artifacts:
   - **`boot.img`** and **`AnyKernel3.zip`**
   - **`kernel-evidence-RUN_ID`** (contains build verification metadata, `.config`, and certified export CRC logs)

---

## Kernel Profile & Optimizations

- **Certified GKI 5.15 Base**: Builds against official certified AOSP Android 13 5.15 manifests. Preserves full Clang LTO, CFI, module versioning, SELinux, preemption, and Google-certified vendor symbol CRCs.
- **Root & Hiding**: SukiSU `builtin` integration with mount-only SUSFS (`CONFIG_KSU_SUSFS_SUS_MOUNT=y`). KPM, debug hooks, and verbose root informational logging are compiled out.
- **ZRAM Compression**:
  - **`lz4` (Default)**: Official upstream **LZ4 v1.11.0** private freestanding implementation with Qualcomm Snapdragon 8 Gen 2 (ARMv9 Cortex-X3 / A715 / A710) optimizations (64-byte wildcopy, fast dec loop, and context-reuse avoiding full table clears on 4KB pages).
  - **`lz4kd` (Optional)**: Available as an alternate backend via sysfs (`echo lz4kd > /sys/block/zram0/comp_algorithm`). Other unused backends are removed.
  - Exported GKI LZ4 library and header remain untouched to guarantee vendor module compatibility.
- **TCP Congestion Control**: Google **BBRv3** as default (adapted for Linux 5.15 with 104-byte KMI-safe socket area), stock **BBRv1** built-in as per-socket fallback, and FQ pacing enabled.
- **Memory & Power Management**: Multi-Gen LRU (MGLRU) enabled by default; TEO cpuidle governor default; Lazy RCU kept at certified default (off).
- **Core Kernel Tweaks**: Retains safe s2idle retry handling, alarmtimer wake timeout, clear-page cache alignment, and idle CPU scan ordering.

---

## Testing & Installation

### Temporary Boot (Recommended First Step)
Test the kernel without flashing to verify bootability:
```sh
fastboot boot boot.img
```
*(Note: `boot.img` is a generic ramdisk-less header-v4 image; your device ramdisk must reside in `init_boot`.)*

### Flashing via Custom Recovery
Flash **`AnyKernel3.zip`** through TWRP or OrangeFox to patch your existing device boot partition in-place.

### Device-Specific Repack (Optional)
If your stock boot image contains vendor-specific ramdisk or cmdline fields:
```sh
python .github/workflows/scripts/repack_boot.py --base-boot stock-boot.img \
  --image Image --output test-boot.img --tools /path/to/mkbootimg
fastboot boot test-boot.img
```

---

## Repository Checks

Validate the build system and configuration integrity locally:
```sh
python -X utf8 -m unittest discover -s .github/workflows/scripts/tests -q
git diff --check
```
