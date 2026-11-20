# Engineering review: GKI_KernelSU_SUSFS

> Historical first-pass review. The subsequent source-refresh changes and current
> behavior are documented in [UPSTREAM_STATUS.md](UPSTREAM_STATUS.md). In
> particular, advisory upstream warnings described below have been replaced by
> mandatory fatal checks, canonical source URLs have changed, and source locks
> now use schema 2. Test counts below describe the earlier delivery.

**Review date:** 2026-11-20. **Input:** the supplied `GKI_KernelSU_SUSFS-main.zip`.
The source archive, not another repository checkout, is the project baseline.
External primary sources are identified separately in section 9.

## 1. Executive decision

The strongest practical improvement is a more trustworthy builder, not another
unmeasured kernel tuning flag. The repository already protects important
properties: published certified-base selection, exact source resolution,
manifest-pinned tools, mandatory patch application, generated configuration
checks, and comparison against the certified build's export CRCs. Preserve these.

The maintenance patch fixes a real preflight tool-argument bug, makes export
parsing fail closed, checks frozen export metadata, binds both packages to the
compiled kernel, independently revalidates evidence, and makes workspace/process
handling safer. It separates profile, policy, provenance, process handling, and
artifact validation from orchestration, with executable regressions.

**Do not interpret this delivery as approval of the existing BBR port's complete
binary KMI.** Its `__GENKSYMS__` guards hide actual type changes from CRC
calculation. Preserving the 104-byte congestion-control private area is useful
but does not establish all affected layouts, data meanings, or callbacks. That
remains a kernel-release acceptance requirement, not something a parser or a
passing Python suite can solve.

The main delivery changes **no file under `patches/`**: 21 files, including all
13 kernel patches, all four `APPLY_ORDER.txt` files, both vendored LZ4 source
files, and patch documentation, are byte-for-byte identical. All five extracted
profile constants and all six literal `CONFIG_*` dictionaries from the original
builder are structurally equal. See the delivered `preservation.json`.

The separate optional BBR owner-check patch has a narrow, reproduced correctness
case. It is deliberately not added to the default source tree or main diff.

### What was and was not established

Offline Python/Bash/native-C validation was performed. The native checks compile
the actual private LZ4 implementation on the x86-64 host using Clang 17 with
ASan/UBSan. They are not a certified AOSP kernel build. Exact commands and final
results are in the accompanying validation record and logs.

No GitHub build was dispatched; no release was published. No device was contacted
or flashed. This environment did not fetch/sync the certified kernel and all
helper repositories, run a real AOSP configuration preflight or full kernel
compile, or compare a resulting kernel against live certified CRCs. The two live
integration tests remain explicitly skipped offline. `actionlint` and
`shellcheck` were not installed; their execution is not claimed.

The archive does not contain the complete dynamically fetched SukiSU, SUSFS,
LZ4KD, AnyKernel3, certified kernel, or compiler trees. These are declared runtime
inputs, not missing project files to invent. Their integration and provenance
logic were reviewed, but complete source audits of those exact future revisions
cannot be inferred from their URLs or revision fields. No current monthly tag is
claimed to have been resolved during this offline implementation.

## 2. Phase 1 — architecture and existing strengths

### Control and data flow

```text
manual workflow_dispatch (stable default, read-only contents permission)
  -> offline regression gates
  -> disposable hosted-runner dependencies / memory provisioning
  -> build.py --resolve-only
       target.py: published certified monthly release + peeled common commit
       certified artifact manifest: exact required source/toolchain projects
       exact SukiSU / SUSFS / LZ4KD helper / AnyKernel3 / repo revisions
       source_lock.py: sources.json + builder commit + local input hashes
  -> live certified-download + exact-SukiSU-source tests
  -> build.py --sources sources.json
       BuildConfig.validate -> fresh external workspace + exclusive lock
       helper clones -> pinned repo launcher -> exact certified projects
       certified compiler selection and checkout verification
       SukiSU builtin integration -> full upstream SUSFS + local glue
       private LZ4 -> BBR series -> tweaks -> LZ4KD/ZRAM series
       reviewed defconfig edits -> release suffix -> AOSP build fragments
       AOSP build.sh config-only hook -> independent generated-config policy
       AOSP build.sh compile -> final config -> release -> certified exports
       AnyKernel3 ZIP + generic/repacked boot image
       independent evidence verification -> atomic evidence publication
  -> upload boot.img / AnyKernel3.zip and separate evidence
```

