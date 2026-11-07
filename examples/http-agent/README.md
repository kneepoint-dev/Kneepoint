# HTTP echo agent

From the repository root, install the example dependencies in an environment
that has Kneepoint installed, then start the server.

```sh
python -m pip install -r examples/http-agent/requirements.txt
HTTP_AGENT_SHOW_SPANS=1 python examples/http-agent/app.py
```

The server binds to `127.0.0.1:8000`. The environment flag prints OTel server spans
to stdout with the incoming W3C trace parent. The echo endpoints use no model
and report no token counts.

In another terminal, validate and run either scenario.

```sh
kneepoint validate examples/http-agent/json.yaml
kneepoint run --scenario examples/http-agent/json.yaml --out reports/http-json
kneepoint validate examples/http-agent/sse.yaml
kneepoint run --scenario examples/http-agent/sse.yaml --out reports/http-sse
```
