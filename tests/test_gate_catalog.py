from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reconstructing_raster_icons.constants import (  # noqa: E402
    AUTOMATIC_GATE_IDS,
    MANDATORY_GATE_IDS,
    SEMANTIC_GATE_IDS,
    ExitCode,
    Status,
)
from reconstructing_raster_icons.reports import (  # noqa: E402
    ArtifactEvidence,
    GateEvidence,
    GateResult,
    MeasurementEvidence,
    evaluate_automatic_gates,
    resolve_status,
)
from reconstructing_raster_icons.schema_io import validate_document  # noqa: E402


NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)
SHA256 = "a" * 64


def evidence(*, geometry: bool = False) -> GateEvidence:
    return GateEvidence(
        artifacts=(ArtifactEvidence(logical_id="candidate.svg", sha256=SHA256),),
        measurements=(
            (MeasurementEvidence(name="deviation", measured=0.25, tolerance=1.0, unit="canonical_px"),)
            if geometry
            else ()
        ),
        basis="deterministic fixture",
    )


def automatic_checks() -> dict[str, dict[str, object]]:
    return {
        gate_id: {
            "passed": True,
            "evidence": evidence(geometry=gate_id in {"auto.primitives.constraints", "auto.paths.integrity"}),
        }
        for gate_id in AUTOMATIC_GATE_IDS
    }


def complete_gates() -> tuple[GateResult, ...]:
    automatic = evaluate_automatic_gates(automatic_checks(), evaluator="test-suite", timestamp=NOW)
    semantic = tuple(
        GateResult(gate_id, "semantic", "pass", evidence(), "reviewer", NOW)
        for gate_id in SEMANTIC_GATE_IDS
    )
    return automatic + semantic


