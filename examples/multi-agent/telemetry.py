"""Small, explicit OTel boundary for the local LanternDesk example."""

import os
from contextlib import contextmanager
from urllib.parse import urlparse

from opentelemetry import baggage, trace
from opentelemetry import context as otel_context
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

PROPAGATOR = CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])


class Telemetry:
    def __init__(self, *, enabled: bool | None = None, provider: TracerProvider | None = None):
        self.enabled = (os.getenv("DEMO_TRACING", "0") == "1") if enabled is None else enabled
        self.provider = provider
        self.owned = False
        if self.enabled and provider is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
            parsed = urlparse(endpoint)
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("OTLP endpoint must be local HTTP")
            if not endpoint.rstrip("/").endswith("/v1/traces"):
                endpoint = endpoint.rstrip("/") + "/v1/traces"
            provider = TracerProvider(resource=Resource.create({"service.name": "lanterndesk"}))
            provider.add_span_processor(BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint), schedule_delay_millis=200,
                max_queue_size=8192, max_export_batch_size=512,
            ))
            self.provider = provider
            self.owned = True
        self.tracer = self.provider.get_tracer("lanterndesk") if self.enabled else None

    @contextmanager
    def span(self, name, *, kind=trace.SpanKind.INTERNAL, context=None, attributes=None):
        if self.tracer is None:
            yield trace.INVALID_SPAN
        else:
            token = otel_context.attach(context) if context is not None else None
            try:
                with self.tracer.start_as_current_span(
                    name, context=context, kind=kind, attributes=attributes,
                ) as span:
                    yield span
            finally:
                if token is not None:
                    otel_context.detach(token)

    def start_span(self, name, *, kind=trace.SpanKind.INTERNAL, context=None,
                   attributes=None):
        if self.tracer is None:
            return trace.INVALID_SPAN
        return self.tracer.start_span(name, context=context, kind=kind,
                                      attributes=attributes)

    def extract(self, headers):
        return PROPAGATOR.extract(headers)

    def inject(self, headers):
        PROPAGATOR.inject(headers)

    @staticmethod
    def baggage_attributes(context):
        return {f"baggage.{key}": value for key, value in baggage.get_all(context).items()
                if key.startswith("kneepoint.")}

    def shutdown(self):
        if self.owned:
            self.provider.shutdown()
