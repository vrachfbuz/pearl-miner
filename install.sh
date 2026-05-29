#!/bin/bash
# install.sh — установка Pearl Miner на HiveOS/Ubuntu
# Запуск: bash install.sh

set -e
echo "=== Pearl Miner — установка ==="

# Python-зависимости (только это нужно для CPU-прототипа)
echo "[1/2] Устанавливаю Python-пакеты..."
pip3 install blake3 numpy 2>/dev/null || python3 -m pip install blake3 numpy

# Проверяем GPU
echo "[2/2] Проверяю GPU..."
if command -v nvidia-smi &>/dev/null; then
    nvidia-smi --query-gpu=index,name,compute_cap --format=csv,noheader
else
    echo "  nvidia-smi не найден (GPU-ускорение недоступно, будет работать CPU)"
fi

echo ""
echo "=== Готово! Запуск: ==="
echo "  python3 miner.py --address ВАШ_АДРЕС --worker rig01"
echo ""
echo "Пример с BaikalMine:"
echo "  python3 miner.py --address ВАШ_АДРЕС --pool pearl.baikalmine.com:2010 --password x"
echo ""
echo "Пример с AlphaPool:"
echo "  python3 miner.py --address ВАШ_АДРЕС --pool us2.alphapool.tech:5566 --password 'x;d=256'"
