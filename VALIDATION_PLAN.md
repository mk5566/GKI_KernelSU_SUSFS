# Ishtar validation plan

Device testing in this plan starts only after the build blockers in [BUILD_AUDIT.md](BUILD_AUDIT.md) are closed and the owner chooses to flash. It is not a claim that an image currently exists. Keep the private [device baseline](DEVICE_BASELINE.md) as the A/B reference.

## Repository and build evidence before any device test

Run the Python regression suite and review
[PATCH_SAFETY_AUDIT.md](PATCH_SAFETY_AUDIT.md). Keep `optional_patches` empty for
the control candidate. All eight retired aliases must fail validation. Do not
restore their source files or weaken the guard to bypass a helper conflict.

After a manually dispatched build, verify `source-safety.json` says `passed`,
its `baseline_revision` matches the selected common SHA, and every protected
file has equal expected/actual hashes. Check the report hash against
`BUILD_INFO.md` and `SHA256SUMS.txt`. Then review the *separate* canonical
config, compiler, ABI/KMI and module-list results. A source-byte report is not
proof that these other gates passed. A legitimate monthly/helper change to a
protected file requires an explicit port review.

Record the actual final default/available TCP algorithms. BBRv3 was removed;
upstream BBRv1 is not silently substituted. The supplied phone baseline uses
`bbr3`, so inspect any ROM/init/script request for it before trying this
candidate. This audit does not change or validate those scripts.

Record runtime F2FS `min_fsync_blocks` after boot without rewriting it. The
supplied baseline reports 20, while the retired patch changed the source
constant from 8 to 20. The candidate follows selected upstream source plus ROM
sysfs policy, so equality with the running value cannot be assumed. Compare
actual runtime configuration before attributing an fsync or network performance
change to another patch. This threshold selects in-place-update behavior; it
is not permission to acknowledge fsync before required persistence completes.

## Before the owner flashes

1. Record the exact common/manifest/helper SHAs, patch hashes, final `.config`, build log, `Image` hash, ABI/KMI result, and all module-list results. Verify the build's kernel release and zRAM deployment against the baseline.
2. Verify the current active slot, real stock boot header/ramdisk/partition layout, proposed installer behavior, and an independently usable recovery path. Save matching original images and hashes. The generic 64 MiB test-key boot artifact is not a validated ishtar boot image.
3. Freeze the test conditions: ROM build, power/thermal mode, charger state, app set, network, and current zRAM/VM policy. Keep bindhost/SUSFS mode unchanged during performance A/B tests.

## First boot: stop conditions

Within the first boot, compare `uname -a`, `/proc/version`, full `/proc/config.gz`, active slot and boot properties with the build record. Check that Android finishes boot, SELinux is enforcing, no boot loop/pstore crash appears, and the manager UAPI matches the built SukiSU UAPI.

Inspect every `modules.load*` path from the baseline and capture boot dmesg for `Unknown symbol`, version mismatch, signature rejection, CFI fault, and failed probes. If a required module is missing or repeatedly failing, stop testing and restore the known-good image.

Verify `/proc/swaps`, zRAM disk size, bracketed active `comp_algorithm`, `mm_stat`, and LMKD state. Losing the current 16 GiB swap setup or unexpectedly changing its active compressor is a stop condition. A native-ZSTD experiment also requires the ROM's init path to select `zstd` **before** zRAM initialization; a compiled default alone does not prove the runtime algorithm. Never force swapoff or reset live zRAM to make a test appear to pass.

## Functional matrix

| Subsystem | Checks after boot and after one long idle/resume |
|---|---|
| Mobile radio | SIM registration, calls, mobile data, airplane toggle, modem logs |
| Camera | Rear/front capture, video recording, stabilization/flash where used; no HAL restart |
| Connectivity | Wi-Fi, Bluetooth audio, NFC, hotspot, GPS, USB data/tethering |
| Display/input | Refresh-rate transitions, brightness/HBM/AOD, touch, fingerprint, rotation |
| Power | Wired/wireless charging if available, battery reporting, doze, alarm wake, overnight idle drain |
| Audio/sensors | Speaker/mic/headset, proximity, accelerometer/gyro and other installed sensors |
| Storage | App install/update, camera recording, database fsync, free-space recovery, F2FS/UFS error logs |
| Root integration | Manager starts, UAPI matches, intended limited SUSFS feature reports correctly; no claim of app/attestation invisibility |

## Stability and performance comparison

- Repeat suspend/resume with alarms, network activity, charging, Bluetooth audio and background I/O. Compare suspend failures, wakeup reasons, pstore, HAL restarts and idle power with the baseline. Debugfs suspend stats were unavailable in the baseline, so use accessible power/dmesg counters and repeatable timing.
- Compare foreground frame deadline misses and p95/p99 frame time under the same workload and thermal state. Record CPU WALT policies/frequency caps, GPU devfreq, skin/battery temperatures and thermal HAL status. Do not infer an improvement from a single benchmark score.
- Compare memory PSI, direct reclaim, zRAM `mm_stat`, swap in/out, LMKD kills, app relaunches and CPU time. Change one compressor or VM variable per trial only after a stable control run.
- Compare p95/p99 fsync and app-install/camera-write latency plus F2FS GC and error counters. Preserve durability and mount options; do not use a write-throughput improvement as proof of safe fsync behavior.
- Run at least one normal-use day and a long-idle night before making a stability claim. Treat any module load failure, storage error, unexplained reboot, sustained thermal regression, or missed alarm as failure requiring rollback and diagnosis.

## Phone settings

No phone setting was changed or is required for the current repository work. Keep the existing 16 GiB `lz4kd` zRAM, WALT, F2FS, thermal and bindhost/SUSFS mode during baseline comparisons. Mode changes intended to strengthen root concealment are outside this kernel performance validation. If native ZSTD is later tested, the ROM's supported init configuration must select it before zRAM setup, and the active algorithm must be read back after boot.