The unchanged effective patch order is: SukiSU quiet glue; full fetched SUSFS
filesystem patch; two SUSFS compatibility glue patches; ordered LZ4 patch;
three ordered BBR patches; four ordered tweaks; two ordered ZRAM patches. There
are 14 applied records because one patch is obtained from the locked SUSFS tree
rather than stored among the 13 local patches.

**Existing strengths.** `target.py` chooses from release-page rows with certified
downloads, not merely the largest visible source tag; handles peeled annotated
tags; requires the certified manifest's common commit to match; pins required
projects and all helper revisions. `KernelBuilder` keeps the certified build
script in charge of toolchain/Kconfig setup and retains source and release checks.
Patch application uses `git apply` without partial-reject mode and fails required
patches rather than skipping them. Existing tests already exercise manifest
selection, root integration, SUSFS options, configuration, and artifact evidence.

**Existing limitations.** Fresh network resolution is inherently dependent on
upstream availability and page shape. Both SukiSU labels actually resolve the
same `builtin` HEAD. The certified monthly manifest branch commit is recorded,
while per-project revisions come from the selected certified build's manifest;
those are different provenance roles and should not be conflated. Upstream
BBR/LZ4 drift is advisory unless strict resolution is explicitly requested.

### Technical debt and correctness findings

`kernel_builder.py` mixed profile constants, subprocess execution, configuration
parsing, orchestration, diagnostics, packaging, and provenance. Its process-global
`os.chdir` made unrelated relative paths sensitive to call order. Git setup wrote
global identity and a broad `safe.directory` exemption. Packaging could merge
into an existing ZIP or leave its staged `Image` behind after failure. Substring
module-list filtering could remove similarly named modules or comments.

The preflight hook expanded `${TOOL_ARGS}` as a scalar although AOSP constructs a
Bash array. That drops subsequent arguments; one valid element such as `LLVM=1`
does not substitute for all capability/tool arguments. Correct quoting must also
preserve an individual argument containing spaces. The external AOSP reference
confirms this array contract [E2]; the new regression checks real Bash argument
expansion rather than only searching generated text.

`abi.py` previously ignored malformed rows and overwrote duplicate names. This
could shrink or replace the reference set and produce a misleading success.
CRC-only records also discarded export class and namespace. The maintenance
version validates every nonblank input row, rejects duplicates, preserves the
required-vmlinux-export scope, and additionally compares class/namespace.

Evidence previously needed stronger binding to the actual compiled image and
independent policy evaluation. Agreement between two copied or packaged stale
images is insufficient. A self-consistent JSON report is not proof that its
claimed `expected` configuration is the reviewed policy. Evidence publication
now verifies both independently before publishing a complete bundle.

The old `PatchPlan.all` described `lz4 -> zram -> bbr -> tweaks`, unlike actual
execution. Its new concatenation reflects execution; the execution order itself
is unchanged. Root/SUSFS glue and the external SUSFS patch are now included in a
complete ordered application record with target directories and hashes.

The workflow mixed substantial runner provisioning with build orchestration,
used floating action tags and `3.x` Python selection, and ran checks after costly
setup. Full-LTO logs are a major diagnostic surface; preflight and compile logs
should not replace one another. Unverified object caches or bypassing AOSP's
cleanup would compromise confidence more than an unmeasured speedup helps.

## 3. Prioritized changes and disposition

Effort is a relative engineering estimate (S = localized, M = cross-module,
L = kernel/toolchain/consumer investigation), not a promised delivery time.

