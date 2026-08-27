"""Demo: route the mock agent's tool calls through the kneepoint chaos proxy.

`kneepoint proxy --upstream http://127.0.0.1:8000 --fault-log faults.jsonl` does
all of this from the CLI; this file stays as the smallest readable version of
what that command runs.

Terminal 1:  python examples/chaos_demo.py
Terminal 2:  $env:MOCK_TOOL_URL = "<url printed by terminal 1>/tool/search"
             python -m uvicorn examples.mock_agent.app:app --port 8000
Terminal 3:  kneepoint run --scenario examples/kneepoint.yaml --fault-log faults.jsonl

The `--fault-log` on both sides is what carries tool faults into the resilience
grid: without it they are served and counted here but never attributed to a
session, and the score is biased toward 100.
"""

import time

from kneepoint.chaos.faults import STANDARD_PROFILE
from kneepoint.chaos.injector import ChaosInjector
from kneepoint.chaos.proxy import start_proxy

if __name__ == "__main__":
    handle = start_proxy("http://127.0.0.1:8000", ChaosInjector(STANDARD_PROFILE, seed=0),
                         fault_log="faults.jsonl")
    print(f"chaos proxy: {handle.url}  (upstream http://127.0.0.1:8000)")
    print(f'point the agent at it:  $env:MOCK_TOOL_URL = "{handle.url}/tool/search"')
    print("faults are being appended to faults.jsonl - pass it to `kneepoint run --fault-log`")
    try:
        while True:
            time.sleep(5)
            if handle.log.counts:
                print(f"faults injected so far: {handle.log.counts} "
                      f"({handle.log.unattributed} without a session header)")
    except KeyboardInterrupt:
        handle.stop()
