#!/usr/bin/env bash
# Статистика для HiveOS dashboard
# HiveOS ждёт JSON: {"hs":[хешрейт по картам], "hs_units":"hs", "temp":[temp], "fan":[fan%], "uptime":сек, "ar":[принято,отклонено]}

MINER_LOG="/var/log/miner/pearl-miner/pearl-miner.log"

# Читаем последние строки лога
if [[ -f "$MINER_LOG" ]]; then
    ACCEPTED=$(grep -c "← OK" "$MINER_LOG" 2>/dev/null || echo 0)
    REJECTED=$(grep -c "← ОШИБКА" "$MINER_LOG" 2>/dev/null || echo 0)
else
    ACCEPTED=0
    REJECTED=0
fi

# Температуры и обороты с nvidia-smi
GPU_COUNT=$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | wc -l)
GPU_COUNT=${GPU_COUNT:-0}

TEMP_ARR=""
FAN_ARR=""
HS_ARR=""

if [[ $GPU_COUNT -gt 0 ]]; then
    while IFS=',' read -r temp fan; do
        TEMP_ARR="${TEMP_ARR}${temp},"
        FAN_ARR="${FAN_ARR}${fan},"
        HS_ARR="${HS_ARR}0,"   # хешрейт: 0 пока CPU режим
    done < <(nvidia-smi --query-gpu=temperature.gpu,fan.speed --format=csv,noheader,nounits 2>/dev/null)
    # Убираем последнюю запятую
    TEMP_ARR="${TEMP_ARR%,}"
    FAN_ARR="${FAN_ARR%,}"
    HS_ARR="${HS_ARR%,}"
else
    TEMP_ARR="0"
    FAN_ARR="0"
    HS_ARR="0"
fi

# Аптайм процесса майнера
MINER_PID=$(pgrep -f "miner.py" | head -1)
if [[ -n "$MINER_PID" ]]; then
    UPTIME=$(( $(date +%s) - $(stat -c %Y /proc/$MINER_PID 2>/dev/null || echo $(date +%s)) ))
else
    UPTIME=0
fi

echo "{\"hs\":[${HS_ARR}],\"hs_units\":\"hs\",\"temp\":[${TEMP_ARR}],\"fan\":[${FAN_ARR}],\"uptime\":${UPTIME},\"ar\":[${ACCEPTED},${REJECTED}]}"
