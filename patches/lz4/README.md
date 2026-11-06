# Private LZ4 1.11.0 for zram's crypto backend

Sources: [official v1.11.0](https://github.com/lz4/lz4/tree/v1.11.0/lib).
The source retains upstream's BSD-2-Clause license and copyright notices.
Standard C type/header includes are adapted to Linux types/limits. The
integer-width assertion uses the compiler's `__SIZEOF_INT__`; local `current`
variables are renamed to avoid Linux's task macro. Internal dictionary-test
helpers and state APIs use local linkage so they cannot collide with GKI's
existing library symbols. Compression logic is unchanged.
Upstream source SHA256 before these header adaptations:

- `lz4.c`: `45061782ff932eb8cf71c3aea9495cec0d392f012395272e0550e30c8593c740`
- `lz4.h`: `6fee8f913ce3bc65bf716060f65ce890d83bea905445256c7a56da5bf4824d73`

`crypto/lz4.c` includes the private source with static visibility and the
upstream freestanding interface. Kernel memcpy/memmove/memset supply memory
operations; no userspace allocation or libc is used. The per-stream state is
initialized once with `LZ4_initStream` in `lz4_alloc_ctx`, and compression
uses `LZ4_compress_fast_extState_fastReset` with caller-owned state and
acceleration 1. Decompression uses `LZ4_decompress_safe` with upstream's
aarch64 fast decode loop and 64-byte wide copy optimizations. Upstream's
default 16 KiB hash table is retained.

The exported GKI `include/linux/lz4.h` and `lib/lz4` are untouched. Their
stream layouts, calling conventions and CRCs must remain compatible with
vendor modules. The old global 1.9.4 replacement patch is removed.
