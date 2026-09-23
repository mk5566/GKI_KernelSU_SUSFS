# ishtar device baseline — 2026-09-23

Read-only ADB capture: `device_profiles/20260923T040039Z/` (private local evidence; excluded from Git). The device was not rebooted or retuned. The collector stalled while reading `/sys/power/wakeup_count`; the remaining safe endpoints were collected separately. This is one charging/idle snapshot, not an A/B performance result.

## Device and source identity

- Xiaomi 13 Ultra (`ishtar`, model `2304FPN6DC`, SM8550), Android 16 / SDK 36, build `OS3.0.307.0.WMACNXM`, security patch 2026-08-01, active slot `_a`.
- Running `5.15.211-android13-8-gdc9467e8f9bf`, built 2026-09-21. `/proc/config.gz` is available.
- The full common SHA `dc9467e8f9bfdec0d012f9345ac5f12f63dc7eba` exists in Google's kernel/common Git and `git merge-base --is-ancestor` confirms that the official September tag target `0b6028f1f30da3c2143bb40eee912c4974108e5c` precedes it. Both report Linux 5.15.211. This confirms source ancestry, not compatibility of a newly modified image.

## Module and KMI requirements

- 405 loaded modules. Recursive inventory resolves 295 unique vendor `.ko` basenames exposed through both `/vendor/lib/modules` and `/vendor_dlkm/lib/modules`, plus two zRAM/zsmalloc `.ko` files under nested `/system_dlkm/lib/modules/5.15.194/`. The initial collector summary's 885 count included duplicate `ls` and `find` lines and missed the nested system_dlkm files; do not use that count as a distinct-module total.
- `modules.load` and `modules.dep` are present in both vendor views and under the nested system_dlkm directory. ODM contained no module files in this capture. The two system_dlkm modules declare `vermagic: 5.15.194 ... modversions ...`, while the running kernel is 5.15.211; their compatibility with a newly built image is unverified. Preserve *all* load/dependency references, including modules not loaded now. Evidence: `modules_loaded.txt`, `modules_inventory.txt`, `modules_metadata.txt`, `modules_system_dlkm_*`.
- A basename-only comparison leaves 115 loaded module names without a match in these mounted module directories. The source location and ABI contract for those modules need tracing before a release; basename differences and early-boot images may account for some, but have not been verified.
- Running config has `CONFIG_MODULES=y`, `CONFIG_MODULE_UNLOAD=y`, `CONFIG_MODVERSIONS=y`, `CONFIG_MODULE_SIG=y`, `CONFIG_MODULE_SIG_PROTECT=y`, `CONFIG_CFI_CLANG=y`, and thin LTO. Preserve GKI module lists and ABI checks. No vendor-module compatibility claim can be made until the modified kernel is built and boot-tested.
- Do-not-prune set: the full `modules.load`/`modules.dep` closure, Qualcomm CPU/WALT/thermal/GPU/UFS/Binder/network/camera/audio/charging dependencies, F2FS/encryption/verity, GKI module infrastructure, and recovery/early-boot support. The raw files are the authoritative inventory.

## Memory and zRAM

- RAM 14.84 GiB; available 4.86 GiB at capture. Swap is a 16 GiB zRAM disk with about 6.00 GiB logical swap in use. Active compressor is **lz4kd**; available list includes zstd. `mm_stat` gives 6.44 GB original vs 2.08 GB compressed (3.09:1); backing device is `none` and writeback counters are zero. Running config has built-in zRAM/zsmalloc, LZ4KD and ZSTD crypto support.
- `vm.swappiness=100`, `page-cluster=0`, dirty ratios 10/20, writeback/expiry 500/3000 centiseconds. Memory PSI `some avg10=0.00`, `full avg10=0.00` at capture; cumulative direct reclaim and swap activity exist but do not establish an interactive bottleneck. `lmkd` and vendor swap HAL are running.
- Google's pinned GKI `gki_defconfig` sets `CONFIG_ZRAM=m` and `CONFIG_ZSMALLOC=m`, while the running phone has both built in. The only on-disk copies found are the nested 5.15.194 system_dlkm modules; they are not qualified for this 5.15.211 Image-only build. Upstream alone cannot be treated as an Image-only replacement for this phone. The available third-party LZ4KD patch also alters `kernel/module.c` to accept module version mismatches, so it is not a safe shortcut.
- **Decision:** preserve the current zRAM topology, compressor, size, and userspace ownership. Native ZSTD is an opt-in comparison candidate only. The builder must fail until a reviewed built-in compressor/module plan is implemented and tested. There is no basis to change live sysctls now.

## F2FS and UFS

- `/data` is F2FS on `dm-49`; `/metadata` is F2FS on `sda16`. The `/data` mount reports `fsync_mode=nobarrier`, `inlinecrypt`, `background_gc=on`, `gc_merge`, `discard`, and `atgc`. This is observed current policy, not a recommendation to set `nobarrier` on another device.
- `/data` exposes `min_fsync_blocks=20`, `ipu_policy=16`, `gc_urgent=GC_NORMAL`; `/metadata` also has 20 with `ipu_policy=129`. This may reflect the currently running custom kernel or vendor policy; a single snapshot cannot attribute it to either.
- Backing UFS reports Samsung `KLUGGARHHD-B0G1`, firmware `1801`; `sda` uses `[cpq]`, 2048 KiB read-ahead, 128 requests, 4 KiB sectors. Preserve queue and filesystem policy until fsync tail latency, GC, and durability are measured.

## Scheduler, GPU, thermal, suspend and IPC

- CPU policies: 0–2, 3–6, and 7; all currently use `walt`. Vendor limits at capture were below reported hardware maximum on policies 3 and 7. GPU uses `msm-adreno-tz`; thermal HAL is ready and reports status 0. Keep vendor WALT, frequency, GPU and thermal controls.
- `/sys/power/mem_sleep` shows `[s2idle] deep`. `dumpsys power` saw a charging, dozing device at 100%. Debugfs suspend/wakeup and Binder stats are unavailable; reading `/sys/power/wakeup_count` did not complete, so the collector was stopped without a device change. Pstore had no files. No suspend regression conclusion follows from this snapshot.
- The last 1,500 kernel log lines contained no `Unknown symbol`, module-version mismatch, F2FS I/O error, suspend failure, or IRQ-affinity error matches. This is a limited log window, not a clean-boot certification.
- TCP currently uses `bbr3` and `fq`; this reflects the installed custom kernel. It is not evidence that BBRv3 improves this phone's UI latency.

## Opportunities and deferred measurements

| Priority | Finding | Action / evidence needed |
|---|---|---|
| 1 | Builder suppresses KMI/defconfig/module checks despite 405 loaded vendor modules | Restore checks before calling any build compatible; compare ABI and module load after a manual flash. |
| 2 | Current active patch order includes unsafe generic SIMD `memcmp` and freeze-timeout changes | Remove from baseline; run exact patch and build validation. |
| 3 | GKI zRAM modules do not match this Image-only phone deployment | Restore built-in zRAM/zsmalloc and a validated compressor without weakening module-version checks; then compare lz4kd vs native ZSTD with repeatable workload, CPU cost, frame latency, and thermal data. |
| 4 | F2FS already exposes 20-block fsync threshold | Determine source and compare p95/p99 fsync latency plus durability before changing it. |
| 5 | Vendor frequency limits varied at capture | Collect repeated frame/thermal traces before touching scheduler or DVFS. |

No memory, storage, CPU/GPU, suspend, Binder, network, or phone setting change is justified by this baseline alone.
