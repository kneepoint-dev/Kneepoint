"""Low-cost, per-level observation of the load generator itself."""

import asyncio
import statistics
import time

from kneepoint.collect.schemas import InjectorLevel, RequestRecord

INTERVAL_S = 0.050


class InjectorMonitor:
    def __init__(self) -> None:
        self._start_wall = time.perf_counter()
        self._start_cpu = time.process_time()
        self._last_cpu = self._start_cpu
        self._cpu_deltas: list[float] = []
        self._lag_ms: list[float] = []
        self._task = asyncio.create_task(self._sample())

    async def _sample(self) -> None:
        due = self._start_wall + INTERVAL_S
        while True:
            await asyncio.sleep(max(0.0, due - time.perf_counter()))
            actual = time.perf_counter()
            self._lag_ms.append(max(0.0, (actual - due) * 1000))
            cpu_now = time.process_time()
            self._cpu_deltas.append(max(0.0, cpu_now - self._last_cpu))
            self._last_cpu = cpu_now
            # Missed ticks count as one observed delay. Catching up by running
            # many zero-delay samples would make the monitor disturb the load.
            due = max(due + INTERVAL_S, actual + INTERVAL_S)

    async def finish(self, records: list[RequestRecord]) -> InjectorLevel:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        elapsed = max(time.perf_counter() - self._start_wall, 1e-9)
        cpu_share = (sum(self._cpu_deltas)
                     + max(0.0, time.process_time() - self._last_cpu)) / elapsed
        lag = sorted(self._lag_ms)
        p50 = statistics.median(lag) if lag else None
        p95 = (statistics.quantiles(lag, n=100, method="inclusive")[94]
               if len(lag) > 1 else p50)
        responses = [r.total_ms for r in records if r.ok]
        median_response = statistics.median(responses) if responses else None
        threshold = max(5.0, 0.02 * median_response) if median_response is not None else 5.0
        return InjectorLevel(
            lag_p50_ms=p50, lag_p95_ms=p95, lag_max_ms=max(lag) if lag else None,
            cpu_core_share=cpu_share, request_rate_rps=len(records) / elapsed,
            samples=len(lag),
            injector_limited=(p95 is not None and p95 > threshold) or cpu_share > 0.70,
        )
