# Latest-compatible upstream refresh — 2026-11-20

## What this replacement changes

This source tree includes the earlier builder-hardening delivery and adds strict
upstream freshness, canonical source migrations, schema-2 provenance, tests,
and workflow fail-fast ordering. It is a complete replacement builder repository,
not a kernel/toolchain source mirror. The full upstream trees are resolved and
fetched during a maintainer-initiated run; generated locks and outputs are not
committed.

All 21 original files in `patches/` remain byte-for-byte unchanged. This is
intentional: the supplied private LZ4 v1.11.1 and BBR port revision still match
the upstream baselines observed below. Local Android adaptations and correctness
fixes are not interchangeable with raw userspace LZ4 or a newer Linux network
stack. The reviewed kernel profile and every APPLY_ORDER.txt are unchanged.

## Public-source observations

The following observations were made through public primary-source pages on
2026-11-20. They are not the output of a real source-resolution/build run in the
review environment. Branch heads can move; each fresh CI resolution queries them
again, records exact revisions, and stops on unsupported port drift.

| Component | Current observation | Selection used by the builder |
| --- | --- | --- |
| Certified GKI | `android13-5.15-2026-09_r5`, kernel `5.15.211`, release date 2026-11-17, build `16941857`, commit `538eb7b24b584a0c2d880f744587b35517e837e3` | Newest published certified monthly tag/respin for Android 13 5.15; not an arbitrary November branch or uncertified point tag |
| SukiSU | Canonical `SukiSU-Nova/SukiSU-Nova`, `builtin`; observed `4bec532dd447e2852aa8932afa768255350c103a` (2026-11-16) | Query supported branch once, fetch the exact resulting commit |
| SUSFS | `simonpunk/susfs4ksu`, `gki-android13-5.15`; observed `c1d1da42b74771944207ec45aaceff9c57e84985` (2026-11-15); header reports v2.4.0 | Only the Android 13 / 5.15 branch and its matching primary patch |
| LZ4 | Latest stable v1.11.1, commit `aa4647fad575d01a874aaaadcdbe8e6a40e072f5` | Retain private reviewed port; require latest release label, peeled commit, and both original upstream source hashes to match |
| BBRv3 | Official `google/bbr` v3 endpoint resolves to `90210de4b779d40496dee0b89081780eeddf2a60` | Retain reviewed 5.15 adaptation; require a live v3 HEAD match at resolution |
| Optional LZ4KD source provider | Canonical `SukiSU-Nova/SukiSU_patch`, main; observed `4ee085a39e3c95e44c9cac7af040e83bf926e6fa` (2026-11-13) | Resolve exact main HEAD; copy only the selected LZ4KD implementation files, never the provider's entire patch collection |
| AnyKernel3 | Canonical owner is `WildKernels`, supported branch `gki-2.0` | Resolve exact branch HEAD on each fresh run. Its complete live HEAD SHA was not independently established in this review; no snapshot SHA is fabricated |
| repo launcher/tool | Google stable branch observed at `d7c431b9968d928373fe9b463a51d304d3b2c48a` | Resolve stable once; launcher and repo init use the same exact revision |
| Build/config/Clang/GCC/build-tools/mkbootimg | Owned by the selected certified build's exact manifest | Never independently upgrade to the globally newest compiler/tools; that would change the certified environment |
| Workflow actions | checkout v7.0.1; setup-python v7.1.0; upload-artifact v7.0.2 | Existing full commit pins retained; latest stable release and peeled tag SHA are now checked at fresh resolution |
| s2idle/alarmtimer/clear-page/CPU-scan/ZRAM glue | Project-maintained Android adaptations, not independently versioned upstream products | Preserve reviewed content/order; required application remains fatal. If a new certified baseline has absorbed a patch, review/remove or rebase it explicitly—never silently skip it |

The `stable` workflow label still selects the supported SukiSU `builtin` HEAD;
it is not a release-tag selector. The `dev` label has the same source selector.

### Source references

