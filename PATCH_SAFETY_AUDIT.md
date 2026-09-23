# Ishtar patch-safety audit and remediation

Date: 2026-09-23. Input: the uploaded `GKI_KernelSU_SUSFS-slim-5.15-sukisu (1).zip`.
Scope: the build project, its eight named patches, selection/configuration
logic, and source-integrity gates. This is not a compiled kernel or a device
qualification report.

## Disposition and performance boundary

The eight overrides have been **removed and rejected**, not repaired in place.
In particular, this delivery does **not** contain a newly corrected BBRv3 port.
Retirement is the conservative fix because the uploaded BBRv3 patch changes
shared TCP metadata as well as private state, and the other overrides either
contain a concrete defect or lack the evidence needed to replace upstream
policy on this device. Adding speculative code to preserve each advertised
optimization would not establish correctness or performance.

The selected upstream implementation is retained for TCP, AArch64 `memcmp`,
freezer, s2idle, alarmtimer, F2FS thresholds and IRQ-affinity diagnostics. No new
kernel SIMD save/restore wrapper, per-packet null-check fallback, allocation,
lock or timer heuristic is added. All new guard work runs in the build process.
This means **no extra runtime instrumentation cost relative to that upstream
code**, not measured performance equality with the removed overrides or the
phone's currently running custom kernel. Throughput, tail latency, suspend
failures and idle energy still require device A/B measurements.

The two existing required native-ZSTD/LZ4KD integration patches are unchanged.
The `cpu-scan` and `clear-page` experiments remain available, both off by default;
this audit does not certify them. Upstream BBRv1 remains explicit opt-in. A
request for BBRv3 fails rather than silently substituting another algorithm.

## Findings verified from the uploaded patches

### 1. BBRv3: real null dereference, incomplete failure handling, broader ABI risk

Original patch anchors: `bbr3_init()` around original patch lines 2667–2683;
`bbr3_cwnd_event()` around lines 1114–1131. `kmalloc(..., GFP_ATOMIC)` failure
returns from a void initializer without setting up a working controller. In
`CA_EVENT_TX_START` with `tp->app_limited`, `bbr3_cwnd_event()` dereferences the
private pointer at `bbr->idle_restart = 1` without checking it. That establishes
a reachable failure-path null dereference, not merely a stylistic concern.
No allocation-failure injection was run on a kernel here.

Several other callbacks already check for an absent or uninitialized private
object, including the main control callback and the TSO helper. It would be
incorrect to describe every callback as unchecked. Adding one more early return
would remove that particular dereference but still leave a selected controller
whose main work can be skipped indefinitely. It is not a complete congestion
control fallback or a validated lifecycle design.

The backport also hides real declarations behind `__GENKSYMS__`. It consumes
spare `tcp_sock` bits, repurposes two 64-bit skb timestamps into unions with
32-bit timestamps plus other metadata, and extends `rate_sample` and diagnostic
structures. Some reserved-bit changes can be layout-preserving; the mere
presence of `__GENKSYMS__` is not proof of a KMI break. However, an unchanged
symbol CRC does not prove that changed widths, semantics and sizes are safe for
already-built modules. Android's ABI process examines compiled interfaces and
related types, not just the intended spelling presented to genksyms [1].

An additional concern is `tcp_notify_skb_loss_event()`: it uses
`strncmp(ca_ops->name, "bbr3", 4)` before treating generic congestion-private
storage as a pointer to a particular object. A prefix is not a type contract;
it also matches a differently named controller beginning with `bbr3`. The
patch thus adds a private-layout assumption and string comparison to common
loss handling. No claim is made that such a second controller is installed on
the phone.

**Remediation:** remove the entire backport and its configuration path; reject
BBR3 selection/configuration and backport source additions; preserve shared TCP
sources against the initial common commit. Do not enlarge shared congestion
storage, spoof symbol CRCs, or weaken module-version checks. A future BBRv3 port
requires a separate lifetime/fallback design, exact-target integration,
allocation fault injection, networking tests and compiled ABI qualification.

### 2. `memcmp`: raw SIMD is not valid as a generic kernel replacement

The supplied long-input assembly uses AArch64 vector registers directly without
owning/preserving a kernel SIMD context. Generic kernel `memcmp` is not a
specialized, caller-restricted SIMD API. Kernel floating-point/SIMD state must be
managed explicitly; a blanket wrapper is not evidence that every generic caller
is in a legal context [2].

**Remediation:** retain upstream `arch/arm64/lib/memcmp.S`. The upstream 5.15
implementation already has a 16-byte integer-register load/compare loop and
specialized smaller/end-range paths [3]. This is not a replacement with a slow
byte-at-a-time C loop. No extra SIMD context operations or wrappers are added.
The build also protects its library Makefile to catch straightforward rerouting
of the audited implementation. This limited file set is not whole-kernel proof
that no other SIMD misuse exists.

### 3. Freeze timeout: writes falsely succeed

