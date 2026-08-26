"""Schema loading, validation, and immutable JSON artifact writes."""

from __future__ import annotations

import json
import os
from fractions import Fraction
from pathlib import Path
import re
import tempfile
from typing import Final

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from .constants import SCHEMA_KINDS
from .errors import FrozenArtifactError


SCHEMA_DIRECTORY: Final = Path(__file__).resolve().parents[2] / "schemas"
FORMAT_CHECKER: Final = FormatChecker()
UTC_TIMESTAMP_RE: Final = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)


@FORMAT_CHECKER.checks("aspect-ratio-1-to-16")
def is_aspect_ratio_in_range(value: object) -> bool:
    """Accept positive integer ratios whose value is within 1:16 through 16:1."""

    if not isinstance(value, str):
        return False
    try:
        width_text, height_text = value.split(":", maxsplit=1)
        ratio = Fraction(int(width_text), int(height_text))
    except (TypeError, ValueError, ZeroDivisionError):
        return False
    return Fraction(1, 16) <= ratio <= Fraction(16, 1)


@FORMAT_CHECKER.checks("utc-date-time")
def is_valid_utc_timestamp(value: object) -> bool:
    """Accept only calendar-valid ISO 8601 timestamps written with a trailing Z."""

    if not isinstance(value, str) or not UTC_TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        from datetime import datetime

        datetime.fromisoformat(value[:-1])
    except ValueError:
        return False
    return True


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

    validator = validator_for(load_schema(schema_name))
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        raise errors[0]


def validator_for(schema: object) -> Draft202012Validator:
    """Build the one validator configuration used by library and CLI callers."""

    return Draft202012Validator(schema, format_checker=FORMAT_CHECKER)


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
