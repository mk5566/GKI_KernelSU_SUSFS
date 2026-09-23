# Kernel change plan — ishtar / latest Android 13 GKI 5.15

## 2026-09-23 follow-up

The owner requested restoration of the saved performance series after the
previous build stopped at the built-in zRAM gate. A new required patch now
integrates LZ4KD and changes the two upstream GKI zRAM modules to built-ins,
matching the observed phone config. It removes the two module-list entries and
copies only the codec files from the current helper revision. The helper's
module-version bypass is excluded. This is a source candidate: canonical
defconfig, Linux compilation, KMI and module checks, and device boot remain
unverified until the owner manually runs the workflow and validates a build.

The full saved series is not active. In particular, the saved BBRv3 backport
has an unhandled allocation failure followed by state dereferences, the SIMD
`memcmp` loop uses vector registers without a kernel SIMD context, and the
freeze-timeout patch returns before applying a sysfs write. The connected
phone's stable running kernel does not prove that these repository patches
are the same code or safe on the selected GKI source. The other suspend,
storage and IRQ changes need separate device measurements and failure checks.

All ten saved performance patches passed exact sequential application against
the selected common SHA with Linux line endings. That establishes source
context only, not correctness or a measured gain.

| Saved patch | Review result | Default build decision |
|---|---|---|
| BBRv3 backport | `bbr3_init()` can return after `kmalloc(GFP_ATOMIC)` fails; later callbacks dereference the unset pointer. It also masks shared TCP layout changes from `__GENKSYMS__`. | Inactive pending a corrected backport, ABI review and failure-path test. |
| `optimise_memcmp` | Its long-input path uses AArch64 vector registers in generic kernel `memcmp` without a kernel SIMD context. | Inactive pending a kernel-safe implementation and correctness/performance tests. |
| `reduce_freeze_timeout` | An early `return n` in `pm_freeze_timeout_store()` discards writes while claiming success; shortening 20 seconds to one also needs suspend validation. | Inactive. |
| `silence_irq_cpu_logspam` | Downgrades an IRQ-affinity failure warning, obscuring a useful failure signal. | Inactive. |
| `avoid_extra_s2idle_wake_attempts` | Changes abort wakeup signaling while ishtar uses s2idle; no wakeup/abort race validation exists. | Inactive. |
| `minimise_wakeup_time` | Changes alarmtimer wake hold duration; no alarm, suspend and idle power comparison exists. | Inactive. |
| `f2fs_reduce_congestion` | Reduces the F2FS congestion wait from 20 ms to 6 ms without device write-pressure or thermal evidence. | Inactive. |
| `f2fs_enlarge_min_fsync_blocks` | Sets a 20-block default; the running phone already exposes 20, but the source of that runtime value and durability impact are unverified. | Inactive. |
| `adjust_cpu_scan_order` | Small scheduler scan order change applies, but vendor WALT and frame/thermal effects have not been measured. | Inactive. |
| `clear_page_16bytes_align` | Alignment-only arm64 change applies; no ishtar benchmark demonstrates a gain. | Inactive. |

This plan uses the read-only [device baseline](DEVICE_BASELINE.md) and the supplied 2026-09-23 audit. The current worktree already contains uncommitted changes to use a shared `patches/5.15/` directory and resolve the latest monthly branch; preserve those edits. Ignore the removed 5.15.180 target.

## Must fix before next build

| Evidence and hypothesis | Exact change | Benefit | KMI/vendor and suspend/thermal risk | Acceptance / rollback |
|---|---|---|---|---|
| `_apply_patch_file` can call a partially reversed patch "already applied". | Port audit diff 0001's whole-patch `git apply --check`, reverse check, and fail-closed required-patch behavior. | No silently incomplete integration. | Low runtime risk; build may stop on real conflicts. | Unit tests for partial reverse and exact application; revert the focused code change if incorrect. |
| Workflow dispatch strings enter shell source before Python validation. | Port diff 0001's environment-based dispatch input handling. | Predictable workflow input validation. | None on device. | Parse workflow and run command-source tests; revert focused workflow hunk. |
| Active shared order still includes `optimise_memcmp`, freezer/s2idle, F2FS delay, IRQ suppression, BBRv3 and unmeasured CPU changes. | Replace `patches/5.15/APPLY_ORDER.txt` with the conservative required new-file fragment only; retain old patch files as review references. | Remove known correctness/suspend risks and unproved tuning. | May change performance; no ABI change from removal itself. | Exact patch check and build; compare post-flash suspend and frame/fsync metrics. Roll back to previous known-good image manually if regressions occur. |
| Current builder forces LZ4KD, rewrites module lists/signature policy, and makes BBRv3 default. Phone already has built-in lz4kd zRAM and vendor policies. | Port diff 0002's conservative defaults: no zRAM/TCP override unless explicit, native ZSTD opt-in only, keep module placement and signature policy. Add the separate ishtar deployment gate below. | Prevent hidden deployment changes; the gate blocks upstream defaults that would lose this phone's current swap setup. | ZSTD trial may increase CPU/thermal cost and must not be default. | Config/gate tests plus post-flash actual algorithm; revert option change or previous image. |
| Builder deletes `check_defconfig`, empties module/ABI inputs, and turns KMI enforcement off; upstream pinned SHA sets strict checks and Xiaomi/QCOM symbol lists. | Remove all bypasses from `configure_kernel()` and `build_kernel()`; invoke branch build config without override. Verify canonical defconfig and recorded KMI artifacts. | Build failures reveal actual incompatibility. | High build/ABI risk from existing root integration; no device effect until flashing. | `check_defconfig` + ABI/KMI + module-list checks must pass; on failure keep build blocked, not bypass validation. |
| Source and helper revisions can drift; `.180` is obsolete. | Record exact common/manifest/helper SHAs and patch hashes for every successful build; keep only Android 13 5.15 latest monthly target. | Reproducible review and rollback. | Build process risk only. | Verify common revision descends from official monthly tag and matches expected Makefile; keep prior known-good image for manual rollback. |
| Phone has built-in zRAM/zsmalloc; upstream GKI makes them modules. The only system_dlkm copies report 5.15.194 vermagic against the running 5.15.211 kernel. Third-party LZ4KD patch suppresses module-version failures. | Add an ishtar deployment gate before compilation. Develop a separate reviewed built-in compressor integration and canonical defconfig; never use the unsafe module-loader hunk. | Prevent a silent swap loss in an Image-only package. | High KMI/module-list and boot risk until resolved; thermal impact depends on compressor. | Gate must pass and branch KMI/module checks must pass; verify actual swap and algorithm after manual flash. Roll back to the saved working image on any loss of swap. |

## Evidence-backed tuning to implement now

None. The read-only snapshot gives no causal latency or energy result. Removing risky defaults is the performance/stability decision for this pass.

## Experiments deferred until after baseline/post-flash data

- Native ZSTD versus current lz4kd, one compressor variable at a time, with zRAM `mm_stat`, PSI, frame p95/p99, CPU time, and skin/SoC temperature. Do not change the running swap device during baseline collection.
- F2FS `min_fsync_blocks` and congestion delay only after fsync tail latency, GC, write-pressure and durability tests. Preserve the observed mount policy for now.
- CPU scan order/clear-page alignment only in separate builds with frame and thermal A/B data.
- SUSFS monitor lifetime candidate only after exact pinned-source application, compile, and fault-path tests. Do not expand concealment behavior.
- Suspend, Binder, IRQ, BBRv3 and GPU settings remain unchanged until corresponding failures or measured bottlenecks are established.
