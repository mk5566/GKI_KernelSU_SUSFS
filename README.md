# Ishtar GKI 5.15 version-check candidate

This branch follows the phone-tested [f3 control build](https://github.com/mk5566/GKI_KernelSU_SUSFS/actions/runs/35969870496). It keeps that build's Android 13 GKI 5.15.211 source, SukiSU/SUSFS integration, built-in LZ4KD, BBRv3, other patches and boot-image recipe.

## One kernel change

The pinned LZ4KD helper patch changes `kernel/module.c` to accept modules whose symbol versions disagree with the kernel. The checked-in `patches/5.15.211/lz4kd-version-check.patch` is the same helper patch with only that hunk removed. Its other hunks, including the module blacklist, remain for this controlled comparison. The builder applies it as a required patch with zero fuzz.

If this image fails to boot while the f3 control boots, collect early-boot module errors before changing anything else. The f3 control image is the rollback reference. A successful compile does not establish module compatibility or device stability.

## Build

Run **Actions → Ishtar GKI version-check build → Run workflow** on this branch. The workflow has only a manual trigger and uploads `Image`, a 64 MiB Header v4 `boot.img`, an AnyKernel3 ZIP, `BUILD_INFO.md`, `manifest.lock.xml` and checksums. The owner performs any flash and keeps the known-good image available.

The eight older performance patches and BBRv3 remain active in this candidate. If the version-check candidate boots, those can be removed in a separate, measured change. No release or notification is sent by this workflow.
