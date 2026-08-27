"""Append-only JSONL sink and reader for RequestRecords, plus the run's metadata
sidecar (docs/output-format.md)."""

import json
import platform
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

from pydantic import BaseModel

from kneepoint.collect.schemas import SCHEMA_VERSION, Environment, RequestRecord, RunMetadata


def _kneepoint_version() -> str | None:
    try:
        return pkg_version("kneepoint")
    except PackageNotFoundError:  # running from a source tree, not an install
        return None


KNEEPOINT_VERSION = _kneepoint_version()


class JsonlWriter:
    """Every line leaves here stamped with the format version that wrote it.

    Stamping at the writer rather than at record construction means no producer
    can forget: the file is the contract, and this is the one door out to it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")

    def write_many(self, records: list[BaseModel]) -> None:
        for record in records:
            stamped = record.model_copy(update={
                "schema_version": SCHEMA_VERSION,
                "kneepoint_version": KNEEPOINT_VERSION,
            })
            self._fh.write(stamped.model_dump_json() + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_jsonl(path: Path, cls: type[BaseModel] = RequestRecord) -> list:
    """Read a JSONL of records. Files written before versioning parse as v0."""
    with path.open(encoding="utf-8") as fh:
        return [cls.model_validate(json.loads(line)) for line in fh if line.strip()]


def current_environment() -> Environment:
    """The interpreter and OS the run happened on — read from the machine, never typed."""
    return Environment(python=platform.python_version(), platform=platform.platform())


def write_run_meta(path: Path, meta: RunMetadata) -> Path:
    """Write the `run-<id>-meta.json` sidecar, stamped exactly like the JSONL lines.

    One indented JSON document — a single record a human will open, not a stream
    anyone tails. The caller owns the timestamps; this is the one door out, so the
    version stamps cannot be forgotten.
    """
    stamped = meta.model_copy(update={
        "schema_version": SCHEMA_VERSION,
        "kneepoint_version": KNEEPOINT_VERSION,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stamped.model_dump(), indent=2) + "\n", encoding="utf-8")
    return path


def read_run_meta(path: Path) -> RunMetadata:
    """Read a metadata sidecar. Callers decide what an absent file means (unknown)."""
    return RunMetadata.model_validate(json.loads(path.read_text(encoding="utf-8")))
