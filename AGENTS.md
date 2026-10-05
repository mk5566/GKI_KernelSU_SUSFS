# GKI 5.15 project rules

This repository has one purpose: manually build the latest published, certified
Android 13 GKI 5.15 release through `.github/workflows/kernel-build.yml`.
`main` is the maintained branch. `README.md` describes the supported profile.

- Preserve the reviewed kernel configuration, source pins, patch contents and
  `APPLY_ORDER.txt` ordering unless the owner explicitly requests kernel changes.
- Keep `workflow_dispatch` as the only build trigger and `stable` as the default
  SukiSU channel. Do not dispatch builds or publish releases during project cleanup.
- Keep required-patch failures, generated-config checks and certified export CRC
  checks fatal. Retain the existing boot packaging behavior.
- Validate project changes with
  `python -X utf8 -m unittest discover -s .github/workflows/scripts/tests -q`
  and `git diff --check`. Repository tests do not establish device stability.
- Keep generated artifacts, temporary sources, device captures and credentials
  out of Git. Preserve existing local evidence outside the checkout when tidying.
- Phone testing, flashing and persistent device changes require a separate request.
