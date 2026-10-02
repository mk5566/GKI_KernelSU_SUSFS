# Xiaomi 13 Ultra: stable GKI + SukiSU + mount-only SUSFS

Build the newest **published, certified android13-5.15 monthly GKI release**.
Each run resolves the source tag to its commit and pins the matching month's
manifest. Point-release tags on the development/LTS branch are not selected.
As checked on 2026-10-03, the release is `android13-5.15-2026-09_r2`, Linux
`5.15.211`, published October 1. [Official GKI releases](https://source.android.com/docs/core/architecture/kernel/gki-android13-5_15-release-builds).

## Kernel profile

- Built-in SukiSU. `stable` pins the UAPI-4 source from the booting September 21 build;
  `dev` explicitly opts into upstream HEAD. Manager and kernel UAPI must match.
- SUSFS mount hiding only, with the known-booting filesystem/root integration.
  SUSFS, LZ4KD helpers and AnyKernel3 use the September 21 source revisions,
  pinned independently of GKI updates.
  KPM, SukiSU debug, and SUSFS logging are disabled.
- Zram offers **lz4kd** (default) and **lz4** only. LZ4 uses the official
  **1.10.0** freestanding source, a private crypto implementation, caller-owned
  compression state, and bounded decompression. The exported GKI LZ4 header,
  library, and filesystem consumers remain unchanged.
- **Google BBRv3**, adapted for Linux 5.15 and the frozen Android KMI, is the
  TCP default. Stock **BBRv1** remains built in and is selected per socket if
  BBRv3 cannot allocate its state. Reno is the mandatory core TCP fallback;
  other optional congestion algorithms are disabled. FQ pacing remains enabled.
- Preserve GKI memory management, security, vendor hooks, module versioning,
  CFI, full Clang LTO, preemption and timing defaults. Use schedutil/performance
  governors and none/mq-deadline I/O scheduling.
- Retain four small patches: s2idle retry handling, alarmtimer wake timeout,
  clear-page alignment, and idle CPU scan order. No unsafe SIMD `memcmp`,
  forced freezer timeout, or unmeasured F2FS congestion/fsync overrides.

## ABI and patch checks

Every selected patch applies once, without fuzz or silent skips. The build
retains AOSP KMI symbol lists and trimming. After compilation, every frozen
export and CRC is compared with **Google's matching certified build's
`vmlinux.symvers`**. A missing export or differing CRC prevents packaging.
Extra root exports are permitted. This check covers the frozen module KMI;
only device testing can establish bootability and runtime stability.

The BBRv3 backport keeps `icsk_ca_priv` at 104 bytes. Private LZ4 1.10 does not
replace vendor-visible `LZ4_stream_t` or exported `LZ4_*` functions. Debug
symbols are omitted; GKI runtime diagnostics that affect structure layout or
vendor interfaces stay at upstream defaults. CI build output is retained.

## Build and temporary boot testing

Run **Kernel Build** in GitHub Actions; choose `stable` or `dev`. The job tests
the builder, integrates sources, compiles, checks the KMI, and uploads
`boot.img` and `AnyKernel3.zip`. Exact source revisions, image hash, and ABI
check result are recorded in `BUILD_INFO.md` in the build workspace.

`boot.img` is a generic **ramdisk-less header-v4** image for devices whose
generic ramdisk is in `init_boot`. It contains no invented 64 MiB partition
padding, random AVB signing key, or device SPL. It does not reproduce OEM
boot metadata or a ramdisk stored in `boot`. Test temporarily:

```sh
fastboot boot boot.img
```

For a device-specific test image, preserve a known booting boot image's
ramdisk, header, cmdline and other unpacked fields with the AOSP boot tools:

```sh
python .github/workflows/scripts/repack_boot.py --base-boot old-boot.img \
  --image Image --output test-boot.img --tools /path/to/tools/mkbootimg
fastboot boot test-boot.img
```

The compiled `Image` is also inside `AnyKernel3.zip`. A local Linux build can
pass `--base-boot /path/to/old-boot.img` to `build.py`. Repacking replaces the
kernel and drops the old signature/footer, which cannot authenticate a
changed kernel. AnyKernel3 repacks the device's installed boot image.

A passing compile/KMI check does not prove the Xiaomi boots. Retain the old
flashed kernel while testing. Boot-loop diagnosis needs the device model,
ROM, failed build identity, and preferably the previous-boot panic/pstore
record. No phone settings or flashed partitions are changed by this project
repair.
