#!/bin/bash
# Сборка Pearl CUDA майнера на HiveOS (sm_75)
set -e
export PATH="/usr/local/cuda-12.4/bin:$PATH"

SRCDIR=/tmp/pearl_src
mkdir -p "$SRCDIR"
cd "$SRCDIR"

BASE="https://raw.githubusercontent.com/vrachfbuz/pearl-miner/master"

echo "=== Скачиваем исходники ==="
wget -q "$BASE/kernel/pearl_mine.cu"    -O pearl_mine.cu
wget -q "$BASE/kernel/blake3_host.c"   -O blake3_host.c

# BLAKE3 (официальный C-референс)
mkdir -p b3
wget -q "https://raw.githubusercontent.com/BLAKE3-team/BLAKE3/master/c/blake3.c"          -O b3/blake3.c
wget -q "https://raw.githubusercontent.com/BLAKE3-team/BLAKE3/master/c/blake3.h"          -O b3/blake3.h
wget -q "https://raw.githubusercontent.com/BLAKE3-team/BLAKE3/master/c/blake3_dispatch.c" -O b3/blake3_dispatch.c
wget -q "https://raw.githubusercontent.com/BLAKE3-team/BLAKE3/master/c/blake3_portable.c" -O b3/blake3_portable.c
wget -q "https://raw.githubusercontent.com/BLAKE3-team/BLAKE3/master/c/blake3_impl.h"     -O b3/blake3_impl.h

echo "=== Компилируем BLAKE3 (CPU) ==="
gcc -O2 -fPIC -c b3/blake3.c          -Ib3 -o b3/blake3.o
gcc -O2 -fPIC -c b3/blake3_dispatch.c -Ib3 -o b3/blake3_dispatch.o
gcc -O2 -fPIC -c b3/blake3_portable.c -Ib3 -o b3/blake3_portable.o
gcc -O2 -fPIC -c blake3_host.c        -Ib3 -o blake3_host.o

echo "=== Компилируем CUDA ядро (sm_75) ==="
nvcc -arch=sm_75 -O3 -Xcompiler -fPIC \
     -I b3 \
     -c pearl_mine.cu -o pearl_mine.o

echo "=== Линкуем pearl_miner ==="
nvcc -arch=sm_75 -O3 \
     pearl_mine.o blake3_host.o \
     b3/blake3.o b3/blake3_dispatch.o b3/blake3_portable.o \
     -lcudart -lpthread \
     -o /tmp/pearl_miner

chmod +x /tmp/pearl_miner
echo ""
echo "=== Готово: /tmp/pearl_miner ==="
/tmp/pearl_miner --help 2>&1 | head -10 || true
