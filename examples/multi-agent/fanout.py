"""Measure one request's internal fan-out from the demo's live counters."""

import json
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:8765"


def stats() -> dict:
    with urlopen(f"{BASE}/debug/stats", timeout=10) as response:
        return json.load(response)


if __name__ == "__main__":
    before = stats()
    payload = json.dumps({
        "model": "lantern-desk", "stream": True,
        "messages": [{"role": "user", "content": "How do I restore the LanternDesk mod042 "
                      "status panel?"}],
    }).encode()
    with urlopen(Request(
        f"{BASE}/v1/chat/completions", data=payload,
        headers={"Content-Type": "application/json"},
    ), timeout=30) as response:
        body = response.read().decode()
        call_headers = {
            "model_calls": int(response.headers["x-demo-model-calls"]),
            "search_calls": int(response.headers["x-demo-search-calls"]),
            "fetch_calls": int(response.headers["x-demo-fetch-calls"]),
        }
    after = stats()
    deltas = {key: after[key] - before[key] for key in call_headers}
    print(json.dumps({"per_request_headers": call_headers, "counter_delta": deltas,
                      "resolved": "[RESOLVED kb-42]" in body}, indent=2))