The patch inserts an unconditional `return n;` before `kstrtoul()` in
`pm_freeze_timeout_store()`. Both valid writes and invalid input appear accepted
without parsing or changing the value. It also changes the default timeout from
20 seconds to one second. The early return is a confirmed correctness bug;
spurious device suspend aborts from the shorter timeout are a risk, not a
measured result in this audit.

**Remediation:** retain upstream parsing, error reporting, writable policy and
source default. The normal freeze loop exits when tasks are frozen; a 20-second
maximum is not a mandatory 20-second cost on every suspend [4]. No substitute
fixed one-second deadline or new sysfs restriction is introduced.

### 4. s2idle wake coalescing: unqualified synchronization change

The patch replaces unconditional `s2idle_wake()` after an atomic increment with a
call only when a relaxed increment returns one. This changes wake/abort
notification coalescing. Upstream s2idle uses locking, state transitions and
pending-wakeup checks [5]. The hunk alone does **not** establish a reproducible
lost-wakeup race; its commit message even argues the existing synchronization is
sufficient. The supplied baseline says the phone uses s2idle, but no race stress
trace accompanies the patch.

**Remediation:** preserve upstream synchronization. This is retirement of an
unqualified optimization, not a claim that a specific race was experimentally
reproduced. Any future coalescing change needs a full ordering argument and
suspend/wakeup stress validation against the exact Android/vendor integration.

### 5. Alarm wake hold: additional signed-to-unsigned timeout hazard

The changed call is in `alarmtimer_suspend()`'s near-deadline suspend-abort path,
which returns `-EBUSY`. It is not a general change to timer periods, nor a
universal two-second hold on every alarm wake. The patch replaces the fixed
hold with `ktime_to_ms(min) + 1` [6]. The surrounding condition has an upper
bound but does not exclude negative deltas if the deadline has already passed.
`pm_wakeup_event()` takes an **unsigned int** millisecond duration [7].

For a delta of exactly -2 ms, the new expression is -1; conversion to the API's
32-bit unsigned argument requests 4,294,967,295 ms (about 49.7 days). This is a
source-level arithmetic/failure-path finding, not an observed 49-day wakelock.
Downstream jiffy conversion and configuration affect actual effective duration.
Smaller positive holds may also change suspend retry behavior. Missed timer
intervals are not established just by this hunk.

**Remediation:** retain upstream bounded suspend-abort wake protection instead
of inventing a new clamp/margin without an end-to-end alarm/suspend analysis.

### 6. F2FS congestion wait: tuning claim is unproved

The patch changes `DEFAULT_IO_TIMEOUT` from 20 to 6 ms via
`msecs_to_jiffies()`. Actual timing is tick-rounded; 6 ms is not necessarily an
exact wall-clock sleep. Shorter retry backoff can change CPU activity and
contention under pressure, but neither a throughput win nor retry thrashing on
this phone was measured.

**Remediation:** retain selected upstream backoff and avoid a new arbitrary
number. This patch does not itself remove write flushes or establish a
persistence bug. Future tuning requires storage-pressure and tail-latency data.

### 7. F2FS `min_fsync_blocks`: not a delayed-durability batching threshold

The patch changes `DEF_MIN_FSYNC_BLOCKS` from 8 to 20. Upstream sysfs documentation
identifies this as a dirty-page-count threshold used for the **in-place-update
policy** during fsync [8]. It is not a rule that twenty blocks must accumulate
before an fsync becomes durable. The patch alone does not demonstrate that
fsync completion or flush barriers become incorrect.

**Remediation:** retain upstream default and existing runtime sysfs control;
there is no new fsync batching mechanism. The supplied device baseline reports
20 already, but does not establish whether a compiled default or Android policy
set it. A candidate following the upstream source value can differ from that
running kernel unless userspace overrides it. Preserve this distinction during
performance comparisons; UFS-generation branding alone does not qualify a
filesystem policy change.

### 8. IRQ logging: the actual failure message was already rate-limited

Despite its title mentioning “no longer affine,” the patch changes the actual
`irq_do_set_affinity()` failure message from `pr_warn_ratelimited()` to
`pr_debug_ratelimited()`. Upstream already rate-limits the failure warning and
uses a separate debug-level message for the informational affinity case [9].

**Remediation:** keep the existing warning level and rate limiting. No new
normal interrupt-handling log or branch is introduced. There may be more visible
error-path logging than with the suppression patch; this preserves diagnostic
value rather than promising identical logs or concealing routing failures.

## Build protection and implementation

`patches/5.15/APPLY_ORDER.txt` now contains two required integration patches and
two optional experiments only. `RETIRED_PATCHES.json` records every removed
filename, alias, original SHA-256 and reason. The original uploaded archive is
the historical source; the executable patch directory no longer contains those
eight files.

`patch_policy.py` rejects retired aliases in argument parsing, `BuildConfig`,
manifest validation and builder preflight. Mixed requests fail as a whole.
Retired filenames are rejected even if unlisted; renamed unified diffs targeting
protected paths are rejected early. This header scan is intentionally only an
early diagnostic, not a security parser for every possible patch format. The
final source-byte checks remain authoritative for the protected files.

