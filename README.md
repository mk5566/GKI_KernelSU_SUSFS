# Xiaomi 13 Ultra: stable GKI + SukiSU + mount-only SUSFS

Build the newest **published, certified android13-5.15 monthly GKI release**.
Each run resolves that tag, its commit, and the matching month's manifest.
The source does not pin a month: a later certified release is selected on
its own. Point-release tags on the development/LTS branch are not selected.
As checked on 2026-10-03, the release was `android13-5.15-2026-09_r2`, Linux
`5.15.211`, published October 1. [Official GKI releases](https://source.android.com/docs/core/architecture/kernel/gki-android13-5_15-release-builds).

## Build with GitHub Actions

1. Open [Kernel Build](https://github.com/mk5566/GKI_KernelSU_SUSFS/actions/workflows/kernel-build.yml).
2. Select **Run workflow**, use branch **main**, and keep **sukisu_channel: stable**
   for the reviewed source stack. Choose `dev` only to test upstream SukiSU.
3. Download the successful run's artifact containing `boot.img` and `AnyKernel3.zip`.

The workflow is manual only. Pushing to `main` does not start a build. Both
channel choices use the same fixed kernel feature set described below.

## Kernel profile

- Built-in SukiSU. `stable` pins the UAPI-4 source from the booting September 21 build;
  `dev` explicitly opts into upstream HEAD. Manager and kernel UAPI must match.
- SUSFS mount hiding only, with the known-booting filesystem/root integration.
  SUSFS, LZ4KD helpers and AnyKernel3 use the September 21 source revisions,
  pinned independently of GKI updates.
  KPM, SukiSU debug, and SUSFS logging are disabled. KernelSU's unconditional
  informational messages are compiled out; warning/error diagnostics stay available.
- Zram offers **lz4kd** (default) and **lz4** only. LZ4 uses the official
  **1.10.0** freestanding source, a private crypto implementation, caller-owned
  compression state, and bounded decompression. The exported GKI LZ4 header,
  library, and filesystem consumers remain unchanged.
  OEM requests to load stock zram/zsmalloc modules receive the normal
  already-loaded result when those drivers are built in. Module version
  checks remain enforced for every module that actually loads.
- **Google BBRv3**, adapted for Linux 5.15 and the frozen Android KMI, is the
  TCP default. Stock **BBRv1** remains built in and is selected per socket if
  BBRv3 cannot allocate its state. Reno is the mandatory core TCP fallback;
  other optional congestion algorithms are disabled. FQ pacing remains enabled.
- Multi-gen LRU is enabled by default through the GKI options. Its statistics
  option stays off. The runtime switch remains `/sys/kernel/mm/lru_gen/enabled`.
- Lazy RCU stays at the certified default, off. No CPU is callback-offloaded.
  Forcing lazy callbacks with `rcu_nocbs=all` stalled `fastboot boot`; the
  phone returned to the flashed kernel. Callback offload remains compiled in.
- TEO is the default cpuidle governor. Menu stays compiled and can be selected
  through the cpuidle sysfs interface.
- Multi-gen LRU and TEO use the kernel's own runtime behavior. A passing build
  does not measure latency or battery life, and it does not show that the image
  boots. That still needs the device.
- Preserve GKI security, vendor hooks, module versioning, CFI, full Clang LTO,
  preemption, timing, cpufreq governors and I/O schedulers. Keep governor
  support that supplies frozen vendor-facing cpufreq exports.
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
replace vendor-visible `LZ4_stream_t` or exported `LZ4_*` functions. Certified
DWARF/BTF metadata and runtime settings that affect vendor interfaces stay at
upstream defaults. BTF module metadata affects `struct module` and cannot be
disabled without changing vendor symbol CRCs. Routine root informational
logging is disabled; CI build output is retained.

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

## Device validation

On October 3, 2026, [build 37125240502](https://github.com/mk5566/GKI_KernelSU_SUSFS/actions/runs/37125240502)
at `d6fec11` passed compilation, the module-order check, the final configuration
checks and all 8,639 certified export CRCs. Its kernel temporarily booted a
Xiaomi 13 Ultra (`ishtar`) running HyperOS 3 / Android 16 and reached Android
with root access, enforcing SELinux and the same 404 vendor modules as the
working fallback. WALT governors, OEM storage schedulers and FQ remained active.

Both zram codecs round-tripped 8 MiB of mixed pages exactly; only lz4/lz4kd
were listed, and deflate/lzo/zstd/lz4hc selections were rejected. Sixteen
disposable network namespaces verified PLB allocation, initialization and
isolation. Phone TCP sockets using the BBRv3 default echoed 1 MiB exactly.
Temporary test devices and forwarding were removed; Android's zram0 stayed
configured at 16 GiB. No partitions were flashed.

These are boot and functional checks, not a long-duration stability test or
a comparative performance benchmark. BBRv1 is available, but its allocation
failure fallback was not forced during device testing.

Run [37288766042](https://github.com/mk5566/GKI_KernelSU_SUSFS/actions/runs/37288766042)
at `eb59b4c` also passed compilation and the 8,639 CRC check. `fastboot boot`
of that image did not start Android, and the phone returned to the flashed
kernel. That image forced lazy RCU on and appended `rcu_nocbs=all`. Those
settings are no longer applied. That image's version string ended in `-dirty`
because `setlocalversion` marks a patched tree. The booting kernel above has
the same suffix. Later builds keep the GKI commit and use the UTC build date
there instead, such as `-oct6`.