| Priority | Finding / recommendation | Status | Effort | Risk / acceptance requirement |
| --- | --- | --- | --- | --- |
| Critical for kernel promotion | Hidden BBR type/semantic changes are not proven compatible by CRC equality | Open; documented with explicit acceptance plan | L | Potential binary-KMI impact; must inspect exact built types and relevant consumers |
| High | Preflight loses AOSP Bash-array tool arguments | Fixed with actual Bash regression | S | Preserves certified tool selection; real certified preflight still required |
| High | Malformed/duplicate export rows silently change the checked set | Fixed, fatal parsing and regression tests | S | Stricter inputs intentionally fail rather than reduce coverage |
| High | Same CRC can coexist with changed export GPL policy or namespace | Fixed in compile gate and evidence recheck | S | Additional fatal checks; no kernel/config change |
| High | Evidence can be internally consistent without matching reviewed config or compiled output | Fixed, independent rechecks and payload binding | M | Boot parser checks identity only, not boot/signing acceptance |
| High | Stale outputs, conflicting ambient build knobs, and shared workspace state | Fixed with refusal/lock boundaries | M | Intentional fresh-workspace contract; no incremental-output reuse |
| High | Dynamic helper source is not covered by the supplied glue-only audit | Acceptance limitation retained | L | Exact SHA improves traceability, not code correctness by itself |
| Medium | BBR callback prefix accepts unrelated `bbr3*` algorithms | Separate optional exact-match proposal | S | Conditional reachability; full stack compile/CRC still required |
| Medium | Builder mixes policy, provenance, execution and packaging | Refactored without profile changes | M | Compatibility aliases retained; unit and clean-archive tests |
| Medium | Workflow action/Python drift and opaque provisioning | SHA pins, 3.12, guarded helper, diagnostics | S/M | Hosted-runner environment still needs actual CI execution |
| Medium | Large-buffer hashing, log parsing, archive leftovers | Streaming helpers, bounded excerpt, cleanup | S | Reduced peak transient memory; not a measured kernel speedup |
| Medium | Compression regression coverage limited to patch markers | Actual vendored-code ASan/UBSan harness | M | Host functional check only; no ARM or crypto-wrapper certification |
| Low | README overstates stable channel, FQ activation, LZ4 tuning and certification | Corrected | S | Documentation now matches code and measured scope |
| Low | No evidence-based cache/concurrency tuning | Timings delivered; optimization deferred pending measurements | M | Restore immutable sources only after validating a full content key |

## 4. Patch-by-patch kernel review

Every patch below remains unchanged in the main delivery. “No intentional public
layout change” is not a substitute for the mandatory generated-config and export
gates. The exact selected monthly kernel may move patch context; failure remains
fatal and requires review, never automatic fuzzy repair.

### BBR series

**`bbr/0001-bbrv3-android-kabi.patch`.** Adds BBR3 and PLB to the 5.15 TCP stack,
with changes across shared TCP headers, ECN/TLP/rate accounting, congestion
control, diagnostics and sysctl handling. Heap state avoids enlarging the
104-byte `icsk_ca_priv`, but incurs an allocation and pointer indirection per
BBR3 socket. The non-local TCP changes mean this is not an isolated congestion
algorithm drop-in. Review lost/delivered/CE accounting, TSO splitting and
retransmissions, ECN negotiation, route metrics, callback contracts, namespace
lifetime, and diagnostic lengths against the exact target tree.

Concrete KMI distinctions:

| Area in the supplied patch | What it preserves | What remains to establish |
| --- | --- | --- |
| `tcp_sock` bitfield | Reuses previously unused bits in the same apparent storage unit | Target-compiler layout and any consumers of formerly unused bits |
| `tcp_skb_cb.tx` | Two unions retain 64-bit-sized slots | Old 64-bit timestamps now overlap 32-bit timestamps plus CE/loss metadata; equal aggregate size is not equal field meaning |
| `rate_sample` | Existing prefix is retained | Hidden appended fields change object size and producer/callback assumptions; prefix-only consumers are not automatically broken |
| `tcp_bbr_info` / `tcp_cc_info` | Earlier diagnostic fields remain | Hidden extension affects diagnostic object size/UAPI handling and potentially embedded union size |
| `icsk_ca_priv` | Original 104-byte area | Heap ownership, fallback/free paths, callbacks and hidden shared types |

The `__GENKSYMS__` branches specifically keep CRC generation from seeing portions
of these changes. Therefore a CRC pass cannot adjudicate them. Determine which
types are reachable from the actual frozen KMI and real vendor consumers before
claiming a break or claiming compatibility. No complete vendor-module set or
built ABI description was supplied. AOSP explicitly ties KMI to constrained
configuration, toolchain and monitored interfaces, with binary ABI tooling [E1].

