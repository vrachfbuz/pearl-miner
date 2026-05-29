#!/bin/bash
echo "=== Скачиваем glibc 2.35 (Ubuntu 22.04 Jammy) ==="
mkdir -p /tmp/glibc35
cd /tmp/glibc35

# Прямые ссылки — пробуем по очереди пока не скачается
DEB=""
for VER in "2.35-0ubuntu3.8" "2.35-0ubuntu3.7" "2.35-0ubuntu3.6" "2.35-0ubuntu3.4" "2.35-0ubuntu3.1" "2.35-0ubuntu3"; do
    URL="http://archive.ubuntu.com/ubuntu/pool/main/g/glibc/libc6_${VER}_amd64.deb"
    echo "Пробую: $URL"
    if wget -q --spider "$URL" 2>/dev/null; then
        echo "Нашёл: $URL"
        wget -q "$URL" -O libc6.deb && DEB="libc6.deb" && break
    fi
done

if [ -z "$DEB" ]; then
    echo "ОШИБКА: не удалось скачать libc6. Проверь интернет."
    exit 1
fi

echo "Скачано: $(du -sh libc6.deb | cut -f1)"
echo "--- Распаковываем ---"
dpkg-deb -x libc6.deb .

echo "--- Файлы glibc ---"
ls lib/x86_64-linux-gnu/ | grep -E "libc|ld-linux|libpthread|librt|libdl"

echo ""
echo "=== Проверяем lpminer --help ==="
/tmp/glibc35/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 \
  --library-path /tmp/glibc35/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu:/usr/local/cuda/lib64 \
  /tmp/lpminer/lpminer --help 2>&1 | head -40

echo "=== Готово ==="
