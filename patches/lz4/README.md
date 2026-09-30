# LZ4 1.10.0

`0001-lz4-1.10.0.patch` replaces `lib/lz4` and `include/linux/lz4.h` with
upstream LZ4 1.10.0 and the arm64 NEON decompressor. `git apply --check`
accepts it on android13-5.15.216 with no fuzz.

The Makefile compiles the library with `-O3`, freestanding mode, the fast
decode loop, and `-DLZ4_MEMORY_USAGE=10`. That is a 1KB hash table. ZRAM
pages are 4KB, so the upstream default of 16KB (usage 14) is larger than
the page it compresses.

Decompress call sites in `crypto/lz4.c`, `crypto/lz4hc.c`,
`fs/f2fs/compress.c`, and `fs/incfs/data_mgmt.c` use
`LZ4_arm64_decompress_safe` when `CONFIG_ARM64` and
`CONFIG_KERNEL_MODE_NEON` are set. The other path stays the 4-argument
`LZ4_decompress_safe`.