The loss notification hook also checks a four-character name prefix before
casting congestion private data as a BBR3-state pointer. A host fixture extracted
from this exact patch reproduces dispatch for `bbr3_alt`. The optional proposal
changes only this condition to an exact name match. There is no evidence that
such a competing algorithm is present in the configured device profile. This is
not presented as a demonstrated device exploit or a throughput gain.

**`bbr/0002-bbrv1-allocation-fallback.patch`.** The allocation-failure path zeroes
the inline private area, switches to built-in BBRv1 ops, clears BBR3-specific
flags, and initializes BBRv1. Changing BBR3 from tristate to bool with
`TCP_CONG_BBR=y` avoids relying on an unpinned fallback module reference. The
BBRv1 ops object gains link visibility, not an `EXPORT_SYMBOL` declaration.
Retain the fallback and its existing regressions. Kernel-level allocation fault
injection, socket lifecycle and lock/context validation remain desirable; no
real allocation-failure kernel test was executed here.

**`bbr/0003-plb-pernet-initialization.patch`.** Moves PLB initialization/inheritance
to its owning pernet callback, registers it before TCP's fs-initcall users, and
returns NULL when its ID is not registered. This directly addresses the
initialization ownership/order defect rather than suppressing a failure. PLB
starts disabled. Keep the patch; review namespace create/destroy/error paths in
the resolved full kernel. Do not enable PLB as a blanket mobile throughput tweak.

Google describes its BBRv3 release as a basis for research and wider testing,
not a universal performance guarantee [E7]. The project port is a reviewed
adaptation of a recorded revision, not evidence of bit-for-bit equivalence to
current upstream.

### Private LZ4 series and sources

**`lz4/0002-crypto-lz4-1.11.1.patch` plus `source/lz4.c`/`lz4.h`.** Private static
symbols in the crypto wrapper preserve the existing exported GKI library/header
boundary. Allocation uses `sizeof(LZ4_stream_t)`, initialization occurs once,
and compression reuses initialized state with acceleration 1. Signed-size guards
protect the unsigned crypto lengths before passing them to int-sized codec APIs;
the safe decompressor and negative-result checks are retained.

This is a defensible integration strategy. It is not proof that the entire
kernel's vendor ABI is unchanged. Fast-reset state reuse and generic upstream
AArch64 paths are opportunities already present in the baseline, not new measured
SM8550 gains from this review. The release version is supported by upstream's
v1.11.1 release page [E6], but upstream release notes are not an independent hash
attestation of this adapted vendored tree.

The added native harness runs 3,300 repeated-state roundtrips across empty,
small-block, 4-KiB-boundary, 8-KiB and 64-KiB sizes, with zero/pattern/random data;
exercises undersized compression destinations and subsequent state reuse; checks
undersized decoder destinations; and runs 20,000 deterministic bounded random
decoder inputs plus a known malformed block. Separate small allocations improve
sanitizer visibility at passed-capacity boundaries. This tests actual codec code,
not merely a reimplementation. It does not test kernel allocator alignment,
crypto concurrency, Android ZRAM initialization, or AArch64 instruction paths.

### SUSFS glue and root integration

**`susfs/0001-sukisu-quiet-production.patch`.** Forces a local header through
SukiSU's compiler flags and maps informational logging to `no_printk` when debug
is off. The include uses the established `drivers/kernelsu` integration symlink.
This retains format checking instead of globally disabling warnings and does not
modify a public kernel type. It can reduce diagnostics; keep failures/errors
available and never infer root-hiding effectiveness from quieter logs.

**`susfs/0002-susfs-ratelimit-log-fallback.patch`.** Defines disabled-mode
rate-limited logging macros to address otherwise undefined calls with production
logging off. This is a source/config compatibility fix rather than a global
warning bypass. A future incompatible upstream revision should fail this patch.

**`susfs/0003-susfs-no-priority-uid-filter.patch`.** Marks two local helpers
`__maybe_unused` for the mount-only configuration. It does not enable the UID
filter, erase source functionality, or globally remove `-Werror`. Preserve it.

