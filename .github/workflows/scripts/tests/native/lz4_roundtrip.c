/* Host regression harness for the exact vendored private LZ4 implementation.
 * Linux type shims are supplied in a temporary include directory by the test.
 * This does not benchmark SM8550 or exercise the kernel crypto/ZRAM plumbing.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>

#define __maybe_unused __attribute__((unused))
#define LZ4_FREESTANDING 1
#define LZ4_memcpy memcpy
#define LZ4_memmove memmove
#define LZ4_memset memset
#define LZ4LIB_VISIBILITY static __attribute__((unused))
#define LZ4_PUBLISH_STATIC_FUNCTIONS
#define LZ4_FORCE_INLINE static inline __attribute__((always_inline))
#define LZ4_FORCE_NOINLINE static __attribute__((noinline))
#include "lz4.c"

#define CHECK(expr) do { if (!(expr)) { \
    fprintf(stderr, "FAIL at %s:%d: %s\n", __FILE__, __LINE__, #expr); return 1; \
} } while (0)

static unsigned rng = 0x12345678U;
static unsigned random_byte(void)
{
    rng ^= rng << 13;
    rng ^= rng >> 17;
    rng ^= rng << 5;
    return rng & 255U;
}

int main(void)
{
    const int lengths[] = {0, 1, 12, 13, 63, 64, 255, 4095, 4096, 8192, 65536};
    LZ4_stream_t *state = malloc(sizeof(*state));
    CHECK(state != NULL);
    CHECK(LZ4_initStream(state, sizeof(*state)) == state);
    unsigned cases = 0;
    for (unsigned iteration = 0; iteration < 3300; ++iteration) {
        int length = lengths[iteration % (sizeof(lengths) / sizeof(lengths[0]))];
        int bound = LZ4_compressBound(length);
        char *src = malloc(length ? (size_t)length : 1U);
        char *packed = malloc((size_t)bound);
        char *decoded = malloc(length ? (size_t)length : 1U);
        CHECK(src && packed && decoded);
        for (int j = 0; j < length; ++j)
            src[j] = iteration % 3 == 0 ? 0 : iteration % 3 == 1 ? (char)(j % 31) : (char)random_byte();
        /* Reuse the same initialized state after a bounded-output failure. */
        if (length > 1) {
            char *small = malloc(1);
            CHECK(small != NULL);
            CHECK(LZ4_compress_fast_extState_fastReset(state, src, small, length, 1, 1) == 0);
            free(small);
        }
        int compressed = LZ4_compress_fast_extState_fastReset(state, src, packed, length, bound, 1);
        CHECK(compressed > 0 && compressed <= bound);
        int restored = LZ4_decompress_safe(packed, decoded, compressed, length);
        CHECK(restored == length);
        CHECK(memcmp(src, decoded, (size_t)length) == 0);
        if (length > 0) {
            char *small = malloc(length > 1 ? (size_t)length - 1U : 1U);
            CHECK(small != NULL);
            CHECK(LZ4_decompress_safe(packed, small, compressed, length - 1) < 0);
            free(small);
        }
        free(src);
        free(packed);
        free(decoded);
        ++cases;
    }
    /* Deterministic malformed/random block coverage with bounded buffers. */
    for (unsigned iteration = 0; iteration < 20000; ++iteration) {
        int size = (int)(random_byte() % 65U);
        char *input = malloc(size ? (size_t)size : 1U);
        char *output = malloc(4096);
        CHECK(input && output);
        for (int j = 0; j < size; ++j)
            input[j] = (char)random_byte();
        CHECK(LZ4_decompress_safe(input, output, size, 4096) <= 4096);
        free(input);
        free(output);
    }
    char malformed[] = {(char)0xf0};
    char output[16];
    CHECK(LZ4_decompress_safe(malformed, output, sizeof(malformed), sizeof(output)) < 0);
    free(state);
    printf("PASS: %u roundtrips; 20000 bounded random decoder inputs\n", cases);
    return 0;
}
