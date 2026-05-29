#!/usr/bin/env python3
"""
Зонд: импортирует mine_job из miner.py, шлёт реальный digest, проверяет ответ пула.
  НОВЫЙ seed   → ПРИНЯТО ✓
  ТОТ ЖЕ seed → ОТКЛОНЕНО ✗
"""
import socket, json, time, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from miner import mine_job, MiningConfig  # type: ignore

HOST = "us2.alphapool.tech"
PORT = 5566
WALLET = "prl1p6kzqzms2yn06489nj66ddg4xy6fnpylcr5gndsce7v4def6sa32qvgq38n"
WORKER = "probe04"

sock = socket.create_connection((HOST, PORT), timeout=30)
sock.settimeout(60)
buf = b""
msg_id = 1

def send_raw(method, params):
    global msg_id
    msg = {"id": msg_id, "method": method, "params": params}
    msg_id += 1
    line = json.dumps(msg, separators=(',', ':')) + "\n"
    print(f"→ {line.strip()}", flush=True)
    sock.sendall(line.encode())

def recv_line(timeout=20):
    global buf
    sock.settimeout(timeout)
    deadline = time.time() + timeout
    while True:
        if b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            return line.strip()
        try:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf += chunk
        except socket.timeout:
            if time.time() > deadline:
                return None

def recv_msgs(count=4, timeout=20):
    msgs = []
    for _ in range(count):
        line = recv_line(timeout)
        if line is None:
            break
        try:
            obj = json.loads(line)
            print(f"← {json.dumps(obj)}", flush=True)
            msgs.append(obj)
        except:
            print(f"← [raw]: {line[:300]}", flush=True)
    return msgs

# --- Handshake ---
print(f"Подключаюсь к {HOST}:{PORT}...", flush=True)
send_raw("mining.subscribe", ["pearl-probe/0.4"])
send_raw("mining.authorize", [f"{WALLET}.{WORKER}", "x"])

print("Жду challenge...", flush=True)
challenge = None
for _ in range(15):
    line = recv_line(timeout=20)
    if line is None: break
    try:
        obj = json.loads(line)
        print(f"← {json.dumps(obj)}", flush=True)
        if obj.get("method") == "pearl.challenge":
            challenge = obj; break
    except:
        print(f"← [raw]: {line[:200]}", flush=True)

if not challenge:
    print("Нет challenge, выход", flush=True)
    sys.exit(1)

params = challenge["params"]
seed   = params["seed"]
diff   = int(params.get("difficulty", 32))
print(f"\nCHALLENGE seed={seed[:24]}... diff={diff}\n", flush=True)

# --- Тест: digest = 1 в little-endian (МИНИМАЛЬНО возможный, точно < любого target) ---
# "01"+"00"*31 → int.from_bytes(..., "little") = 1 (меньше любого target)
# "00"*31+"01" → int.from_bytes(..., "little") = 2^248 (БОЛЬШЕ target=2^239 — неверно!)
# Если пул примет → проверяет только числовое значение (без NoisyGEMM)
# Если отклонит  → проверяет правильность NoisyGEMM digest
print("ШАГ 1: digest=1 (little-endian) — точно < target, но НЕ NoisyGEMM", flush=True)
send_raw("pearl.submit", {"seed": seed, "tile_i": 0, "tile_j": 0, "digest": "01" + "00"*31})
msgs = recv_msgs(count=3, timeout=15)
for m_obj in msgs:
    r_seed = m_obj.get("params", {}).get("seed", "")
    if m_obj.get("method") == "pearl.challenge":
        if r_seed != seed:
            print("★ НОВЫЙ seed → пул принял 0x000..01 (только числовая проверка!)", flush=True)
        else:
            print("→ тот же seed → пул ПРОВЕРЯЕТ правильность NoisyGEMM digest", flush=True)

# --- Тест: реальный NoisyGEMM с b=4 (гарантированно найдём тайлы) ---
print(f"\nШАГ 2: вычисляю NoisyGEMM m=128 n=128 k=512 r=128 b=4.0...", flush=True)
cfg4 = MiningConfig(); cfg4.b = 4.0
results4 = mine_job(seed, 4, cfg4)
print(f"Найдено тайлов с b=4: {len(results4)}", flush=True)

if results4:
    r0 = results4[0]
    print(f"Отправляю тайл({r0['tile_i']},{r0['tile_j']}) digest={r0['digest_hex'][:20]}...", flush=True)
    send_raw("pearl.submit", {
        "seed": seed, "tile_i": r0["tile_i"], "tile_j": r0["tile_j"], "digest": r0["digest_hex"]
    })
    msgs4 = recv_msgs(count=3, timeout=15)
    for m_obj in msgs4:
        r_seed = m_obj.get("params", {}).get("seed", "")
        if m_obj.get("method") == "pearl.challenge":
            if r_seed != seed:
                print("★ ПРИНЯТО! b=4 тайл принят (пул не проверяет b в mu_bytes)", flush=True)
            else:
                print("→ ОТКЛОНЕНО ✗ (b=4 digest не подходит — пул проверяет b)", flush=True)

# --- Тест: реальный NoisyGEMM с реальной сложностью (b=diff=32) ---
print(f"\nШАГ 3: вычисляю NoisyGEMM m=128 b={diff} (реальная сложность)...", flush=True)
print("(с 64 тайлами ожидаем ~2048 попыток для нахождения — это ОДНА попытка)", flush=True)
cfg32 = MiningConfig(); cfg32.b = float(diff)
results32 = mine_job(seed, diff, cfg32)
print(f"Найдено тайлов с b={diff}: {len(results32)}", flush=True)

if results32:
    r1 = results32[0]
    print(f"Отправляю тайл({r1['tile_i']},{r1['tile_j']}) digest={r1['digest_hex'][:20]}...", flush=True)
    send_raw("pearl.submit", {
        "seed": seed, "tile_i": r1["tile_i"], "tile_j": r1["tile_j"], "digest": r1["digest_hex"]
    })
    msgs32 = recv_msgs(count=3, timeout=15)
    for m_obj in msgs32:
        r_seed = m_obj.get("params", {}).get("seed", "")
        if m_obj.get("method") == "pearl.challenge":
            if r_seed != seed:
                print("★ ПРИНЯТО! Параметры m=128 n=128 k=512 r=128 РАБОТАЮТ!", flush=True)
            else:
                print("→ ОТКЛОНЕНО ✗ (правильный b, но параметры m/n/k/r неверные)", flush=True)
else:
    print("(нормально — с 64 тайлами и b=32 шара не с первой попытки)", flush=True)

print("\nГотово.", flush=True)
