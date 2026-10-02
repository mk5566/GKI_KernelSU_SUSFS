# Private LZ4 1.10.0 for zram's crypto backend

Sources: [official v1.10.0](https://github.com/lz4/lz4/tree/v1.10.0/lib).
The source retains upstream's BSD-2-Clause license and copyright notices.
Standard C type/header includes are adapted to Linux types/limits. The
integer-width assertion uses the compiler's `__SIZEOF_INT__`; local `current`
variables are renamed to avoid Linux's task macro. Compression logic is unchanged.
Upstream source SHA256 before these header adaptations:

- `lz4.c`: `9396f7de527bc8435de9c7569fb7998e56545a84b4f3c2d808c0235c01774539`
- `lz4.h`: `26b82efc53d1570f3b54eef02e9c4764c1ad374ff03cac04e2ced5ea4d4c552f`

`crypto/lz4.c` includes the private source with static visibility and the
upstream freestanding interface. Kernel memcpy/memmove/memset supply memory
operations; no userspace allocation or libc is used. Compression uses
`LZ4_compress_fast_extState` with caller-owned state and acceleration 1.
Decompression uses `LZ4_decompress_safe`. Upstream's default 16 KiB hash table
is retained; smaller tables need device measurements before claiming a win.

The exported GKI `include/linux/lz4.h` and `lib/lz4` are untouched. Their
stream layouts, calling conventions and CRCs must remain compatible with
vendor modules. The old global 1.9.4 replacement patch is removed.