- [AOSP published certified releases](https://source.android.com/docs/core/architecture/kernel/gki-android13-5_15-release-builds)
- [Selected GKI tag](https://android.googlesource.com/kernel/common/+/refs/tags/android13-5.15-2026-09_r5)
- [SukiSU's own current setup script](https://raw.githubusercontent.com/SukiSU-Nova/SukiSU-Nova/builtin/kernel/setup.sh)
- [SukiSU builtin head](https://github.com/SukiSU-Nova/SukiSU-Nova/commit/builtin.patch)
- [Observed SukiSU UAPI header](https://raw.githubusercontent.com/SukiSU-Nova/SukiSU-Nova/4bec532dd447e2852aa8932afa768255350c103a/kernel/include/uapi/supercall.h)
- [SUSFS branch API](https://gitlab.com/api/v4/projects/simonpunk%2Fsusfs4ksu/repository/branches/gki-android13-5.15)
- [Observed SUSFS version header](https://gitlab.com/simonpunk/susfs4ksu/-/raw/c1d1da42b74771944207ec45aaceff9c57e84985/kernel_patches/include/linux/susfs.h)
- [Official LZ4 release](https://github.com/lz4/lz4/releases/tag/v1.11.1)
- [Official Google BBR v3 head](https://github.com/google/bbr/commit/v3.patch)
- [LZ4KD source-provider head](https://github.com/SukiSU-Nova/SukiSU_patch/commit/main.patch)
- [Canonical AnyKernel3 repository](https://github.com/WildKernels/AnyKernel3)
- [Google repo stable branch](https://gerrit.googlesource.com/git-repo/+/refs/heads/stable)
- [checkout release](https://github.com/actions/checkout/releases/latest), [setup-python release](https://github.com/actions/setup-python/releases/latest), [upload-artifact release](https://github.com/actions/upload-artifact/releases/latest)

## Freshness and reproducibility contract

1. A normal manual CI run performs fresh resolution. Unsupported newer ports,
   changed tags/hashes, conflicting remote references or certified build rows,
   and unavailable required upstream checks are fatal. Transient release/file
   downloads get at most three bounded attempts; no stale cached result or
   alternate source silently substitutes for a failed check.
2. BBR and LZ4 are reviewed backports, not live source overlays. When upstream
   advances, a maintainer must review/rebase the port and rerun the kernel gates.
   The resolver intentionally cannot certify arbitrary new code by changing a
   version number. Likewise, a newer Action must be reviewed and its full SHA
   updated in both workflow and ACTION_PINS.
3. GKI, SukiSU, SUSFS, helper trees, repo and the certified manifest are resolved
   once. Test checkout, preflight, compilation and evidence use those exact
   revisions. `--sources` is a deliberate replay of a previous observation and
   does not claim that upstream still has the same HEAD today.
4. Source-lock schema 2 records origins, selectors, commits, timestamp, reviewed
   ports, original LZ4 file digests and action pins. The lock is checked when
   saved, loaded, used for builder preflight and reloaded for final evidence.
   Old schema-1 locks must be regenerated. The record is inspectable provenance,
   not a cryptographic attestation from the upstream maintainers.
5. Explicit commit CLI overrides remain available for investigation. They are
   labeled `explicit-commit`, not latest, and are not exposed by this workflow.
   A commit override never disables freshness checks on vendored ports/actions,
   required patches, generated config, or certified export comparisons.
6. The certified manifest is the compatibility authority for Clang, host GCC
   prebuilts, build scripts/tools and boot tools. Ubuntu 24.04/Python 3.12 remain
   reviewed host choices; setup-python checks for the latest patch release within
   3.12 rather than silently preferring an older cached patch. Host package installation uses that Ubuntu release's
   repositories; no claim is made that every package is its globally newest
   upstream major version.

## Compatibility boundaries and migration notes

**SukiSU migration:** the observed Nova source changes its manager package to
`com.sukisu.nova` and declares kernel UAPI 6. The builder reads the actual checked
out header, not a hardcoded version. Do not assume an older Ultra manager's
package/signature/UAPI is accepted. No manager installation or phone operation
is performed by this repository update.

**Upstream Makefile metadata:** SukiSU's own Makefile currently performs network
queries for display/version metadata and may unshallow its Git history. The
builder does not execute the mutable setup.sh installer, and it verifies final
source HEADs. This refresh does not rewrite SukiSU's version-number semantics
or claim fully hermetic/offline, bit-reproducible compilation. This is distinct
from the exact kernel/source/tool revision policy.

**KMI:** staying on the same supported GKI family and preserving all certified
export CRCs is necessary, not sufficient for complete binary compatibility.
The inherited BBR port still has `__GENKSYMS__`-hidden type changes. Its actual
compiled layouts/semantics and vendor consumers require separate ABI review.
No check is bypassed or weakened, and no full-KMI certification is claimed.
The source resolver cannot turn an uncompiled branch HEAD into a proven device-
compatible kernel. Required patch/config/CRC failures stop rather than downgrade.

**Validation actually performed for this refresh:** offline unittest suite,
host LZ4 ASan/UBSan tests, source/syntax/workflow checks, archive extraction and
source-preservation checks. See the delivery's validation report for exact counts.
The review environment could read web sources but could not fetch full upstream
Git trees/toolchains. Real latest-source resolution, source-stack application,
certified preflight/full compilation, final compiled export checks, and full
binary ABI comparison must run in a network-enabled maintainer environment.
No workflow was dispatched, no release was published and no phone was contacted.

Use [VALIDATION.md](VALIDATION.md) for all acceptance commands. Commit this source
update before generating a new source lock; do not commit generated build locks,
temporary source trees, logs, artifacts or credentials.
