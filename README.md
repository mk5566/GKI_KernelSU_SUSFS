# GKI SukiSU-Ultra + SUSFS Build System (Slim 5.15)

Automated Generic Kernel Image (GKI) build system for Xiaomi 13 Ultra (`ishtar`) tracking the newest published **android13-5.15** monthly branch, with integrated SukiSU-Ultra and a limited SUSFS profile. The manual workflow offers Stable or Dev SukiSU, with built-in ishtar LZ4KD zRAM, upstream BBRv1 as the default TCP controller, no optional tuning patches, no release job, and no notification branch. Eight unsafe or unqualified overrides have been removed and are rejected if reintroduced.

> [!NOTE]
> The current engineering target is Xiaomi 13 Ultra (`ishtar`). The connected device baseline is in [DEVICE_BASELINE.md](DEVICE_BASELINE.md). A successful repository dry run does not qualify an image for flashing.

---

## Features

| Feature | Description | Status |
|---|---|---|
| [SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra) | Advanced kernel-based root solution | Included |
| SUSFS | Limited mount profile; correctness and module compatibility still need a full build and device checks | Included |
| TCP | Upstream BBRv1 and CUBIC; BBRv1 is the CI default. | Fixed profile |
| zRAM | Built-in zRAM/zsmalloc with ishtar LZ4KD. | Fixed profile; full device qualification still required |
| Patch safety | Eight overrides retired; pinned-source regression checks before integration, compilation and packaging. | Build-time policy; not device qualification |
| Optional tuning patches | Present only for local engineering review; the CI workflow applies none. | Disabled in CI |

---

## Manual GitHub Actions build

This project builds only through a manually dispatched GitHub Actions workflow. Pushing a commit does not start a build. The workflow offers a Stable or Dev SukiSU choice.

1. Open the repository **Actions** tab.
2. Select **Build Xiaomi 13 Ultra Kernel**.
3. Click **Run workflow**.
4. The workflow resolves the newest published Android 13 / 5.15 GKI target once, validates the repository safety policy, and builds the fixed ishtar profile.
5. Download the uploaded artifacts and review `Image`, the AnyKernel candidate, `BUILD_INFO.md`, `final.config`, `Module.symvers`, `manifest.lock.xml`, `source-safety.json`, `target-selection.json`, the build log, and `SHA256SUMS.txt`.

The default CI profile uses `Stable(standard)` SukiSU, built-in LZ4KD, upstream BBRv1, an empty optional-patch selection, and no GitHub release or Telegram notification path. The command-line backend retains engineering switches for local diagnostics, but GitHub Actions does not expose them.

The ishtar built-in LZ4KD source patch addresses the earlier pre-build zRAM gate. The build now uploads `Module.symvers` for a read-only vendor CRC comparison: `python .github/workflows/scripts/module_crc_audit.py Module.symvers --adb --adb-path C:/Apps/Platform-tools/adb.exe`. This audits modules visible under `/vendor/lib/modules` without saving their binaries. A successful CI build or CRC comparison still does not prove bootability, complete module compatibility, suspend behavior, storage durability, thermals, battery life, or performance on the phone.

---

## Image and flash boundary

The archive contains raw `Image`, an AnyKernel ZIP, and a 64 MiB Header v4 `boot.img` with a generated AVB test key. This image is for an owner-run **temporary `fastboot boot` comparison**, matching the format used by builds #60 and #61. #60 boots by that method; #61 does not. The device boot partition is physically 192 MiB, but a 192 MiB image is no longer a requirement for this temporary-boot experiment. Direct flashing remains unqualified. A successful CI build does not prove vendor-module CRC compatibility or device boot. Follow [BUILD_AUDIT.md](BUILD_AUDIT.md) and [VALIDATION_PLAN.md](VALIDATION_PLAN.md) before any phone test.

---

## Patch policy (`patches/5.15`)

Use a clean extraction of the hardened project, or apply the repository-level
delivery diff to the matching original project. Merely overlaying ZIP contents
can leave the eight deleted patch files behind; the new guard deliberately
rejects those leftovers. Do not put the delivery diff into the kernel patch
manifest or apply it to `kernel/common`.

