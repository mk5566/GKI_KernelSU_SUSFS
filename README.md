# Xiaomi 13 Ultra: GKI 5.15 + SukiSU + Mount-Only SUSFS

Builds a customized kernel from the latest **published, certified Android 13 GKI
5.15 monthly release** for the Xiaomi 13 Ultra profile (*ishtar*, SM8550), with
SukiSU, mount-only SUSFS, BBRv3, and private LZ4/LZ4KD ZRAM integration.

The **base release** is Google-certified. The customized output is not itself a
Google-certified kernel. Frozen export CRC verification is mandatory, but is not
proof of complete binary KMI compatibility, device bootability, or stability.

Each run resolves the published certified release, its exact source/toolchain
manifest, and helper revisions once into `sources.json`. Reviewed BBR and LZ4
ports are retained **only while they match the checked upstream baseline**.
Each fresh resolution now fails on a newer BBR v3 HEAD, newer stable LZ4
release, moved release tag, mismatched original LZ4 source hash, newer reviewed
GitHub Action release, or an unavailable required freshness check. There is no
warning-only fallback and no automatic import of unreviewed kernel code.

See [upstream status and policy](docs/UPSTREAM_STATUS.md) for the 2026-11-20
observations, exact source selectors, migration notes, and remaining acceptance
gates. This is a complete builder repository; it intentionally does not vendor
the full GKI tree, certified toolchain, or dynamically resolved helper trees.

## Manual workflow

Use `.github/workflows/kernel-build.yml` through **Run workflow** on `main`.
`workflow_dispatch` remains the only trigger; `stable` remains the default.

**Channel semantics:** in this repository, both `stable` and `dev` resolve the
same SukiSU **`builtin` branch HEAD**, then pin its exact commit for that run.
These labels do not currently select a stable release versus a development
branch. Manager compatibility requires the kernel's actual UAPI version, not
merely a matching channel label. The build records that UAPI in `BUILD_INFO.md`.
The configured source is `https://git.zako.shirkneko.top:20004/shirkneko/SukiSU.git`
(`builtin` branch), which declares kernel UAPI 6; do not assume an older
manager is compatible with a new kernel without checking the recorded UAPI.

Successful runs upload `boot.img` and `AnyKernel3.zip`, plus a separate
`kernel-evidence-RUN_ID-RUN_ATTEMPT` bundle. Artifact names include the run attempt
to distinguish reruns. Failed runs upload available logs and diagnostics instead
of claiming successful evidence.

## Reviewed kernel profile

**Integrity and toolchain.** Preserve the certified manifest's compiler and build
tools, full Clang LTO, CFI, module versioning, SELinux, and DWARF/BTF requirements.
Preflight and compilation both run the certified AOSP `build/build.sh`. Missing
required patches, configuration mismatches, missing frozen exports, CRC drift,
and frozen export type/namespace drift are fatal.

**SukiSU/SUSFS.** Built-in integration with SUSFS core and `SUS_MOUNT` enabled.
Other SUSFS subfeatures, KPM, and debug/informational root logging are disabled.
Mount-only hiding is a deliberately narrow profile, not a universal root-hiding
or application-integrity guarantee. The complete upstream filesystem patch is
applied; Kconfig narrows its enabled functionality.

**ZRAM.** `lz4` is the configured default, using a private freestanding vendored
LZ4 1.11.1 implementation in the crypto wrapper. It reuses initialized compression
state and retains the exported GKI `lib/lz4` implementation and public header.
The upstream AArch64 fast decode/wildcopy paths are not evidence of measured
Cortex-X3/A715/A710-specific gains. `lz4kd` is also built in, with implementation
sources fetched from the exact recorded helper revision. Selecting a different
ZRAM compressor is a runtime initialization decision; an already initialized
ZRAM device cannot simply have its compressor switched. No device change is part
of repository validation.

