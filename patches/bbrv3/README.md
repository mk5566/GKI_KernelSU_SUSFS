# BBRv3 Linux 5.15 backport notes

Reference: [`google/bbr` `v3`](https://github.com/google/bbr/tree/v3), commit
`90210de4b779d40496dee0b89081780eeddf2a60`. The source comparison used
Google's `net/ipv4/tcp_bbr.c` and `tcp_plb.c` at that revision against the
Android `android13-5.15.216_r00` kernel commit
`5bfe2b8c1439354d25dc5b1779cd0bb75bb90a7f`. The algorithm file is
adapted for 5.15 callbacks and registers as `bbr3`; stock BBRv1 retains `bbr`.
The comparison found the BBR state machine and gain logic substantially shared
with Google v3, with API and state-storage differences concentrated near
registration, initialization, TSO sizing, ACK/loss callbacks, and diagnostics.

| Patch | Role and upstream comparison |
|---|---|
| `0001` | Backports rate-sample fields, ECN/loss accounting, fast ACK and TLP observations, route ECN-low flag, and `inet_diag` v3 data. The unused `TCP_USEC_TS` route flag from the old patch was removed. Android-specific TCP context remains in place. |
| `0002` | Adds Google's TCP PLB implementation used by BBR's ECN/repath logic. |
| `0003` | Ports the Google v3 algorithm to 5.15 callback signatures. It uses the in-socket congestion private area rather than the old patch's fallible per-socket `kmalloc`; a compile-time size check enforces the 160-byte limit. |
| `0004` | Adds the opt-in Kconfig symbol and build wiring. The selected default is `bbr3`, while the v1 path applies none of this series. |

The backport keeps Google's bandwidth model, ACK aggregation, ECN/loss
response, ProbeBW/ProbeRTT transitions, PLB state transitions, and FQ pacing
requirements. Linux 5.15 lacks newer BPF kfunc and TSO callback forms, so
those interfaces use their 5.15 equivalents. `tcp_input.c` calls BBRv3's loss
hook only when the socket's registered congestion algorithm is `bbr3`.

The old patch hid runtime TCP field changes from `genksyms` with conditional
definitions and `__kabi_placeholder_*` members. This series removes those
guards and represents the runtime fields directly. It grows
`icsk_ca_priv` from 104 to 160 bytes to hold BBRv3 state and fails compilation
if that state exceeds the allocation. This intentionally changes structure
layout; the builder disables frozen KMI enforcement and makes no vendor ABI
compatibility claim.

Source-level validation is `git apply --check`, then actual application in
series order against the selected GKI checkout. `scripts/checkpatch.pl`
reported zero errors for these four patches at the reference source, with
remaining style warnings inherited from the upstream/backport code. A Clang
kernel build and runtime traffic test are still required before treating this
backport as release-qualified.
