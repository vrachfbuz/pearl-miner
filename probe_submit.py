#!/usr/bin/env python3
"""
Зонд: вычисляем настоящий NoisyGEMM digest и смотрим — пул принимает или нет.
Пробуем разные размеры матриц. Ответ пула:
  - НОВЫЙ seed   → ПРИНЯТО ✓
  - ТОТ ЖЕ seed → ОТКЛОНЕНО ✗ (параметры или вычисление неверные)
"""
import socket, json, time, sys, struct
import blake3
import numpy as np

HOST = "us2.alphapool.tech"
PORT = 5566
WALLET = "prl1p6kzqzms2yn06489nj66ddg4xy6fnpylcr5gndsce7v4def6sa32qvgq38n"
WORKER = "probe02"

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

def recv_msgs(count=5, timeout=12):
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
            print(f"← [raw]: {line[:200]}", flush=True)
    return msgs

# ---------------------------------------------------------------------------
# NoisyGEMM (копия из miner.py)
# ---------------------------------------------------------------------------
def rotl32(x, s):
    x &= 0xFFFFFFFF
    return ((x << s) | (x >> (32 - s))) & 0xFFFFFFFF

def blake3_stream(seed, tag, nbytes):
    return blake3.blake3(tag + seed).digest(length=nbytes)

def generate_ab(seed, m, n, k):
    raw_a = blake3_stream(seed, b"matrix_A", m * k)
    raw_b = blake3_stream(seed, b"matrix_B", k * n)
    A = (np.frombuffer(raw_a, dtype=np.uint8).astype(np.int64) % 128 - 64).reshape(m, k)
    B = (np.frombuffer(raw_b, dtype=np.uint8).astype(np.int64) % 128 - 64).reshape(k, n)
    return A, B

def commitment_hash(A, B, sigma, m, n, k, r, tm, tn, b, acc_type=0):
    mu_bytes = struct.pack("<7q d", m, n, k, r, tm, tn, acc_type, b)
    kappa = blake3.blake3(sigma + mu_bytes).digest()
    HA = blake3.blake3(A.astype(np.int8).tobytes(order="C"), key=kappa).digest()
    HB = blake3.blake3(B.astype(np.int8).T.tobytes(order="C"), key=kappa).digest()
    sB = blake3.blake3(kappa + HB).digest()
    sA = blake3.blake3(sB + HA).digest()
    return sA, sB, HA, HB

def uniform_matrix(seed, rows, cols):
    raw = blake3_stream(seed, b"EL", rows * cols)
    vals = np.frombuffer(raw, dtype=np.uint8).astype(np.int64)
    return ((vals % 64) - 32).reshape(rows, cols)

def choice_matrix_left(seed, rows, cols):
    raw = blake3_stream(seed, b"ER", cols * 8)
    M = np.zeros((rows, cols), dtype=np.int64)
    for c in range(cols):
        a = raw[c*8] % rows
        d = raw[c*8+1] % rows
        if d == a:
            d = (d+1) % rows
        M[a,c] += 1
        M[d,c] -= 1
    return M

def generate_noise(sA, sB, m, n, k, r):
    EL = uniform_matrix(sA, m, r)
    ER = choice_matrix_left(sA, r, k)
    FL = choice_matrix_left(sB, k, r)
    FR = uniform_matrix(sB, r, n)
    return EL, ER, FL, FR

def find_tiles(Ap, Bp, m, n, k, r, tm, tn, sA, b, max_tiles=64):
    target = int((2.0 ** (256 - b)) * r * tm * tn)
    found = []
    for i in range(0, m, tm):
        h = min(tm, m - i)
        for j in range(0, n, tn):
            w = min(tn, n - j)
            Cblk = Ap[i:i+h, :] @ Bp[:, j:j+w]
            if h == tm and w == tn:
                M = [0] * 16
                l = 0
                for s in range(0, k, r):
                    d = min(r, k-s)
                    sub = Ap[i:i+h, s:s+d] @ Bp[s:s+d, j:j+w]
                    X = 0
                    for v in sub.flatten():
                        X ^= int(v) & 0xFFFFFFFF
                    idx = l % 16
                    M[idx] = rotl32(M[idx], 13) ^ X
                    l += 1
                Mbytes = struct.pack("<16I", *[x & 0xFFFFFFFF for x in M])
                digest = blake3.blake3(Mbytes, key=sA).digest()
                hval = int.from_bytes(digest, "little")
                if hval <= target:
                    found.append((i, j, digest.hex()))
                    if len(found) >= max_tiles:
                        return found
    return found

