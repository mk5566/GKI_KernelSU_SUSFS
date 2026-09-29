# Android 13 Linux 5.15 GKI-derived kernel builder

This project builds a custom arm64 kernel from the newest official
`android13-5.15.<sublevel>_r<revision>` point-release tag in AOSP
`kernel/common`. Each run selects the highest numeric sublevel and revision,
pins the peeled Git commit, and verifies that the checkout's Makefile reports
`5.15.x`. A new point tag requires no builder source edit. If a selected patch
stops applying, the build fails at that tag and identifies the patch.

The build includes SukiSU-Ultra, a mount-only SUSFS integration, and ZRAM with
LZ4KD. It intentionally disables frozen GKI KMI/ABI enforcement and **does not
claim frozen GKI KMI compatibility**. Device boot, vendor module loading, and
manager compatibility require validation on the target device.

## Build choices

GitHub Actions → **Kernel Build** → **Run workflow** exposes exactly:

| Input | Choices | Default |
|---|---|---|
| `sukisu_channel` | `stable`, `dev` | `stable` |
| `bbr_version` | `v1`, `v3` | `v3` |
| `apply_tweaks` | `true`, `false` | `true` |

`stable` resolves GitHub's latest formal SukiSU release tag to its exact
commit. `dev` resolves the official default branch HEAD to its exact commit.
The builder checks that SukiSU's setup script actually checked out that commit
and records the kernel UAPI. Manager UAPI must match the built kernel; the
release version label alone does not establish compatibility. SUSFS is pinned
in builder source and has no workflow override.

`v1` uses the stock Android 5.15 BBR implementation. It applies no BBRv3 or
PLB backport patches and sets the default congestion algorithm to `bbr`.
`v3` applies every patch in `patches/bbrv3/APPLY_ORDER.txt` and registers the
backport as `bbr3`, so it can coexist in source with stock BBR without naming
collision. The backport is based on [Google's official `google/bbr` v3 branch](https://github.com/google/bbr/tree/v3).
The reference commit is recorded in `BUILD_INFO.md`. The series separates TCP
rate and ACK infrastructure, PLB, the algorithm, and Kconfig wiring. It removes
the previous fake `__GENKSYMS__` TCP structure layout and placeholder fields.

`apply_tweaks=true` applies all nine patches in
`patches/tweaks/APPLY_ORDER.txt`; any missing or conflicting patch is fatal.
`false` applies none. These performance tweaks include ARM64, scheduler,
F2FS, IRQ, freezer, and wakeup changes. Patch application proves source
compatibility only; test performance, suspend, and storage behavior on the
target device before relying on them.

## Source and config preflight

The builder resolves sources, syncs the manifest with `kernel/common` pinned to
the selected tag commit, integrates SukiSU and SUSFS, copies LZ4KD source,
applies the selected patches with `git apply --check` followed by `git apply`,
performs source transformations, generates `.config`, and asserts the requested
settings **before** compilation. The `--preflight-only` CLI path stops after
these same steps. There is no file-presence-only dry run or patch fuzz.

The SUSFS port retains an allowlist of mount-related filesystem changes and
does not import legacy root hooks. Only `CONFIG_KSU_SUSFS` and
`CONFIG_KSU_SUSFS_SUS_MOUNT` are selected. The exact SUSFS and SukiSU commits,
applied patch names, final kernel hash, and compiler version appear in
`BUILD_INFO.md` when available.

The standard ZRAM profile compiles LZ4KD and advertises only `lz4kd` to ZRAM.
The local patch deliberately excludes the helper project's unrelated
`kernel/module.c` changes. Other crypto compression code may remain in the
generic kernel because filesystem features can use it; ZRAM does not offer it.

The config keeps TCP Reno and the selected BBR, FQ pacing, the core Android
networking stack, the `none` and `mq-deadline` I/O schedulers, and the
`schedutil` and `performance` CPU frequency governors. It disables optional
TCP congestion algorithms, BFQ, Kyber, and unused powersave, conservative,
ondemand, and userspace governors. Core fair, RT, and deadline scheduling and
Android scheduler infrastructure are untouched. Final generated `.config`
assertions catch dependencies that overturn these settings.

## GitHub Actions execution

Run **Kernel Build** from the repository's Actions tab and select the three
choices above. The Ubuntu 24.04 job installs host build dependencies, syncs
only the AOSP manifest projects needed for this kernel, and uses the manifest's
Clang release for both generated-config validation and compilation. Its source
preflight and build use the same integration path. Python tests and Git diff
checks can run locally, but this Windows PC is not the kernel build host.

Successful workflow runs upload the AnyKernel3 zip and `BUILD_INFO.md`, with a
boot image when packaging succeeds. The boot image is ramdisk-less and signed
with a test key; use it only where the device's boot layout permits it. The
builder does not flash a device or certify vendor module compatibility.
