"""`docs/output-format.md` is the contract other people's tools read, so this
pins the page to the code: every field table lists exactly the model's fields,
in the order they are written, with the type the model declares; the sidecar
example is a document the reader would accept; the published `tpot_ms`
expression is the shipped property; and every enumerated value the page names
is one the code can actually produce.

The page is parsed, not quoted — if a field is added to a record and not to
its table (or the other way round), this fails.
"""

import json
import re
import types
import typing
from pathlib import Path

from pydantic import BaseModel
from typer.main import get_command

from kneepoint.chaos.faults import LLM_FAULTS, STANDARD_PROFILE, TOOL_FAULTS, FaultType
from kneepoint.cli import app
from kneepoint.collect.schemas import RequestRecord, RunMetadata, SessionRecord
from kneepoint.collect.writer import JsonlWriter, read_run_meta, write_run_meta
from kneepoint.config import ChaosConfig

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/output-format.md"
TEXT = DOC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# reading the page
# ---------------------------------------------------------------------------


def _section(heading: str) -> str:
    """The text under one `## heading`, up to the next `## `."""
    marker = f"\n## {heading}"
    start = TEXT.index(marker)
    body = TEXT[start + 1:]
    nxt = body.find("\n## ", 1)
    return body if nxt == -1 else body[:nxt]


REQUEST = _section("`run-<id>.jsonl`")
SESSION = _section("`run-<id>-sessions.jsonl`")
META = _section("`run-<id>-meta.json`")


def _rows(section: str) -> list[tuple[list[str], str, str]]:
    """Each field-table row as (backticked names, type cell, notes cell)."""
    rows = []
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        rows.append((re.findall(r"`([a-z_0-9]+)`", cells[0]), cells[1], cells[2]))
    return rows


def _documented(section: str) -> list[tuple[str, str]]:
    """(field, type) in table order. The sessions and sidecar tables fold the two
    version stamps into one 'as above' row; those take the request table's types."""
    stamps = {name: kind for names, kind, _ in _rows(REQUEST) for name in names if kind}
    out = []
    for names, kind, _ in _rows(section):
        for name in names:
            out.append((name, kind or stamps[name]))
    return out


def _notes(section: str, field: str) -> str:
    return next(notes for names, _, notes in _rows(section) if names == [field])


