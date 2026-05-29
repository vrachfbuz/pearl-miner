#!/bin/bash
LDSO=/tmp/glibc35/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2
LIBPATH=/tmp/glibc35/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu:/usr/local/cuda/lib64:/usr/lib/nvidia

WALLET="prl1p6kzqzms2yn06489nj66ddg4xy6fnpylcr5gndsce7v4def6sa32qvgq38n.CMP400"
POOL="stratum+tcp://pearl.baikalmine.com:2010"

echo "=== Шаг 1: pearl-verify (проверка CUDA, одна попытка с max target) ==="
"$LDSO" --library-path "$LIBPATH" /tmp/lpminer/lpminer \
  --pearl-verify --device 0 2>&1 | head -30

echo ""
echo "=== Шаг 2: pearl-share-dump (дамп реальной шары) ==="
mkdir -p /tmp/dump
"$LDSO" --library-path "$LIBPATH" /tmp/lpminer/lpminer \
  --pearl-share-dump /tmp/dump \
  --pool "$POOL" \
  --wallet "$WALLET" \
  --device 0 2>&1

echo ""
echo "=== Файлы дампа ==="
ls -la /tmp/dump/ 2>/dev/null || echo "Папка пуста"

for f in /tmp/dump/*.bin; do
  [ -f "$f" ] || continue
  echo "--- $f ---"
  xxd "$f" | head -20
done