The complete external SUSFS filesystem patch and sources are applied before the
local compatibility patches. The builder also scans declared SUSFS suboptions
and enforces the exact enabled set in generated configuration. That is stronger
than slicing the external patch to a few files. However, full filesystem/root
security review must use the actual pinned external trees. Mount-only hiding is
not a promise of defeating application attestation or every root detector.

### Memory/power/latency tweaks

**`tweaks/avoid_extra_s2idle_wake_attempts.patch`.** Restricts wake processing to
the relevant transition instead of repeated wake attempts. No intentional public
layout/export change is visible in this small patch. Preserve the existing
synchronization model; proving a power benefit needs wake/suspend traces and
real workload validation, not only inspection.

**`tweaks/minimise_wakeup_time.patch`.** Clamps negative alarmtimer wake duration
to zero. This makes the boundary treatment explicit. No intentional type/export
change; regression validation should cover expired, zero and future deadlines
and suspend/wakeup semantics in the exact kernel.

**`tweaks/clear_page_16bytes_align.patch`.** Adds explicit 16-byte instruction
alignment. It does not change an exported C prototype. Whether it improves
layout or is redundant depends on the certified assembler/linker and existing
function alignment. Inspect final disassembly before assigning any speedup.

**`tweaks/adjust_cpu_scan_order.patch`.** Begins the wrapped idle scan after the
previously considered target. This changes candidate ordering and may avoid
reconsideration; effects on locality, contention and asymmetric CPU placement
are workload dependent. No new scheduler policy or SM8550 capacity tables are
introduced by this maintenance work.

### ZRAM series

**`zram/lz4kd-integration.patch`.** Adds crypto/library Kconfig wiring, selects
LZ4 as default, limits the advertised and accepted ZRAM algorithms to LZ4/LZ4KD,
and retains unrelated exported compression consumers. The whitelist makes the
configured profile explicit. The implementation of LZ4KD itself is dynamically
copied from the exact helper commit and is not in the upload; its bounds,
format correctness and performance were not established by this review.
Changing a live initialized compressor is not a valid general instruction [E5].

**`zram/0003-built-in-provider-load.patch`.** Returns `-EEXIST` only for exact
`zram`/`zsmalloc` names when the corresponding provider is built in. It does not
install the broad module-version-check bypass found in some external patch
collections. The supplied hunk is not the full module-loader function: verify
its placement relative to the actual selected kernel's blacklist, signature,
security and version handling. Do not generalize its result to all modules or
claim every loader ordering property was proven from this hunk.

## 5. Phase 2 — implemented builder changes and verification rationale

Every change below is in the main maintenance diff unless explicitly marked as
an optional proposal. They leave the reviewed kernel configuration and patch
bytes untouched. Any previously omitted tool arguments may change a generated
configuration by restoring the intended environment; the policy must then pass
normally, not be relaxed to hide the difference.

