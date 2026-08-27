import subprocess
import sys


def test_demo_agent_importable_and_configurable():
    from kneepoint.demo.agent import OUTPUT_TOKENS, app, create_app

    assert callable(app)
    assert callable(create_app(capacity=2, output_tokens=5))
    assert OUTPUT_TOKENS >= 1


def test_demo_agent_never_imports_fastapi():
    """The wheel must not need fastapi: importing the demo agent in a fresh
    interpreter must not pull it in."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; import kneepoint.demo.agent; print('fastapi' in sys.modules)"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "False"
