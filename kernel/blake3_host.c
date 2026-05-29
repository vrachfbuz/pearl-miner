/*
 * blake3_host.c — обёртка BLAKE3 для вызова из CUDA host-кода.
 * Вызывается из pearl_mine() в noisy_gemm.cu для проверки открытия тайла.
 */

#include "blake3.h"
#include <stdint.h>
#include <string.h>

void blake3_keyed(
    const uint8_t* key,     // 32 байта = sA (seed A)
    const uint8_t* input,   // 64 байта transcript
    uint8_t*       output   // 32 байта дайджест
) {
    blake3_hasher hasher;
    blake3_hasher_init_keyed(&hasher, key);
    blake3_hasher_update(&hasher, input, 64);
    blake3_hasher_finalize(&hasher, output, 32);
}
