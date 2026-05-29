#!/bin/bash
set -e
echo "=== Скачиваем glibc 2.35 (Ubuntu 22.04 Jammy) ==="
mkdir -p /tmp/glibc35
cd /tmp/glibc35

echo "--- Ищем URL пакета libc6 ---"
URL=$(wget -qO- "http://archive.ubuntu.com/ubuntu/dists/jammy/main/binary-amd64/Packages.gz" \
  | zcat \
  | grep -A10 "^Package: libc6$" \
  | grep "^Filename:" \
  | head -1 \
  | awk '{print "http://archive.ubuntu.com/ubuntu/" $2}')
echo "URL: $URL"

echo "--- Скачиваем пакет ---"
wget -q "$URL" -O libc6.deb
echo "Скачано: $(du -sh libc6.deb | cut -f1)"

echo "--- Распаковываем ---"
dpkg-deb -x libc6.deb .

echo "--- Файлы glibc ---"
ls lib/x86_64-linux-gnu/ | grep -E "libc|ld-linux|libpthread|librt|libdl"

echo ""
echo "=== Проверяем lpminer --help ==="
/tmp/glibc35/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 \
  --library-path /tmp/glibc35/lib/x86_64-linux-gnu \
  /tmp/lpminer/lpminer --help 2>&1 | head -40

echo ""
echo "=== Готово ==="
