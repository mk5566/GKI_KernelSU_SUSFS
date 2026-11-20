# Validation without a phone

## 1. Replace and inspect

Use the complete replacement source archive, including `.github`, `.gitignore`,
and `.gitattributes`. Keep your existing `.git` directory and repository history;
the archive intentionally contains no `.git` directory. Make a backup or use a
clean checkout first. Overlay the supplied tree, inspect `git diff --stat` and
`git diff --check`, and review any unrelated local changes separately. No
original patch file or application-order file is changed by this refresh.
The earlier optional BBR owner-check proposal is not applied.

Commit approved builder changes in your real repository before resolving build
inputs. No artificial test commit is included in the source archive or usable as upstream provenance. Do not reuse source locks produced before
builder inputs changed.

## 2. Run the offline gates

```sh
python --version  # >= 3.11; CI uses 3.12
GKI_REQUIRE_NATIVE=1 GKI_TEST_SANITIZERS=1 \
  python -X utf8 -m unittest discover -s .github/workflows/scripts/tests -v
bash -n .github/workflows/scripts/ci_runner.sh
git diff --check
```

The native test requires host Clang with AddressSanitizer and
UndefinedBehaviorSanitizer support. It tests the exact private vendored codec,
not the kernel crypto wrapper or ARM performance. Without the explicit native
requirement, lack of Clang is reported as a skip, not a native-code pass.

The live certified-download and exact-SukiSU-source tests remain skipped unless
their prerequisites are explicitly supplied. CI supplies both and makes native
compilation mandatory. Optional additional linting: run `actionlint` against the
workflow and `shellcheck` against `ci_runner.sh` when installed. Their absence is
not equivalent to a lint pass.

## 3. Resolve once, then run the live tests

These commands require network access and a genuine committed checkout. They do
not dispatch GitHub Actions, publish a release, or contact a phone.

```sh
set -euo pipefail
ROOT="$PWD"
SCRIPT="$ROOT/.github/workflows/scripts/build.py"
LOCK_DIR="$(mktemp -d /tmp/gki-lock.XXXXXX)"
python "$SCRIPT" --resolve-only --sukisu-channel stable --workspace "$LOCK_DIR"

SUKISU_DIR="$(mktemp -d /tmp/gki-sukisu-test.XXXXXX)"
COMMIT="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["sukisu_commit"])' "$LOCK_DIR/sources.json")"
REPO_URL="$(python -c 'import sys; sys.path.insert(0,".github/workflows/scripts"); from config import KSU_REPO_CONFIG; print(KSU_REPO_CONFIG["repo_url"])')"
git init "$SUKISU_DIR"
git -C "$SUKISU_DIR" fetch --depth=1 "$REPO_URL" "$COMMIT"
git -C "$SUKISU_DIR" checkout --detach FETCH_HEAD
test "$(git -C "$SUKISU_DIR" rev-parse HEAD)" = "$COMMIT"
GKI_TEST_NETWORK=1 GKI_TEST_SOURCES="$LOCK_DIR/sources.json" \
  SUKISU_TEST_SOURCE="$SUKISU_DIR" GKI_REQUIRE_NATIVE=1 GKI_TEST_SANITIZERS=1 \
  python -X utf8 -m unittest discover -s .github/workflows/scripts/tests -v
```

Review `sources.json`: the selected tag must be published with certified
downloads; the kernel and every required project must be exact commits from its
matching certified manifest. Schema-2 `upstream_status` must record canonical
origins, compatible selectors, checked BBR/LZ4 baselines, original LZ4 hashes,
and current full-SHA action pins. Default resolution is mandatory-strict;
`--strict-upstreams` is now only a compatibility alias during resolution and is
not accepted with `--sources`. There is no non-strict build mode. A controlled
explicit commit override is labeled as such and is not a latest-source run.
Never substitute a moving branch, stale release, or another build's CRC
reference when a download/check fails. GitHub API rate limits also fail closed;
retry after the service is available rather than weakening the checks.

