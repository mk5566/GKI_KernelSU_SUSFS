# Ishtar GKI 5.15 project rules

Read [DEVICE_BASELINE.md](DEVICE_BASELINE.md), [KERNEL_CHANGE_PLAN.md](KERNEL_CHANGE_PLAN.md), [BUILD_AUDIT.md](BUILD_AUDIT.md), and [VALIDATION_PLAN.md](VALIDATION_PLAN.md) before changing the build. Preserve the existing uncommitted work. The only supported target is the latest published Android 13 GKI 5.15 monthly branch; the old 5.15.180 layout is retired.

The build runs only through the manually dispatched GitHub Actions workflow. Keep `workflow_dispatch` as its sole trigger; a push must never start the build. Do not dispatch a workflow, publish a release, or send a notification while editing or validating the repository. Local Python commands may validate workflow components but are not a build path.

## Device boundary

ADB/root access is for read-only profiling unless the owner gives a separate instruction. Do not reboot, flash, remount, swapoff, reset zRAM, change sysctls/governors, alter AVB/SELinux, or change persistent phone settings during baseline work. Keep raw device captures under ignored `device_profiles/`; do not commit logs, identifiers, tokens, profiles, or original images.

Do not add concealment or attestation-bypass features. Correctness repairs in existing SukiSU/SUSFS integration may be considered only with exact-source application, compilation, and failure-path tests.

## Release gates

- Keep the eight retired aliases and their source overrides unavailable; use `PATCH_SAFETY_AUDIT.md` as the current disposition. Do not implement silent BBRv3-to-BBRv1 substitution.
- Preserve the initial-common-SHA source guard and `source-safety.json` artifact. Do not substitute mutable helper `HEAD` or weaken checks because integration now fails. A protected-source change requires an explicit review.

- Use a fresh build workspace and record `manifest.lock.xml` plus exact helper/common SHAs. Required patches must apply as a whole with zero fuzz; `APPLY_ORDER.txt` is authoritative.
- Keep upstream `check_defconfig`, ABI/KMI enforcement, GKI module lists, and module-signature/version checks. Never change `kernel/module.c` to accept symbol-version mismatches.
- Preserve the phone's built-in zRAM/zsmalloc and usable compressor path. Google's unmodified GKI defconfig has these as modules, while the captured system_dlkm copies have 5.15.194 vermagic; the built-in LZ4KD candidate must still pass canonical defconfig, KMI and module-list validation before it is qualified.
- Pass Python tests, strict repo guard, canonical defconfig, full Linux build, ABI/module checks, and packaging review before proposing any flashable image. A Windows dry run or raw `Image` is not a device-qualified release.
- Only the owner performs a flash after verifying original images and a rollback route. After flashing, follow [VALIDATION_PLAN.md](VALIDATION_PLAN.md) before claiming stability or performance improvement.
