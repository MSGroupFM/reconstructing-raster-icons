"""Lifecycle tests for the immutable reconstruction acceptance pipeline."""

from __future__ import annotations

import copy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
import sys

import numpy as np
from PIL import Image
from jsonschema import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reconstructing_raster_icons.constants import ExitCode, Status
from reconstructing_raster_icons.errors import FrozenArtifactError, InvalidInputError
from reconstructing_raster_icons.pipeline import (
    evaluate_candidate,
    finalize_review,
    is_stalled,
    prepare_reference,
)
from reconstructing_raster_icons.renderer import RenderResult, RendererEvidence


REPOSITORY = Path(__file__).resolve().parents[1]
CONTRACTS = REPOSITORY / "tests" / "fixtures" / "contracts"


def fixture(name: str) -> dict[str, object]:
    return copy.deepcopy(json.loads((CONTRACTS / name).read_text(encoding="utf-8")))


def png_bytes(mask: np.ndarray) -> bytes:
    output = BytesIO()
    Image.fromarray(np.where(mask, 0, 255).astype(np.uint8), mode="L").save(output, format="PNG")
    return output.getvalue()


def write_reference_inputs(root: Path) -> tuple[Path, Path]:
    source_mask = np.zeros((64, 64), dtype=bool)
    source_mask[16:48, 16:48] = True
    source = root / "source.png"
    source.write_bytes(png_bytes(source_mask))
    component = root / "mark.png"
    component.write_bytes(png_bytes(source_mask))

    draft = fixture("valid-draft.json")
    draft["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    draft["components"][0]["source_mask_path"] = component.name  # type: ignore[index]
    draft_path = root / "draft.json"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    return source, draft_path


def fake_renderer(document, size: tuple[int, int], workspace: Path) -> RenderResult:
    mask = np.zeros((size[1], size[0]), dtype=bool)
    mask[size[1] // 4 : 3 * size[1] // 4, size[0] // 4 : 3 * size[0] // 4] = True
    rgba = np.zeros((size[1], size[0], 4), dtype=np.uint8)
    rgba[mask, 3] = 255
    output = BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(output, format="PNG")
    payload = output.getvalue()
    return RenderResult(
        status=Status.ACCEPTED,
        path=None,
        png_bytes=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        size=size,
        diagnostic="",
        observed=RendererEvidence(node_version="22.14.0"),
        expected=RendererEvidence(node_version="22.14.0"),
        attestation={"render_status": "ok"},
    )


def fake_diagnostics(document, components, size: tuple[int, int], workspace: Path):
    mask = np.zeros((size[1], size[0]), dtype=bool)
    mask[size[1] // 4 : 3 * size[1] // 4, size[0] // 4 : 3 * size[0] // 4] = True
    return {"visible": {"mark": mask}, "isolated": {"mark": mask}}


class PrepareReferenceTests(unittest.TestCase):
    def test_refuses_unconfirmed_draft_before_creating_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, draft_path = write_reference_inputs(root)
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            draft["accuracy_confirmed"] = False
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            output = root / "reference"

            with self.assertRaises((ValidationError, InvalidInputError)):
                prepare_reference(source, draft_path, output)

            self.assertFalse(output.exists())

    def test_freezes_revision_and_masks_without_ever_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, draft = write_reference_inputs(root)
            output = root / "reference"

            summary = prepare_reference(source, draft, output)
            map_path = output / "reconstruction-map-r01.json"
            original_map = map_path.read_bytes()

            self.assertEqual(summary["artifact_id"], "reconstruction-map-r01")
            self.assertTrue(map_path.is_file())
            self.assertTrue((output / "reference-r01" / "reference-mask.png").is_file())
            self.assertTrue((output / "reference-r01" / "uncertainty-mask.png").is_file())
            self.assertTrue((output / "reference-r01" / "component-mark.png").is_file())
            self.assertTrue((output / "reference-stage-report-r01.json").is_file())

            with self.assertRaises(FrozenArtifactError):
                prepare_reference(source, draft, output)

            self.assertEqual(original_map, map_path.read_bytes())

    def test_freezes_the_declared_noncenter_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mask = np.zeros((64, 32), dtype=bool)
            mask[16:48, 8:24] = True
            source = root / "source.png"
            source.write_bytes(png_bytes(mask))
            component = root / "mark.png"
            component.write_bytes(png_bytes(mask))
            draft = fixture("valid-draft.json")
            draft["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            draft["viewport"]["alignment"] = "top-left"  # type: ignore[index]
            draft["components"][0]["source_mask_path"] = component.name  # type: ignore[index]
            draft_path = root / "draft.json"
            draft_path.write_text(json.dumps(draft), encoding="utf-8")

            prepare_reference(source, draft_path, root / "reference")
            frozen = json.loads(
                (root / "reference" / "reconstruction-map-r01.json").read_text(encoding="utf-8")
            )

            self.assertLess(frozen["components"][0]["bbox"][0], 200)


class EvaluateCandidateTests(unittest.TestCase):
    def _prepared(self, root: Path) -> tuple[Path, Path]:
        source, draft = write_reference_inputs(root)
        output = root / "reference"
        prepare_reference(source, draft, output)
        candidate = root / "candidate.svg"
        candidate.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            '<rect id="mark" x="16" y="16" width="32" height="32" fill="currentColor"/>'
            "</svg>",
            encoding="utf-8",
        )
        return output / "reconstruction-map-r01.json", candidate

    def test_enforces_refinement_limit_without_mutating_frozen_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)
            before = map_path.read_bytes()

            with self.assertRaises(InvalidInputError):
                evaluate_candidate(map_path, candidate, 9, root / "run")

            self.assertEqual(before, map_path.read_bytes())
            self.assertFalse((root / "run" / "evaluation-i09.json").exists())

    def test_writes_versioned_evaluation_and_retains_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)
            before = map_path.read_bytes()
            run_dir = root / "run"

            summary = evaluate_candidate(
                map_path,
                candidate,
                0,
                run_dir,
                renderer=fake_renderer,
                diagnostic_renderer=fake_diagnostics,
            )

            self.assertEqual(summary["artifact_id"], "evaluation-i00")
            self.assertTrue((run_dir / "evaluation-i00.json").is_file())
            self.assertTrue((run_dir / "diagnostics-i00.json").is_file())
            self.assertTrue((run_dir / "candidate-i00.svg").is_file())
            self.assertTrue((run_dir / "run-state.json").is_file())
            self.assertEqual(before, map_path.read_bytes())

            with self.assertRaises(FrozenArtifactError):
                evaluate_candidate(
                    map_path,
                    candidate,
                    0,
                    run_dir,
                    renderer=fake_renderer,
                    diagnostic_renderer=fake_diagnostics,
                )

    def test_rejects_nonzero_iteration_without_contiguous_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)

            with self.assertRaisesRegex(InvalidInputError, "contiguous"):
                evaluate_candidate(
                    map_path,
                    candidate,
                    1,
                    root / "new-run",
                    renderer=fake_renderer,
                    diagnostic_renderer=fake_diagnostics,
                )

    def test_disposable_run_directory_cannot_contain_frozen_or_candidate_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)

            with self.assertRaisesRegex(InvalidInputError, "disposable"):
                evaluate_candidate(
                    map_path,
                    candidate,
                    0,
                    root,
                    renderer=fake_renderer,
                    diagnostic_renderer=fake_diagnostics,
                )

    def test_declared_geometry_constraints_drive_the_automatic_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, draft_path = write_reference_inputs(root)
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            draft["geometry_constraints"]["lines"] = [
                {
                    "component_id": "mark",
                    "start": [0, 0],
                    "end": [1, 0],
                    "tolerance": 0.01,
                }
            ]
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            reference = root / "reference"
            prepare_reference(source, draft_path, reference)
            candidate = root / "candidate.svg"
            candidate.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<rect id="mark" x="16" y="16" width="32" height="32" fill="currentColor"/>'
                "</svg>",
                encoding="utf-8",
            )

            evaluate_candidate(
                reference / "reconstruction-map-r01.json",
                candidate,
                0,
                root / "run",
                renderer=fake_renderer,
                diagnostic_renderer=fake_diagnostics,
            )
            evaluation = json.loads((root / "run" / "evaluation-i00.json").read_text())
            gate = next(
                item
                for item in evaluation["report"]["gates"]
                if item["gate_id"] == "auto.primitives.constraints"
            )

            self.assertEqual(gate["state"], "fail")


