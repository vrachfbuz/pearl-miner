/*
 * noisy_gemm.cu — NoisyGEMM CUDA ядро для NVIDIA Turing (sm_75 / CMP 40HX)
 *
 * Алгоритм: whitepaper Pearl, раздел 4 + pearl_noisygemm_reference.py
 *
 * ПУТЬ ВЫЧИСЛЕНИЙ:
 *   1. Host подготавливает A' = A+E (int8) и B' = B+F (int8) — передаёт на GPU
 *   2. Ядро noisy_gemm_kernel: тайловый int8-matmul через DP4A, transcript rotl32-XOR
 *   3. Проверка открытия: BLAKE3(transcript, key=sA) <= target
 *   4. При успехе — запись индексов тайла в result_tile
 *
 * АРХИТЕКТУРА SM_75 (Turing):
 *   - DP4A через __dp4a() — гарантирован на sm_75 (4x int8 → int32 за 1 такт)
 *   - INT8 Tensor Cores (IMMA / mma.sync) — экспериментальный путь, включить #define USE_IMMA
 *   - BLAKE3 на host (первая итерация); TODO перенести BLAKE3 на GPU
 *
 * КОМПИЛЯЦИЯ (на HiveOS/Linux):
 *   nvcc -arch=sm_75 -O3 -o noisy_gemm.so --shared \
 *        -Xcompiler -fPIC noisy_gemm.cu blake3_device.cu
 */

#include <cuda_runtime.h>
#include <stdint.h>
#include <string.h>

// ---------------------------------------------------------------------------
// Конфигурация
// ---------------------------------------------------------------------------

#define HASH_TILE_H   16       // размер тайла для проверки открытия (строки)
#define HASH_TILE_W   16       // размер тайла для проверки открытия (столбцы)
#define TRANSCRIPT_U32 16      // 16 × uint32 = 64 байта transcript
#define WARP_SIZE     32

// Ориентир из pearl-research-labs: noise_rank = 128
#define DEFAULT_NOISE_RANK 128

// ---------------------------------------------------------------------------
// Structures
// ---------------------------------------------------------------------------

struct MiningConfig {
    int m, n, k;              // размеры матриц
    int noise_rank;           // r
    int hash_tile_h;          // tm
    int hash_tile_w;          // tn
};

// Результат: индексы открытого тайла (-1 если не найдено)
struct TileResult {
    int tile_i;               // начальная строка тайла
    int tile_j;               // начальный столбец тайла
    uint32_t transcript[TRANSCRIPT_U32];  // transcript M для доказательства
    int found;                // 1 = открыт, 0 = нет
};

// ---------------------------------------------------------------------------
// Вспомогательные функции устройства
// ---------------------------------------------------------------------------

__device__ __forceinline__ uint32_t rotl32(uint32_t x, int s) {
    return (x << s) | (x >> (32 - s));
}

// DP4A: 4-байтовый int8 dot-product, накопление в int32.
// На sm_75 транслируется в одну инструкцию dp4a.
__device__ __forceinline__ int dp4a_s8s8(int a, int b, int c) {
    int result;
    asm volatile("dp4a.s32.s8 %0, %1, %2, %3;" : "=r"(result) : "r"(a), "r"(b), "r"(c));
    return result;
}

// ---------------------------------------------------------------------------
// BLAKE3 на GPU — заглушка (первая итерация: вызываем host-версию через атомик).
// Полноценная GPU-реализация BLAKE3 — следующий шаг.
// ---------------------------------------------------------------------------

// Буфер transcript'ов всех активных тайлов для проверки на host.
// GPU только записывает transcript; host читает и проверяет BLAKE3.
// Это неоптимально (PCIe трафик), но корректно для начала.

// ---------------------------------------------------------------------------
// Главное ядро NoisyGEMM
//
// Каждый блок потоков обрабатывает один hash-тайл (hash_tile_h × hash_tile_w).
// Внутри блока warp'ы совместно вычисляют C'[i:i+h, j:j+w] = sum_d A'[i:,d:d+r] @ B'[d:d+r,j:]
// и обновляют transcript.
//
// Grid: (ceil(m/tm), ceil(n/tn))
// Block: (32, tm) — один warp на строку, tm строк параллельно
// ---------------------------------------------------------------------------