| Change and problem | Implemented solution | Safety / expected benefit | Verification |
| --- | --- | --- | --- |
| Preflight tool arguments truncated | Pass `"${TOOL_ARGS[@]}"` inside the AOSP hook | Same certified setup; no host-side Kconfig emulation | Real Bash fixture checks every argument, including embedded spaces, and no compile stage |
| Mixed profile/policy/orchestration | Extract `kernel_profile.py` and `config_checks.py`; keep compatibility wrappers | One policy for both stages and publication; easier review | AST equality of constants/config dictionaries; existing profile tests |
| Trusting report expectations | Independently verify both config snapshots, compare reports exactly, record complete delta | Forged/stale report cannot redefine acceptable config | Consistent-report tampering and snapshot/delta regressions |
| Lossy export parsing / missing export policy | Strict structured export parser, duplicate rejection, CRC normalization, export type/namespace comparison | More stringent frozen export contract, additional exports still permitted | Malformed rows, duplicates, missing/drifted exports, same-CRC policy drift; download consumer tests |
| Evidence not bound to compiler output | Compare workspace image, boot kernel payload and exactly one ZIP `Image` to `dist/Image` | Rejects equally stale packages; does not alter packaging format | v0–v4 parser fixtures, truncation/header cases, payload corruption, dist mismatch, duplicate ZIP handling |
| Incomplete patch provenance | Record actual apply sequence/path/hash/target and verify against the full selected plan | No changed application order; catches changed/misdirected records | Order/hash/target and external SUSFS-patch mutation tests |
| Partial evidence directories | Validate first; copy to staging; publish with rename only after complete checksums | Failures do not create successful-looking bundles or overwrite prior evidence | Copy-failure cleanup and prior-evidence preservation tests |
| Ambiguous/partially written JSON | Shared duplicate-key/nonfinite-number rejection and atomic JSON replacement | Unambiguous evidence and no truncated previous records | Duplicate keys at depth; nonfinite inputs; replacement-failure preservation |
| Global cwd changes and shell quoting | Use subprocess `cwd`; use argv for dynamic paths; explicit empty env remains empty | Stable relative CLI outputs and fewer shell/path hazards | Workspace names with spaces/apostrophes, empty-env and cwd tests |
| Persistent Git mutations | Process-local Git identity entries; no broad global safe-directory exemption | No change to user's global Git configuration | Real Git query with preexisting process config preserved |
| Stale/overlapping builds | Fresh external workspace checks plus exclusive owned lock | Rejects stale outputs; never treats old outputs as current success | Existing evidence/lock preservation and owned-lock cleanup |
| Ambient tool/build overrides | Reject known stage bypasses, output redirects, tool overrides and hooks before AOSP setup | Prevents accidental local profile/environment divergence | Representative compiler/LTO/defconfig/output/hook overrides rejected |
| Imprecise module list edits | Remove exact `zram.ko`/`zsmalloc.ko` basenames only | Retains similarly named modules and comments | Exact-name/comment regression |
| ZIP merging and temporary `Image` leaks | Refuse existing archive/staged image; always clean owned staging | No stale archive entries after a rerun/failure | Real ZIP success case, stale-output refusal and induced ZIP failure |
| Large transient buffers / weak failure context | Streaming hashes/repack comparisons; bounded error excerpt; separate preflight/compile logs | Lower transient memory use and preserved root-cause diagnostics | Large-log/one-huge-line regressions, postcompile policy-failure propagation |
| No phase-level performance evidence | Record elapsed monotonic time and status for each integration/build/package phase | Enables later tuning from evidence instead of guesses | Failure timing recorded without swallowing original failure |
| Source archive tests depend on provenance | Explicit test-only provenance fixture plus separate real-Git tests | Archive tests run; production still requires an actual commit | Full clean-archive suite and empty/committed temporary Git test |
| Inputs omitted from source fingerprint | Hash shell/native helpers and primary policy docs as well as scripts/patches; reject symlinks | Detects changed build inputs and dangling-link ambiguity | Hash coverage and dangling-symlink regression |
| Ambiguous CLI / malformed manifest records | Mutually exclusive modes, reject strict re-resolution with a lock, validate project triples/months | Clear fail-fast behavior before build work | CLI and malformed-project/month tests |

The environment blocklist is an accidental-misconfiguration boundary, not a
sandbox against a malicious same-user process. Similarly, file hashes and JSON
reports are reproducibility/integrity evidence, not signed third-party
attestations. The post-integration checkout record is not a cryptographic proof
that no process altered a source file later during compilation.

`read_symvers`, `write_sources`, `load_sources` and selected builder configuration
helpers remain accessible through compatibility aliases where existing callers
used them. This avoids an unnecessary public-API break during the modular split.

## 6. Workflow and safe build-system optimization

### Delivered improvements

The workflow remains one manual build job with default `stable`, `contents: read`,
no automatic trigger, and no release-publishing action. All five action uses are
pinned to verified full commits in the official action repositories: checkout
v7.0.1, setup-python v7.1.0 and upload-artifact v7.0.2. Python selects 3.12 rather
than floating across all 3.x releases. Checkout does not persist credentials.
Full-SHA pins reduce mutable-reference risk, but do not make the entire hosted
runner immutable; current GitHub guidance also describes dependency lockfiles
for broader/transitive locking [E3].

