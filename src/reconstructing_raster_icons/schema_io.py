"""Schema loading, validation, and immutable JSON artifact writes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Final

from jsonschema import Draft202012Validator, ValidationError

from .constants import SCHEMA_KINDS
from .errors import FrozenArtifactError


SCHEMA_DIRECTORY: Final = Path(__file__).resolve().parents[2] / "schemas"


def _schema_path(schema_name: str) -> Path:
    if schema_name not in SCHEMA_KINDS:
        raise ValueError(f"unknown schema kind: {schema_name}")
    return SCHEMA_DIRECTORY / f"{schema_name}.schema.json"


def load_schema(schema_name: str, schemas_dir: Path | None = None) -> dict[str, object]:
    """Load and meta-validate a named Draft 2020-12 schema."""

    if schema_name not in SCHEMA_KINDS:
        raise ValueError(f"unknown schema kind: {schema_name}")
    path = (schemas_dir / f"{schema_name}.schema.json") if schemas_dir else _schema_path(schema_name)
    with path.open(encoding="utf-8") as source:
        schema = json.load(source)
    Draft202012Validator.check_schema(schema)
    return schema


def validate_document(document: object, schema_name: str) -> None:
    """Raise ``ValidationError`` when *document* violates its named contract."""

    validator = Draft202012Validator(load_schema(schema_name))
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        raise errors[0]


def atomic_write_json(path: Path, document: object) -> None:
    """Create a JSON artifact atomically without replacing a frozen destination."""

    destination = Path(path)
    if destination.exists():
        raise FrozenArtifactError(f"refusing to overwrite frozen artifact: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(document, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if destination.exists():
            raise FrozenArtifactError(f"refusing to overwrite frozen artifact: {destination}")
        os.replace(temporary, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