**Networking.** BBRv3 is the configured default; stock BBRv1 remains built in as
the allocation-failure fallback. The congestion-control private socket area
remains 104 bytes. That preserves one layout constraint, not the entire KMI:
the existing port also contains `__GENKSYMS__`-hidden type changes requiring
binary ABI and consumer review. FQ is compiled in, but the builder does not force
the active qdisc on runtime network interfaces.

**Memory/power.** MGLRU is configured default-on with statistics off; the built-in
command line requests TEO. Lazy RCU stays at the reviewed default-off setting,
without callback-offload command-line additions. Existing s2idle, alarmtimer,
clear-page alignment, and idle-CPU scan patches remain unchanged. Runtime policy
and performance depend on the actual vendor/system environment and workload.

## Builder organization

| Responsibility | Files under `.github/workflows/scripts/` |
| --- | --- |
| CLI and exact dependency resolution | `build.py`, `target.py`, `config.py` |
| Integration/build orchestration | `kernel_builder.py` |
| Reviewed selections and generated-config verification | `kernel_profile.py`, `config_checks.py` |
| Freshness policy and resolve-once input provenance | `upstream_policy.py`, `source_lock.py` |
| Patch ordering and applied-record verification | `patch_plan.py` |
| Frozen exports and artifact payload identity | `abi.py`, `boot_validation.py` |
| Atomic evidence publication | `evidence.py`, `integrity.py` |
| Processes, diagnostics, boot repacking | `build_runtime.py`, `repack_boot.py` |
| Disposable GitHub-hosted runner provisioning | `ci_runner.sh` |

Builds require a **fresh workspace outside the source checkout**. Two processes
cannot build in the same workspace. Existing source trees, packages, and evidence
are not overwritten to simulate a clean build. Ambient toolchain overrides and
build-stage bypasses are rejected. `ci_runner.sh` deliberately refuses ordinary
local or persistent/self-hosted machines because it removes preinstalled tools
and configures host swap.

`sources.json` is schema 2. It includes canonical origins, supported refs,
selection methods, resolution timestamp, checked port releases/hashes, and
reviewed action pins. Old schema-1 locks must be resolved again. Explicit
`--*-commit` overrides remain available for controlled investigation, are
marked `explicit-commit`, and are **not** described as latest. CI uses none.
Replaying `--sources` deliberately does not re-resolve moving refs between
preflight and compilation; freshness means **at resolution**, not forever.

Evidence includes exact source revisions and local hashes, actual checkout
revisions, ordered patch hashes/targets, precompile and final configuration
snapshots and their delta, certified/built export tables, per-phase timings,
artifact identity reports, and checksums. Both packages must contain exactly the
kernel from the compiled `dist/Image`. Evidence publication rechecks the policy
and publishes only a complete validated bundle.

## Repository validation

Use Python 3.11 or newer; CI selects Python 3.12. Host Clang enables the
native LZ4 regression. To require it and enable address/undefined-behavior
sanitizers:

```sh
GKI_REQUIRE_NATIVE=1 GKI_TEST_SANITIZERS=1 \
  python -X utf8 -m unittest discover -s .github/workflows/scripts/tests -v
bash -n .github/workflows/scripts/ci_runner.sh
git diff --check
```

Offline tests also work from a source archive. **Real source resolution/build
provenance requires an actual Git checkout with a commit**; an archive name is
not an upstream revision. The two live integration tests are deliberately
conditional; a default offline pass does not mean that those tests ran.

See [validation instructions](docs/VALIDATION.md) for resolve-only, exact-lock
preflight, full build, and artifact verification without using a phone. See
[engineering review](docs/ENGINEERING_REVIEW.md) for findings, implemented changes,
KMI limitations, and deferred kernel proposals.

## Packaging boundary

Packaging behavior is unchanged: generic ramdisk-less header-v4 `boot.img` by
default, or `--base-boot` repacking through the manifest-pinned mkbootimg tools.
The packaging choice does not establish compatibility with a particular device's
partition layout. Payload checks do not validate AVB/signatures or bootability.
Phone testing, flashing, and persistent device changes require a separate review.
