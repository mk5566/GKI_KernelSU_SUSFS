# Ishtar GKI 5.15 control build

This branch starts from the phone-tested `f3d57d4` build recipe. It locks the Android 13 GKI 5.15.211 source at `dc9467e8f9bfdec0d012f9345ac5f12f63dc7eba` and keeps the same SukiSU, SUSFS, LZ4KD, BBRv3 and patch selections. It is a control for the current bootloop investigation, not a new stability or performance claim.

## Build

Run **Actions → Ishtar GKI control build → Run workflow**. The workflow has no push trigger, choices, release, or notification step. It uses a fresh runner and uploads `Image`, a 64 MiB Header v4 `boot.img`, an AnyKernel3 ZIP, `BUILD_INFO.md`, `manifest.lock.xml`, and checksums. Only the owner should flash after confirming a rollback image.

## What changed from `f3d57d4`

- Removed the retired 5.15.180 target and its patches.
- Pinned the LZ4KD helper repository to the revision already used by the recent build; the kernel recipe is otherwise unchanged.
- Added a source manifest lock and raw `Image` to the artifacts.
- Kept the manual build workflow and removed release and Telegram actions.

## Known limitation

The working recipe applies the helper's `lz4kd.patch`, which changes `kernel/module.c` so it accepts symbol version mismatches. Its BBRv3 and performance patches also remain active. These are useful for reproducing the working boot, but they have not been shown safe for a final kernel. After this control boots, changes to the kernel recipe should be made and phone-tested one at a time. A successful compile alone does not qualify the image for the phone.