class StallTests(unittest.TestCase):
    def test_stalls_after_three_flat_refinements(self) -> None:
        history = [97.50, 97.54, 97.56, 97.58]
        self.assertTrue(is_stalled(history, gate_improvements=[False, False, False]))

    def test_gate_improvement_prevents_stall(self) -> None:
        history = [97.50, 97.54, 97.56, 97.58]
        self.assertFalse(is_stalled(history, gate_improvements=[False, True, False]))

    def test_exact_point_ten_improvement_does_not_stall(self) -> None:
        self.assertFalse(is_stalled([97.50, 97.54, 97.56, 97.60], [False, False, False]))


class FinalizeReviewTests(unittest.TestCase):
    def _evaluation(self, root: Path) -> Path:
        map_path, candidate = EvaluateCandidateTests()._prepared(root)
        run_dir = root / "run"
        evaluate_candidate(
            map_path,
            candidate,
            0,
            run_dir,
            renderer=fake_renderer,
            diagnostic_renderer=fake_diagnostics,
        )
        return run_dir / "evaluation-i00.json"

    def _review(self, root: Path, state: str = "pass") -> Path:
        review = fixture("valid-semantic-review.json")
        for gate in review["gates"]:  # type: ignore[union-attr]
            gate["state"] = state
        path = root / "review.json"
        path.write_text(json.dumps(review), encoding="utf-8")
        return path

    def test_not_evaluated_semantic_review_is_incomplete_exit_five(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluation = self._evaluation(root)
            review = self._review(root, "not_evaluated")

            summary = finalize_review(evaluation, review, root / "acceptance-report.json")

            self.assertEqual(summary["status"], "incomplete")
            self.assertEqual(summary["exit_code"], ExitCode.INCOMPLETE_REVIEW)

    def test_exit_precedence_gate_then_score_then_noncanonical(self) -> None:
        cases = (
            ({"semantic_state": "fail"}, ExitCode.GATE_FAILED),
            ({"score": 97.0, "semantic_state": "not_evaluated"}, ExitCode.SCORE_BELOW_TARGET),
            ({"canonical": False, "semantic_state": "fail", "score": 0.0}, ExitCode.NON_CANONICAL),
        )
        for index, (changes, expected) in enumerate(cases):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evaluation_path = self._evaluation(root)
                evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
                report = evaluation["report"]
                if "score" in changes:
                    for key in ("silhouette_raw", "silhouette", "contour_raw", "contour", "layout_raw", "layout", "topology_raw", "topology", "composite_raw", "composite"):
                        report["metrics"][key] = changes["score"]
                    report["target_met"] = changes["score"] >= report["accuracy_target"]
                if "canonical" in changes:
                    report["canonical_environment"] = changes["canonical"]
                report["status"] = (
                    "non_canonical"
                    if report["canonical_environment"] is False
                    else "not_accepted"
                    if report["target_met"] is False
                    else report["status"]
                )
                evaluation_path.write_text(json.dumps(evaluation), encoding="utf-8")
                review = self._review(root, changes["semantic_state"])

                summary = finalize_review(evaluation_path, review, root / f"report-{index}.json")

                self.assertEqual(summary["exit_code"], expected)

    def test_report_has_only_logical_artifacts_and_cleanup_is_recorded_afterward(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluation = self._evaluation(root)
            run_dir = evaluation.parent
            review = self._review(root)
            output = root / "acceptance-report.json"

            summary = finalize_review(evaluation, review, output)
            report = json.loads(output.read_text(encoding="utf-8"))

            self.assertEqual(summary["exit_code"], ExitCode.ACCEPTED)
            self.assertFalse(run_dir.exists())
            self.assertTrue((root / "cleanup-inventory.json").is_file())
            self.assertTrue(all(set(item) == {"logical_id", "sha256", "retention"} for item in report["artifacts"]))
            self.assertNotIn(str(root), output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
