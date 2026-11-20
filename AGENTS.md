# GKI 5.15 project rules

This repository has one purpose: manually build the latest published, certified
Android 13 GKI 5.15 release through `.github/workflows/kernel-build.yml`.
`main` is the maintained branch. `README.md` describes the supported profile.

- Preserve the reviewed kernel configuration, patch contents and
  `APPLY_ORDER.txt` ordering unless the owner explicitly requests kernel changes.
  Resolve supported upstream dependencies once per run to exact commits and
  retain those revisions in build evidence. Keep the compiler and build tools
  from the matching certified GKI manifest. New BBR/LZ4 ports require review.
  Fresh resolutions must fail if reviewed BBR/LZ4/action baselines are stale,
  retagged, inconsistent, or cannot be verified. Do not reinstate warning-only
  behavior, silently downgrade, or replace reviewed private ports with raw
  upstream Linux/userspace files. Freshness does not establish binary KMI.
- Keep `workflow_dispatch` as the only build trigger and `stable` as the default
  SukiSU channel. Do not dispatch builds or publish releases during project cleanup.
- Keep required-patch failures, generated-config checks and certified export CRC
  checks fatal. Retain the existing boot packaging behavior.
- Fix bugs at their cause, reasoning from first principles and established best
  practice. Trace the failing dependency or invariant, fix the responsible layer,
  and add regression coverage; do not bypass checks or patch over symptoms.
  Configuration preflight and compilation must use the same certified AOSP build
  environment, including host tools and Kconfig capability probes.
- Validate project changes with
  `python -X utf8 -m unittest discover -s .github/workflows/scripts/tests -q`
  and `git diff --check`. Repository tests do not establish device stability.
- Keep generated artifacts, temporary sources, device captures and credentials
  out of Git. Preserve existing local evidence outside the checkout when tidying.
- Phone testing, flashing and persistent device changes require a separate request.

## Builder verification boundaries

- Reviewed selections live in `kernel_profile.py`; generated-config policy lives
  in `config_checks.py`. Preserve their values when reorganizing the builder.
  AOSP's `TOOL_ARGS` is a Bash array: pass `"${TOOL_ARGS[@]}"` to preflight
  `olddefconfig`, not scalar expansion. Record the complete precompile/final
  config delta and verify both stages; do not assume AOSP leaves every
  non-profile configuration value unchanged after its defconfig hook.
- Certified CRC equality is necessary, not a complete binary KMI proof. Keep
  frozen export presence, CRC, export type and namespace checks fatal; review
  hidden structure changes independently. Do not describe a customized output
  as Google-certified or claim measured device gains without measurements.
- Keep a fresh external build workspace and process-local Git configuration.
  Do not silently restore generated kernel outputs or skip AOSP build stages.
  Reject conflicting ambient build overrides rather than weakening verification.
- Evidence must reverify configuration policy and the full ordered patch record,
  and bind `boot.img`, AnyKernel3's `Image`, and workspace `Image` to the compiled
  `dist/Image`. Publish evidence atomically, and preserve existing evidence.
- For changes involving the private codec or CI, also run the native regression:
  `GKI_REQUIRE_NATIVE=1 GKI_TEST_SANITIZERS=1 python -X utf8 -m unittest discover -s .github/workflows/scripts/tests -v`
  and `bash -n .github/workflows/scripts/ci_runner.sh`. Host sanitizer tests do not
  replace certified compilation, full binary ABI analysis or device testing.
- Both existing SukiSU labels currently resolve `builtin` HEAD. Keep `stable`
  as the default, but do not document it as release-tag selection unless source
  resolution is explicitly changed and independently reviewed.

## Upstream refresh policy

Use the canonical URLs and family-specific selectors in `config.py`.
`upstream_policy.py` owns strict release parsing, workflow pin inventory, and
source-selection evidence. Source locks use schema 2; validate the entire
upstream record on write, replay, builder preflight, and evidence publication.
Exact-commit investigation overrides must be explicitly labeled, never presented
as latest. A replay uses the recorded commit set without chasing HEAD again.

Keep `ACTION_PINS` synchronized with full workflow SHA pins. Updating either
requires tests and release review; a GitHub release label alone is not a pin.
Do not upgrade Clang, GCC host prebuilts, build tools, or mkbootimg independently
of the selected certified build manifest. Python/runner major versions are
reviewed host choices, not kernel compiler substitutions. Do not import every
patch in SukiSU_patch; only the documented LZ4KD source slice is selected.

The SukiSU source now uses the canonical Nova repository and manager package.
Read the actual checked-out UAPI; do not spoof it or assume an older manager is
compatible. The static observations in `docs/UPSTREAM_STATUS.md` are review
notes, not a generated lock or proof that a kernel has compiled.
