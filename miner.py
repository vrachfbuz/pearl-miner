#!/usr/bin/env python3
"""
Pearl Miner v0.1 — прототип, CPU + AlphaPool (pearl.challenge протокол)
Цель: проверить полный цикл job → NoisyGEMM → submit на реальном пуле.
После подтверждения протокола — заменить вычисления на CUDA ядро.
"""

import argparse
import json
import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional

import blake3
import numpy as np

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-12s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pearl")

# ---------------------------------------------------------------------------
# Конфигурация майнинга (μ)
# Параметры по умолчанию из whitepaper / pearl-research-labs reference
# ---------------------------------------------------------------------------
@dataclass
class MiningConfig:
    m: int = 128
    n: int = 128
    k: int = 512
    r: int = 128        # noise_rank из pearl-research-labs
    tm: int = 16        # hash_tile_h
    tn: int = 16        # hash_tile_w
    b: float = 32.0     # сложность (бит); берём из difficulty пула
    acc_type: int = 0

    def to_bytes(self) -> bytes:
        return struct.pack("<7q d", self.m, self.n, self.k, self.r,
                           self.tm, self.tn, self.acc_type, self.b)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def rotl32(x: int, s: int) -> int:
    x &= 0xFFFFFFFF
    return ((x << s) | (x >> (32 - s))) & 0xFFFFFFFF


def blake3_stream(seed: bytes, tag: bytes, nbytes: int) -> bytes:
    """Детерминированный поток байт из seed+tag через BLAKE3 XOF."""
    return blake3.blake3(tag + seed).digest(length=nbytes)


# ---------------------------------------------------------------------------
# Генерация матриц A и B из seed (σ)
# В пуле AlphaPool seed — это состояние цепи σ.
# Матрицы генерируются детерминированно через BLAKE3 XOF.
# ---------------------------------------------------------------------------

def generate_ab(seed: bytes, cfg: MiningConfig):
    """Генерирует матрицы A (m×k) и B (k×n) из seed как int8 [-64, 63]."""
    raw_a = blake3_stream(seed, b"matrix_A", cfg.m * cfg.k)
    raw_b = blake3_stream(seed, b"matrix_B", cfg.k * cfg.n)

    A = (np.frombuffer(raw_a, dtype=np.uint8).astype(np.int64) % 128 - 64
         ).reshape(cfg.m, cfg.k)
    B = (np.frombuffer(raw_b, dtype=np.uint8).astype(np.int64) % 128 - 64
         ).reshape(cfg.k, cfg.n)
    return A, B


# ---------------------------------------------------------------------------
# Commitment Hash (раздел 4.3 whitepaper)
# ---------------------------------------------------------------------------

def commitment_hash(A, B, mu: MiningConfig, sigma: bytes):
    kappa = blake3.blake3(sigma + mu.to_bytes()).digest()
    HA = blake3.blake3(A.astype(np.int8).tobytes(order="C"), key=kappa).digest()
    HB = blake3.blake3(B.astype(np.int8).T.tobytes(order="C"), key=kappa).digest()
    sB = blake3.blake3(kappa + HB).digest()
    sA = blake3.blake3(sB + HA).digest()
    return sA, sB, HA, HB


# ---------------------------------------------------------------------------
# Генерация шума (раздел 4.4)
# ---------------------------------------------------------------------------

def uniform_matrix(seed: bytes, rows: int, cols: int) -> np.ndarray:
    raw = blake3_stream(seed, b"EL", rows * cols)
    vals = np.frombuffer(raw, dtype=np.uint8).astype(np.int64)
    return ((vals % 64) - 32).reshape(rows, cols)


def choice_matrix(seed: bytes, rows: int, cols: int) -> np.ndarray:
    raw = blake3_stream(seed, b"ER", cols * 8)
    M = np.zeros((rows, cols), dtype=np.int64)
    for c in range(cols):
        a = raw[c * 8] % rows
        d = raw[c * 8 + 1] % rows
        if d == a:
            d = (d + 1) % rows
        M[a, c] += 1
        M[d, c] -= 1
    return M


def generate_noise(mu: MiningConfig, sA: bytes, sB: bytes):
    EL = uniform_matrix(sA, mu.m, mu.r)
    ER = choice_matrix(sA, mu.r, mu.k)
    FL = choice_matrix(sB, mu.k, mu.r)
    FR = uniform_matrix(sB, mu.r, mu.n)
    return EL, ER, FL, FR


# ---------------------------------------------------------------------------
# Тайловый matmul с проверкой открытия (раздел 4.5)
# ---------------------------------------------------------------------------