Fast offline checks run before destructive provisioning and large downloads.
The live tests use the same already-resolved SukiSU SHA and source lock as the
build. Native codec compilation and sanitizers are mandatory in that CI test
step; host Clang is an explicit test dependency, not a replacement for the
certified kernel compiler. A GCC fallback was rejected after its treatment of
an unavailable legacy static freestanding API failed the harness warning gate;
no warning gate or vendored kernel source was weakened to accommodate it. Runner provisioning is extracted to `ci_runner.sh`, guarded to the
explicit disposable GitHub-hosted environment. Existing memory sizing and
swappiness choices are preserved, rather than claiming a newly measured optimum.
Swap files receive private permissions before allocation and existing paths are
not overwritten. No provisioning script was executed on this review host.

Separate preflight/compile logs, JSON phase timings, resource diagnostics,
complete failure-evidence paths and attempt-specific artifact names improve
observability. Compression level 1 on the already compressed/large binary
artifact bundle is a CPU-versus-transfer tradeoff, not a measured wall-clock
claim. Successful evidence remains strict; diagnostic upload tolerance does not
turn a failed build green.

### Why no generated-output cache was added

The pipeline builds the newest published month with changing helper SHAs, full
LTO and BTF. Restoring `out/`, setting `SKIP_MRPROPER`, or trusting compiler outputs
from a partial key would contradict the clean-build and certification boundaries.
The maintenance version deliberately refuses these paths. A job that fails early
with a precise diagnostic is preferable to a faster stale build.

A future cache should first target immutable source/toolchain objects, not
configuration or generated kernels. Its key must cover the certified project
revision map, repo-tool revision, platform and relevant format/tool versions;
restored checkouts must still be verified against exact commits. No broad
restore-prefix fallback should supply incompatible generated state. Measure
cache restore/upload cost, compressed size and hit rate across actual monthly
runs before adding storage complexity. The delivered phase timings provide that
measurement foundation, without inventing a numeric gain.

Keep the current bounded repo sync parallelism and AOSP-owned compile settings
until resource evidence supports a change. More compiler jobs can increase
full-LTO/BTF memory pressure rather than shorten the critical path. Retain
`MALLOC_ARENA_MAX=2`, existing niceness/ionice and reviewed host memory strategy
for now. No kernel FullLTO-to-ThinLTO substitution, BTF removal or CFI weakening
is used as a CI optimization.

### Remaining build-system opportunities

The resolver's HTML row parsing and ref parsing can receive additional
conflicting-duplicate and format-drift tests in a later isolated change. No
unsupported replacement release API was invented. Git source fetches could gain
bounded operation-specific retry/timeouts without changing exact SHAs; distinguish
transient transport failure from an absent revision, and never retry by choosing
a different branch. These are documented follow-ups, not implemented claims.

## 7. Phase 3 — kernel optimization decisions

**Retain the present profile; do not add speculative tuning.** CPU-specific
`-mcpu`, globally aggressive optimizer flags, alternate compilers, disabling
CFI/SELinux/MODVERSIONS, changing FullLTO, removing BTF/DWARF, or generic vendor
module load bypasses do not satisfy this project's safety contract. No such
change is delivered. The host sanitizer compiler is used only for test fixtures,
never to build the certified kernel.

| Area | Practical decision | KMI/CRC impact | Benefit hypothesis and validation |
| --- | --- | --- | --- |
| LZ4/ZRAM | Retain initialized fast-reset LZ4, safe decode and `lz4` default | Existing private wrapper boundary retained; all final gates still required | Test representative 4-KiB page corpora, compression ratio, compress/decompress latency, incompressible tails and peak memory; host functional harness delivered, device gains unmeasured |
| LZ4KD | Retain optional availability, not a new default | No new proposal to exported GKI LZ4; fetched codec still needs independent review | Audit exact helper code and compare ratio/CPU costs before any default change |
| BBR3 | Retain BBR1 fallback; require hidden-type/consumer ABI work | Existing shared-type concerns unresolved | Validate loss, ECN, retransmission, TSO and lifecycle behavior on an appropriate test kernel; evaluate throughput with latency/fairness, not throughput alone |
| BBR callback owner | Optional exact-name comparison only | No public type/prototype/export/config change; CRC equality expected but must be built and checked | Prevents conditional prefix misdispatch; extracted C regression passes; no performance claim |
| FQ | Keep availability; no runtime interface rewrite | No new kernel/config change | Determine the actual qdisc/stack policy before attributing BBR behavior to FQ |
| MGLRU / TEO | Retain reviewed selections | No new layout/config change | Later workload evaluation must include reclaim stalls, tail latency and residency/power, not just enabled flags |
| RCU | Retain lazy-default-off and no callback-offload command line | No change | Avoid unreviewed boot/lifecycle effects; do not infer alternate settings are universally faster |
| Small scheduler/power patches | Preserve exact existing series | No newly introduced ABI changes | Inspect codegen and targeted boundary cases; any quantitative gain remains a hypothesis |

