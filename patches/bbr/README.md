# BBRv3 on android13-5.15.211

`0001-bbrv3-android-kabi.patch` is the series that booted on this phone.
It registers BBRv3 as `bbr3` and leaves stock BBRv1 registered as `bbr`.

The patch keeps `icsk_ca_priv` at 104 bytes and hides the extra BBRv3
fields from `genksyms` with `__GENKSYMS__` and `__kabi_placeholder_*`.
Vendor modules on Xiaomi 13 Ultra HyperOS 3 are built against that
layout. Growing the array, or dropping the genksyms guards, changes
symbol CRCs. Those modules then fail to load and the device stays on
the Xiaomi logo.

Do not replace this patch with a "cleaner" backport that edits
`struct tcp_sock` or `icsk_ca_priv` in a way `genksyms` can see.

5.15.211 calls `tcp_in_ack_event()` between the RACK window update and
the TLP ack in `tcp_ack()`. The patch context includes that call. Do
not delete it to match an older 5.15.180 hunk.
