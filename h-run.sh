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

# Устанавливаем pip если нет, затем пакеты
if ! python3 -c "import blake3, numpy" 2>/dev/null; then
    echo "Устанавливаю зависимости..."
    if ! command -v pip3 &>/dev/null; then
        curl -sS https://bootstrap.pypa.io/get-pip.py | python3
    fi
    pip3 install -q blake3 numpy
fi

exec python3 "${MINER_DIR}/miner.py" \
    --pool "${POOL}" \
    --address "${WALLET}" \
    --worker "${WORKER}" \
    --password "${PASS}"
