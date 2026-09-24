# Build audit — 2026-09-23

## 2026-09-24 controlled temporary-boot comparison

The owner reports that build #60 boots through `fastboot boot` while build #61, using the same 64 MiB Header v4 format, is handed off by fastboot but does not remain booted. Read-only ADB afterward found the phone on #60; the #61 file in Platform-tools contained the #61 `Image` payload. Pstore and `/proc/last_kmsg` held no failure trace. This A/B result rules out the 64-versus-192 MiB image size as the difference between #60 and #61 temporary boots. It does not qualify direct flashing of a 64 MiB image.

The sole kernel-source change from #60 to #61 restored symbol CRC checks in `kernel/module.c`. All 295 vendor modules readable through `/vendor/lib/modules` declare 5.15.78 vermagic while the test kernel is 5.15.211. Linux ignores the release prefix when module CRCs are present ([Android common loader source](https://android.googlesource.com/kernel/common/+/e51df6ce668a8f75ce27f83ce0f60103c568c375/kernel/module.c)), so this version-string difference alone does not prove a CRC failure. The conservative successor excludes BBRv3 and the retired tuning patches, retains CRC checks, and requires a compiled `Module.symvers` artifact so the actual vendor CRCs can be compared before another device test.

## Historical 2026-09-24 direct-flash failure

Build `132177e` (run 35962562123) passed CI, but the owner reports that flashing its `boot.img` through fastboot did not boot. Read-only ADB after recovery shows the phone is back on the working `gdc9467e8f9bf` kernel. No failed-boot pstore or `/proc/last_kmsg` survived, so the exact stop point is unknown. The raw `Image` and AnyKernel ZIP from this run were not reported as tested.

`blockdev` reports 201,326,592-byte (192 MiB) `boot_a` and `boot_b` partitions. The working Header v4 boot partition has a zero-byte ramdisk, zero OS-version header field, and an AVB footer in the final 64 bytes of that 192 MiB partition. The failed build's Header v4 `boot.img` also has no ramdisk, but is only 64 MiB, with its footer at that incorrect boundary and a nonzero OS-version header field. Its generated AVB public key differs from the working image's. `/proc/bootconfig` reports an unlocked bootloader; Android properties report green/locked, so the properties are not reliable evidence of AVB enforcement on this rooted boot.

The earlier 192 MiB packaging change addressed a direct-flash hypothesis. The newer #60/#61 temporary-boot A/B result supersedes that hypothesis for `fastboot boot`: the successor now produces a 64 MiB test image and still requires `avbtool` and a generated RSA-2048 key. The measured partition size remains a device fact, not a build-size gate for temporary boot. Private header/footer captures and artifact metadata are under ignored `device_profiles/20260924-bootloop-audit/`.

## 2026-09-24 boot regression follow-up

The owner reports a bootloop or stuck logo after installing the `0c6007b` build via both its AnyKernel ZIP and `boot.img`. The downloaded archive contains a raw `Image`; it is the same kernel payload in the ZIP and boot image. The phone is not currently available to ADB, so no pstore or boot log confirms the exact failure.

The archive's `final.config` disables `CONFIG_TMPFS_POSIX_ACL` and `CONFIG_TMPFS_XATTR`; both are enabled in the recorded working phone config. It also drops zRAM writeback and three netfilter settings present on that phone. The builder now restores these settings in Kconfig order and checks the resolved config. Loss of tmpfs xattrs is a plausible early Android boot cause, not a proven diagnosis without a boot log.

The failing build also removed `check_defconfig`, module-order checking, module-list inputs, and three build configs from the initial-common-SHA guard. The repair restores those upstream source guards and uses the upstream `GKI_BUILD_CONFIG_FRAGMENT` hook for the required full-symbol-export, non-enforced-KMI Image-only profile. Canonical defconfig and module-order checks are retained. Vendor ABI compatibility and phone boot remain unverified.

The first replacement run (`adbafa8`, GitHub Actions run 35953829529) compiled the kernel but stopped at the upstream module-order gate: enabling `TCP_CONG_ADVANCED` for BBRv1 also makes BIC, Westwood and H-TCP modules by Kconfig default. The expected GKI list is empty. The builder now explicitly disables those three unused algorithms while keeping BBRv1 and the module-order gate. No flashable replacement artifact came from that failed run.

## Current patch-safety audit addendum

The safety-by-retirement changes are documented in
[PATCH_SAFETY_AUDIT.md](PATCH_SAFETY_AUDIT.md). Eight risky overrides are removed
and hard-rejected; only `cpu-scan` and `clear-page` remain optional. The default
selects neither. BBRv3 is no longer provided. No replacement kernel algorithm
or timing heuristic was introduced.