def tiled_matmul_mine(Ap, Bp, mu: MiningConfig, sA: bytes):
    m, n, k, r, tm, tn = mu.m, mu.n, mu.k, mu.r, mu.tm, mu.tn
    Cp = np.zeros((m, n), dtype=np.int64)
    opened = []
    target = int((2.0 ** (256 - mu.b)) * r * tm * tn)

    for i in range(0, m, tm):
        h = min(tm, m - i)
        for j in range(0, n, tn):
            w = min(tn, n - j)
            Cblk = np.zeros((h, w), dtype=np.int64)
            M = [0] * 16
            l = 0
            for s in range(0, k, r):
                d = min(r, k - s)
                Cblk = Cblk + Ap[i:i+h, s:s+d] @ Bp[s:s+d, j:j+w]
                if h == tm and w == tn and d == r:
                    X = 0
                    for v in Cblk.flatten():
                        X ^= int(v) & 0xFFFFFFFF
                    idx = l % 16
                    M[idx] = rotl32(M[idx], 13) ^ X
                    l += 1
            Cp[i:i+h, j:j+w] = Cblk
            if h == tm and w == tn:
                Mbytes = struct.pack("<16I", *[x & 0xFFFFFFFF for x in M])
                digest = blake3.blake3(Mbytes, key=sA).digest()
                hval = int.from_bytes(digest, "little")
                if hval <= target:
                    opened.append((i, j, hval, M, digest))
    return Cp, opened


# ---------------------------------------------------------------------------
# Полный цикл майнинга одного job'а
# ---------------------------------------------------------------------------

def mine_job(seed_hex: str, difficulty: int, cfg: MiningConfig):
    """Возвращает список открытых тайлов (может быть пустым)."""
    sigma = bytes.fromhex(seed_hex)
    cfg.b = float(difficulty)

    A, B = generate_ab(sigma, cfg)
    sA, sB, HA, HB = commitment_hash(A, B, cfg, sigma)
    EL, ER, FL, FR = generate_noise(cfg, sA, sB)

    E = EL @ ER
    F = FL @ FR
    Ap = (A + E).clip(-128, 127).astype(np.int8)
    Bp = (B + F).clip(-128, 127).astype(np.int8)

    Cp, opened = tiled_matmul_mine(Ap.astype(np.int64), Bp.astype(np.int64), cfg, sA)

    results = []
    for (tile_i, tile_j, hval, transcript_M, digest) in opened:
        results.append({
            "tile_i": tile_i,
            "tile_j": tile_j,
            "hval": hval,
            "digest_hex": digest.hex(),
            "sA": sA.hex(),
            "sB": sB.hex(),
            "HA": HA.hex(),
            "HB": HB.hex(),
            "transcript": bytes(struct.pack("<16I", *[x & 0xFFFFFFFF for x in transcript_M])).hex(),
        })
    return results


# ---------------------------------------------------------------------------
# Stratum клиент (AlphaPool: pearl.challenge протокол)
# ---------------------------------------------------------------------------

