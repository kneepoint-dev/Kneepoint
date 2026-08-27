from importlib.metadata import version as pkg_version

from typer.testing import CliRunner

from kneepoint.cli import app


def test_version_flag_prints_version():
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    # compare against installed metadata, not a hardcoded string: a stale
    # editable install can lag pyproject, and CI installs fresh
    assert f"kneepoint {pkg_version('kneepoint')}" in result.output