__global__ void noisy_gemm_kernel(
    const int8_t*  __restrict__ Ap,        // A' (m×k), row-major, int8
    const int8_t*  __restrict__ Bp,        // B' (k×n), row-major, int8  [транспонируем при загрузке]
    int32_t*       __restrict__ Cp,        // C' выход (m×n), row-major, int32
    uint32_t*      __restrict__ transcripts, // буфер transcript'ов [n_tiles × TRANSCRIPT_U32]
    int32_t*       __restrict__ tile_found,  // [n_tiles], -1=нет, иначе индекс
    const MiningConfig cfg
) {
    const int tm = cfg.hash_tile_h;
    const int tn = cfg.hash_tile_w;
    const int r  = cfg.noise_rank;

    // Индексы тайла
    const int bi = blockIdx.x;   // тайл по строкам
    const int bj = blockIdx.y;   // тайл по столбцам
    const int ti = bi * tm;      // начало тайла в матрице
    const int tj = bj * tn;

    if (ti >= cfg.m || tj >= cfg.n) return;

    const int h = min(tm, cfg.m - ti);   // реальная высота тайла
    const int w = min(tn, cfg.n - tj);   // реальная ширина тайла

    // Каждый поток отвечает за одну ячейку (row, col) внутри тайла
    const int row = threadIdx.y;   // строка внутри тайла  [0..tm-1]
    const int col = threadIdx.x;   // столбец внутри тайла [0..tn-1]

    if (row >= h || col >= w) return;

    // Аккумулятор ячейки (int32, достаточно для int8 × int8 × k)
    int32_t acc = 0;

    // Transcript обновляется на полных блоках r-колонн матрицы A'.
    // Хранится в shared memory для warp-reduce.
    __shared__ uint32_t sM[TRANSCRIPT_U32];
    if (threadIdx.x == 0 && threadIdx.y == 0) {
        for (int ii = 0; ii < TRANSCRIPT_U32; ii++) sM[ii] = 0u;
    }
    __syncthreads();

    int reduction_count = 0;

    // Внешний цикл по шагам r колонн k-размерности
    for (int d = 0; d + r <= cfg.k; d += r) {
        // Обнуляем аккумулятор для этого куска
        int32_t chunk_acc = 0;

        // Внутренний DP4A-цикл: 4 int8 за раз
        // Для sm_75: компилятор должен использовать dp4a.s32.s8
        for (int s = d; s < d + r; s += 4) {
            int remaining = min(4, d + r - s);
            if (remaining == 4) {
                // Упаковываем 4 int8 → int32 (LE)
                int va = 0, vb = 0;
                const int8_t* ap = Ap + (ti + row) * cfg.k + s;
                const int8_t* bp = Bp + (tj + col) * cfg.k + s;  // B' хранится транспонированной
                // TODO: использовать asm dp4a для гарантии sm_75
                for (int q = 0; q < 4; q++) {
                    va |= ((int32_t)(uint8_t)ap[q]) << (q * 8);
                    vb |= ((int32_t)(uint8_t)bp[q]) << (q * 8);
                }
                chunk_acc = dp4a_s8s8(va, vb, chunk_acc);
            } else {
                // Неполный хвост: скалярно
                for (int q = 0; q < remaining; q++) {
                    chunk_acc += (int32_t)Ap[(ti + row) * cfg.k + s + q]
                               * (int32_t)Bp[(tj + col) * cfg.k + s + q];
                }
            }
        }
        acc += chunk_acc;

        // Проверка: полный кусок (h==tm, w==tn, шаг==r) → обновляем transcript
        if (h == tm && w == tn) {
            // XOR всех acc по warp'у в одно 32-битное слово
            uint32_t X = (uint32_t)(acc & 0xFFFFFFFF);
            // Warp-reduce XOR
            for (int offset = WARP_SIZE / 2; offset > 0; offset >>= 1) {
                X ^= __shfl_down_sync(0xFFFFFFFF, X, offset);
            }
            // Только lane 0 каждого warp'а обновляет M[reduction_count % 16]
            if ((col % WARP_SIZE) == 0) {
                uint32_t idx = reduction_count % TRANSCRIPT_U32;
                atomicXor(&sM[idx], rotl32(sM[idx], 13) ^ X);
            }
            reduction_count++;
        }
    }

    __syncthreads();

    // Записываем C'[row, col] в глобальную память
    Cp[(ti + row) * cfg.n + (tj + col)] = acc;

    // Сохраняем transcript тайла для проверки на host
    if (threadIdx.x == 0 && threadIdx.y == 0 && h == tm && w == tn) {
        int tile_idx = bi * gridDim.y + bj;
        uint32_t* dst = transcripts + tile_idx * TRANSCRIPT_U32;
        for (int ii = 0; ii < TRANSCRIPT_U32; ii++) {
            dst[ii] = sM[ii];
        }
    }
}

// ---------------------------------------------------------------------------
// Host-сторона: запуск ядра + проверка BLAKE3 на host
// ---------------------------------------------------------------------------

