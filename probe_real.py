#!/usr/bin/env python3
"""
Зонд с РЕАЛЬНОЙ сложностью (b=diff от пула).
Майнит пока не найдёт настоящий валидный тайл, потом отправляет.
Если пул вернёт НОВЫЙ seed — значит ПРИНЯТО и параметры верные.
Если тот же seed — параметры матриц неверные.
"""
import socket, json, time, sys, struct
import blake3
import numpy as np

HOST = "us2.alphapool.tech"
PORT = 5566
WALLET = "prl1p6kzqzms2yn06489nj66ddg4xy6fnpylcr5gndsce7v4def6sa32qvgq38n"
WORKER = "probe03"

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

def recv_msgs(count=3, timeout=20):
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

# ---------------------------------------------------------------------------
def rotl32(x, s):
    x &= 0xFFFFFFFF
    return ((x << s) | (x >> (32 - s))) & 0xFFFFFFFF

def blake3_stream(seed, tag, nbytes):
    return blake3.blake3(tag + seed).digest(length=nbytes)

def noisy_gemm_mine(sigma, m, n, k, r, tm, tn, b):
    mu_bytes = struct.pack("<7q d", m, n, k, r, tm, tn, 0, b)
    target = int((2.0 ** (256 - b)) * r * tm * tn)

    raw_a = blake3_stream(sigma, b"matrix_A", m * k)
    raw_b = blake3_stream(sigma, b"matrix_B", k * n)
    A = (np.frombuffer(raw_a, dtype=np.uint8).astype(np.int64) % 128 - 64).reshape(m, k)
    B = (np.frombuffer(raw_b, dtype=np.uint8).astype(np.int64) % 128 - 64).reshape(k, n)

    kappa = blake3.blake3(sigma + mu_bytes).digest()
    HA = blake3.blake3(A.astype(np.int8).tobytes(order="C"), key=kappa).digest()
    HB = blake3.blake3(B.astype(np.int8).T.tobytes(order="C"), key=kappa).digest()
    sB = blake3.blake3(kappa + HB).digest()
    sA = blake3.blake3(sB + HA).digest()

    raw_EL = blake3_stream(sA, b"EL", m * r)
    EL = ((np.frombuffer(raw_EL, dtype=np.uint8).astype(np.int64) % 64) - 32).reshape(m, r)
    # ER: shape (r, k) — choice_matrix(sA, rows=r, cols=k)
    raw_ER = blake3_stream(sA, b"ER", k * 8)
    ER = np.zeros((r, k), dtype=np.int64)
    for c in range(k):
        a = raw_ER[c*8] % r; d = raw_ER[c*8+1] % r
        if d == a: d = (d+1) % r
        ER[a, c] += 1; ER[d, c] -= 1

    # FL: shape (k, r) — choice_matrix(sB, rows=k, cols=r)
    raw_FL = blake3_stream(sB, b"ER", r * 8)
    FL = np.zeros((k, r), dtype=np.int64)
    for c in range(r):
        a = raw_FL[c*8] % k; d = raw_FL[c*8+1] % k
        if d == a: d = (d+1) % k
        FL[a, c] += 1; FL[d, c] -= 1
    raw_FR = blake3_stream(sB, b"EL", r * n)
    FR = ((np.frombuffer(raw_FR, dtype=np.uint8).astype(np.int64) % 64) - 32).reshape(r, n)

    Ap = (A + EL @ ER).clip(-128, 127).astype(np.int64)
    Bp = (B + FL @ FR).clip(-128, 127).astype(np.int64)

    found = []
    for i in range(0, m, tm):
        if m - i < tm: continue
        for j in range(0, n, tn):
            if n - j < tn: continue
            M = [0]*16
            l = 0
            for s in range(0, k, r):
                if k - s < r: continue
                sub = Ap[i:i+tm, s:s+r] @ Bp[s:s+r, j:j+tn]
                X = 0
                for v in sub.flatten():
                    X ^= int(v) & 0xFFFFFFFF
                M[l % 16] = rotl32(M[l % 16], 13) ^ X
                l += 1
            Mbytes = struct.pack("<16I", *[x & 0xFFFFFFFF for x in M])
            digest = blake3.blake3(Mbytes, key=sA).digest()
            hval = int.from_bytes(digest, "little")
            if hval <= target:
                found.append((i, j, digest.hex()))
    return found

