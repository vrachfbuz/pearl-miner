#!/usr/bin/env python3
"""
Сырой зонд AlphaPool — логирует все байты и пробует разные форматы pearl.submit
Запуск: python3 probe_raw.py
"""
import socket, json, time, sys, struct

HOST = "us2.alphapool.tech"
PORT = 5566
WALLET = "prl1p6kzqzms2yn06489nj66ddg4xy6fnpylcr5gndsce7v4def6sa32qvgq38n"
WORKER = "probe01"

sock = socket.create_connection((HOST, PORT), timeout=30)
sock.settimeout(60)
buf = b""
msg_id = 1

def send_raw(method, params):
    global msg_id
    msg = {"id": msg_id, "method": method, "params": params}
    msg_id += 1
    line = json.dumps(msg, separators=(',', ':')) + "\n"
    print(f"\n→ [{msg_id-1}] {line.strip()}", flush=True)
    sock.sendall(line.encode())

def recv_line(timeout=15):
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
            print(f"  [RAW +{len(chunk)}b]: {chunk[:200]}", flush=True)
            buf += chunk
        except socket.timeout:
            if time.time() > deadline:
                return None

def recv_until_challenge(max_msgs=20):
    """Читаем пока не придёт pearl.challenge"""
    for _ in range(max_msgs):
        line = recv_line(timeout=20)
        if line is None:
            print("  [таймаут]", flush=True)
            return None
        try:
            obj = json.loads(line)
            print(f"← {json.dumps(obj)}", flush=True)
            if obj.get("method") == "pearl.challenge":
                return obj
        except:
            print(f"← [не JSON]: {line[:200]}", flush=True)
    return None

def recv_response(timeout=8):
    """Ждём ответ на submit"""
    line = recv_line(timeout=timeout)
    if line is None:
        print("  [нет ответа за 8 сек]", flush=True)
        return None
    try:
        obj = json.loads(line)
        print(f"← ОТВЕТ: {json.dumps(obj)}", flush=True)
        return obj
    except:
        print(f"← [не JSON]: {line[:200]}", flush=True)
        return None

print("=== Pearl AlphaPool raw probe ===", flush=True)
print(f"Подключаюсь к {HOST}:{PORT}...", flush=True)

# --- Handshake ---
send_raw("mining.subscribe", ["pearl-probe/0.1"])
send_raw("mining.authorize", [f"{WALLET}.{WORKER}", "x"])

print("\nЖду challenge...", flush=True)
ch = recv_until_challenge()
if ch is None:
    print("Нет challenge, выход", flush=True)
    sys.exit(1)

params = ch.get("params", {})
seed   = params.get("seed", "")
diff   = params.get("difficulty", 32)
print(f"\n=== CHALLENGE: seed={seed[:24]}... diff={diff} ===", flush=True)

FAKE_DIGEST = "ab" * 32   # заведомо неверный digest — нам важен тип ответа

# --- Пробуем разные форматы ---

print("\n\n[ФОРМАТ 1] pearl.submit объект {seed, tile_i, tile_j, digest}", flush=True)
send_raw("pearl.submit", {
    "seed":   seed,
    "tile_i": 0,
    "tile_j": 0,
    "digest": FAKE_DIGEST,
})
recv_response()

print("\n[ФОРМАТ 2] pearl.submit объект {seed, i, j, proof}", flush=True)
send_raw("pearl.submit", {
    "seed":  seed,
    "i":     0,
    "j":     0,
    "proof": FAKE_DIGEST,
})
recv_response()

print("\n[ФОРМАТ 3] pearl.submit массив [seed, 0, 0, digest]", flush=True)
send_raw("pearl.submit", [seed, 0, 0, FAKE_DIGEST])
recv_response()

print("\n[ФОРМАТ 4] mining.submit массив [worker, seed, 0, 0, digest]", flush=True)
send_raw("mining.submit", [f"{WALLET}.{WORKER}", seed, "0", "0", FAKE_DIGEST])
recv_response()

print("\n[ФОРМАТ 5] pearl.submit объект {job_id, tile_i, tile_j, digest}", flush=True)
send_raw("pearl.submit", {
    "job_id": seed,
    "tile_i": 0,
    "tile_j": 0,
    "digest": FAKE_DIGEST,
})
recv_response()

print("\n[ФОРМАТ 6] pearl.submit объект {seed, nonce, digest}", flush=True)
send_raw("pearl.submit", {
    "seed":   seed,
    "nonce":  "00" * 8,
    "digest": FAKE_DIGEST,
})
recv_response()

print("\n\n=== Ждём ещё 10 сек (вдруг придёт ещё что-то) ===", flush=True)
for _ in range(5):
    r = recv_response(timeout=2)
    if r is None:
        break

print("\nГотово.", flush=True)