`source_safety.py` compares **32 protected files** with Git blobs from the full
common SHA captured immediately after sync and before any third-party setup.
It does not trust mutable `HEAD`, the index, or a reverse-apply check as proof of
pristine source. Git replacement objects are disabled for the comparison.
Missing files, symlink files/ancestors, byte differences, missing baseline blobs
and added `tcp_bbr3.c`/`tcp_plb.c` stop the build. Staging or committing a change
therefore does not bypass the comparison. An upstream update that introduces
these forbidden paths deliberately requires a reviewed policy refresh.

Protected paths include the audited subsystem code, shared TCP/UAPI files, the
three upstream GKI/arm64 build configs, and `kernel/module.c`. Source checks run
after initial sync, after integration, before and after compilation, and before
packaging. BBR3 built-in/module/default selections are rejected in the relevant
defconfig and final-config checks. A failed source check replaces an older pass
report with a failed one; packaging cannot proceed after a failed guard.

`source-safety.json` records the baseline SHA, expected/actual per-file SHA-256,
status and errors. The workflow includes it in artifact checksums and failure
diagnostics; `BUILD_INFO.md` records its hash. Existing exact whole-patch/no-fuzz
application, canonical defconfig, strict GKI/KMI/module and module-version checks
are not disabled. The trigger remains manual `workflow_dispatch` only.

These are regression gates, **not** a general sandbox for executable helpers,
a complete-tree attestation, a proof of module compatibility or a performance
benchmark. A helper legitimately modifying a protected file will be blocked and
needs review. The build worktree must be exclusively owned by the build job;
checks are not designed to defeat concurrent hostile source modification.

## Validation performed and limits

The original project passed 53 Python tests in this environment. The revised
project passes **87 tests**, including 34 new tests for retirement, configuration,
source mutation and compile/package refusal. Source-guard tests use synthetic
Git repositories with fixture contents, not genuine compiled kernel objects.
Retained option combinations still pass repository validation.

The delivery validation log additionally records Python compilation, workflow
YAML/shell syntax, retained patch syntax, dry runs using a **supplied historical
target fixture**, and exact application of the **repository-level delivery diff**
to a freshly extracted copy of the original ZIP. Do not confuse delivery-diff
application with applying kernel patches to Android common.

The input does not include the complete selected kernel, toolchains or vendor
module artifacts. Primary upstream 5.15 sources and Android ABI documentation
were checked online. The exact recorded September 2026 common tree could not be
retrieved during this audit, so no current-target source application result is
claimed. Historical application results in older project documents are retained
as supplied records and explicitly marked historical.

**Not performed:** full kernel compile, canonical defconfig execution, ABI/KMI
diff, module build/load verification, kernel allocation-failure injection,
suspend race stress, memory-pressure or storage durability testing, alarm timing,
battery/performance A/B measurements, AnyKernel installer qualification, boot or
flash. No phone settings were changed and no workflow/release was dispatched.
No flashable kernel image is delivered.

Before a release, run the manually dispatched pipeline on its selected exact
source and inspect all independent build/config/KMI/module gates. Review ROM
scripts requesting `bbr3`, the actual F2FS threshold, and the unchanged zRAM
integration. Then use `VALIDATION_PLAN.md` for owner-controlled device testing
with verified rollback. A green Python suite or source report alone is not
release approval.

## Primary references

Original patch contents in the supplied archive are the source for all hunk-specific
findings; original hashes are in `patches/5.15/RETIRED_PATCHES.json`. References
below verify upstream/API context, not the exact selected 2026 Android source.

1. Android kernel ABI monitoring: https://source.android.com/docs/core/architecture/kernel/abi-monitor
2. Linux kernel floating-point API: https://www.kernel.org/doc/html/latest/core-api/floating-point.html
3. Linux v5.15 AArch64 memcmp: https://raw.githubusercontent.com/torvalds/linux/v5.15/arch/arm64/lib/memcmp.S
4. Linux v5.15 freezer: https://raw.githubusercontent.com/torvalds/linux/v5.15/kernel/power/process.c
5. Linux v5.15 suspend/s2idle: https://raw.githubusercontent.com/torvalds/linux/v5.15/kernel/power/suspend.c
6. Linux v5.15 alarmtimer: https://raw.githubusercontent.com/torvalds/linux/v5.15/kernel/time/alarmtimer.c
7. Linux v5.15 wakeup API: https://raw.githubusercontent.com/torvalds/linux/v5.15/include/linux/pm_wakeup.h
8. Linux v5.15 F2FS sysfs ABI: https://raw.githubusercontent.com/torvalds/linux/v5.15/Documentation/ABI/testing/sysfs-fs-f2fs
9. Linux v5.15 IRQ CPU hotplug: https://raw.githubusercontent.com/torvalds/linux/v5.15/kernel/irq/cpuhotplug.c
