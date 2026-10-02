# LZ4 1.9.4

`0001-lz4-1.9.4.patch` updates `lib/lz4` and `include/linux/lz4.h` on
android13-5.15.211. `LZ4_MEMORY_USAGE` is 10, a 1KB hash table, because
zram pages are 4KB.

The patch is the SukiSU 1.9.4 update, retargeted so `git apply` accepts
this exact kernel with no fuzz. Three context fixes were required: the
Makefile SPDX line is already in 5.15.211, one header hunk was
whitespace only and named the wrong following function, and Android has
three extra comment lines inside `LZ4_decompress_generic`.

LZ4 1.10.0 is newer upstream. The 5.15 ports that advertise it delete
the in-tree sources and apply with fuzz. Do not replace this patch with
one of those.
