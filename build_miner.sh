#!/bin/bash
# Сборка Pearl CUDA майнера на HiveOS (sm_75)
set -e
export PATH="/usr/local/cuda-12.4/bin:$PATH"

SRCDIR=/tmp/pearl_src
rm -rf "$SRCDIR"
mkdir -p "$SRCDIR"
cd "$SRCDIR"

BASE="https://raw.githubusercontent.com/vrachfbuz/pearl-miner/main"
B3="https://raw.githubusercontent.com/BLAKE3-team/BLAKE3/1.5.4/c"

echo "=== Скачиваем исходники майнера ==="
wget -q "$BASE/kernel/pearl_mine.cu"  -O pearl_mine.cu
wget -q "$BASE/kernel/blake3_host.c"  -O blake3_host.c

echo "=== Скачиваем BLAKE3 (фиксированная версия 1.5.4) ==="
mkdir -p b3
for f in blake3.c blake3.h blake3_dispatch.c blake3_portable.c blake3_impl.h \
          blake3_sse2.c blake3_sse41.c blake3_avx2.c blake3_avx512.c; do
    wget -q "$B3/$f" -O "b3/$f"
done

echo "=== Компилируем BLAKE3 (CPU, все SIMD) ==="
NOSIMD="-DBLAKE3_NO_AVX512 -DBLAKE3_NO_AVX2 -DBLAKE3_NO_SSE41 -DBLAKE3_NO_SSE2"
gcc -O2 -fPIC -c b3/blake3.c          -Ib3 $NOSIMD -o b3/blake3.o
gcc -O2 -fPIC -c b3/blake3_dispatch.c -Ib3 $NOSIMD -o b3/blake3_dispatch.o
gcc -O2 -fPIC -c b3/blake3_portable.c -Ib3          -o b3/blake3_portable.o
gcc -O2 -fPIC -c blake3_host.c        -Ib3          -o blake3_host.o
echo "BLAKE3 OK"

echo "=== Компилируем CUDA ядро (sm_75) ==="
nvcc -arch=sm_75 -O3 -Xcompiler "-fPIC -fopenmp" -Ib3 \
     -c pearl_mine.cu -o pearl_mine.o 2>&1 | grep -v "^$" | grep -v "warning" || true
echo "CUDA OK"

echo "=== Линкуем pearl_miner ==="
nvcc -arch=sm_75 -O3 \
     pearl_mine.o blake3_host.o \
     b3/blake3.o b3/blake3_dispatch.o b3/blake3_portable.o \
     -lcudart -lpthread -lm -lgomp \
     -o /tmp/pearl_miner

chmod +x /tmp/pearl_miner
echo ""
echo "=== Готово: /tmp/pearl_miner ==="
/tmp/pearl_miner --help
