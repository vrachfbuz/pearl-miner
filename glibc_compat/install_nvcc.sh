#!/bin/bash
set -e
echo "=== Добавляем репозиторий NVIDIA CUDA ==="
wget -q "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2004/x86_64/cuda-keyring_1.1-1_all.deb" \
     -O /tmp/cuda-keyring.deb
dpkg -i /tmp/cuda-keyring.deb
apt-get update -q

echo "=== Ставим cuda-nvcc-12-4 (nvcc + заголовки) ==="
apt-get install -y cuda-nvcc-12-4 cuda-cudart-dev-12-4

echo "=== Добавляем nvcc в PATH ==="
export PATH="/usr/local/cuda-12.4/bin:$PATH"
echo 'export PATH="/usr/local/cuda-12.4/bin:$PATH"' >> /etc/profile.d/cuda.sh

echo "=== Проверка ==="
nvcc --version
echo "Готово!"