`APPLY_ORDER.txt` is authoritative. Required `!` entries apply exactly or the build fails. Optional entries remain available only to the local engineering backend; the GitHub workflow always passes an empty optional selection. CI retains built-in zRAM/zsmalloc and LZ4KD, does not request ZSTD, and selects upstream BBRv1. The LZ4KD codec files come from the freshly cloned SukiSU_patch revision; its `kernel/module.c` changes are excluded.

| Alias for `optional_patches` | Retained experiment |
|---|---|
| `cpu-scan` | `adjust_cpu_scan_order.patch` |
| `clear-page` | `clear_page_16bytes_align.patch` |

The GitHub workflow always leaves `optional_patches` empty. The two retained experiments are not performance- or device-qualified and are not part of the CI build.

**Retired and rejected:** `bbrv3`, `memcmp`, `freeze-timeout`, `s2idle-wake`,
`alarm-wakeup`, `f2fs-congestion`, `f2fs-fsync`, and `irq-log`. Their patch files
have been removed, not replaced by no-ops. Stale local aliases and restored patch files fail validation; CI has no tuning-patch input to select them. Original names,
SHA-256 hashes and reasons are in
[`RETIRED_PATCHES.json`](patches/5.15/RETIRED_PATCHES.json).

This correction retains the selected upstream TCP, scalar AArch64 `memcmp`,
freezer, s2idle, alarmtimer, F2FS policy and rate-limited IRQ failure warning.
It **does not supply a repaired BBRv3 backport**. The GitHub workflow selects upstream BBRv1 explicitly. Existing ROM scripts that request `bbr3` must be reviewed before trying an image without it; the build does not rewrite the phone's settings.

Protected source bytes are checked against the common commit captured before
helper setup, not a helper-modified `HEAD`. The checks reject changed protected
files, new BBRv3/PLB backport source, and enabled BBR3 configuration. They run at
multiple integration/build/package boundaries and emit `source-safety.json`.
An upstream monthly change that violates this policy stops the build for
review. These checks do not replace canonical defconfig, full compilation,
ABI/KMI or module validation. See [PATCH_SAFETY_AUDIT.md](PATCH_SAFETY_AUDIT.md)
for verified findings, scope and limitations.

The safety checks add build-time work only; no new kernel hot-path logic was introduced. Performance equality with the removed patches or the running custom kernel has **not** been measured. The simplified workflow uploads artifacts only and has no release path.

---

## Repository Structure

```
.
├── .github/
│   └── workflows/
│       ├── kernel-build.yml       # latest android13-5.15 workflow
│       └── scripts/               # Python build engine & config
├── patches/
│   ├── 5.15/                      # Active order, retained experiments and retirement inventory
│   └── susfs/                     # SUSFS patch context adaptation
└── README.md
```

## Source selection and minimal SUSFS profile

The workflow queries AOSP's dated `common-android13-5.15-YYYY-MM` manifest branches,
selects the newest, and reads the matching kernel/common Makefile at the selected common revision for the exact
5.15 sublevel. The selection is passed to validation and build without a second network resolution. The CLI rejects an explicit older target. The branch and source revision are recorded in `BUILD_INFO.md`.
The KMI family remains **android13-5.15**.
This does not certify vendor-module ABI compatibility or bootability.

The GitHub workflow uses the recorded stable SukiSU revision `cf87e3f4ddd3f6e5464d85acf56aaa6950e70841`. SUSFS tracks `gki-android13-5.15`. The command-line backend can still pin engineering checkouts, but CI exposes no source-selection inputs. The selected SukiSU build compiles it into the kernel (`CONFIG_KSU=y`)
and apply this repository's mount-only SUSFS integration. The branch name
`builtin` is not required to compile SukiSU into a kernel.

### Manager compatibility (checked 2026-09-21)

This build requires **UAPI 4**, matching Manager `40922-4`. It retains
upstream's scoped su-session driver descriptors and version-matching behavior;
the UAPI constant is not modified. Old kernels built from `builtin` expose
UAPI 2 and still need a UAPI-2 manager until the new kernel is flashed.

The build verifies the requested checkout, rejects other UAPI versions,
applies all required integration patches with zero fuzz, and records the actual source
and UAPI in `BUILD_INFO.md`. Tracking a branch selects its latest revision;
compatibility is established only when the required patches and full kernel compile
succeed. Upstream changes may require a port refresh. After flashing, reboot
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

The latest branch does not guarantee a build or a working device. A full Linux
kernel build, ABI check for the target device, and post-flash checks are still required.
