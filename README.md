# GKI SukiSU-Ultra + SUSFS Build System (Slim 5.15)

Automated Generic Kernel Image (GKI) build system tracking the newest published **android13-5.15** monthly branch, with integrated SukiSU-Ultra and a limited SUSFS profile. The current September 2026 branch reports **5.15.211** (checked 2026-09-23). The default source candidate uses built-in ishtar LZ4KD zRAM and leaves upstream TCP unchanged. Native ZSTD, upstream BBRv1, and the saved performance patches are per-build choices.

> [!NOTE]
> The current engineering target is Xiaomi 13 Ultra (`ishtar`). The connected device baseline is in [DEVICE_BASELINE.md](DEVICE_BASELINE.md). A successful repository dry run does not qualify an image for flashing.

---

## Features

| Feature | Description | Status |
|---|---|---|
| [SukiSU-Ultra](https://github.com/SukiSU-Ultra/SukiSU-Ultra) | Advanced kernel-based root solution | Included |
| SUSFS | Limited mount profile; correctness and module compatibility still need a full build and device checks | Included |
| TCP | Preserve the branch default; `--bbr` requests upstream BBRv1 | No override by default |
| zRAM | Built-in zRAM/zsmalloc and LZ4KD by default; `--zram` requests native ZSTD. | Source candidate; full build unverified |
| Performance patches | Each saved patch can be selected by alias in a manual run. | All off by default |

---

## Manual GitHub Actions build

This project builds only in GitHub Actions. The workflow has a single `workflow_dispatch` trigger: pushing a commit does not start a build. Run it yourself from the repository's default branch after reviewing the inputs.

1. Navigate to the **Actions** tab in your repository.
2. Select **Kernel Build**.
3. Click **Run workflow**.
4. Review the fixed target (`android13` / `5.15`) and configure options:
   * **Sublevel and OS patch**: resolved from the newest published Android GKI monthly branch
   * **SukiSU Version**: `Dev(development)` tracks `main`; `Stable(standard)` uses a recorded revision
   * **SUSFS**: tracks the `gki-android13-5.15` branch unless a commit is supplied
   * **Native ZSTD request**: `false` by default; leave off for the current `lz4kd` phone baseline.
   * **Upstream BBRv1 request**: `false` by default.
   * **Optional patches**: leave blank for none, or enter comma-separated aliases from the table below.
5. Review the uploaded `Image`, AnyKernel candidate, build log, `BUILD_INFO.md`, final `.config`, locked manifest, target selection, and `SHA256SUMS.txt`. The target is selected once per run; a changed common branch causes a failed build instead of silently building different source. No boot image is generated. The GitHub workflow has not been run for the modified tree in this Windows session.

The `make_release` and `send_telegram` options are off by default. A release is handled in a separate job after the uploaded files pass checksum verification. The build job uses read-only repository permission; only the optional release job receives `contents: write`. The Python CLI is an internal workflow component; local runs are for diagnostics only.

The ishtar built-in LZ4KD source patch addresses the earlier pre-build zRAM gate. The full Linux build, canonical defconfig, KMI and module checks have not yet been run for this candidate.

---

## Image and flash boundary

The builder does not produce a generic `boot.img`: its previous header-v4/test-key recipe was not checked against this phone's stock boot image. The AnyKernel package also needs device-specific review. **Do not flash an artifact solely because the build succeeds.** Follow [BUILD_AUDIT.md](BUILD_AUDIT.md) and [VALIDATION_PLAN.md](VALIDATION_PLAN.md); the owner performs any flash only after image packaging, recovery, and rollback are verified.

---

## Patch policy (`patches/5.15`)

`APPLY_ORDER.txt` is authoritative. `!` entries always apply; `?alias:filename.patch` entries apply only when their alias is selected for that run. The default retains built-in zRAM/zsmalloc and LZ4KD; `--zram` selects ZSTD for a separate experiment. The LZ4KD codec files come from the freshly cloned SukiSU_patch revision; its `kernel/module.c` changes are excluded. A selected patch must apply exactly or the build fails. The artifact name and `BUILD_INFO.md` record the selected set.

| Alias for `optional_patches` | Saved patch |
|---|---|
| `cpu-scan` | `adjust_cpu_scan_order.patch` |
| `clear-page` | `clear_page_16bytes_align.patch` |
| `f2fs-fsync` | `f2fs_enlarge_min_fsync_blocks.patch` |
| `f2fs-congestion` | `f2fs_reduce_congestion.patch` |
| `alarm-wakeup` | `minimise_wakeup_time.patch` |
| `s2idle-wake` | `avoid_extra_s2idle_wake_attempts.patch` |
| `irq-log` | `silence_irq_cpu_logspam.patch` |
| `memcmp` | `optimise_memcmp.patch` |
| `freeze-timeout` | `reduce_freeze_timeout.patch` |
| `bbrv3` | `0001-net-tcp-backport-BBRv3-to-android13-5.15.patch` |

Example manual input: `cpu-scan,clear-page`. Selecting `bbrv3` builds it in and makes it the default TCP congestion control; leave **Upstream BBRv1 request** off for that run. These patches remain unverified, and the [change plan](KERNEL_CHANGE_PLAN.md) identifies concrete defects in `bbrv3`, `memcmp`, and `freeze-timeout`. A build with them selected is an experiment, not a device-qualified release. Keep `make_release` off while testing.

---

## Repository Structure

```
.
├── .github/
│   └── workflows/
│       ├── kernel-build.yml       # latest android13-5.15 workflow
│       └── scripts/               # Python build engine & config
├── patches/
│   ├── 5.15/                      # Active patch order and inactive review references
│   └── susfs/                     # SUSFS patch context adaptation
└── README.md
```

## Source selection and minimal SUSFS profile

The workflow queries AOSP's dated `common-android13-5.15-YYYY-MM` manifest branches,
selects the newest, and reads the matching kernel/common Makefile at the selected common revision for the exact
5.15 sublevel. The selection is passed to validation and build without a second network resolution. The CLI rejects an explicit older target. The branch and source revision are recorded in `BUILD_INFO.md`.
The KMI family remains **android13-5.15**.
This does not certify vendor-module ABI compatibility or bootability.

The default SukiSU option tracks `main`; Stable selects the recorded SukiSU
revision `cf87e3f4ddd3f6e5464d85acf56aaa6950e70841`.
SUSFS tracks `gki-android13-5.15`. Optional SukiSU and SUSFS commit inputs
can pin either checkout. Both SukiSU options compile it into the kernel (`CONFIG_KSU=y`)
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
