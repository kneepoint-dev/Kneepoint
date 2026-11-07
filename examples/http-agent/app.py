"""Loopback JSON and SSE echo endpoints for the HTTP target examples."""

import json
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel

app = FastAPI()
provider = TracerProvider()
if os.getenv("HTTP_AGENT_SHOW_SPANS") == "1":
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
tracer = provider.get_tracer("kneepoint.http-echo")
propagator = TraceContextTextMapPropagator()


class EchoInput(BaseModel):
    input: str


@app.post("/invoke")
async def invoke(body: EchoInput, request: Request):
    parent = propagator.extract(request.headers)
    with tracer.start_as_current_span("echo.invoke", context=parent, kind=SpanKind.SERVER):
        return {"output": {"text": body.input}}


@app.post("/stream")
async def stream(body: EchoInput, request: Request):
    parent = propagator.extract(request.headers)

    async def events():
        with tracer.start_as_current_span("echo.stream", context=parent, kind=SpanKind.SERVER):
            midpoint = len(body.input) // 2
            for part in (body.input[:midpoint], body.input[midpoint:]):
                if part:
                    yield "data: " + json.dumps({"delta": part}) + "\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
