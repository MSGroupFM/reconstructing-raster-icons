"""Regression coverage for the versioned JSON contract layer."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reconstructing_raster_icons.errors import FrozenArtifactError
from reconstructing_raster_icons.schema_io import atomic_write_json, validate_document

import jsonschema


FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def load_fixture(name: str) -> dict[str, object]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def accepted_report_fixture() -> dict[str, object]:
    return copy.deepcopy(load_fixture("valid-acceptance-report.json"))


def frozen_map_fixture() -> dict[str, object]:
    return copy.deepcopy(load_fixture("valid-map.json"))


def draft_fixture() -> dict[str, object]:
    return copy.deepcopy(load_fixture("valid-draft.json"))


def semantic_review_fixture() -> dict[str, object]:
    return copy.deepcopy(load_fixture("valid-semantic-review.json"))


class SchemaContractTests(unittest.TestCase):
    def test_draft_requires_confirmed_accuracy_and_components(self) -> None:
        draft = {"schema_version": "1.0.0"}
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(draft, "reconstruction-map-draft")

    def test_accepted_report_rejects_not_evaluated_gate(self) -> None:
        report = accepted_report_fixture()
        report["gates"][0]["state"] = "not_evaluated"  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

    def test_frozen_map_requires_refinement_limit(self) -> None:
        document = frozen_map_fixture()
        del document["refinement_limit"]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(document, "reconstruction-map")

    def test_maps_enforce_actual_aspect_ratio_range(self) -> None:
        cases = (
            (draft_fixture, "reconstruction-map-draft"),
            (frozen_map_fixture, "reconstruction-map"),
        )
        for make_document, schema_name in cases:
            for ratio in ("1:16", "16:1", "2:16"):
                document = make_document()
                document["viewport"]["aspect_ratio"] = ratio  # type: ignore[index]
                validate_document(document, schema_name)
            for ratio in ("96:1", "1:96", "0:1", "1:0"):
                document = make_document()
                document["viewport"]["aspect_ratio"] = ratio  # type: ignore[index]
                with self.assertRaises(jsonschema.ValidationError, msg=f"{schema_name}: {ratio}"):
                    validate_document(document, schema_name)

    def test_required_timestamps_are_valid_utc_z_values(self) -> None:
        cases = (
            (draft_fixture, "reconstruction-map-draft", "created_at"),
            (frozen_map_fixture, "reconstruction-map", "frozen_at"),
            (semantic_review_fixture, "semantic-review", "reviewed_at"),
            (accepted_report_fixture, "acceptance-report", "created_at"),
        )
        for make_document, schema_name, field in cases:
            for timestamp in ("not-a-date", "2026-08-26T00:00:00+00:00"):
                document = make_document()
                document[field] = timestamp
                with self.assertRaises(jsonschema.ValidationError, msg=f"{schema_name}: {timestamp}"):
                    validate_document(document, schema_name)

    def test_required_timestamps_enforce_calendar_validity_and_leap_days(self) -> None:
        document = draft_fixture()
        document["created_at"] = "2024-02-29T00:00:00Z"
        validate_document(document, "reconstruction-map-draft")

        for timestamp in ("2026-02-30T00:00:00Z", "2025-02-29T00:00:00Z"):
            document = draft_fixture()
            document["created_at"] = timestamp
            with self.assertRaises(jsonschema.ValidationError, msg=timestamp):
                validate_document(document, "reconstruction-map-draft")

    def test_gate_catalog_rejects_duplicate_stable_ids(self) -> None:
        report = accepted_report_fixture()
        report["gates"][-1]["gate_id"] = report["gates"][0]["gate_id"]  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

        review = semantic_review_fixture()
        review["gates"][-1]["gate_id"] = review["gates"][0]["gate_id"]  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(review, "semantic-review")

    def test_nested_unknown_fields_are_rejected(self) -> None:
        document = draft_fixture()
        document["normalization"]["unexpected"] = True  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(document, "reconstruction-map-draft")

    def test_automatic_gate_rejects_not_evaluated_in_nonaccepted_report(self) -> None:
        report = accepted_report_fixture()
        report["status"] = "not_accepted"
        report["gates"][0]["state"] = "not_evaluated"  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

    def test_cli_routes_schema_kind_and_exits_two_for_invalid_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            document = Path(directory) / "invalid.json"
            document.write_text(json.dumps({"schema_kind": "acceptance-report"}), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "scripts/validate_schemas.py", "--schemas", "schemas", "--documents", str(document)],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(2, result.returncode)
        self.assertIn('"valid": false', result.stdout)

    def test_atomic_write_serializes_valid_json_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact.json"
            atomic_write_json(destination, {"value": 1})
            self.assertEqual({"value": 1}, json.loads(destination.read_text(encoding="utf-8")))
            with self.assertRaises(FrozenArtifactError):
                atomic_write_json(destination, {"value": 2})
            self.assertEqual({"value": 1}, json.loads(destination.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