class GateCatalogTests(unittest.TestCase):
    def test_catalog_contains_exactly_nine_automatic_and_seven_semantic_ids_once(self) -> None:
        self.assertEqual(len(AUTOMATIC_GATE_IDS), 9)
        self.assertEqual(len(SEMANTIC_GATE_IDS), 7)
        self.assertEqual(len(MANDATORY_GATE_IDS), 16)
        self.assertEqual(len(set(MANDATORY_GATE_IDS)), 16)
        self.assertTrue(all(gate_id.startswith("auto.") for gate_id in AUTOMATIC_GATE_IDS))
        self.assertTrue(all(gate_id.startswith("semantic.") for gate_id in SEMANTIC_GATE_IDS))

    def test_automatic_gate_rejects_not_evaluated(self) -> None:
        with self.assertRaisesRegex(ValueError, "automatic.*pass or fail"):
            GateResult("auto.svg.render", "automatic", "not_evaluated", evidence(), "renderer", NOW)

    def test_gate_result_rejects_naive_or_non_utc_timestamp(self) -> None:
        with self.assertRaisesRegex(ValueError, "UTC"):
            GateResult("auto.svg.render", "automatic", "pass", evidence(), "renderer", datetime(2026, 8, 26))

    def test_gate_result_rejects_malformed_public_boundary_types(self) -> None:
        valid = ("auto.svg.render", "automatic", "pass", evidence(), "renderer", NOW)
        mutations = (
            ([], *valid[1:]),
            (valid[0], [], *valid[2:]),
            (*valid[:2], [], *valid[3:]),
            (*valid[:3], {}, *valid[4:]),
            (*valid[:4], 1, valid[5]),
            (*valid[:5], "2026-08-26T00:00:00Z"),
        )

        for arguments in mutations:
            with self.subTest(arguments=arguments), self.assertRaises((TypeError, ValueError)):
                GateResult(*arguments)  # type: ignore[arg-type]

    def test_automatic_evaluation_requires_every_id_exactly_once(self) -> None:
        checks = automatic_checks()
        checks.pop("auto.svg.render")
        with self.assertRaisesRegex(ValueError, "exact automatic gate catalog"):
            evaluate_automatic_gates(checks, evaluator="test-suite", timestamp=NOW)

    def test_geometry_gate_state_cannot_contradict_measurement_evidence(self) -> None:
        checks = automatic_checks()
        checks["auto.primitives.constraints"] = {
            "passed": True,
            "evidence": GateEvidence(
                artifacts=(ArtifactEvidence(logical_id="candidate.svg", sha256=SHA256),),
                measurements=(
                    MeasurementEvidence(name="deviation", measured=2.0, tolerance=1.0, unit="canonical_px"),
                ),
                basis="contradictory caller flag",
            ),
        }

        gates = evaluate_automatic_gates(checks, evaluator="test-suite", timestamp=NOW)
        primitive = next(gate for gate in gates if gate.gate_id == "auto.primitives.constraints")

        self.assertEqual(primitive.state, "fail")

    def test_nested_evidence_item_types_are_enforced_at_construction(self) -> None:
        with self.assertRaises(TypeError):
            GateEvidence(artifacts=("not-artifact-evidence",))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            GateEvidence(measurements=(object(),))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            MeasurementEvidence(name="deviation", measured=-0.1, tolerance=1.0, unit="canonical_px")

    def test_geometry_gate_evidence_is_typed_and_contains_deviation_and_tolerance(self) -> None:
        gates = evaluate_automatic_gates(automatic_checks(), evaluator="test-suite", timestamp=NOW)
        primitive = next(gate for gate in gates if gate.gate_id == "auto.primitives.constraints")

        self.assertIsInstance(primitive.evidence, GateEvidence)
        self.assertEqual(primitive.evidence.measurements[0].measured, 0.25)
        self.assertEqual(primitive.evidence.measurements[0].tolerance, 1.0)

    def test_static_gate_evidence_has_logical_artifact_id_and_sha256(self) -> None:
        gates = evaluate_automatic_gates(automatic_checks(), evaluator="test-suite", timestamp=NOW)
        safe_subset = next(gate for gate in gates if gate.gate_id == "auto.svg.safe_subset")

        self.assertEqual(safe_subset.evidence.artifacts[0].logical_id, "candidate.svg")
        self.assertEqual(safe_subset.evidence.artifacts[0].sha256, SHA256)

    def test_serialized_typed_gates_fit_the_versioned_acceptance_report_schema(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "contracts" / "valid-acceptance-report.json"
        report = json.loads(fixture_path.read_text(encoding="utf-8"))
        report["gates"] = [gate.to_dict() for gate in complete_gates()]

        validate_document(report, "acceptance-report")


class StatusResolutionTests(unittest.TestCase):
    def test_accepted_requires_target_canonical_environment_and_every_gate_pass(self) -> None:
        resolution = resolve_status(score=98.5, target=98.0, gates=complete_gates())

        self.assertEqual(resolution.status, Status.ACCEPTED)
        self.assertEqual(resolution.exit_code, ExitCode.ACCEPTED)

    def test_gate_failure_makes_accepted_impossible(self) -> None:
        gates = list(complete_gates())
        gates[-1] = GateResult(gates[-1].gate_id, "semantic", "fail", evidence(), "reviewer", NOW)

        resolution = resolve_status(score=100.0, target=98.0, gates=gates)

        self.assertEqual(resolution.status, Status.NOT_ACCEPTED)
        self.assertEqual(resolution.exit_code, ExitCode.GATE_FAILED)

    def test_known_gate_failure_outranks_incomplete_semantic_review_and_score_failure(self) -> None:
        gates = list(complete_gates())
        gates[-1] = GateResult(gates[-1].gate_id, "semantic", "not_evaluated", evidence(), "reviewer", NOW)
        gates[-2] = GateResult(gates[-2].gate_id, "semantic", "fail", evidence(), "reviewer", NOW)

        resolution = resolve_status(score=10.0, target=98.0, gates=gates)

        self.assertEqual(resolution.status, Status.NOT_ACCEPTED)
        self.assertEqual(resolution.exit_code, ExitCode.GATE_FAILED)

    def test_score_failure_outranks_incomplete_semantic_review(self) -> None:
        gates = list(complete_gates())
        gates[-1] = GateResult(gates[-1].gate_id, "semantic", "not_evaluated", evidence(), "reviewer", NOW)

        resolution = resolve_status(score=97.99, target=98.0, gates=gates)

        self.assertEqual(resolution.status, Status.NOT_ACCEPTED)
        self.assertEqual(resolution.exit_code, ExitCode.SCORE_BELOW_TARGET)

    def test_status_precedence_is_invalid_runtime_noncanonical_failure_incomplete_accepted(self) -> None:
        pending = list(complete_gates())
        pending[-1] = GateResult(pending[-1].gate_id, "semantic", "not_evaluated", evidence(), "reviewer", NOW)

        invalid = resolve_status(score=100.0, target=98.0, gates=pending, invalid_input=True, runtime_error=True, canonical_environment=False)
        runtime = resolve_status(score=100.0, target=98.0, gates=pending, runtime_error=True, canonical_environment=False)
        noncanonical = resolve_status(score=100.0, target=98.0, gates=pending, canonical_environment=False)
        incomplete = resolve_status(score=100.0, target=98.0, gates=pending)

        self.assertEqual((invalid.status, invalid.exit_code), (Status.INVALID_INPUT, ExitCode.INVALID_INPUT))
        self.assertEqual((runtime.status, runtime.exit_code), (Status.RUNTIME_ERROR, ExitCode.RUNTIME_ERROR))
        self.assertEqual((noncanonical.status, noncanonical.exit_code), (Status.NON_CANONICAL, ExitCode.NON_CANONICAL))
        self.assertEqual((incomplete.status, incomplete.exit_code), (Status.INCOMPLETE, ExitCode.INCOMPLETE_REVIEW))

    def test_missing_mandatory_gate_cannot_resolve_to_accepted(self) -> None:
        resolution = resolve_status(score=100.0, target=98.0, gates=complete_gates()[:-1])

        self.assertEqual(resolution.status, Status.INCOMPLETE)
        self.assertEqual(resolution.exit_code, ExitCode.INCOMPLETE_REVIEW)

    def test_out_of_range_raw_score_is_invalid_input(self) -> None:
        resolution = resolve_status(score=100.01, target=98.0, gates=complete_gates())

        self.assertEqual(resolution.status, Status.INVALID_INPUT)
        self.assertEqual(resolution.exit_code, ExitCode.INVALID_INPUT)

    def test_derived_invalid_input_outranks_gate_failure(self) -> None:
        gates = list(complete_gates())
        gates[-1] = GateResult(gates[-1].gate_id, "semantic", "fail", evidence(), "reviewer", NOW)

        resolution = resolve_status(score=float("nan"), target=98.0, gates=gates)

        self.assertEqual((resolution.status, resolution.exit_code), (Status.INVALID_INPUT, ExitCode.INVALID_INPUT))

    def test_missing_metrics_and_automatic_catalog_outrank_known_failures(self) -> None:
        failing = list(complete_gates())
        failing[-1] = GateResult(failing[-1].gate_id, "semantic", "fail", evidence(), "reviewer", NOW)
        missing_auto = [gate for gate in failing if gate.gate_id != "auto.svg.render"]

        missing_metrics = resolve_status(score=None, target=None, gates=failing)
        missing_catalog = resolve_status(score=1.0, target=98.0, gates=missing_auto)

        self.assertEqual((missing_metrics.status, missing_metrics.exit_code), (Status.RUNTIME_ERROR, ExitCode.RUNTIME_ERROR))
        self.assertEqual((missing_catalog.status, missing_catalog.exit_code), (Status.RUNTIME_ERROR, ExitCode.RUNTIME_ERROR))


if __name__ == "__main__":
    unittest.main()
