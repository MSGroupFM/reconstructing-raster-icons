"""Regression coverage for the versioned JSON contract layer."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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

    def test_component_and_svg_ids_are_unique_across_map_records(self) -> None:
        for make_document, schema_name in (
            (draft_fixture, "reconstruction-map-draft"),
            (frozen_map_fixture, "reconstruction-map"),
        ):
            document = make_document()
            document["components"].append(copy.deepcopy(document["components"][0]))
            with self.assertRaises(jsonschema.ValidationError):
                validate_document(document, schema_name)

    def test_semantic_artifact_evidence_requires_hash_and_strict_logical_id(self) -> None:
        for evidence in (
            {"basis": "checked", "artifact_id": "candidate-i00"},
            {"artifact_id": "../../preview.png", "sha256": "0" * 64},
            {"artifact_id": "folder/preview", "sha256": "0" * 64},
        ):
            review = semantic_review_fixture()
            review["gates"][0]["evidence"] = evidence
            with self.assertRaises(jsonschema.ValidationError):
                validate_document(review, "semantic-review")

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
            for ratio, width, height, raster_width, raster_height in (
                ("1:16", 4, 64, 64, 1024),
                ("16:1", 64, 4, 1024, 64),
                ("2:16", 8, 64, 128, 1024),
            ):
                document = make_document()
                document["viewport"]["aspect_ratio"] = ratio  # type: ignore[index]
                document["viewport"]["view_box"] = [0, 0, width, height]  # type: ignore[index]
                document["canonical_canvas"]["width"] = width  # type: ignore[index]
                document["canonical_canvas"]["height"] = height  # type: ignore[index]
                document["canonical_canvas"]["raster_width"] = raster_width  # type: ignore[index]
                document["canonical_canvas"]["raster_height"] = raster_height  # type: ignore[index]
                validate_document(document, schema_name)
            for ratio in ("96:1", "1:96", "0:1", "1:0"):
                document = make_document()
                document["viewport"]["aspect_ratio"] = ratio  # type: ignore[index]
                with self.assertRaises(jsonschema.ValidationError, msg=f"{schema_name}: {ratio}"):
                    validate_document(document, schema_name)

    def test_custom_ratio_uses_the_default_64_max_side_grid(self) -> None:
        for make_document, schema_name in (
            (draft_fixture, "reconstruction-map-draft"),
            (frozen_map_fixture, "reconstruction-map"),
        ):
            document = make_document()
            document["viewport"]["aspect_ratio"] = "5:7"  # type: ignore[index]
            document["viewport"]["grid"] = 64  # type: ignore[index]
            document["viewport"]["view_box"] = [0, 0, 45.714286, 64]  # type: ignore[index]
            document["canonical_canvas"] = {  # type: ignore[index]
                "width": 45.714286,
                "height": 64,
                "raster_width": 731,
                "raster_height": 1024,
            }
            validate_document(document, schema_name)

    def test_source_color_scope_uses_a_structured_merge_confirmation(self) -> None:
        for make_document, schema_name in (
            (draft_fixture, "reconstruction-map-draft"),
            (frozen_map_fixture, "reconstruction-map"),
        ):
            document = make_document()
            document["source_color_scope"] = {  # type: ignore[index]
                "classification": "meaningful_multicolor",
                "merge_to_monochrome": {
                    "decision": "merge colors into one silhouette",
                    "confirmed": True,
                    "confirmed_at": "2026-08-26T00:00:00Z",
                },
            }
            validate_document(document, schema_name)
            document["source_color_scope"]["merge_to_monochrome"] = {"confirmed": True}  # type: ignore[index]
            with self.assertRaises(jsonschema.ValidationError):
                validate_document(document, schema_name)

    def test_explicit_override_confirmation_is_required_in_every_contract(self) -> None:
        cases = (
            (draft_fixture, "reconstruction-map-draft"),
            (frozen_map_fixture, "reconstruction-map"),
            (accepted_report_fixture, "acceptance-report"),
        )
        for make_document, schema_name in cases:
            document = make_document()
            if schema_name != "acceptance-report":
                document["source_color_scope"] = {  # type: ignore[index]
                    "classification": "monochrome", "merge_to_monochrome": None,
                }
            normalization = document["normalization"]  # type: ignore[index]
            normalization["estimator_basis"] = "explicit_override"
            normalization["explicit_overrides"] = {
                "background_luminance": 1,
                "foreground_luminance": 0,
                "reason": "needed for ambiguous source",
            }
            with self.assertRaises(jsonschema.ValidationError, msg=schema_name):
                validate_document(document, schema_name)

    def test_draft_and_frozen_map_require_source_color_scope(self) -> None:
        for make_document, schema_name in (
            (draft_fixture, "reconstruction-map-draft"),
            (frozen_map_fixture, "reconstruction-map"),
        ):
            document = make_document()
            del document["source_color_scope"]
            with self.assertRaises(jsonschema.ValidationError, msg=schema_name):
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

    def test_accepted_report_requires_raw_target_and_metric_consistency(self) -> None:
        report = accepted_report_fixture()
        for raw_name, rounded_name in (
            ("silhouette_raw", "silhouette"),
            ("contour_raw", "contour"),
            ("layout_raw", "layout"),
            ("topology_raw", "topology"),
        ):
            report["metrics"][raw_name] = 0  # type: ignore[index]
            report["metrics"][rounded_name] = 0  # type: ignore[index]
        report["metrics"]["composite_raw"] = 0  # type: ignore[index]
        report["metrics"]["composite"] = 0  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

        report = accepted_report_fixture()
        report["metrics"]["silhouette_raw"] = 98.125  # type: ignore[index]
        report["metrics"]["silhouette"] = 98.12  # type: ignore[index]
        report["metrics"]["composite_raw"] = 99.15625  # type: ignore[index]
        report["metrics"]["composite"] = 99.16  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

        report = accepted_report_fixture()
        report["status"] = "not_accepted"
        report["target_met"] = False
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

    def test_map_contract_includes_canonical_reference_detail(self) -> None:
        for make_document, schema_name in (
            (draft_fixture, "reconstruction-map-draft"),
            (frozen_map_fixture, "reconstruction-map"),
        ):
            document = make_document()
            for field in ("canonical_canvas", "applicable_gates", "ambiguities"):
                self.assertIn(field, document)
                missing = make_document()
                del missing[field]
                with self.assertRaises(jsonschema.ValidationError):
                    validate_document(missing, schema_name)
            component = document["components"][0]  # type: ignore[index]
            for field in ("expected_hole_count", "applicable_gates"):
                self.assertIn(field, component)
            self.assertIsInstance(document["geometry_constraints"], dict)

        document = draft_fixture()
        constraints = document["geometry_constraints"]  # type: ignore[assignment]
        constraints["endpoints"].append(  # type: ignore[index]
            {"component_id": "mark", "start": [0, 0], "end": [1, 1], "tolerance": 0.01}
        )
        validate_document(document, "reconstruction-map-draft")
        constraints["endpoints"][0]["unexpected"] = True  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(document, "reconstruction-map-draft")

    def test_universal_line_relation_constraints_are_schema_expressible(self) -> None:
        for make_document, schema_name in (
            (draft_fixture, "reconstruction-map-draft"),
            (frozen_map_fixture, "reconstruction-map"),
        ):
            document = make_document()
            constraints = document["geometry_constraints"]  # type: ignore[assignment]
            constraints["lines"].append(  # type: ignore[index]
                {"component_id": "mark", "start": [0, 0], "end": [1, 0], "tolerance": 0.01}
            )
            constraints["orthogonality"].append(  # type: ignore[index]
                {"first": "mark", "second": "mark", "tolerance": 0.01}
            )
            constraints["parallelism"].append(  # type: ignore[index]
                {"first": "mark", "second": "mark", "tolerance": 0.01}
            )

            validate_document(document, schema_name)

            constraints["lines"][0]["stand"] = True  # type: ignore[index]
            with self.assertRaises(jsonschema.ValidationError):
                validate_document(document, schema_name)

    def test_radial_constraint_variants_are_mutually_exclusive(self) -> None:
        for make_document, schema_name in (
            (draft_fixture, "reconstruction-map-draft"),
            (frozen_map_fixture, "reconstruction-map"),
        ):
            circle = make_document()
            circle["geometry_constraints"]["radial"] = [  # type: ignore[index]
                {"component_id": "mark", "geometry": "circle", "center": [0.5, 0.5], "radius": 1, "tolerance": 0}
            ]
            validate_document(circle, schema_name)

            ellipse = make_document()
            ellipse["geometry_constraints"]["radial"] = [  # type: ignore[index]
                {"component_id": "mark", "geometry": "ellipse", "center": [0.5, 0.5], "radius_x": 1, "radius_y": 2, "tolerance": 0}
            ]
            validate_document(ellipse, schema_name)

            for invalid_radial in (
                {"component_id": "mark", "geometry": "circle", "center": [0.5, 0.5], "radius": 1, "radius_x": 1, "tolerance": 0},
                {"component_id": "mark", "geometry": "ellipse", "center": [0.5, 0.5], "radius": 1, "radius_x": 1, "radius_y": 2, "tolerance": 0},
                {"component_id": "mark", "geometry": "ellipse", "center": [0.5, 0.5], "radius_x": 1, "tolerance": 0},
            ):
                invalid = make_document()
                invalid["geometry_constraints"]["radial"] = [invalid_radial]  # type: ignore[index]
                with self.subTest(schema=schema_name, radial=invalid_radial), self.assertRaises(jsonschema.ValidationError):
                    validate_document(invalid, schema_name)

    def test_acceptance_report_includes_runtime_viewport_and_topology_nodes(self) -> None:
        report = accepted_report_fixture()
        for field in ("canonical_renderer", "topology_nodes"):
            self.assertIn(field, report)
            missing = accepted_report_fixture()
            del missing[field]
            with self.assertRaises(jsonschema.ValidationError):
                validate_document(missing, "acceptance-report")
        for field in ("aspect_ratio", "grid", "canonical_canvas"):
            self.assertIn(field, report["viewport"])  # type: ignore[index]

    def test_gate_evidence_requires_text_or_artifact_hash(self) -> None:
        report = accepted_report_fixture()
        report["gates"][0]["evidence"] = {  # type: ignore[index]
            "artifact_id": "safe-parse",
            "sha256": "0" * 64,
        }
        validate_document(report, "acceptance-report")

        review = semantic_review_fixture()
        review["gates"][0]["evidence"] = {"basis": "review checklist"}  # type: ignore[index]
        validate_document(review, "semantic-review")

        review = semantic_review_fixture()
        review["gates"][0]["evidence"] = {  # type: ignore[index]
            "artifact_id": "review-checklist",
            "sha256": "0" * 64,
        }
        validate_document(review, "semantic-review")

        report = accepted_report_fixture()
        report["gates"][0]["evidence"] = {}  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

    def test_atomic_write_rejects_destination_created_during_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact.json"
            original_link = __import__("os").link

            def race_link(source: Path, target: Path) -> None:
                Path(target).write_text('{"winner": true}\n', encoding="utf-8")
                original_link(source, target)

            with mock.patch("reconstructing_raster_icons.schema_io.os.link", side_effect=race_link):
                with self.assertRaises(FrozenArtifactError):
                    atomic_write_json(destination, {"value": 1})
            self.assertEqual({"winner": True}, json.loads(destination.read_text(encoding="utf-8")))

    def test_normal_report_statuses_follow_mandatory_precedence(self) -> None:
        report = accepted_report_fixture()
        for raw_name, rounded_name in (
            ("silhouette_raw", "silhouette"),
            ("contour_raw", "contour"),
            ("layout_raw", "layout"),
            ("topology_raw", "topology"),
            ("composite_raw", "composite"),
        ):
            report["metrics"][raw_name] = 0  # type: ignore[index]
            report["metrics"][rounded_name] = 0  # type: ignore[index]
        report["target_met"] = False
        report["status"] = "incomplete"
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

        report = accepted_report_fixture()
        report["status"] = "not_accepted"
        report["gates"][-1]["state"] = "not_evaluated"  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

    def test_canonical_profile_is_pinned(self) -> None:
        report = accepted_report_fixture()
        report["canonical_renderer"]["runtime_version"] = "0"  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

        report = accepted_report_fixture()
        report["canonical_renderer"]["wasm_sha256"] = "f" * 64  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

        report = accepted_report_fixture()
        report["canonical_renderer"]["runner_sha256"] = "f" * 64  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

        report = accepted_report_fixture()
        report["canonical_renderer"]["resource_controls"]["v8_old_space_mib"] = 511  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

    def test_canvas_relationships_are_enforced(self) -> None:
        for make_document, schema_name in (
            (draft_fixture, "reconstruction-map-draft"),
            (frozen_map_fixture, "reconstruction-map"),
        ):
            document = make_document()
            document["canonical_canvas"]["height"] = 128  # type: ignore[index]
            with self.assertRaises(jsonschema.ValidationError):
                validate_document(document, schema_name)

        report = accepted_report_fixture()
        report["viewport"]["canonical_canvas"]["height"] = 128  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

        report = accepted_report_fixture()
        report["viewport"]["canonical_canvas"]["raster_width"] = 1025  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

    def test_raster_dimensions_follow_declared_aspect_ratio(self) -> None:
        for make_document, schema_name in (
            (draft_fixture, "reconstruction-map-draft"),
            (frozen_map_fixture, "reconstruction-map"),
        ):
            document = make_document()
            document["canonical_canvas"]["raster_height"] = 128  # type: ignore[index]
            with self.assertRaises(jsonschema.ValidationError):
                validate_document(document, schema_name)

        report = accepted_report_fixture()
        report["viewport"]["canonical_canvas"]["raster_height"] = 128  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

        document = draft_fixture()
        document["viewport"]["aspect_ratio"] = "2:16"  # type: ignore[index]
        document["viewport"]["view_box"] = [0, 0, 8, 64]  # type: ignore[index]
        document["canonical_canvas"] = {"width": 8, "height": 64, "raster_width": 128, "raster_height": 1024}
        validate_document(document, "reconstruction-map-draft")

    def test_grid_and_raster_dimensions_are_derived_from_ratio(self) -> None:
        cases = (
            ("1:1", 64, 64, 1024, 1024),
            ("3:2", 64, 42.666667, 1024, 683),
            ("2:3", 42.666667, 64, 683, 1024),
            ("16:9", 64, 36, 1024, 576),
            ("9:16", 36, 64, 576, 1024),
            ("7:11", 40.727273, 64, 652, 1024),
        )
        for make_document, schema_name in (
            (draft_fixture, "reconstruction-map-draft"),
            (frozen_map_fixture, "reconstruction-map"),
        ):
            for ratio, width, height, raster_width, raster_height in cases:
                document = make_document()
                document["viewport"]["aspect_ratio"] = ratio  # type: ignore[index]
                document["viewport"]["view_box"] = [0, 0, width, height]  # type: ignore[index]
                document["canonical_canvas"] = {  # type: ignore[index]
                    "width": width,
                    "height": height,
                    "raster_width": raster_width,
                    "raster_height": raster_height,
                }
                validate_document(document, schema_name)

        for ratio, width, height, raster_width, raster_height in cases:
            report = accepted_report_fixture()
            report["viewport"]["aspect_ratio"] = ratio  # type: ignore[index]
            report["viewport"]["view_box"] = [0, 0, width, height]  # type: ignore[index]
            report["viewport"]["canonical_canvas"] = {  # type: ignore[index]
                "width": width,
                "height": height,
                "raster_width": raster_width,
                "raster_height": raster_height,
            }
            validate_document(report, "acceptance-report")

    def test_composite_raw_uses_normative_float64_evaluation(self) -> None:
        import math

        report = accepted_report_fixture()
        report["accuracy_target"] = 0.01
        report["metrics"] = {  # type: ignore[index]
            "silhouette_raw": 9.26,
            "silhouette": 9.26,
            "contour_raw": 15,
            "contour": 15,
            "layout_raw": 13.9,
            "layout": 13.9,
            "topology_raw": 59.15,
            "topology": 59.15,
            "composite_raw": 16.666999999999998,
            "composite": 16.67,
        }
        validate_document(report, "acceptance-report")

        expected = 16.666999999999998
        for wrong_score in (math.nextafter(expected, -math.inf), math.nextafter(expected, math.inf), 16.5):
            report["metrics"]["composite_raw"] = wrong_score  # type: ignore[index]
            report["metrics"]["composite"] = 16.67 if wrong_score != 16.5 else 16.5  # type: ignore[index]
            with self.assertRaises(jsonschema.ValidationError):
                validate_document(report, "acceptance-report")

    def test_failed_topology_gate_records_observed_hole_mismatch(self) -> None:
        report = accepted_report_fixture()
        report["status"] = "not_accepted"
        for gate in report["gates"]:  # type: ignore[index]
            if gate["gate_id"] == "auto.topology.facts":
                gate["state"] = "fail"
        report["topology_nodes"][0]["hole_count"] = 1  # type: ignore[index]
        validate_document(report, "acceptance-report")

        report = accepted_report_fixture()
        report["topology_nodes"][0]["hole_count"] = 1  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

    def test_topology_nodes_cover_components_once_with_expected_holes(self) -> None:
        report = accepted_report_fixture()
        report["topology_nodes"] = []
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

        report = accepted_report_fixture()
        report["topology_nodes"][0]["component_id"] = "other"  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

        report = accepted_report_fixture()
        report["topology_nodes"].append(report["topology_nodes"][0].copy())  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

        report = accepted_report_fixture()
        report["topology_nodes"][0]["hole_count"] = 1  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

    def test_rounded_scores_are_numeric_not_lexical_precision(self) -> None:
        from decimal import Decimal

        report = accepted_report_fixture()
        report["metrics"]["silhouette"] = Decimal("100.000")  # type: ignore[index]
        validate_document(report, "acceptance-report")

        report = accepted_report_fixture()
        report["metrics"]["silhouette_raw"] = 98.125  # type: ignore[index]
        report["metrics"]["silhouette"] = 98.131  # type: ignore[index]
        report["metrics"]["composite_raw"] = 99.15625  # type: ignore[index]
        report["metrics"]["composite"] = 99.16  # type: ignore[index]
        with self.assertRaises(jsonschema.ValidationError):
            validate_document(report, "acceptance-report")

        with tempfile.TemporaryDirectory() as directory:
            equivalent = Path(directory) / "equivalent.json"
            equivalent.write_text(
                (FIXTURES / "valid-acceptance-report.json")
                .read_text(encoding="utf-8")
                .replace('"silhouette": 100', '"silhouette": 100.000', 1),
                encoding="utf-8",
            )
            accepted = subprocess.run(
                [sys.executable, "scripts/validate_schemas.py", "--schemas", "schemas", "--documents", str(equivalent)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, accepted.returncode)

            invalid = Path(directory) / "invalid.json"
            invalid.write_text(json.dumps(report), encoding="utf-8")
            rejected = subprocess.run(
                [sys.executable, "scripts/validate_schemas.py", "--schemas", "schemas", "--documents", str(invalid)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(2, rejected.returncode)


if __name__ == "__main__":
    unittest.main()
