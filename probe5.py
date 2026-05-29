#!/usr/bin/env python3
"""
Перебирает разные формулы digest и параметры m/n/k/r.
Для каждого варианта ищет валидный тайл и отправляет пулу.
НОВЫЙ seed = нашли правильные параметры.
Запуск: python3 probe5.py (miner.py должен быть рядом)
"""
import socket, json, time, sys, os, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blake3
import numpy as np

HOST = "us2.alphapool.tech"
PORT = 5566
WALLET = "prl1p6kzqzms2yn06489nj66ddg4xy6fnpylcr5gndsce7v4def6sa32qvgq38n"
WORKER = "probe05"

sock = socket.create_connection((HOST, PORT), timeout=30)
sock.settimeout(60)
buf = b""
msg_id = 1

def send_raw(method, params):
    global msg_id
    msg = {"id": msg_id, "method": method, "params": params}
    msg_id += 1
    line = json.dumps(msg, separators=(',', ':')) + "\n"
    print(f"→ {method} {str(params)[:80]}", flush=True)
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
            if not chunk: return None
            buf += chunk
        except socket.timeout:
            if time.time() > deadline: return None

def recv_msgs(count=3, timeout=20):
    msgs = []
    for _ in range(count):
        line = recv_line(timeout)
        if line is None: break
        try:
            obj = json.loads(line)
            print(f"← {json.dumps(obj)}", flush=True)
            msgs.append(obj)
        except:
            print(f"← [raw]: {line[:200]}", flush=True)
    return msgs

def check_response(msgs, orig_seed):
    for obj in msgs:
        r_seed = obj.get("params", {}).get("seed", "")
        if obj.get("method") == "pearl.challenge":
            if r_seed and r_seed != orig_seed:
                return "ACCEPTED"
            else:
                return "REJECTED"
    return "NO_RESPONSE"

# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------
print(f"Подключаюсь к {HOST}:{PORT}...", flush=True)
send_raw("mining.subscribe", ["pearl-probe/0.5"])
send_raw("mining.authorize", [f"{WALLET}.{WORKER}", "x"])

challenge = None
for _ in range(15):
    line = recv_line(timeout=20)
    if line is None: break
    try:
        obj = json.loads(line)
        print(f"← {json.dumps(obj)}", flush=True)
        if obj.get("method") == "pearl.challenge":
            challenge = obj; break
    except: pass

if not challenge:
    print("Нет challenge, выход", flush=True); sys.exit(1)

params = challenge["params"]
seed = params["seed"]
diff = float(params.get("difficulty", 32))
sigma = bytes.fromhex(seed)
print(f"\nCHALLENGE seed={seed[:24]}... diff={diff}\n", flush=True)

# ---------------------------------------------------------------------------
# Вспомогательные функции NoisyGEMM
# ---------------------------------------------------------------------------
def rotl32(x, s):
    x &= 0xFFFFFFFF
    return ((x << s) | (x >> (32 - s))) & 0xFFFFFFFF

def bstream(seed_b, tag, n):
    return blake3.blake3(tag + seed_b).digest(length=n)

def choice_matrix(seed_b, tag, rows, cols):
    raw = bstream(seed_b, tag, cols * 8)
    M = np.zeros((rows, cols), dtype=np.int64)
    for c in range(cols):
        a = raw[c*8] % rows; d = raw[c*8+1] % rows
        if d == a: d = (d+1) % rows
        M[a,c] += 1; M[d,c] -= 1
    return M

def noisy_gemm(sigma_b, m, n, k, r, tm, tn, b):
    mu = struct.pack("<7q d", m, n, k, r, tm, tn, 0, b)
    target = int((2.0**(256-b)) * r * tm * tn)
    A = (np.frombuffer(bstream(sigma_b, b"matrix_A", m*k), np.uint8).astype(np.int64) % 128 - 64).reshape(m,k)
    B = (np.frombuffer(bstream(sigma_b, b"matrix_B", k*n), np.uint8).astype(np.int64) % 128 - 64).reshape(k,n)
    kappa = blake3.blake3(sigma_b + mu).digest()
    HA = blake3.blake3(A.astype(np.int8).tobytes("C"), key=kappa).digest()
    HB = blake3.blake3(B.astype(np.int8).T.tobytes("C"), key=kappa).digest()
    sB = blake3.blake3(kappa + HB).digest()
    sA = blake3.blake3(sB + HA).digest()
    EL = ((np.frombuffer(bstream(sA,b"EL",m*r),np.uint8).astype(np.int64)%64)-32).reshape(m,r)
    ER = choice_matrix(sA, b"ER", r, k)
    FL = choice_matrix(sB, b"ER", k, r)
    FR = ((np.frombuffer(bstream(sB,b"EL",r*n),np.uint8).astype(np.int64)%64)-32).reshape(r,n)
    Ap = (A + EL@ER).clip(-128,127).astype(np.int64)
    Bp = (B + FL@FR).clip(-128,127).astype(np.int64)
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
                for v in sub.flatten(): X ^= int(v)&0xFFFFFFFF
                M[l%16] = rotl32(M[l%16],13)^X; l+=1
            Mb = struct.pack("<16I", *[x&0xFFFFFFFF for x in M])
            dg = blake3.blake3(Mb, key=sA).digest()
            if int.from_bytes(dg,"little") <= target:
                found.append((i, j, dg.hex(), sA))
    return found

