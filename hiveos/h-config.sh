#!/usr/bin/env bash
# Генерация конфига из переменных HiveOS Flight Sheet
# Вызывается HiveOS перед запуском майнера

[[ -z $MINER_FORK ]] && MINER_FORK="pearl-miner"
[[ -z $MINER_NAME ]] && MINER_NAME="pearl-miner"

# Читаем переменные flight sheet
POOL_URL="${CUSTOM_URL:-us2.alphapool.tech:5566}"
WALLET="${CUSTOM_TEMPLATE:-${CUSTOM_WALLET}}"
WORKER="${WORKER_NAME:-rig01}"
PASS="${CUSTOM_PASS:-x;d=256}"

# Убираем stratum+tcp:// если есть
POOL_URL="${POOL_URL#stratum+tcp://}"

# Сохраняем в конфиг
mkdir -p /hive/miners/${MINER_FORK}
cat > /hive/miners/${MINER_FORK}/miner.conf <<EOF
PEARL_POOL="${POOL_URL}"
PEARL_WALLET="${WALLET}"
PEARL_WORKER="${WORKER}"
PEARL_PASS="${PASS}"
EOF

echo "Конфиг сохранён: pool=${POOL_URL} wallet=${WALLET:0:12}... worker=${WORKER}"
