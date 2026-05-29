#!/bin/bash
echo "=== GPU compute capability ==="
nvidia-smi --query-gpu=index,name,compute_cap --format=csv,noheader 2>/dev/null | head -5

echo ""
echo "=== CUDA driver version ==="
nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1

echo ""
echo "=== Архитектуры в бинарнике lpminer ==="
strings /tmp/lpminer/lpminer | grep -oE "sm_[0-9]+" | sort -u
echo "---"
strings /tmp/lpminer/lpminer | grep -oE "compute_[0-9]+" | sort -u

echo ""
echo "=== NEEDED библиотеки lpminer ==="
objdump -p /tmp/lpminer/lpminer 2>/dev/null | grep NEEDED

echo ""
echo "=== Поиск libcuda/libcudart ==="
ldconfig -p 2>/dev/null | grep -E "libcuda|libcudart"
find /usr/local /usr/lib -name "libcudart.so*" 2>/dev/null | head -5
