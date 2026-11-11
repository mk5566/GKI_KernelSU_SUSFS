# Private LZ4 1.11.1 for zram's crypto backend

Sources: [official v1.11.1](https://github.com/lz4/lz4/tree/v1.11.1/lib).
The source retains upstream's BSD-2-Clause license and copyright notices.
Standard C type/header includes are adapted to Linux types/limits. The
integer-width assertion uses the compiler's `__SIZEOF_INT__`; local `current`
variables are renamed to avoid Linux's task macro. Compiler attributes use
canonical `__<attr>__` forms and kernel inlining macros (`noinline`,
`__always_inline`) to prevent macro collisions with Linux's
`compiler_attributes.h`. Internal dictionary-test helpers and state APIs use
local linkage so they cannot collide with GKI's existing library symbols.
Compression logic is unchanged.
Upstream source SHA256 before these header adaptations:

- `lz4.c`: `2e58f124f1032f15aecc99772b7de624c4289245729fa5403506f00b06a54937`
- `lz4.h`: `c204caad0638a12c9ce80831094ec8fffb34aa57e74307067b08fc9452fc2611`

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