## 4. Run certified-environment configuration preflight

Install host build dependencies through your normal local administration process.
The package list is visible in `ci_runner.sh`. Do **not** run that script locally
or spoof its hosted-runner guard; it performs destructive disposable-runner
provisioning. The compiler, pahole and build-tool selection remain owned by the
certified manifest and AOSP build environment, not host compiler defaults.

```sh
PREFLIGHT_DIR="$(mktemp -d /tmp/gki-preflight.XXXXXX)"
python "$SCRIPT" --sources "$LOCK_DIR/sources.json" \
  --workspace "$PREFLIGHT_DIR" --preflight-only
```

Inspect `preflight.log`, `precompile.config`, `CONFIG_REPORT.json`,
`PATCH_REPORT.json`, `checkouts.json`, `BUILD_TIMINGS.json`, and `BUILD_INFO.md`.
Required patches and the generated profile must pass. This mode stops before
kernel compilation and **does not** claim certified CRC verification.

## 5. Build and verify, using a separate fresh workspace

A preflight-only run integrates sources and consumes its workspace. It is not an
incremental build cache. Use another fresh workspace with the same source lock;
a normal full build includes its own certified-environment preflight.

```sh
BUILD_DIR="$(mktemp -d /tmp/gki-build.XXXXXX)"
python "$SCRIPT" --sources "$LOCK_DIR/sources.json" \
  --workspace "$BUILD_DIR" --output-json "$BUILD_DIR/result.json"
```

Required result: exit 0, positive certified export match count, complete
`evidence/`, both configuration stages passed, and packages bound to compiled
`dist/Image`. Any nonempty configuration delta must be inspected. In particular,
AOSP's post-hook LTO and `TRIM_UNUSED_KSYMS`/whitelist setup can legitimately
change non-profile keys; an empty delta is not a universal requirement.

`ABI_REPORT.json` verifies export presence, CRCs, export type and namespace. It is
not a DWARF/BTF type-layout or semantic-ABI report. `ARTIFACT_REPORT.json` verifies
kernel payload identity, not AVB, signatures, partitions or bootability.

Check the complete checksum set in a disposable staging directory. Artifact
hashes intentionally live in the same manifest as evidence metadata, while the
large artifacts are uploaded separately:

```sh
CHECK_DIR="$(mktemp -d /tmp/gki-check.XXXXXX)"
cp -a "$BUILD_DIR/evidence/." "$CHECK_DIR/"
cp "$BUILD_DIR/boot.img" "$BUILD_DIR/AnyKernel3.zip" "$CHECK_DIR/"
unzip -p "$BUILD_DIR/AnyKernel3.zip" Image > "$CHECK_DIR/Image"
(cd "$CHECK_DIR" && sha256sum -c SHA256SUMS)
```

For downloaded workflow artifacts, substitute their extracted directories for
`BUILD_DIR`. Do not edit the manifest to remove failures.

## 6. Complete binary-KMI review before promoting a kernel build

Using the exact selected certified manifest's ABI tooling and the real built
`vmlinux`, compare the ABI description against the corresponding certified
baseline. Do not treat a copied baseline `abi.xml` as an ABI dump generated from
the modified binary. Inspect layout, offsets, bitfields, and users of
`tcp_sock`, `tcp_skb_cb`, `rate_sample`, `tcp_bbr_info`, `tcp_cc_info`, and related
callback contracts; examine actual vendor modules where needed. The existing
`__GENKSYMS__` guards make CRC-only conclusions particularly insufficient.

For a kernel proposal, rerun all previous gates after applying it and resolving a
new source lock. The separate exact-owner proposal includes a host regression
that reproduces the prefix collision and tests the proposed fix; that is not a
substitute for applying the full stack to the resolved kernel and compiling it.

No flashing, booting, release publication, or workflow dispatch is performed by
these validation instructions automatically. The hosted workflow itself remains
manual-only.
