"""Opt-in one-second injector health; process CPU is percent of one core."""

import asyncio
import json
import socket
import time
from pathlib import Path

import psutil


class HealthSampler:
    def __init__(self, path: Path, *, population=lambda: 0, labels=lambda: {}):
        self.path = Path(path)
        self.population = population
        self.labels = labels
        self.process = psutil.Process()
        self.process.cpu_percent(None)
        psutil.cpu_percent(None)

    async def run(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        due = time.perf_counter() + 1
        while True:
            await asyncio.sleep(max(0, due - time.perf_counter()))
            actual = time.perf_counter()
            # A second event-loop scheduling probe distinguishes timer drift
            # from time spent collecting OS counters below.
            await asyncio.sleep(0)
            loop_delay = (time.perf_counter() - actual) * 1000
            row = {"t": time.time(), "host": socket.gethostname(), "source": "injector",
                   **self.labels(), "lag_ms": max(0, (actual - due) * 1000),
                   "loop_delay_ms": loop_delay, "cpu_pct": self.process.cpu_percent(None),
                   "host_cpu_pct": psutil.cpu_percent(None), "inflight": self.population()}
            try:
                row["open_connections"] = len(self.process.net_connections(kind="tcp"))
            except (psutil.AccessDenied, psutil.Error, OSError) as exc:
                row.update(open_connections=None, open_connections_reason=type(exc).__name__)
            with self.path.open("a") as out:
                out.write(json.dumps(row) + "\n")
            due = max(due + 1, actual + 1)
