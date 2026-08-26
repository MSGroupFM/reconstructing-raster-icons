"""Regression coverage for the versioned JSON contract layer."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reconstructing_raster_icons.schema_io import validate_document

import jsonschema


FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def load_fixture(name: str) -> dict[str, object]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def accepted_report_fixture() -> dict[str, object]:
    return copy.deepcopy(load_fixture("valid-acceptance-report.json"))


def frozen_map_fixture() -> dict[str, object]:
    return copy.deepcopy(load_fixture("valid-map.json"))


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


if __name__ == "__main__":
    unittest.main()
