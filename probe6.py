#!/usr/bin/env python3
"""
Probe6: пробует digest с sigma (raw seed) как BLAKE3-ключ вместо sA.
Whitepaper: BLAKE3(seed, M_ij) < 2^(256-b) => blake3(M_ij, key=sigma).
Майнит несколько попыток с разными target формулами, при нахождении — шлёт пулу.
"""
import socket, json, time, sys, os, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blake3, numpy as np

HOST = "us2.alphapool.tech"
PORT = 5566
WALLET = "prl1p6kzqzms2yn06489nj66ddg4xy6fnpylcr5gndsce7v4def6sa32qvgq38n"
WORKER = "probe06"

sock = socket.create_connection((HOST, PORT), timeout=30)
sock.settimeout(60)
buf = b""; mid = 1

def send_raw(method, params):
    global mid
    msg = {"id": mid, "method": method, "params": params}; mid += 1
    line = json.dumps(msg, separators=(',', ':')) + "\n"
    print(f"→ {method}", flush=True)
    sock.sendall(line.encode())

def recv_line(timeout=20):
    global buf
    sock.settimeout(timeout); deadline = time.time() + timeout
    while True:
        if b"\n" in buf:
            line, buf = buf.split(b"\n", 1); return line.strip()
        try: buf += sock.recv(4096)
        except socket.timeout:
            if time.time() > deadline: return None

def recv_msgs(count=3, timeout=20):
    msgs = []
    for _ in range(count):
        line = recv_line(timeout)
        if line is None: break
        try:
            obj = json.loads(line); print(f"← {json.dumps(obj)}", flush=True); msgs.append(obj)
        except: print(f"← [raw] {line[:200]}", flush=True)
    return msgs

def check(msgs, orig_seed):
    for o in msgs:
        s2 = o.get("params", {}).get("seed", "")
        if o.get("method") == "pearl.challenge":
            return "ACCEPTED" if (s2 and s2 != orig_seed) else "REJECTED"
    return "NO_RESP"

# ---------------------------------------------------------------------------
def rotl32(x, s):
    x &= 0xFFFFFFFF; return ((x << s) | (x >> (32 - s))) & 0xFFFFFFFF

def bstream(key, tag, n): return blake3.blake3(tag + key).digest(length=n)

def choice_mat(key, tag, rows, cols):
    raw = bstream(key, tag, cols * 8); M = np.zeros((rows, cols), dtype=np.int64)
    for c in range(cols):
        a = raw[c*8] % rows; d = raw[c*8+1] % rows
        if d == a: d = (d+1) % rows
        M[a,c] += 1; M[d,c] -= 1
    return M

def make_noisy(sigma, m, n, k, r):
    A = ((np.frombuffer(bstream(sigma,b"matrix_A",m*k),np.uint8).astype(np.int64))%128-64).reshape(m,k)
    B = ((np.frombuffer(bstream(sigma,b"matrix_B",k*n),np.uint8).astype(np.int64))%128-64).reshape(k,n)
    mu_bytes = struct.pack("<7q d", m, n, k, r, 16, 16, 0, 32.0)
    kappa = blake3.blake3(sigma + mu_bytes).digest()
    HA = blake3.blake3(A.astype(np.int8).tobytes("C"), key=kappa).digest()
    HB = blake3.blake3(B.astype(np.int8).T.tobytes("C"), key=kappa).digest()
    sB = blake3.blake3(kappa + HB).digest()
    sA = blake3.blake3(sB + HA).digest()
    EL = ((np.frombuffer(bstream(sA,b"EL",m*r),np.uint8).astype(np.int64))%64-32).reshape(m,r)
    ER = choice_mat(sA, b"ER", r, k)
    FL = choice_mat(sB, b"ER", k, r)
    FR = ((np.frombuffer(bstream(sB,b"EL",r*n),np.uint8).astype(np.int64))%64-32).reshape(r,n)
    Ap = (A + EL@ER).clip(-128,127).astype(np.int64)
    Bp = (B + FL@FR).clip(-128,127).astype(np.int64)
    return Ap, Bp, sA

