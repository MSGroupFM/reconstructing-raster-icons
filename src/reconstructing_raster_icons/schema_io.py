"""Schema loading, validation, and immutable JSON artifact writes."""

from __future__ import annotations

import json
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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

    validate_instance(document, load_schema(schema_name))


def validate_instance(document: object, schema: object) -> None:
    """Validate a document with the common schema, format, and contract checks."""

    validator = validator_for(schema)
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        raise errors[0]
    if isinstance(document, dict) and document.get("schema_kind") == "acceptance-report":
        _validate_acceptance_coherence(document)


def _decimal(value: object, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValidationError(f"{field} must be a finite decimal number") from error


def _validate_acceptance_coherence(report: dict[str, object]) -> None:
    """Enforce acceptance-model relationships that JSON Schema cannot express."""

    metrics = report["metrics"]
    gates = report["gates"]
    if not isinstance(metrics, dict) or not isinstance(gates, list):
        raise ValidationError("acceptance report metrics and gates must be structured values")

    pairs = (
        ("silhouette_raw", "silhouette"),
        ("contour_raw", "contour"),
        ("layout_raw", "layout"),
        ("topology_raw", "topology"),
        ("composite_raw", "composite"),
    )
    raw_scores: dict[str, Decimal] = {}
    for raw_name, rounded_name in pairs:
        raw_score = _decimal(metrics[raw_name], raw_name)
        rounded_score = _decimal(metrics[rounded_name], rounded_name)
        if rounded_score.as_tuple().exponent < -2:
            raise ValidationError(f"{rounded_name} must have at most two decimal places")
        if raw_score.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != rounded_score:
            raise ValidationError(f"{rounded_name} must be {raw_name} rounded half-up to two decimals")
        raw_scores[raw_name] = raw_score

    expected_composite = (
        Decimal("0.45") * raw_scores["silhouette_raw"]
        + Decimal("0.30") * raw_scores["contour_raw"]
        + Decimal("0.15") * raw_scores["layout_raw"]
        + Decimal("0.10") * raw_scores["topology_raw"]
    )
    if raw_scores["composite_raw"] != expected_composite:
        raise ValidationError("composite_raw must equal the specified weighted raw metric sum")

    target_met = raw_scores["composite_raw"] >= _decimal(report["accuracy_target"], "accuracy_target")
    if report["target_met"] != target_met:
        raise ValidationError("target_met must equal composite_raw >= accuracy_target")

    all_gates_pass = all(isinstance(gate, dict) and gate.get("state") == "pass" for gate in gates)
    accepted = target_met and report["canonical_environment"] is True and all_gates_pass
    if (report["status"] == "accepted") != accepted:
        raise ValidationError("accepted status requires and is required by target, canonical environment, and passing gates")


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
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise FrozenArtifactError(f"refusing to overwrite frozen artifact: {destination}") from error
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
            temporary.unlink()
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
