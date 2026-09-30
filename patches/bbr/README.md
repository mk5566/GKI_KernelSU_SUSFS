# BBRv3 on android13-5.15

`0001-bbrv3-android-kabi.patch` registers BBRv3 as `bbr3` and leaves stock
BBRv1 registered as `bbr`.

The patch keeps `icsk_ca_priv` at 104 bytes and hides the extra BBRv3
fields from `genksyms` with `__GENKSYMS__` and `__kabi_placeholder_*`.
Vendor modules on this phone are built against that layout. Growing the
array, or dropping the genksyms guards, changes symbol CRCs. Those
modules then fail to load and the device stays on the Xiaomi logo.

Do not replace this patch with a backport that edits `struct tcp_sock` or
`icsk_ca_priv` in a way `genksyms` can see.

`tcp_ack()` calls `tcp_in_ack_event()` between the RACK window update and
the TLP ack. That call is present on 5.15.211 and 5.15.216. The patch
context includes it, and `git apply --check` accepts the file on 5.15.216
with no fuzz.