The regression suite passes 87 tests on Linux. New coverage checks all eight
aliases, restored/renamed patches, enabled BBR3 config, 32 protected source
files against the initially pinned SHA, staged/committed changes, symlinks,
forbidden source additions and compile/package refusal. Source-guard tests use
synthetic Git repositories, not a complete Android kernel. The workflow now
retains and checksums `source-safety.json` with successful artifacts, and uploads
it with failure diagnostics where available.

Existing upstream build configs and `kernel/module.c` are protected byte-for-byte
along with the audited subsystem files. This closes the specific possibility of
accepting helper-committed changes merely because mutable `HEAD` agrees with the
worktree. It is not a general third-party-script security sandbox or ABI checker.
The required zRAM/LZ4KD source patches are unchanged and still require full-build
and module-list qualification. Do not disable strict checks to get a build out.

**Not run in this audit:** exact application to the selected full Android
common tree, canonical defconfig, full compilation, ABI/KMI diff, module load,
installer review on real images, or device testing. No workflow was dispatched,
release published, notification sent, or phone setting changed. The input is a
build project, not a built or flash-qualified image.

## Historical per-build optional patch selection (superseded)

The records below predate retirement. Their earlier source-application and
Windows test results were supplied with the archive; they were not rerun on
those kernel revisions during this audit. The old ten-alias selection policy
and BBRv3 default path no longer exist.

The ten saved performance patches are listed as selectable `?alias:filename`
entries in `patches/5.15/APPLY_ORDER.txt`. The manual GitHub Action leaves all
ten off when `optional_patches` is blank. It accepts comma-separated aliases,
rejects unknown or duplicate names before source sync, and applies selected
patches exactly in manifest order. A selected patch is required for that run;
it cannot be silently skipped. `bbrv3` also requests a built-in BBRv3 default
and cannot be combined with the upstream BBRv1 default input. The artifact
name and build information record the selected set. Exact source application
does not establish compile success, KMI compatibility, runtime safety, or a
performance gain; the known defects remain documented in the change plan.

## Follow-up source candidate (not built)

After the manual build failed at the intentional zRAM gate, the repository
gained a required ishtar LZ4KD patch. It sets zRAM, zsmalloc and LZ4KD built
in, removes their now-obsolete GKI module-list entries, and copies nine codec
files from the freshly cloned SukiSU_patch tree. It never applies that
helper's `kernel/module.c` hunk. Both required patches applied sequentially
with exact checks to the selected September common SHA. The Python suite ran
44 tests (43 passed, one Windows symlink test skipped), strict repo guard
passed, and the manual-only workflow trigger remained unchanged. These checks
do not establish canonical defconfig, a successful Linux build, KMI/module
compatibility, or a bootable ishtar image. The owner must manually run GitHub
Actions to expose those results. The saved BBRv3/SIMD/suspend/F2FS/IRQ series
is still inactive for the defects and missing device evidence in the change
plan.

## Outcome

**No release image was produced or flashed.** The GitHub Actions build was not dispatched. The exact upstream GKI defconfig differs from the phone's built-in zRAM/zsmalloc deployment. The two on-disk system_dlkm modules are from 5.15.194 and are unqualified for this 5.15.211 build. The builder now rejects the mismatch before compilation. Passing a manifest-only dry run is not a kernel build.

The working tree was already uncommitted when this run began. Its latest-month target resolver and removal of the 5.15.180 layout were preserved; this run adds the conservative patch/build changes on top. Do not assume the current worktree is a clean checkout.

The project build path is now manual GitHub Actions only. The workflow retains `workflow_dispatch` as its sole event, exposes no build inputs, resolves the target once, records the selected common SHA, checks the synced SHA, uses per-run ccache keys, and creates checksums before artifact upload. Repository permission remains read-only and the workflow has no release or notification job. No workflow was dispatched during these edits.

## Source and phone evidence

| Item | Verified value |
|---|---|
| Phone | Xiaomi 13 Ultra `ishtar`, Android 16, running `5.15.211-android13-8-gdc9467e8f9bf` |
| Phone common SHA | `dc9467e8f9bfdec0d012f9345ac5f12f63dc7eba` |
| Official September tag target | `0b6028f1f30da3c2143bb40eee912c4974108e5c`; verified ancestor of the phone SHA |
| September branch head checked | `b0c574c9c0b2a6be687cf765a431782414034bb3`, Makefile 5.15.211 |
| SUSFS source checked | `e565931d19256fd821ada01b35263506e7c7a364` |
| SukiSU `main` checked | `cf87e3f4ddd3f6e5464d85acf56aaa6950e70841` |
| SukiSU_patch reviewed | `547ae94bcaec53d030398f857950c64662043a5d` |

Google common source came from `android.googlesource.com/kernel/common`; the month manifest branch came from `android.googlesource.com/kernel/manifest`. The connected device, not the supplied static audit, established the runtime zRAM, module, F2FS, WALT and boot-partition facts.

## Repository changes

