# Build audit — 2026-09-23

## Outcome

**No release image was produced or flashed.** The GitHub Actions build was not dispatched. The exact upstream GKI defconfig differs from the phone's built-in zRAM/zsmalloc deployment. The two on-disk system_dlkm modules are from 5.15.194 and are unqualified for this 5.15.211 build. The builder now rejects the mismatch before compilation. Passing a manifest-only dry run is not a kernel build.

The working tree was already uncommitted when this run began. Its latest-month target resolver and removal of the 5.15.180 layout were preserved; this run adds the conservative patch/build changes on top. Do not assume the current worktree is a clean checkout.

The project build path is now manual GitHub Actions only. The workflow retains `workflow_dispatch` as its sole event, resolves the target once, records the selected common SHA, checks the synced SHA, uses per-run ccache keys, creates checksums before artifact upload, and gives repository write permission only to the optional release job. No workflow was dispatched during these edits.

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

## Remaining blockers and manual rollback

1. Implement a safe built-in zRAM/zsmalloc compressor path that preserves the phone's init/runtime behavior. The available third-party LZ4KD patch is unsuitable as-is: it changes `kernel/module.c` to accept module-version mismatches and adds module blacklist behavior. Do not re-enable it wholesale.
2. Manually dispatch the GitHub Actions workflow when the deployment gate is resolved. Its clean Linux workspace must retain `manifest.lock.xml`, pass `check_defconfig` and strict ABI/KMI/module-list checks, then provide the final `.config` and full log. Do not remove a check because it exposes a mismatch.
3. Compare vendor/ODM/system_dlkm module dependencies and KMI symbol versions to the built image. The baseline has 405 loaded modules, 295 distinct vendor `.ko` basenames in two partition views, and two nested 5.15.194 system_dlkm `.ko` files. A basename-only reconciliation leaves 115 loaded names without a path match in these mounted directories; trace them before release. The raw private inventory is under `device_profiles/20260923T040039Z/`.
4. Review packaging for ishtar before flashing. The phone is on slot `_a`; its `boot_a` partition is **201,326,592 bytes**, while the removed generic test-key boot recipe used a fixed 64 MiB footer. `init_boot_a` is 8 MiB and `vendor_boot_a` is 96 MiB. No boot image is generated by the revised build; the AnyKernel installer remains unqualified.
5. Before any owner-performed flash, save and hash the original images or the exact matching ROM package, verify the target slot/partition and recovery route, and keep a known-good image available. If boot, swap, modules, thermal, suspend, or core hardware fail, restore the matching known-good image through the previously verified recovery route. Do not relock a modified device on the assumption that a generated key matches OEM trust.

See [VALIDATION_PLAN.md](VALIDATION_PLAN.md) for the post-flash acceptance checks. No phone setting needs changing for the current read-only engineering pass.
