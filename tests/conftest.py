import threading
import time

import pytest
import uvicorn

from examples.mock_agent.app import app as mock_app


@pytest.fixture(scope="session")
def mock_agent_url() -> str:
    """Run the mock agent on a free port for the whole test session."""
    config = uvicorn.Config(mock_app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("mock agent failed to start within 10s")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/v1"
    server.should_exit = True
    thread.join(timeout=5)
