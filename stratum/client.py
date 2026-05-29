"""
stratum/client.py — EthereumStratum/1.0.0 клиент для BaikalMine Pearl пула.

Протокол (из зонда pearl_stratum_probe.py):
  subscribe  → result: [true, "EthereumStratum/1.0.0"]
  authorize  → result: true
  пул шлёт:   set_extranonce [extranonce1_hex, extranonce2_size]
               mining.set_difficulty [diff]
               mining.notify [job_id, ...поля job'а...]   ← формат уточнить по зонду
  отвечаем:   mining.submit [worker, job_id, nonce_hex, ...proof...]
"""

import json
import socket
import threading
import time
import logging
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger("stratum")


@dataclass
class Job:
    job_id: str
    raw_params: list          # весь params из mining.notify — пока сырой
    extranonce1: bytes        # префикс extranonce от пула (2 байта)
    extranonce2_size: int     # размер нашей части nonce (6 байт)
    difficulty: float
    clean: bool = True        # если True — старые шары отбрасывать


class StratumClient:
    """
    EthereumStratum/1.0.0 клиент для Pearl пула.
    Запускается в отдельном потоке; передаёт job'ы через callback on_job.
    """

    def __init__(
        self,
        host: str,
        port: int,
        address: str,
        worker: str,
        password: str = "x",
        on_job: Optional[Callable[[Job], None]] = None,
    ):
        self.host = host
        self.port = port
        self.user = f"{address}.{worker}"
        self.password = password
        self.on_job = on_job

        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._pending: dict[int, threading.Event] = {}
        self._results: dict[int, object] = {}

        self._extranonce1 = b""
        self._extranonce2_size = 6
        self._difficulty = 1.0
        self._current_job: Optional[Job] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="stratum")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def submit(self, job_id: str, nonce2_hex: str, proof_data: list) -> bool:
        """Отправить шару. proof_data — список полей специфичных для Pearl."""
        params = [self.user, job_id, nonce2_hex] + proof_data
        msg_id = self._send("mining.submit", params)
        # TODO: дождаться подтверждения пула (accepted/rejected)
        log.info("submit job=%s nonce2=%s", job_id, nonce2_hex)
        return True

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _loop(self):
        backoff = 2
        while self._running:
            try:
                self._connect()
                self._handshake()
                backoff = 2
                self._recv_loop()
            except OSError as e:
                log.warning("соединение потеряно: %s — переподключение через %ds", e, backoff)
            except Exception as e:
                log.exception("stratum error: %s", e)
            if self._running:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _connect(self):
        log.info("подключаюсь к %s:%d", self.host, self.port)
        self._sock = socket.create_connection((self.host, self.port), timeout=15)
        self._sock.settimeout(None)  # блокирующий режим
        self._buf = b""
        log.info("соединение установлено")

    def _handshake(self):
        self._send("mining.subscribe", ["pearl-miner/0.1", "EthereumStratum/1.0.0"])
        self._send("mining.authorize", [self.user, self.password])

    def _send(self, method: str, params: list) -> int:
        with self._id_lock:
            msg_id = self._next_id
            self._next_id += 1
        msg = {"id": msg_id, "method": method, "params": params}
        line = (json.dumps(msg) + "\n").encode()
        self._sock.sendall(line)
        log.debug("→ %s", json.dumps(msg))
        return msg_id

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
                    self._handle(json.loads(raw))

    def _handle(self, obj: dict):
        log.debug("← %s", json.dumps(obj, ensure_ascii=False))
        method = obj.get("method")

        if method == "mining.set_difficulty":
            params = obj.get("params", [])
            if params:
                self._difficulty = float(params[0])
                log.info("difficulty → %s", self._difficulty)

        elif method == "set_extranonce":
            params = obj.get("params", [])
            if len(params) >= 2:
                self._extranonce1 = bytes.fromhex(params[0])
                self._extranonce2_size = int(params[1])
                log.info("extranonce1=%s size=%d", params[0], self._extranonce2_size)

        elif method == "mining.notify":
            params = obj.get("params", [])
            # TODO: распарсить поля job'а после того как зонд поймает реальный notify.
            # Пока сохраняем сырые params.
            if not params:
                return
            job_id = str(params[0]) if params else "?"
            clean = bool(params[-1]) if len(params) > 1 else True
            job = Job(
                job_id=job_id,
                raw_params=params,
                extranonce1=self._extranonce1,
                extranonce2_size=self._extranonce2_size,
                difficulty=self._difficulty,
                clean=clean,
            )
            self._current_job = job
            log.info("новый job: id=%s clean=%s diff=%s", job_id, clean, self._difficulty)
            if self.on_job:
                self.on_job(job)

        elif "result" in obj or "error" in obj:
            # Ответ на наш запрос
            err = obj.get("error")
            res = obj.get("result")
            if err:
                log.warning("ошибка от пула: %s", err)
            else:
                log.debug("result: %s", res)


# ------------------------------------------------------------------
# Быстрый тест (аналог зонда)
# ------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    def on_job(job: Job):
        print(f"\n=== JOB RECEIVED ===")
        print(f"  id={job.job_id}")
        print(f"  diff={job.difficulty}")
        print(f"  extranonce1={job.extranonce1.hex()}")
        print(f"  raw_params={job.raw_params}")

    client = StratumClient(
        host="pearl.baikalmine.com",
        port=2010,
        address="prl1p6kzqzms2yn06489nj66ddg4xy6fnpylcr5gndsce7v4def6sa32qvgq38n",
        worker="test",
        password="x",
        on_job=on_job,
    )
    client.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        client.stop()