No universally best kernel for this phone can be established from source flags
alone. The actionable result here is a stricter and more inspectable build
pipeline, preserved reviewed behavior, and a clearly bounded kernel correctness
proposal—not fabricated benchmark improvements.

## 8. Phase 4 — deliverables and acceptance

The package contains the complete reviewed source archive, a main unified diff,
this report, validation instructions, preservation hashes, exact test logs and
machine-readable validation results. The optional BBR proposal contains its own
repository-level patch, standalone host regression, executed log and adoption
instructions. It is not silently activated.

The baseline suite ran 77 tests with two skips after giving the archive a local
synthetic Git baseline. The new suite runs 117 tests with the same two offline
skips, adding 40 tests. Source-archive tests no longer need that synthetic Git
baseline; real build provenance still does. Synthetic test commits, generated
binaries, caches, downloaded kernel trees and credentials are excluded from the
reviewed source ZIP.

Before a maintainer promotes a kernel build, the same exact lock must pass live
source checks, real certified AOSP preflight, full compilation, final reviewed
configuration, kernel release checks, certified export presence/CRC/type/namespace
checks and artifact/evidence verification. The BBR hidden-type issues additionally
need binary ABI/consumer review. The delivered local validation does not replace
those steps. `docs/VALIDATION.md` supplies executable commands that do not require
a phone or automatically dispatch a workflow.

## 9. External primary references (separate from repository evidence)

The attached code and diffs are the basis for project-specific findings. These
references were opened to verify external contracts, not to substitute for
missing project files. The historical AOSP build-script reference establishes
the Bash-array/hook behavior; it is not asserted to be the exact future monthly
manifest revision selected by this repository.

**[E1] AOSP, Maintain a stable kernel module interface.** Constrained toolchain,
configuration, monitored interfaces and ABI-description tooling.
`https://source.android.com/docs/core/architecture/kernel/stable-kmi`

**[E2] AOSP kernel/build, build.sh at
`ef08cc421bf22a7570eb5e9a57e32efa589c185a`.** `TOOL_ARGS=()`, array expansion at
make calls, POST_DEFCONFIG ordering before LTO and symbol-trimming adjustments.
`https://android.googlesource.com/kernel/build/+/ef08cc421bf22a7570eb5e9a57e32efa589c185a/build.sh`

**[E3] GitHub Actions secure-use reference; official pinned action commits.**
`https://docs.github.com/en/actions/reference/security/secure-use`
`https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1`
`https://github.com/actions/setup-python/commit/6d4fd22b20465ccc0f7354476588e8df7c9ca0e0`
`https://github.com/actions/upload-artifact/commit/cf430e030ddbb5b0abf93d22962f4752f3646cd9`

**[E4] AOSP mkbootimg bootimg.h.** Android boot-header layouts used by the
payload-identity validator; no AVB/bootability claim.
`https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/heads/main/include/bootimg/bootimg.h`

**[E5] Linux ZRAM documentation.** Compressor selection before initialization.
Newer documentation features not present in this 5.15 profile are not proposed.
`https://www.kernel.org/doc/html/latest/admin-guide/blockdev/zram.html`

**[E6] LZ4 upstream v1.11.1 release.** Release identity; not proof of adaptation
identity or Snapdragon-specific performance.
`https://github.com/lz4/lz4/releases/tag/v1.11.1`

**[E7] Google BBR v3 README.** Research/testing scope of the upstream release.
`https://github.com/google/bbr/blob/v3/README.md`

**[E8] Linux 5.15 external modules documentation.** Module.symvers fields,
versioning and full-build requirements. The additional class/namespace checks
preserve properties that CRC equality alone does not compare.
`https://docs.kernel.org/5.15/kbuild/modules.html`