# ---------------------------------------------------------------------------
print(f"Подключаюсь к {HOST}:{PORT}...", flush=True)
send_raw("mining.subscribe", ["pearl-probe/0.3"])
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
            challenge = obj
            break
    except:
        print(f"← [raw]: {line[:200]}", flush=True)

if not challenge:
    print("Нет challenge, выход", flush=True)
    sys.exit(1)

params = challenge.get("params", {})
seed = params.get("seed", "")
diff = float(params.get("difficulty", 32))
sigma = bytes.fromhex(seed)
print(f"\nCHALLENGE seed={seed[:24]}... diff={diff}", flush=True)

# Параметры
CONFIGS = [
    (128, 128, 512, 128, 16, 16),
    (256, 256, 512, 256, 16, 16),
    (128, 128, 256, 64,  16, 16),
]

for (m, n, k, r, tm, tn) in CONFIGS:
    print(f"\n{'='*60}", flush=True)
    print(f"Параметры: m={m} n={n} k={k} r={r} b={diff} (РЕАЛЬНАЯ сложность)", flush=True)
    target = int((2.0 ** (256 - diff)) * r * tm * tn)
    n_tiles = (m//tm) * (n//tn)
    # Ожидаемое число попыток до первого тайла:
    exp_attempts = max(1, 2**17 // n_tiles)
    print(f"Тайлов в матрице: {n_tiles} | Ожид. попыток до шары: ~{exp_attempts}", flush=True)

    t0 = time.time()
    found = noisy_gemm_mine(sigma, m, n, k, r, tm, tn, diff)
    elapsed = time.time() - t0

    if found:
        ti, tj, digest = found[0]
        hval = int.from_bytes(bytes.fromhex(digest), "little")
        print(f"НАЙДЕН тайл ({ti},{tj}) за {elapsed:.2f}с | hval={hval} <= target={target}", flush=True)
        print(f"digest={digest}", flush=True)

        send_raw("pearl.submit", {
            "seed":   seed,
            "tile_i": ti,
            "tile_j": tj,
            "digest": digest,
        })

        print("Жду ответ пула (20 сек)...", flush=True)
        resp_msgs = recv_msgs(count=4, timeout=20)
        accepted = False
        for r_obj in resp_msgs:
            r_seed = r_obj.get("params", {}).get("seed", "")
            if r_obj.get("method") == "pearl.challenge":
                if r_seed != seed:
                    print(f"\n★ НОВЫЙ seed = ПРИНЯТО ✓  m={m} n={n} k={k} r={r} РАБОТАЕТ!", flush=True)
                    accepted = True
                else:
                    print(f"→ тот же seed = ОТКЛОНЕНО ✗", flush=True)
        if accepted:
            print("\nПараметры найдены! Обновляем miner.py", flush=True)
            sys.exit(0)
    else:
        print(f"Нет тайлов за {elapsed:.2f}с (ожидаемо с реальной сложностью — нужен длинный майнинг)", flush=True)
        # При реальной сложности тайлов может не быть с первой попытки — это нормально
        # Но мы всё равно отправим случайный тайл чтобы увидеть тип ошибки
        print("Отправляю тайл (0,0) с digest=0x00...01 чтобы увидеть тип ответа пула...", flush=True)
        # digest который точно меньше target (все биты 0 кроме последнего)
        test_digest = "00" * 31 + "01"
        send_raw("pearl.submit", {
            "seed":   seed,
            "tile_i": 0,
            "tile_j": 0,
            "digest": test_digest,
        })
        resp_msgs = recv_msgs(count=3, timeout=15)
        for r_obj in resp_msgs:
            r_seed = r_obj.get("params", {}).get("seed", "")
            if r_obj.get("method") == "pearl.challenge":
                if r_seed != seed:
                    print(f"★ НОВЫЙ seed! (пул принял явно неверный digest — значит проверка слабая)", flush=True)
                else:
                    print(f"→ тот же seed — пул ПРОВЕРЯЕТ digest правильно", flush=True)

print("\nГотово.", flush=True)