def _render(annotation) -> str:
    """The page's type vocabulary for a pydantic annotation."""
    origin = typing.get_origin(annotation)
    if origin in (types.UnionType, typing.Union):
        inner = [a for a in typing.get_args(annotation) if a is not type(None)]
        assert len(inner) == 1, annotation
        return f"{_render(inner[0])} | null"
    if origin is list:
        return f"{_render(typing.get_args(annotation)[0])}[]"
    if annotation is str:
        return "string"
    if annotation in (int, float, bool):
        return annotation.__name__
    if annotation is dict or (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
        return "object"
    raise AssertionError(f"no documented spelling for {annotation!r}")


def _modelled(model: type[BaseModel]) -> list[tuple[str, str]]:
    return [(name, _render(field.annotation)) for name, field in model.model_fields.items()]


# ---------------------------------------------------------------------------
# the three field tables are the three models, exactly
# ---------------------------------------------------------------------------


def test_request_table_is_the_request_record_in_written_order():
    assert _documented(REQUEST) == _modelled(RequestRecord)


def test_sessions_table_is_the_session_record_in_written_order():
    assert _documented(SESSION) == _modelled(SessionRecord)


def test_sidecar_table_is_the_run_metadata_in_written_order():
    assert _documented(META) == _modelled(RunMetadata)


def test_every_field_row_has_notes():
    for section in (REQUEST, SESSION, META):
        for names, _, notes in _rows(section):
            assert notes, f"{names} has an empty notes cell"


# ---------------------------------------------------------------------------
# what the writer puts on disk is what the tables say, key for key
# ---------------------------------------------------------------------------


def _meta_example() -> dict:
    block = re.search(r"```json\n(.*?)```", META, re.S).group(1)
    return json.loads(block)


def test_written_lines_carry_exactly_the_documented_keys(tmp_path):
    with JsonlWriter(tmp_path / "r.jsonl") as w:
        w.write_many([RequestRecord(
            session_id="s", concurrency=1, started_at=0.0, total_ms=1.0, ok=True,
        )])
    with JsonlWriter(tmp_path / "s.jsonl") as w:
        w.write_many([SessionRecord(
            session_id="s", concurrency=1, started_at=0.0, total_ms=1.0,
            turns_requested=1, turns_completed=1, ok=True,
        )])
    request_keys = list(json.loads((tmp_path / "r.jsonl").read_text(encoding="utf-8")))
    session_keys = list(json.loads((tmp_path / "s.jsonl").read_text(encoding="utf-8")))
    assert request_keys == [name for name, _ in _documented(REQUEST)]
    assert session_keys == [name for name, _ in _documented(SESSION)]

    path = write_run_meta(tmp_path / "m.json", RunMetadata.model_validate(_meta_example()))
    assert list(json.loads(path.read_text(encoding="utf-8"))) == [
        name for name, _ in _documented(META)
    ]


def test_sidecar_example_is_a_document_the_reader_accepts(tmp_path):
    """The example is complete and valid, not an illustration with `...` in it —
    and its `standard` profile lists exactly the faults `--chaos standard` runs."""
    example = _meta_example()
    meta = RunMetadata.model_validate(example)
    assert meta.command == "run"
    assert meta.chaos.profile == "standard"
    assert meta.chaos.faults == STANDARD_PROFILE
    assert [name for name, _ in _documented(META)] == list(example)
    # round-trips through the real writer and reader unchanged, stamps aside
    back = read_run_meta(write_run_meta(tmp_path / "m.json", meta))
    assert back.model_dump(exclude={"schema_version", "kneepoint_version"}) == \
        meta.model_dump(exclude={"schema_version", "kneepoint_version"})


# ---------------------------------------------------------------------------
# the published expressions and enumerations are the shipped ones
# ---------------------------------------------------------------------------


def test_published_tpot_expression_is_the_property():
    """The page prints the expression third parties should reimplement. Evaluate
    the page's own text against the property over every guard case."""
    block = re.search(r"```python\n(.*?)```", REQUEST, re.S).group(1)
    cases = [
        dict(ttft_ms=200.0, output_tokens=40, total_ms=1000.0),
        dict(ttft_ms=None, output_tokens=40, total_ms=1000.0),
        dict(ttft_ms=200.0, output_tokens=None, total_ms=1000.0),
        dict(ttft_ms=200.0, output_tokens=0, total_ms=1000.0),
        dict(ttft_ms=1000.0, output_tokens=40, total_ms=1000.0),   # decode == 0
        dict(ttft_ms=1200.0, output_tokens=40, total_ms=1000.0),   # decode < 0
        dict(ttft_ms=1.0, output_tokens=1, total_ms=1000.0),
    ]
    for case in cases:
        record = RequestRecord(session_id="s", concurrency=1, started_at=0.0, ok=True, **case)
        assert eval(block, {"__builtins__": {}}, dict(case)) == record.tpot_ms, case


def test_fault_values_named_for_request_lines_are_the_llm_faults():
    """Only LLM faults ride the response; tool faults come through the proxy log."""
    named = set(re.findall(r"`([a-z_]+)`", _notes(REQUEST, "fault")))
    assert LLM_FAULTS <= named
    assert not (TOOL_FAULTS & named)


def test_chaos_row_names_every_fault_type_and_every_profile():
    notes = _notes(META, "chaos")
    named = set(re.findall(r"`([a-z_*]+)`", notes))
    assert set(typing.get_args(FaultType)) <= named
    scenario_profiles = set(typing.get_args(ChaosConfig.model_fields["profile"].annotation))
    assert scenario_profiles | {"demo"} <= named


def test_resolution_methods_are_the_ones_the_judges_write():
    documented = set(re.findall(r"`([a-z_]+)`", _notes(SESSION, "resolution_method"))) - {"null"}
    written = set()
    for path in (ROOT / "kneepoint" / "judge").glob("*.py"):
        written |= set(re.findall(r'resolution_method = "(\w+)"', path.read_text(encoding="utf-8")))
    assert documented == written
    assert written  # the grep found the assignments, not an empty set on both sides


def test_command_values_are_what_run_and_demo_stamp():
    documented = set(re.findall(r"`([a-z]+)`", _notes(META, "command")))
    assert documented == {"run", "demo"}
    cli = (ROOT / "kneepoint" / "cli.py").read_text(encoding="utf-8")
    demo = (ROOT / "kneepoint" / "demo" / "run.py").read_text(encoding="utf-8")
    assert 'command="run"' in cli and 'command="demo"' in demo


def test_report_usage_line_names_real_flags():
    section = _section("Re-rendering the report")
    usage = re.search(r"```\n(kneepoint report .*?)\n```", section, re.S).group(1)
    report = get_command(app).commands["report"]
    real = {opt for param in report.params for opt in param.opts}
    for flag in re.findall(r"--[a-z-]+", usage):
        assert flag in real, f"{flag} is not an option of `kneepoint report`"