def try_submit(label, tile_i, tile_j, digest):
    print(f"\n[{label}] tile({tile_i},{tile_j}) digest={digest[:16]}...", flush=True)
    send_raw("pearl.submit", {"seed": seed, "tile_i": tile_i, "tile_j": tile_j, "digest": digest})
    msgs = recv_msgs(count=3, timeout=18)
    result = check_response(msgs, seed)
    print(f"  → {result}", flush=True)
    return result == "ACCEPTED"

# ---------------------------------------------------------------------------
# ФОРМУЛА A: простой blake3(seed + tile_i + tile_j) без NoisyGEMM
# Ищем (i,j) такой что blake3(sigma || pack(i) || pack(j)) < target
# ---------------------------------------------------------------------------
print("="*60, flush=True)
print("ФОРМУЛА A: blake3(seed||tile_i||tile_j) — без NoisyGEMM", flush=True)
target_a = int(2.0**(256 - diff))  # чистый difficulty без умножителей
found_a = None
for ti in range(8192):
    for tj in range(8192):
        d = blake3.blake3(sigma + struct.pack("<QQ", ti, tj)).digest()
        if int.from_bytes(d, "little") <= target_a:
            found_a = (ti, tj, d.hex()); break
    if found_a: break
    if ti % 1000 == 999:
        print(f"  поиск... ti={ti}", flush=True)

if found_a:
    ti, tj, dg = found_a
    if try_submit("A: blake3(seed+i+j)", ti, tj, dg): sys.exit(0)

# ---------------------------------------------------------------------------
# ФОРМУЛА B: NoisyGEMM с разными параметрами (b=diff)
# ---------------------------------------------------------------------------
PARAM_SETS = [
    (128, 128, 512, 128, 16, 16, "m=128 k=512 r=128"),
    (256, 256, 512, 256, 16, 16, "m=256 k=512 r=256"),
    (128, 128, 256, 64,  16, 16, "m=128 k=256 r=64"),
    (64,  64,  256, 64,  16, 16, "m=64  k=256 r=64"),
    (512, 512, 512, 128, 16, 16, "m=512 k=512 r=128"),
]

for (m, n, k, r, tm, tn, label) in PARAM_SETS:
    print(f"\n{'='*60}", flush=True)
    print(f"ФОРМУЛА B NoisyGEMM {label} b={diff}", flush=True)
    t0 = time.time()
    tiles = noisy_gemm(sigma, m, n, k, r, tm, tn, diff)
    print(f"  вычислено за {time.time()-t0:.2f}с | тайлов: {len(tiles)}", flush=True)
    if tiles:
        ti, tj, dg, _ = tiles[0]
        if try_submit(f"B: {label}", ti, tj, dg): sys.exit(0)

# ---------------------------------------------------------------------------
# ФОРМУЛА C: NoisyGEMM с b=4 (сниженная сложность) — проверяем что пул
# принимает только реальную difficulty или любую
# ---------------------------------------------------------------------------
print(f"\n{'='*60}", flush=True)
print(f"ФОРМУЛА C: NoisyGEMM m=128 b=4.0 (не реальная сложность)", flush=True)
tiles_c = noisy_gemm(sigma, 128, 128, 512, 128, 16, 16, 4.0)
print(f"  тайлов: {len(tiles_c)}", flush=True)
if tiles_c:
    ti, tj, dg, _ = tiles_c[0]
    try_submit("C: b=4.0", ti, tj, dg)

print("\nГотово.", flush=True)
