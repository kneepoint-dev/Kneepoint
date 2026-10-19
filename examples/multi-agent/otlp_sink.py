"""Loopback OTLP/HTTP protobuf span sink: python otlp_sink.py spans.jsonl."""

import argparse
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)


def _value(value):
    kind = value.WhichOneof("value")
    if kind == "string_value":
        return value.string_value
    if kind == "int_value":
        return value.int_value
    if kind == "double_value":
        return value.double_value
    if kind == "bool_value":
        return value.bool_value
    return None


def flatten(payload: bytes):
    request = ExportTraceServiceRequest.FromString(payload)
    for resource in request.resource_spans:
        resource_attrs = {a.key: _value(a.value) for a in resource.resource.attributes}
        for scope in resource.scope_spans:
            for span in scope.spans:
                yield {
                    "trace_id": span.trace_id.hex(), "span_id": span.span_id.hex(),
                    "parent_span_id": span.parent_span_id.hex(), "name": span.name,
                    "kind": span.kind, "start_ns": span.start_time_unix_nano,
                    "end_ns": span.end_time_unix_nano,
                    "attributes": {a.key: _value(a.value) for a in span.attributes},
                    "resource": resource_attrs,
                }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--first-payload", type=Path,
                        help="save one real OTLP protobuf request for receiver tests")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/traces":
                self.send_error(404)
                return
            try:
                length = int(self.headers["Content-Length"])
                if not 0 < length <= 16_000_000:
                    raise ValueError("invalid Content-Length")
                payload = self.rfile.read(length)
                if len(payload) != length:
                    raise ValueError("short OTLP payload")
                if self.headers.get("Content-Encoding") == "gzip":
                    payload = gzip.decompress(payload)
                rows = list(flatten(payload))
            except (KeyError, TypeError, ValueError, OSError, EOFError, DecodeError):
                self.send_error(400, "invalid OTLP protobuf")
                return
            with lock, args.output.open("a", encoding="utf-8") as output:
                if args.first_payload and not args.first_payload.exists():
                    args.first_payload.parent.mkdir(parents=True, exist_ok=True)
                    args.first_payload.write_bytes(payload)
                for row in rows:
                    output.write(json.dumps(row, separators=(",", ":")) + "\n")
            response = ExportTraceServiceResponse().SerializeToString()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