- Ported the supplied `0001-fail-closed-patching-and-workflow-inputs.patch` semantics into the already modified checkout: exact whole-patch checks, full reverse checks, strict manifest parsing, stale-source rejection, and workflow dispatch inputs passed through environment variables.
- Ported the supplied `0002-conservative-defaults-native-zstd.patch` semantics: no active BBRv3/SIMD/suspend/F2FS/IRQ performance series; no third-party compressor copy or module-list/signature rewrite; no regex repair of `task_mmu.c`; no forced TCP or zRAM default. Native ZSTD remains opt-in.
- Removed the builder's mutations and command overrides that disabled `check_defconfig`, ABI/KMI enforcement, GKI module lists, and system-DLKM/artifact checks. A source-integrity guard compares the three build-config files with the synced common commit before compiling. Build logs, `final.config`, manifest lock, source identities and hashes are now retained by the build path.
- Added an ishtar zRAM deployment gate. The phone has `CONFIG_ZRAM=y`, `CONFIG_ZSMALLOC=y`, and active `lz4kd`; upstream GKI has both as modules, while the available nested system_dlkm copies have 5.15.194 vermagic. This mismatch must be solved with a reviewed built-in compressor/config and module-list plan before a candidate Image is packaged.
- The supplied SUSFS monitor lifetime C patch remains a candidate. Its exact hunk did **not** apply to the pinned current `fs/susfs.c` snapshot, and it has not been compiled or fault-tested. No additional concealment capability was added.

## Validation performed

| Check | Result |
|---|---|
| Python compileall | Passed |
| Python tests | 42 run: 41 passed, 1 skipped because Windows symlink creation lacks privilege; 12 subtests passed |
| Manual-only workflow trigger and target selection | Passed static workflow check; live target resolution selected September 2026, sublevel 211, common `b0c574c9c0b2a6be687cf765a431782414034bb3` |
| CI target-file dry run | Passed manifest/files validation only; no source sync or compile |
| Workflow YAML and embedded Bash syntax | Parsed with YAML loader; each embedded `run` script passed `bash -n` |
| Build CLI `--dry-run --no-zram --no-bbr` | Passed manifest/files validation only |
| Supplied `repo_guard.py . --strict` | Passed, no flagged patterns |
| `git diff --check` | Passed; Git reported only local CRLF conversion notices |
| SukiSU integration patch against checked `main` | `git apply --check` passed |
| SUSFS context adaptation against checked SUSFS patch | Exact check and staged application passed |
| Selected mount-only SUSFS patch against phone common SHA and September branch head | Exact `git apply --cached --check` passed after line-ending normalization for the Windows test fixture |
| Reboot dispatch patch after mount-only patch | Exact `git apply --cached --check` passed for both source revisions |
| Active native-ZSTD new-file patch | Exact `git apply --cached --check` passed against both source revisions |
| SUSFS monitor lifetime candidate | Failed exact application; deferred |
| Full Linux compile, canonical defconfig, KMI/ABI diff, module load, device boot | **Not run; release blockers** |

## Release blockers and manual rollback (historical details retained)

1. Qualify the existing built-in zRAM/zsmalloc compressor source candidate against the exact target; its addition does not prove the phone's init/runtime behavior is preserved. The available third-party LZ4KD patch is unsuitable as-is: it changes `kernel/module.c` to accept module-version mismatches and adds module blacklist behavior. Do not re-enable it wholesale.
2. Manually dispatch the GitHub Actions workflow when the deployment gate is resolved. Its clean Linux workspace must retain `manifest.lock.xml`, pass `check_defconfig` and module-list checks, then provide the final `.config`, `Module.symvers`, ABI/KMI review inputs, and full log. Do not remove a check because it exposes a mismatch.
3. Compare the uploaded `Module.symvers` with vendor module CRCs using `module_crc_audit.py`, then reconcile vendor/ODM/system_dlkm dependencies and KMI symbol versions with the built image. The baseline has 405 loaded modules, 295 distinct vendor `.ko` basenames in two partition views, and two nested 5.15.194 system_dlkm `.ko` files. A basename-only reconciliation leaves 115 loaded names without a path match in these mounted directories; trace them before release. The raw private inventory is under `device_profiles/20260923T040039Z/`.
4. Use the generated 64 MiB boot image only for a temporary `fastboot boot` comparison. Direct flash packaging and the AnyKernel installer remain unqualified. The measured `boot_a` partition size is **201,326,592 bytes**, but matching that size is not a gate for the temporary test.
5. Before any owner-performed flash, save and hash the original images or the exact matching ROM package, verify the target slot/partition and recovery route, and keep a known-good image available. If boot, swap, modules, thermal, suspend, or core hardware fail, restore the matching known-good image through the previously verified recovery route. Do not relock a modified device on the assumption that a generated key matches OEM trust.

See [VALIDATION_PLAN.md](VALIDATION_PLAN.md) for the post-flash acceptance checks. No phone setting needs changing for the current read-only engineering pass.