def try_params(seed_hex, diff, m, n, k, r, tm=16, tn=16, b_override=None):
    b = float(b_override if b_override is not None else diff)
    sigma = bytes.fromhex(seed_hex)
    print(f"\n  Вычисляю m={m} n={n} k={k} r={r} b={b}...", flush=True)
    t0 = time.time()
    A, B = generate_ab(sigma, m, n, k)
    sA, sB, HA, HB = commitment_hash(A, B, sigma, m, n, k, r, tm, tn, b)
    EL, ER, FL, FR = generate_noise(sA, sB, m, n, k, r)
    E = EL @ ER
    F = FL @ FR
    Ap = (A + E).clip(-128, 127).astype(np.int8).astype(np.int64)
    Bp = (B + F).clip(-128, 127).astype(np.int8).astype(np.int64)
    tiles = find_tiles(Ap, Bp, m, n, k, r, tm, tn, sA, b)
    elapsed = time.time() - t0
    print(f"  Готово за {elapsed:.2f}с | найдено тайлов: {len(tiles)}", flush=True)
    return tiles

# ---------------------------------------------------------------------------
# Подключение и handshake
# ---------------------------------------------------------------------------
print(f"Подключаюсь к {HOST}:{PORT}...", flush=True)
send_raw("mining.subscribe", ["pearl-probe/0.2"])
send_raw("mining.authorize", [f"{WALLET}.{WORKER}", "x"])

print("\nЖду subscribe/authorize ответы и challenge...", flush=True)
challenge = None
for _ in range(15):
    line = recv_line(timeout=20)
    if line is None:
        break
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

params  = challenge.get("params", {})
seed    = params.get("seed", "")
diff    = params.get("difficulty", 32)
print(f"\n=== CHALLENGE seed={seed[:24]}... diff={diff} ===\n", flush=True)

# ---------------------------------------------------------------------------
# Тест 1: наши текущие маленькие параметры с b=4 (гарантированно найдём тайл)
# ---------------------------------------------------------------------------
print("=" * 60, flush=True)
print("ТЕСТ 1: m=128 n=128 k=512 r=128 b=4.0", flush=True)
tiles = try_params(seed, diff, m=128, n=128, k=512, r=128, b_override=4.0)
if tiles:
    ti, tj, digest = tiles[0]
    print(f"\n  Отправляю тайл ({ti},{tj}) digest={digest[:16]}...", flush=True)
    send_raw("pearl.submit", {
        "seed":   seed,
        "tile_i": ti,
        "tile_j": tj,
        "digest": digest,
    })
    print("  Жду ответ пула (15 сек)...", flush=True)
    resp_msgs = recv_msgs(count=3, timeout=15)
    for r_obj in resp_msgs:
        r_seed = r_obj.get("params", {}).get("seed", "")
        if r_obj.get("method") == "pearl.challenge":
            if r_seed == seed:
                print(f"  → ТОТ ЖЕ seed = ОТКЛОНЕНО ✗", flush=True)
            else:
                print(f"  → НОВЫЙ seed = ПРИНЯТО ✓ !!!!", flush=True)
else:
    print("  Тайлов не найдено (b=4 должно было найти — ошибка алгоритма?)", flush=True)

# ---------------------------------------------------------------------------
# Тест 2: параметры как у lpminer (но с крошечной матрицей — только посмотреть формат)
# ---------------------------------------------------------------------------
print("\n" + "=" * 60, flush=True)
print("ТЕСТ 2: m=256 n=256 k=512 r=256 b=2.0 (быстрая проверка бо́льших r)", flush=True)
tiles2 = try_params(seed, diff, m=256, n=256, k=512, r=256, b_override=2.0)
if tiles2:
    ti2, tj2, digest2 = tiles2[0]
    print(f"\n  Отправляю тайл ({ti2},{tj2}) digest={digest2[:16]}...", flush=True)
    send_raw("pearl.submit", {
        "seed":   seed,
        "tile_i": ti2,
        "tile_j": tj2,
        "digest": digest2,
    })
    print("  Жду ответ пула (15 сек)...", flush=True)
    resp_msgs2 = recv_msgs(count=3, timeout=15)
    for r_obj in resp_msgs2:
        r_seed = r_obj.get("params", {}).get("seed", "")
        if r_obj.get("method") == "pearl.challenge":
            if r_seed == seed:
                print(f"  → ТОТ ЖЕ seed = ОТКЛОНЕНО ✗", flush=True)
            else:
                print(f"  → НОВЫЙ seed = ПРИНЯТО ✓ !!!!", flush=True)
else:
    print("  Тайлов не найдено", flush=True)

print("\nГотово.", flush=True)