def compute_tiles(Ap, Bp, m, n, k, r, tm, tn, hash_key, target):
    """Вычисляет транскрипты тайлов и проверяет против target. hash_key = 32 байта."""
    found = []
    for i in range(0, m, tm):
        if m-i < tm: continue
        for j in range(0, n, tn):
            if n-j < tn: continue
            M = [0]*16; l = 0
            for s in range(0, k, r):
                if k-s < r: continue
                sub = Ap[i:i+tm, s:s+r] @ Bp[s:s+r, j:j+tn]
                X = 0
                for v in sub.flatten(): X ^= int(v) & 0xFFFFFFFF
                M[l%16] = rotl32(M[l%16], 13) ^ X; l += 1
            Mb = struct.pack("<16I", *[x & 0xFFFFFFFF for x in M])
            dg = blake3.blake3(Mb, key=hash_key).digest()
            if int.from_bytes(dg, "little") <= target:
                found.append((i, j, dg.hex()))
    return found

# ---------------------------------------------------------------------------
print(f"Подключаюсь {HOST}:{PORT}...", flush=True)
send_raw("mining.subscribe", ["pearl-probe/0.6"])
send_raw("mining.authorize", [f"{WALLET}.{WORKER}", "x"])

challenge = None
for _ in range(15):
    line = recv_line(20)
    if not line: break
    try:
        obj = json.loads(line); print(f"← {json.dumps(obj)}", flush=True)
        if obj.get("method") == "pearl.challenge": challenge = obj; break
    except: pass
if not challenge: print("Нет challenge"); sys.exit(1)

params = challenge["params"]
seed = params["seed"]; diff = float(params.get("difficulty", 32))
sigma = bytes.fromhex(seed)
print(f"\nCHALLENGE seed={seed[:24]}... diff={diff}\n", flush=True)

m, n, k, r, tm, tn = 128, 128, 512, 128, 16, 16
print(f"Параметры: m={m} n={n} k={k} r={r}", flush=True)
print("Генерирую Ap, Bp...", flush=True)
Ap, Bp, sA = make_noisy(sigma, m, n, k, r)

TESTS = [
    # (описание, hash_key, target_formula)
    ("sigma-key  target=2^(256-b)*k*tm*tn", sigma, int(2.0**(256-diff)*k*tm*tn)),
    ("sigma-key  target=2^(256-b)*r*tm*tn", sigma, int(2.0**(256-diff)*r*tm*tn)),
    ("sigma-key  target=2^(256-b)         ", sigma, int(2.0**(256-diff))),
    ("sA-key     target=2^(256-b)*k*tm*tn", sA,    int(2.0**(256-diff)*k*tm*tn)),
    ("sA-key     target=2^(256-b)*r*tm*tn", sA,    int(2.0**(256-diff)*r*tm*tn)),
    ("sA-key     target=2^(256-b)         ", sA,    int(2.0**(256-diff))),
]

MAX_ATTEMPTS = 800   # при 0.16с/попытка = ~130сек максимум

for (label, hkey, target) in TESTS:
    print(f"\n{'='*60}", flush=True)
    print(f"ТЕСТ: {label}", flush=True)
    print(f"target bits: {target.bit_length()} (target~2^{target.bit_length()})", flush=True)

    found = []
    t0 = time.time()
    attempts = 0

    while not found and attempts < MAX_ATTEMPTS:
        found = compute_tiles(Ap, Bp, m, n, k, r, tm, tn, hkey, target)
        attempts += 1
        if attempts == 1 and not found:
            # С первой попытки может не быть — перегенерим Ap/Bp с другим seed на следующих итерациях?
            # Нет, у нас фиксированный challenge. Просто ждём — но тайлы фиксированы!
            # Если не нашли с 1 попытки — никогда не найдём при том же Ap/Bp.
            break

    elapsed = time.time() - t0
    if found:
        ti, tj, dg = found[0]
        hval = int.from_bytes(bytes.fromhex(dg), "little")
        print(f"  ✓ НАЙДЕН тайл({ti},{tj}) за {elapsed:.3f}с hval~2^{hval.bit_length()}", flush=True)
        print(f"    digest={dg}", flush=True)

        send_raw("pearl.submit", {"seed": seed, "tile_i": ti, "tile_j": tj, "digest": dg})
        msgs = recv_msgs(count=3, timeout=20)
        result = check(msgs, seed)
        print(f"  ОТВЕТ ПУЛА: {result}", flush=True)
        if result == "ACCEPTED":
            print(f"\n★★★ ПРИНЯТО! Правильная формула: {label} ★★★", flush=True)
            sys.exit(0)
    else:
        print(f"  ✗ Тайлов нет (target слишком низкий для 64 тайлов)", flush=True)

print("\nВсе тесты завершены. Пул не принял ни одного варианта.", flush=True)
print("Возможно нужны другие параметры m/n/k/r.", flush=True)