// Объявление BLAKE3 (реализация в blake3_device.c или отдельная lib)
extern "C" void blake3_keyed(
    const uint8_t* key,         // 32 байта = sA
    const uint8_t* input,       // 64 байта transcript
    uint8_t*       output       // 32 байта дайджест
);

extern "C" int pearl_mine(
    const int8_t*  Ap,          // A' (m×k), host или device ptr
    const int8_t*  Bp_T,        // B'^T (n×k), row-major (транспонированная), host или device
    const uint8_t* sA,          // seed A, 32 байта
    uint64_t       target_lo,   // target (256-бит, LE) — нижние 64 бита
    uint64_t       target_hi,   // верхние 64 бита (для сравнения, остальные = 0)
    MiningConfig   cfg,
    TileResult*    result        // выходной результат
) {
    const int n_tiles_i = (cfg.m + cfg.hash_tile_h - 1) / cfg.hash_tile_h;
    const int n_tiles_j = (cfg.n + cfg.hash_tile_w - 1) / cfg.hash_tile_w;
    const int n_tiles   = n_tiles_i * n_tiles_j;

    // Аллоцируем GPU-буферы
    size_t szAp  = (size_t)cfg.m * cfg.k * sizeof(int8_t);
    size_t szBp  = (size_t)cfg.n * cfg.k * sizeof(int8_t);
    size_t szCp  = (size_t)cfg.m * cfg.n * sizeof(int32_t);
    size_t szTr  = (size_t)n_tiles * TRANSCRIPT_U32 * sizeof(uint32_t);
    size_t szFnd = (size_t)n_tiles * sizeof(int32_t);

    int8_t  *d_Ap, *d_Bp;
    int32_t *d_Cp, *d_found;
    uint32_t *d_transcripts;

    cudaMalloc(&d_Ap, szAp);
    cudaMalloc(&d_Bp, szBp);
    cudaMalloc(&d_Cp, szCp);
    cudaMalloc(&d_transcripts, szTr);
    cudaMalloc(&d_found, szFnd);

    cudaMemcpy(d_Ap, Ap,    szAp, cudaMemcpyHostToDevice);
    cudaMemcpy(d_Bp, Bp_T,  szBp, cudaMemcpyHostToDevice);
    cudaMemset(d_found, -1, szFnd);

    // Запуск ядра
    dim3 block(cfg.hash_tile_w, cfg.hash_tile_h);
    dim3 grid(n_tiles_i, n_tiles_j);
    noisy_gemm_kernel<<<grid, block>>>(
        d_Ap, d_Bp, d_Cp, d_transcripts, d_found, cfg
    );
    cudaDeviceSynchronize();

    // Копируем transcripts на host для проверки BLAKE3
    uint32_t* h_transcripts = (uint32_t*)malloc(szTr);
    cudaMemcpy(h_transcripts, d_transcripts, szTr, cudaMemcpyDeviceToHost);

    // Проверяем каждый тайл
    result->found = 0;
    uint8_t digest[32];
    for (int t = 0; t < n_tiles && !result->found; t++) {
        uint8_t transcript_bytes[64];
        // Упаковываем uint32 → bytes LE
        for (int ii = 0; ii < TRANSCRIPT_U32; ii++) {
            uint32_t v = h_transcripts[t * TRANSCRIPT_U32 + ii];
            transcript_bytes[ii * 4 + 0] = (v >>  0) & 0xFF;
            transcript_bytes[ii * 4 + 1] = (v >>  8) & 0xFF;
            transcript_bytes[ii * 4 + 2] = (v >> 16) & 0xFF;
            transcript_bytes[ii * 4 + 3] = (v >> 24) & 0xFF;
        }
        blake3_keyed(sA, transcript_bytes, digest);

        // Сравниваем с target (uint256 little-endian)
        // Простая проверка: сравниваем первые 16 байт (достаточно для difficulty<=128 бит)
        uint64_t d_lo, d_hi;
        memcpy(&d_lo, digest,     8);
        memcpy(&d_hi, digest + 8, 8);

        if (d_lo <= target_lo && d_hi <= target_hi) {
            result->found  = 1;
            result->tile_i = (t / n_tiles_j) * cfg.hash_tile_h;
            result->tile_j = (t % n_tiles_j) * cfg.hash_tile_w;
            memcpy(result->transcript, h_transcripts + t * TRANSCRIPT_U32,
                   TRANSCRIPT_U32 * sizeof(uint32_t));
        }
    }

    free(h_transcripts);
    cudaFree(d_Ap);
    cudaFree(d_Bp);
    cudaFree(d_Cp);
    cudaFree(d_transcripts);
    cudaFree(d_found);

    return result->found;
}