class PearlStratumClient:
    def __init__(self, host, port, address, worker, password, on_challenge):
        self.host = host
        self.port = port
        self.user = f"{address}.{worker}"
        self.password = password
        self.on_challenge = on_challenge
        self._sock = None
        self._buf = b""
        self._id = 1
        self._running = False

    def start(self):
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True, name="stratum")
        t.start()

    def stop(self):
        self._running = False
        if self._sock:
            try: self._sock.close()
            except: pass

    def submit(self, seed: str, tile_i: int, tile_j: int, proof: dict):
        """Отправить найденную шару."""
        # Формат pearl.submit — уточняется по ответу пула.
        # Отправляем всё что знаем, посмотрим на ошибку.
        params = {
            "seed": seed,
            "tile_i": tile_i,
            "tile_j": tile_j,
            "sA": proof["sA"],
            "sB": proof["sB"],
            "HA": proof["HA"],
            "HB": proof["HB"],
            "transcript": proof["transcript"],
            "digest": proof["digest_hex"],
        }
        self._send("pearl.submit", params)
        log.info("SUBMIT tile(%d,%d) digest=%s...", tile_i, tile_j, proof["digest_hex"][:16])

    def _loop(self):
        backoff = 3
        while self._running:
            try:
                self._connect()
                self._handshake()
                backoff = 3
                self._recv_loop()
            except OSError as e:
                log.warning("соединение потеряно: %s", e)
            except Exception as e:
                log.exception("stratum: %s", e)
            if self._running:
                log.info("переподключение через %ds...", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _connect(self):
        log.info("подключаюсь к %s:%d", self.host, self.port)
        self._sock = socket.create_connection((self.host, self.port), timeout=15)
        self._sock.settimeout(None)
        self._buf = b""
        self._id = 1
        log.info("соединено")

    def _handshake(self):
        self._send("mining.subscribe", ["pearl-miner/0.1"])
        self._send("mining.authorize", [self.user, self.password])

    def _send(self, method, params):
        msg = {"id": self._id, "method": method, "params": params}
        self._id += 1
        self._sock.sendall((json.dumps(msg) + "\n").encode())
        log.debug("→ %s", method)

    def _recv_loop(self):
        while self._running:
            chunk = self._sock.recv(8192)
            if not chunk:
                raise OSError("сервер закрыл соединение")
            self._buf += chunk
            while b"\n" in self._buf:
                raw, self._buf = self._buf.split(b"\n", 1)
                raw = raw.strip()
                if raw:
                    try:
                        obj = json.loads(raw)
                        self._handle(obj)
                    except json.JSONDecodeError:
                        pass

    def _handle(self, obj):
        method = obj.get("method", "")
        params = obj.get("params", {})

        if method == "pearl.challenge":
            seed = params.get("seed", "")
            diff = params.get("difficulty", 32)
            log.info("JOB seed=%s... diff=%d", seed[:12], diff)
            if self.on_challenge:
                # Запускаем в отдельном потоке чтобы не блокировать stratum
                t = threading.Thread(
                    target=self.on_challenge,
                    args=(seed, diff, self),
                    daemon=True
                )
                t.start()

        elif method == "mining.set_difficulty":
            log.info("difficulty → %s", params)

        elif "result" in obj or "error" in obj:
            err = obj.get("error")
            res = obj.get("result")
            if err:
                log.warning("← ОШИБКА от пула: %s", err)
            else:
                log.info("← OK: %s", res)
        else:
            log.debug("← %s %s", method, params)


# ---------------------------------------------------------------------------
# Главный цикл майнинга
# ---------------------------------------------------------------------------

def on_challenge(seed: str, difficulty: int, client: PearlStratumClient):
    cfg = MiningConfig()
    cfg.b = float(difficulty)

    t0 = time.time()
    log.info("майним seed=%s... diff=%d (m=%d n=%d k=%d r=%d)",
             seed[:12], difficulty, cfg.m, cfg.n, cfg.k, cfg.r)

    try:
        results = mine_job(seed, difficulty, cfg)
    except Exception as e:
        log.exception("ошибка майнинга: %s", e)
        return

    elapsed = time.time() - t0
    n_tiles = (cfg.m // cfg.tm) * (cfg.n // cfg.tn)
    log.info("готово за %.2f сек | тайлов: %d | открыто: %d",
             elapsed, n_tiles, len(results))

    for r in results:
        log.info("ОТКРЫТИЕ тайл(%d,%d) digest=%s...",
                 r["tile_i"], r["tile_j"], r["digest_hex"][:16])
        client.submit(seed, r["tile_i"], r["tile_j"], r)


def main():
    ap = argparse.ArgumentParser(description="Pearl CPU Miner v0.1")
    ap.add_argument("--pool",    default="us2.alphapool.tech:5566")
    ap.add_argument("--address", required=True)
    ap.add_argument("--worker",  default="rig01")
    ap.add_argument("--password",default="x;d=256")
    args = ap.parse_args()

    host, port = args.pool.rsplit(":", 1)
    port = int(port)

    # Быстрый само-тест алгоритма
    log.info("само-тест NoisyGEMM...")
    cfg_test = MiningConfig(m=64, n=64, k=128, r=32, tm=16, tn=16, b=1.0)
    sigma_test = b"test-sigma-1234567890123456789012"[:32]
    A_t, B_t = generate_ab(sigma_test, cfg_test)
    sA_t, sB_t, _, _ = commitment_hash(A_t, B_t, cfg_test, sigma_test)
    EL, ER, FL, FR = generate_noise(cfg_test, sA_t, sB_t)
    E = EL @ ER
    F = FL @ FR
    Ap_t = A_t + E
    Bp_t = B_t + F
    Cp_t, _ = tiled_matmul_mine(Ap_t, Bp_t, cfg_test, sA_t)
    ref = A_t @ B_t
    C_recovered = Cp_t - (A_t @ FL) @ FR - EL @ (ER @ Bp_t)
    assert np.array_equal(C_recovered, ref), "САМО-ТЕСТ ПРОВАЛЕН — ошибка алгоритма!"
    log.info("само-тест ОК")

    client = PearlStratumClient(
        host=host, port=port,
        address=args.address,
        worker=args.worker,
        password=args.password,
        on_challenge=on_challenge,
    )
    client.start()

    log.info("майнер запущен. Ctrl+C для выхода.")
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        log.info("выход")
        client.stop()


if __name__ == "__main__":
    main()
