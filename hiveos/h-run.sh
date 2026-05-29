#!/usr/bin/env bash
# Запуск Pearl Miner для HiveOS

MINER_DIR="$(dirname "$(readlink -f "$0")")"
CONF="/hive/miners/pearl-miner/miner.conf"

# Загружаем конфиг
[[ -f "$CONF" ]] && source "$CONF"

POOL="${PEARL_POOL:-us2.alphapool.tech:5566}"
WALLET="${PEARL_WALLET}"
WORKER="${PEARL_WORKER:-rig01}"
PASS="${PEARL_PASS:-x;d=256}"

if [[ -z "$WALLET" ]]; then
    echo "Ошибка: WALLET не задан в flight sheet"
    exit 1
fi

echo "=== Pearl Miner v0.1.0 ==="
echo "Pool:   ${POOL}"
echo "Wallet: ${WALLET:0:20}..."
echo "Worker: ${WORKER}"

# Установить зависимости если нет
python3 -c "import blake3, numpy" 2>/dev/null || pip3 install blake3 numpy -q

exec python3 "${MINER_DIR}/miner.py" \
    --pool "${POOL}" \
    --address "${WALLET}" \
    --worker "${WORKER}" \
    --password "${PASS}"
