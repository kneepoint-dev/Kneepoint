"""Compat shim: the mock agent now ships inside the package (kneepoint.demo.agent)
so `kneepoint demo` works from a wheel. This module keeps the old import path
and the `python -m uvicorn examples.mock_agent.app:app` invocation working."""

from kneepoint.demo.agent import (  # noqa: F401
    BASE_DELAY_MS,
    CAPACITY,
    OUTPUT_TOKENS,
    TOKEN_DELAY_MS,
    app,
    create_app,
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
