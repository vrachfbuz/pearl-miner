#!/usr/bin/env python3
"""
Подключается к BaikalMine (LuckyPool протокол) и логирует всё сырьё.
Цель: увидеть 76-байтный header — там могут быть параметры m/n/k/r.
"""
import socket, json, time, binascii

HOST = "pearl.baikalmine.com"
PORT = 2010
WALLET = "prl1p6kzqzms2yn06489nj66ddg4xy6fnpylcr5gndsce7v4def6sa32qvgq38n"

sock = socket.create_connection((HOST, PORT), timeout=20)
sock.settimeout(30)
buf = b""
mid = 1

def send(method, params):
    global mid
    msg = {"id": mid, "method": method, "params": params}
    mid += 1
    line = json.dumps(msg) + "\n"
    print(f"→ {line.strip()}", flush=True)
    sock.sendall(line.encode())

def recv_all(secs=25):
    global buf
    deadline = time.time() + secs
    while time.time() < deadline:
        sock.settimeout(max(0.5, deadline - time.time()))
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            print(f"  [+{len(chunk)}b raw] {chunk[:120]}", flush=True)
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    print(f"← JSON: {obj}", flush=True)

                    # Если есть header — декодируем
                    params = obj.get("params") or {}
                    if isinstance(params, list) and len(params) > 0:
                        for p in params:
                            if isinstance(p, str) and len(p) > 60:
                                try:
                                    raw = bytes.fromhex(p)
                                    print(f"  [hex field {len(raw)}b]: {raw.hex()}", flush=True)
                                    print(f"  [as uint32 LE]: {[int.from_bytes(raw[i:i+4],'little') for i in range(0,min(len(raw),76),4)]}", flush=True)
                                except:
                                    pass
                    if isinstance(params, dict):
                        for k, v in params.items():
                            if isinstance(v, str) and len(v) > 60:
                                try:
                                    raw = bytes.fromhex(v)
                                    print(f"  [field '{k}' {len(raw)}b]: {raw.hex()}", flush=True)
                                    print(f"  [as uint32 LE]: {[int.from_bytes(raw[i:i+4],'little') for i in range(0,min(len(raw),80),4)]}", flush=True)
                                except:
                                    pass
                except Exception as e:
                    print(f"← [raw] {line[:200]}", flush=True)
        except socket.timeout:
            pass

print(f"Подключаюсь к {HOST}:{PORT}...", flush=True)
send("mining.subscribe", ["lpminer/0.1.9", None])
send("mining.authorize", [f"{WALLET}.t1", "x"])
print("Жду данные 25 сек...", flush=True)
recv_all(25)
print("Готово.", flush=True)
