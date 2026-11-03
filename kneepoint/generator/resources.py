"""Best-effort Linux host sampling for the machine running the load generator."""

import asyncio
import time
from pathlib import Path

from kneepoint.collect.schemas import ResourceSample

SAMPLE_INTERVAL_S = 5.0
PROC_STAT = Path("/proc/stat")


def _read_cpu() -> tuple[list[int], int]:
    cpu = None
    runnable = None
    with PROC_STAT.open(encoding="ascii") as stream:
        for line in stream:
            if line.startswith("cpu "):
                # guest and guest_nice are already in user and nice.
                cpu = [int(value) for value in line.split()[1:9]]
            elif line.startswith("procs_running "):
                runnable = int(line.split()[1])
                break
    if cpu is None or len(cpu) != 8 or runnable is None or runnable < 0:
        raise ValueError("incomplete host counters")
    return cpu, runnable


async def sample_local_resources(
    concurrency: int, level_start: float, samples: list[ResourceSample],
) -> None:
    """Collect interval deltas until the pool cancels this level's sampler."""
    try:
        previous, _ = _read_cpu()
        while True:
            await asyncio.sleep(SAMPLE_INTERVAL_S)
            current, runnable = _read_cpu()
            delta = [new - old for new, old in zip(current, previous, strict=True)]
            previous = current
            total = sum(delta)
            if total <= 0 or any(value < 0 for value in delta):
                continue
            user, nice, system, idle, _, irq, softirq, steal = delta
            samples.append(ResourceSample(
                concurrency=concurrency, elapsed_s=time.perf_counter() - level_start,
                cpu_user_pct=100 * (user + nice) / total,
                cpu_system_pct=100 * (system + irq + softirq) / total,
                cpu_idle_pct=100 * idle / total, cpu_steal_pct=100 * steal / total,
                run_queue=runnable,
            ))
    except (OSError, ValueError):
        # Missing or unreadable counters must never interrupt the load run.
        return
