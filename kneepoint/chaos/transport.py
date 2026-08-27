"""LLM-layer chaos: synthetic 429/503 injected in kneepoint's own client path."""

import httpx

from kneepoint.chaos.injector import ChaosInjector

_STATUS = {"llm_rate_limit": 429, "llm_server_error": 503}


class ChaosTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, injector: ChaosInjector) -> None:
        self.inner = inner
        self.injector = injector

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        fault = self.injector.pick("llm")
        if fault is not None and fault.type in _STATUS:
            headers = {"x-kneepoint-fault": fault.type}
            if fault.type == "llm_rate_limit":
                headers["retry-after"] = "1"
            return httpx.Response(_STATUS[fault.type], headers=headers, request=request)
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()
