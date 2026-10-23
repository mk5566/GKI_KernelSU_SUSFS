# Xiaomi 13 Ultra: stable GKI + SukiSU + mount-only SUSFS

Build the newest **published, certified android13-5.15 monthly GKI release**.
Each run resolves that tag, its commit, and the matching month's manifest.
The source does not pin a month: a later certified release is selected on
its own. Point-release tags on the development/LTS branch are not selected.
[Official GKI releases](https://source.android.com/docs/core/architecture/kernel/gki-android13-5_15-release-builds).

## Build with GitHub Actions

1. Open [Kernel Build](https://github.com/mk5566/GKI_KernelSU_SUSFS/actions/workflows/kernel-build.yml).
2. Select **Run workflow**, use branch **main**, and keep **sukisu_channel: stable**.
   Both `stable` and the existing `dev` choice now follow the latest upstream SukiSU.
3. Download the successful run's artifact containing `boot.img` and `AnyKernel3.zip`.
4. Keep the separate **kernel-evidence-RUN_ID** download with those images.

The workflow is manual only. Pushing to `main` does not start a build. Both
channel choices use the same fixed kernel feature set described below.
CI resolves sources, tests the exact SukiSU checkout, validates configuration,
compiles, checks certified export CRCs, and verifies packaging/evidence. Upstream
changes during the job cannot change the selected revisions. Required source or
configuration changes fail explicitly, without fuzz or skipped hunks.
CI also resolves the latest certified GKI release and validates both its pinned
manifest and its symbol CRC reference through AOSP's
[public artifact download route](https://android.googlesource.com/kernel/build/+/refs/heads/main/gki/download_from_ci)
before syncing or compiling the kernel. Both downloads retain the exact build ID
  and `kernel_aarch64` target; unavailable or invalid certified data is fatal.

## Dependency updates

Every run resolves fresh upstream revisions into `sources.json` once. Testing,
checkout and compilation use that same record, and actual checkouts must match.
There is no fallback to an old source when resolution or integration fails.

| Dependency | Update policy |
| --- | --- |
| GKI | Newest published certified monthly branch release |
| Compiler, kernel build tools, boot tools | Exact revisions in that certified build's manifest |
| SukiSU | Latest default-branch HEAD; CI tests the exact commit compiled |
| SUSFS | Latest `gki-android13-5.15` HEAD, mount-only integration |
| LZ4KD sources | Latest `SukiSU_patch/main` HEAD; only the three codec components are copied |
| AnyKernel3 | Latest `gki-2.0` HEAD |
| Repo source-sync tool | Latest `stable` branch, resolved to an exact commit |
| Python / CI actions | Latest stable Python 3; current v7 action tags follow upstream fixes |
| Ubuntu build packages | Fresh package index and current packages from Ubuntu 24.04 repositories |
| Local BBRv3 / LZ4 ports | Check Google's `v3` HEAD and LZ4's latest stable release every run |

BBRv3 and private LZ4 are reviewed local integrations, not replaceable upstream
kernel trees. If either upstream advances, the build stops with an update request;
rebase the port and validate it before building again. Required patch changes,
configuration drift and vendor CRC mismatches also stop the build.

## Kernel profile

- Built-in SukiSU follows the latest upstream default-branch HEAD for both
  channel choices. Each run resolves it once to an exact commit for setup and
  checkout, recorded in `BUILD_INFO.md`. The kernel uses that source's UAPI;
  the manager must use the same UAPI. The mount integration anchors additions
  to core build entries and hook includes, so unrelated upstream feature entries
  can change without requiring a rebase. Regression tests cover the previous
  layout and the October 18 mount-hide additions; required hook changes remain fatal.
- SUSFS mount hiding only, preserving the filesystem/root integration's scope.
  Helper revisions resolve independently of the certified GKI release.
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
generates and verifies `.config` before compiling, then checks the final build's
configuration again. Checks include performance optimization, quiet root logging,
the mount-only profile, BBR selection, zram, security metadata and runtime defaults.
Both stages run through AOSP's `build/build.sh` with the same build configuration
and certified tools. Preflight stops at the post-defconfig hook, after resolving
Kconfig dependencies with the manifest's `pahole`, Clang and host toolchain.
The build retains AOSP KMI symbol lists and trimming. After compilation, every frozen
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

## Successful-build evidence

The separate evidence artifact contains:

- `BUILD_INFO.md`: build result, compiler, source revisions and image hash.
- `sources.json`: exact dependency commits, certified source manifest, builder
  commit, runner identity, Python version, and hashes of build scripts, workflow,
  local patches and vendored codec files.
- `checkouts.json`: verified revisions of the actual kernel, tools and helpers.
- `.config`: the final configuration used by the completed build.
- `CONFIG_REPORT.json`: required/actual settings and configuration hashes before
  compilation and after the build, including runtime command-line checks.
- `ABI_REPORT.json`, `vmlinux.symvers`, `GKI-reference.symvers`: CRC result and
  both tables used for the comparison.
- `SHA256SUMS`: checksums for the evidence files, `boot.img`, `AnyKernel3.zip`,
  and the uncompressed `Image` inside the zip. To check all entries, extract the
  evidence and boot artifact into one directory and copy `Image` there from the zip.

Missing or stale evidence makes the build fail. Evidence creation rechecks the
published CRC tables and verifies the zip contains the matching kernel Image.
Failure diagnostics retain the resolved source record, available configuration
reports/symbol tables and patch rejects. Local builds require a fresh workspace;
existing source trees, outputs and evidence are preserved.

## Build and temporary boot testing

Run **Kernel Build** in GitHub Actions; choose `stable` or `dev`. The job tests
the builder, integrates sources, compiles, checks the KMI, and uploads
`boot.img`, `AnyKernel3.zip`, and the separate evidence artifact.

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

Historical baseline: [run 37125240502](https://github.com/mk5566/GKI_KernelSU_SUSFS/actions/runs/37125240502)
at `d6fec11` passed 8,639 certified CRCs and temporarily booted Xiaomi 13 Ultra
(`ishtar`), HyperOS 3 / Android 16, with root, enforcing SELinux and 404 vendor
modules. Functional checks covered both zram codecs, PLB namespace isolation and
BBRv3 TCP. They did not establish long-term stability or comparative performance.

[Run 37288766042](https://github.com/mk5566/GKI_KernelSU_SUSFS/actions/runs/37288766042)
passed compilation/CRCs but failed temporary boot with forced lazy RCU and
`rcu_nocbs=all`; those settings remain disabled. New upstream dependency revisions
need new device validation. No phone operations run as part of project checks.

## Repository checks

```sh
python -X utf8 -m unittest discover -s .github/workflows/scripts/tests -q
git diff --check
```

CI additionally enables `GKI_TEST_NETWORK=1` and supplies `GKI_TEST_SOURCES` and
`SUKISU_TEST_SOURCE` to test the resolved public downloads and real SukiSU source.
