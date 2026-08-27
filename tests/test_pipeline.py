"""Lifecycle tests for the immutable reconstruction acceptance pipeline."""

from __future__ import annotations

import copy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
import sys
from unittest import mock

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
import reconstructing_raster_icons.pipeline as pipeline_module
import reconstructing_raster_icons.schema_io as schema_io_module


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
    def test_rejects_tampered_automatic_estimator_before_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, draft_path = write_reference_inputs(root)
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            draft["normalization"]["estimator"]["background_luminance"] = 0.8
            draft_path.write_text(json.dumps(draft), encoding="utf-8")

            with self.assertRaisesRegex(InvalidInputError, "automatic normalization estimator"):
                prepare_reference(source, draft_path, root / "reference")

    def test_explicit_normalization_override_requires_structured_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, draft_path = write_reference_inputs(root)
            pixels = np.full((64, 64), 153, dtype=np.uint8)
            pixels[16:48, 16:48] = 102
            encoded = BytesIO()
            Image.fromarray(pixels, mode="L").save(encoded, format="PNG")
            source.write_bytes(encoded.getvalue())
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            draft["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            with self.assertRaisesRegex(InvalidInputError, "automatic foreground/background estimate is ambiguous"):
                prepare_reference(source, draft_path, root / "automatic-reference")
            draft["normalization"]["estimator_basis"] = "explicit_override"
            draft["normalization"]["explicit_overrides"] = {
                "background_luminance": 0.32,
                "foreground_luminance": 0.13,
                "reason": "source is ambiguous",
            }
            draft_path.write_text(json.dumps(draft), encoding="utf-8")

            with self.assertRaisesRegex(InvalidInputError, "explicit normalization override"):
                prepare_reference(source, draft_path, root / "reference")

            draft["normalization"]["explicit_overrides"].update({
                "confirmed": True,
                "confirmed_at": "2026-08-26T00:00:00Z",
            })
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            summary = prepare_reference(source, draft_path, root / "confirmed-reference")
            self.assertEqual(summary["artifact_id"], "reconstruction-map-r01")

    def test_meaningful_multicolor_source_stops_without_merge_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, draft_path = write_reference_inputs(root)
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            draft["source_color_scope"] = {
                "classification": "meaningful_multicolor",
                "merge_to_monochrome": None,
            }
            draft_path.write_text(json.dumps(draft), encoding="utf-8")

            with self.assertRaisesRegex(InvalidInputError, "merge-to-monochrome"):
                prepare_reference(source, draft_path, root / "reference")

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

            self.assertEqual(
                [path.name for path in output.iterdir()], ["failure-report.json"]
            )

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

    def test_source_hash_and_pixels_use_one_immutable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, draft = write_reference_inputs(root)
            original = source.read_bytes()
            replacement_mask = np.zeros((64, 64), dtype=bool)
            replacement_mask[8:24, 8:24] = True
            replacement = png_bytes(replacement_mask)
            real_loader = pipeline_module.load_raster

            def replace_then_load(path: Path):
                source.write_bytes(replacement)
                return real_loader(path)

            with mock.patch.object(pipeline_module, "load_raster", side_effect=replace_then_load):
                prepare_reference(source, draft, root / "reference")
            frozen = json.loads((root / "reference" / "reconstruction-map-r01.json").read_text())
            with Image.open(root / "reference" / "reference-r01" / "reference-mask.png") as image:
                frozen_mask = np.asarray(image.convert("L")) < 128
            rows, columns = np.nonzero(frozen_mask)

            self.assertEqual(frozen["source_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual((columns.min(), columns.max()), (252, 771))

    def test_draft_validation_and_freeze_use_one_immutable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, draft_path = write_reference_inputs(root)
            real_validate = pipeline_module.validate_document

            def mutate_after_validate(document, schema_name):
                result = real_validate(document, schema_name)
                if schema_name == "reconstruction-map-draft":
                    replacement = json.loads(draft_path.read_text())
                    replacement["accuracy_target"] = 97
                    draft_path.write_text(json.dumps(replacement))
                return result

            with mock.patch.object(
                pipeline_module, "validate_document", side_effect=mutate_after_validate
            ):
                prepare_reference(source, draft_path, root / "reference")
            frozen = json.loads(
                (root / "reference" / "reconstruction-map-r01.json").read_text()
            )

            self.assertEqual(frozen["accuracy_target"], 98)

    def test_duplicate_component_ids_fail_before_any_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, draft_path = write_reference_inputs(root)
            draft = json.loads(draft_path.read_text())
            draft["components"].append(copy.deepcopy(draft["components"][0]))
            draft_path.write_text(json.dumps(draft))

            with self.assertRaises(ValidationError):
                prepare_reference(source, draft_path, root / "reference")

            self.assertEqual(
                [path.name for path in (root / "reference").iterdir()], ["failure-report.json"]
            )

    def test_failed_publication_rolls_back_and_same_revision_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, draft = write_reference_inputs(root)
            output = root / "reference"
            real_write = pipeline_module._atomic_write_bytes

            def fail_final_publish(path: Path, payload: bytes) -> None:
                if path == output / "reference-stage-report-r01.json":
                    raise OSError("publication interrupted")
                real_write(path, payload)

            with mock.patch.object(
                pipeline_module, "_atomic_write_bytes", side_effect=fail_final_publish
            ):
                with self.assertRaises(OSError):
                    prepare_reference(source, draft, output)

            self.assertEqual(
                [path.name for path in output.iterdir()], ["failure-report.json"]
            )
            summary = prepare_reference(source, draft, output)
            self.assertEqual(summary["artifact_id"], "reconstruction-map-r01")


class EvaluateCandidateTests(unittest.TestCase):
    def test_reversed_top_level_candidate_layers_fail_paint_order_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, draft_path = write_reference_inputs(root)
            draft = json.loads(draft_path.read_text(encoding="utf-8"))
            component = dict(draft["components"][0])
            component["component_id"] = "back"
            component["svg_id"] = "back"
            component["source_mask_path"] = "back.png"
            draft["components"] = [component, draft["components"][0]]
            draft["topology_facts"] = [
                {"relation": "overlaps", "subject": "back", "object": "mark"},
                {"relation": "paint_order", "subject": "back", "object": "mark"}
            ]
            (root / "back.png").write_bytes((root / "mark.png").read_bytes())
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            reference = root / "reference"
            prepare_reference(source, draft_path, reference)
            candidate = root / "candidate.svg"
            candidate.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<rect id="mark" x="16" y="16" width="32" height="32" fill="currentColor"/>'
                '<rect id="back" x="16" y="16" width="32" height="32" fill="currentColor"/>'
                "</svg>",
                encoding="utf-8",
            )

            def diagnostics(document, components, size, workspace):
                mask = np.zeros((size[1], size[0]), dtype=bool)
                mask[256:768, 256:768] = True
                return {"visible": {"back": mask, "mark": mask}, "isolated": {"back": mask, "mark": mask}}

            evaluate_candidate(
                reference / "reconstruction-map-r01.json",
                candidate,
                0,
                root / "run",
                renderer=fake_renderer,
                diagnostic_renderer=diagnostics,
            )
            evaluation = json.loads((root / "run" / "evaluation-i00.json").read_text())
            topology_gate = next(
                item for item in evaluation["report"]["gates"]
                if item["gate_id"] == "auto.topology.facts"
            )
            self.assertEqual(topology_gate["state"], "fail")

    def test_candidate_component_roots_are_unique_and_top_level(self) -> None:
        candidates = {
            "duplicate": (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<g id="mark"/><rect id="mark" x="16" y="16" width="32" height="32" '
                'fill="currentColor"/></svg>'
            ),
            "nested": (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<g><rect id="mark" x="16" y="16" width="32" height="32" '
                'fill="currentColor"/></g></svg>'
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)
            for name, payload in candidates.items():
                with self.subTest(candidate=name):
                    candidate.write_text(payload, encoding="utf-8")
                    with self.assertRaisesRegex(InvalidInputError, "component roots"):
                        evaluate_candidate(map_path, candidate, 0, root / name)

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
            self.assertEqual((run_dir / "map-snapshot-i00.json").read_bytes(), before)
            self.assertTrue((run_dir / "preview-i00.png").is_file())
            self.assertTrue((run_dir / "overlay-i00.png").is_file())
            self.assertTrue((run_dir / "diff-i00.png").is_file())
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

    def test_fill_component_rejects_stroke_only_candidate_style(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)
            candidate.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<path id="mark" d="M16 16 L48 16 L48 48 L32.05 48 '
                'L32 60 L31.95 48 L16 48 Z" '
                'fill="none" stroke="currentColor" stroke-width="1"/>'
                "</svg>"
            )

            summary = evaluate_candidate(
                map_path,
                candidate,
                0,
                root / "run",
                renderer=fake_renderer,
                diagnostic_renderer=fake_diagnostics,
            )
            evaluation = json.loads((root / "run" / "evaluation-i00.json").read_text())
            gate = next(
                item for item in evaluation["report"]["gates"]
                if item["gate_id"] == "auto.style.monochrome"
            )

            self.assertEqual(gate["state"], "fail")
            self.assertNotEqual(summary["status"], "accepted")

    def test_narrow_spike_fails_path_integrity_even_with_canonical_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)
            candidate.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<path id="mark" d="M16 16 L48 16 L48 48 L32.05 48 '
                'L32 60 L31.95 48 L16 48 Z" fill="currentColor"/>'
                "</svg>"
            )

            evaluate_candidate(
                map_path,
                candidate,
                0,
                root / "run",
                renderer=fake_renderer,
                diagnostic_renderer=fake_diagnostics,
            )
            evaluation = json.loads((root / "run" / "evaluation-i00.json").read_text())
            gate = next(
                item for item in evaluation["report"]["gates"]
                if item["gate_id"] == "auto.paths.integrity"
            )

            self.assertEqual(gate["state"], "fail")

    def test_nonadjacent_endpoint_self_touch_fails_path_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)
            candidate.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<path id="mark" d="M16 16 L48 16 L32 32 L48 48 '
                'L16 48 L32 32 Z" fill="currentColor"/>'
                "</svg>"
            )

            summary = evaluate_candidate(
                map_path,
                candidate,
                0,
                root / "run",
                renderer=fake_renderer,
                diagnostic_renderer=fake_diagnostics,
            )
            evaluation = json.loads((root / "run" / "evaluation-i00.json").read_text())
            gate = next(
                item for item in evaluation["report"]["gates"]
                if item["gate_id"] == "auto.paths.integrity"
            )

            self.assertEqual(gate["state"], "fail")
            self.assertNotEqual(summary["status"], "accepted")

    def test_path_integrity_preserves_subpath_and_child_boundaries(self) -> None:
        candidates = {
            "subpaths": (
                '<path id="mark" d="M8 8 L24 8 L24 24 L8 24 Z '
                'M40 32 L56 32 L56 48 L40 48 Z" fill="currentColor"/>'
            ),
            "children": (
                '<g id="mark" fill="currentColor">'
                '<rect x="8" y="8" width="16" height="16"/>'
                '<rect x="40" y="32" width="16" height="16"/>'
                "</g>"
            ),
        }
        for label, body in candidates.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                map_path, candidate = self._prepared(root)
                candidate.write_text(
                    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                    f"{body}</svg>"
                )

                evaluate_candidate(
                    map_path,
                    candidate,
                    0,
                    root / "run",
                    renderer=fake_renderer,
                    diagnostic_renderer=fake_diagnostics,
                )
                evaluation = json.loads(
                    (root / "run" / "evaluation-i00.json").read_text()
                )
                gate = next(
                    item for item in evaluation["report"]["gates"]
                    if item["gate_id"] == "auto.paths.integrity"
                )

                self.assertEqual(gate["state"], "pass")

    def test_viewport_rejects_painted_extents_outside_every_canvas_edge(self) -> None:
        cases = (
            ("stroke-left", "stroke", '<line id="mark" x1="-0.25" y1="16" x2="-0.25" y2="48" fill="none" stroke="currentColor" stroke-width="1"/>'),
            ("stroke-right", "stroke", '<line id="mark" x1="64.25" y1="16" x2="64.25" y2="48" fill="none" stroke="currentColor" stroke-width="1"/>'),
            ("fill-top", "fill", '<rect id="mark" x="16" y="-0.25" width="32" height="16" fill="currentColor"/>'),
            ("mixed-bottom", "mixed", '<rect id="mark" x="16" y="48.25" width="32" height="16" fill="currentColor" stroke="currentColor" stroke-width="1"/>'),
        )
        for label, paint_type, body in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, draft_path = write_reference_inputs(root)
                draft = json.loads(draft_path.read_text())
                draft["components"][0]["paint_type"] = paint_type
                if paint_type in {"stroke", "mixed"}:
                    draft["geometry_constraints"]["strokes"] = [{
                        "component_id": "mark",
                        "expected_width": 16,
                        "cap": "butt",
                        "join": "miter",
                    }]
                draft_path.write_text(json.dumps(draft))
                reference = root / "reference"
                prepare_reference(source, draft_path, reference)
                candidate = root / "candidate.svg"
                candidate.write_text(
                    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                    f"{body}</svg>"
                )

                evaluate_candidate(
                    reference / "reconstruction-map-r01.json",
                    candidate,
                    0,
                    root / "run",
                    renderer=fake_renderer,
                    diagnostic_renderer=fake_diagnostics,
                )
                evaluation = json.loads(
                    (root / "run" / "evaluation-i00.json").read_text()
                )
                gate = next(
                    item for item in evaluation["report"]["gates"]
                    if item["gate_id"] == "auto.viewport.geometry"
                )

                self.assertEqual(gate["state"], "fail")

    def test_style_uses_the_single_foreground_color_frozen_in_map(self) -> None:
        cases = (
            ("fixed-color", '<rect id="mark" x="16" y="16" width="32" height="32" fill="#ff0000"/>', "pass"),
            ("mismatch", '<rect id="mark" x="16" y="16" width="32" height="32" fill="#00ff00"/>', "fail"),
            ("current-color", '<rect id="mark" x="16" y="16" width="32" height="32" fill="currentColor"/>', "fail"),
            ("multiple", '<g id="mark"><rect x="16" y="16" width="16" height="32" fill="#ff0000"/><rect x="32" y="16" width="16" height="32" fill="#00ff00"/></g>', "fail"),
        )
        for label, body, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, draft_path = write_reference_inputs(root)
                draft = json.loads(draft_path.read_text())
                draft["foreground_color"] = "#ff0000"
                draft_path.write_text(json.dumps(draft))
                reference = root / "reference"
                prepare_reference(source, draft_path, reference)
                frozen = json.loads(
                    (reference / "reconstruction-map-r01.json").read_text()
                )
                self.assertEqual(frozen["foreground_color"], "#ff0000")
                candidate = root / "candidate.svg"
                candidate.write_text(
                    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                    f"{body}</svg>"
                )

                evaluate_candidate(
                    reference / "reconstruction-map-r01.json",
                    candidate,
                    0,
                    root / "run",
                    renderer=fake_renderer,
                    diagnostic_renderer=fake_diagnostics,
                )
                evaluation = json.loads(
                    (root / "run" / "evaluation-i00.json").read_text()
                )
                gate = next(
                    item for item in evaluation["report"]["gates"]
                    if item["gate_id"] == "auto.style.monochrome"
                )

                self.assertEqual(gate["state"], expected)

    def test_viewport_gate_rejects_stretch_geometry_for_contain_map(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)
            candidate.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" '
                'preserveAspectRatio="none">'
                '<rect id="mark" x="16" y="16" width="32" height="32" fill="currentColor"/>'
                "</svg>"
            )

            evaluate_candidate(
                map_path,
                candidate,
                0,
                root / "run",
                renderer=fake_renderer,
                diagnostic_renderer=fake_diagnostics,
            )
            evaluation = json.loads((root / "run" / "evaluation-i00.json").read_text())
            gate = next(
                item for item in evaluation["report"]["gates"]
                if item["gate_id"] == "auto.viewport.geometry"
            )

            self.assertEqual(gate["state"], "fail")

    def test_integrity_gate_rejects_map_not_bound_to_frozen_stage_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)
            frozen = json.loads(map_path.read_text())
            frozen["source_sha256"] = "0" * 64
            map_path.write_text(json.dumps(frozen))

            evaluate_candidate(
                map_path,
                candidate,
                0,
                root / "run",
                renderer=fake_renderer,
                diagnostic_renderer=fake_diagnostics,
            )
            evaluation = json.loads((root / "run" / "evaluation-i00.json").read_text())
            gate = next(
                item for item in evaluation["report"]["gates"]
                if item["gate_id"] == "auto.integrity.hashes"
            )

            self.assertEqual(gate["state"], "fail")

    def test_map_configuration_and_hash_use_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)
            old = map_path.read_bytes()
            real_validate = pipeline_module.validate_document

            def mutate_after_validate(document, schema_name):
                result = real_validate(document, schema_name)
                if schema_name == "reconstruction-map":
                    mutated = json.loads(old)
                    mutated["accuracy_target"] = 97
                    map_path.write_text(json.dumps(mutated))
                return result

            with mock.patch.object(pipeline_module, "validate_document", side_effect=mutate_after_validate):
                evaluate_candidate(
                    map_path,
                    candidate,
                    0,
                    root / "run",
                    renderer=fake_renderer,
                    diagnostic_renderer=fake_diagnostics,
                )
            evaluation = json.loads((root / "run" / "evaluation-i00.json").read_text())

            self.assertEqual(evaluation["report"]["accuracy_target"], 98)
            self.assertEqual(evaluation["map_sha256"], hashlib.sha256(old).hexdigest())
            self.assertEqual((root / "run" / "map-snapshot-i00.json").read_bytes(), old)

    def test_candidate_validation_hash_and_publication_use_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)
            old = candidate.read_bytes()
            replacement = old.replace(b'x="16"', b'x="8"')
            real_validate = pipeline_module._validate_svg_snapshot

            def mutate_after_validate(payload: bytes):
                document = real_validate(payload)
                candidate.write_bytes(replacement)
                return document

            with mock.patch.object(
                pipeline_module, "_validate_svg_snapshot", side_effect=mutate_after_validate
            ):
                evaluate_candidate(
                    map_path,
                    candidate,
                    0,
                    root / "run",
                    renderer=fake_renderer,
                    diagnostic_renderer=fake_diagnostics,
                )
            evaluation = json.loads((root / "run" / "evaluation-i00.json").read_text())

            self.assertEqual((root / "run" / "candidate-i00.svg").read_bytes(), old)
            self.assertEqual(evaluation["report"]["hashes"]["candidate"], hashlib.sha256(old).hexdigest())

    def test_runtime_renderer_result_publishes_no_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)

            def runtime_renderer(document, size, workspace):
                return RenderResult(
                    status=Status.RUNTIME_ERROR,
                    path=None,
                    png_bytes=b"not-a-png",
                    sha256="",
                    size=size,
                    diagnostic="renderer crashed",
                    observed=RendererEvidence(),
                    expected=RendererEvidence(),
                    attestation=None,
                )

            with self.assertRaises(RuntimeError):
                evaluate_candidate(map_path, candidate, 0, root / "run", renderer=runtime_renderer)

            self.assertFalse((root / "run" / "evaluation-i00.json").exists())
            self.assertFalse((root / "run" / "preview-i00.png").exists())

    def test_false_accepted_renderer_png_is_runtime_failure_and_never_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)

            def invalid_renderer(document, size, workspace):
                payload = b"not-a-png"
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

            with self.assertRaises(RuntimeError):
                evaluate_candidate(
                    map_path, candidate, 0, root / "run", renderer=invalid_renderer
                )

            self.assertFalse((root / "run" / "evaluation-i00.json").exists())
            self.assertFalse((root / "run" / "preview-i00.png").exists())

    def test_only_explicit_noncanonical_result_maps_to_exit_six(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)

            def noncanonical_renderer(document, size, workspace):
                return RenderResult(
                    status=Status.NON_CANONICAL,
                    path=None,
                    png_bytes=b"must-not-be-published",
                    sha256="",
                    size=size,
                    diagnostic="canonical runtime unavailable",
                    observed=RendererEvidence(),
                    expected=RendererEvidence(),
                    attestation=None,
                )

            summary = evaluate_candidate(
                map_path, candidate, 0, root / "run", renderer=noncanonical_renderer
            )

            self.assertEqual(summary["exit_code"], ExitCode.NON_CANONICAL)
            self.assertFalse((root / "run" / "preview-i00.png").exists())

    def test_diagnostic_exception_publishes_no_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)

            def broken(*args):
                raise RuntimeError("diagnostics crashed")

            with self.assertRaises(RuntimeError):
                evaluate_candidate(
                    map_path, candidate, 0, root / "run", renderer=fake_renderer, diagnostic_renderer=broken
                )

            self.assertFalse((root / "run" / "evaluation-i00.json").exists())

    def test_failed_publication_rolls_back_and_same_iteration_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path, candidate = self._prepared(root)
            run_dir = root / "run"
            real_write = pipeline_module._atomic_write_bytes

            def fail_final_publish(path: Path, payload: bytes) -> None:
                if path == run_dir / "diagnostics-i00.json":
                    raise OSError("publication interrupted")
                real_write(path, payload)

            with mock.patch.object(
                pipeline_module, "_atomic_write_bytes", side_effect=fail_final_publish
            ):
                with self.assertRaises(OSError):
                    evaluate_candidate(
                        map_path,
                        candidate,
                        0,
                        run_dir,
                        renderer=fake_renderer,
                        diagnostic_renderer=fake_diagnostics,
                    )

            self.assertEqual([path.name for path in run_dir.iterdir()], ["failure-report.json"])
            summary = evaluate_candidate(
                map_path,
                candidate,
                0,
                run_dir,
                renderer=fake_renderer,
                diagnostic_renderer=fake_diagnostics,
            )
            self.assertEqual(summary["artifact_id"], "evaluation-i00")


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
            cleanup = json.loads((root / "cleanup-report.json").read_text(encoding="utf-8"))
            artifacts = {item["logical_id"]: item for item in report["artifacts"]}

            self.assertEqual(summary["exit_code"], ExitCode.ACCEPTED)
            self.assertFalse(run_dir.exists())
            self.assertTrue((root / "cleanup-report.json").is_file())
            self.assertTrue(all(set(item) == {"logical_id", "sha256", "retention"} for item in report["artifacts"]))
            self.assertNotIn(str(root), output.read_text(encoding="utf-8"))
            self.assertTrue(
                {
                    "source-r01",
                    "reconstruction-map-r01",
                    "map-snapshot-i00",
                    "reference-mask-r01",
                    "uncertainty-mask-r01",
                    "reference-component-mark-r01",
                    "candidate-i00",
                    "preview-i00",
                    "overlay-i00",
                    "diff-i00",
                    "diagnostics-i00",
                    "evaluation-i00",
                }.issubset(artifacts)
            )
            for item in cleanup["artifacts"]:
                self.assertEqual(item["sha256"], artifacts[item["logical_id"]]["sha256"])
                if item["logical_id"] in {
                    "map-snapshot-i00",
                    "candidate-i00",
                    "preview-i00",
                    "overlay-i00",
                    "diff-i00",
                    "diagnostics-i00",
                    "evaluation-i00",
                }:
                    self.assertEqual(item["retention"], "deleted")
                    self.assertTrue(item["deleted_at"].endswith("Z"))
                else:
                    self.assertEqual(item["retention"], "retained")
                    self.assertTrue(item["recorded_at"].endswith("Z"))

    def test_multi_iteration_finalization_catalogs_every_iteration_before_and_after_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_evaluation = self._evaluation(root)
            run_dir = first_evaluation.parent
            evaluate_candidate(
                root / "reference" / "reconstruction-map-r01.json",
                root / "candidate.svg",
                1,
                run_dir,
                renderer=fake_renderer,
                diagnostic_renderer=fake_diagnostics,
            )
            review = self._review(root)
            output = root / "acceptance-report.json"

            finalize_review(run_dir / "evaluation-i01.json", review, output)
            report = json.loads(output.read_text())
            cleanup = json.loads((root / "cleanup-report.json").read_text())
            report_artifacts = {item["logical_id"]: item for item in report["artifacts"]}
            cleanup_artifacts = {item["logical_id"]: item for item in cleanup["artifacts"]}
            disposable = {
                f"{kind}-i{iteration:02d}"
                for iteration in (0, 1)
                for kind in (
                    "map-snapshot",
                    "candidate",
                    "preview",
                    "overlay",
                    "diff",
                    "diagnostics",
                    "evaluation",
                )
            }

            self.assertTrue(disposable.issubset(report_artifacts))
            self.assertEqual(set(report_artifacts), set(cleanup_artifacts))
            for logical_id, item in cleanup_artifacts.items():
                self.assertEqual(item["sha256"], report_artifacts[logical_id]["sha256"])
                if logical_id in disposable:
                    self.assertEqual(item["retention"], "deleted")
                    self.assertTrue(item["deleted_at"].endswith("Z"))
                else:
                    self.assertEqual(item["retention"], "retained")
                    self.assertTrue(item["recorded_at"].endswith("Z"))

    def test_finalizing_earlier_best_catalogs_later_published_iterations_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_evaluation = self._evaluation(root)
            run_dir = first_evaluation.parent
            evaluate_candidate(
                root / "reference" / "reconstruction-map-r01.json",
                root / "candidate.svg",
                1,
                run_dir,
                renderer=fake_renderer,
                diagnostic_renderer=fake_diagnostics,
            )
            review = self._review(root)
            output = root / "acceptance-report.json"

            finalize_review(first_evaluation, review, output)
            report = json.loads(output.read_text())
            cleanup = json.loads((root / "cleanup-report.json").read_text())
            report_ids = {item["logical_id"] for item in report["artifacts"]}
            cleanup_ids = {item["logical_id"] for item in cleanup["artifacts"]}
            later_ids = {
                f"{kind}-i01"
                for kind in (
                    "map-snapshot",
                    "candidate",
                    "preview",
                    "overlay",
                    "diff",
                    "diagnostics",
                    "evaluation",
                )
            }

            self.assertTrue(later_ids.issubset(report_ids))
            self.assertTrue(later_ids.issubset(cleanup_ids))
            self.assertEqual(report["iteration"], 0)

    def test_cleanup_sidecar_failure_restores_workspace_and_allows_same_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluation = self._evaluation(root)
            run_dir = evaluation.parent
            review = self._review(root)
            output = root / "acceptance-report.json"
            real_write = pipeline_module.atomic_write_json
            failed = False

            def fail_cleanup_once(path, document):
                nonlocal failed
                if Path(path).name == "cleanup-report.json" and not failed:
                    failed = True
                    raise OSError("cleanup sidecar publication interrupted")
                return real_write(path, document)

            with mock.patch.object(
                pipeline_module, "atomic_write_json", side_effect=fail_cleanup_once
            ):
                with self.assertRaises(OSError):
                    finalize_review(evaluation, review, output)

            self.assertTrue(run_dir.is_dir())
            self.assertTrue(evaluation.is_file())
            self.assertFalse(output.exists())
            self.assertFalse((root / "cleanup-report.json").exists())

            summary = finalize_review(evaluation, review, output)
            self.assertEqual(summary["exit_code"], ExitCode.ACCEPTED)
            self.assertTrue(output.is_file())
            self.assertTrue((root / "cleanup-report.json").is_file())

    def test_rollback_preserves_foreign_main_or_sidecar_race_winner(self) -> None:
        for destination_name in ("acceptance-report.json", "cleanup-report.json"):
            with self.subTest(destination=destination_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                evaluation = self._evaluation(root)
                run_dir = evaluation.parent
                review = self._review(root)
                output = root / "acceptance-report.json"
                sidecar = root / "cleanup-report.json"
                foreign_destination = root / destination_name
                foreign = b"foreign immutable evidence\n"
                real_link = schema_io_module.os.link
                raced = False

                def race_link(source, target, *args, **kwargs):
                    nonlocal raced
                    if Path(target) == foreign_destination and not raced:
                        raced = True
                        foreign_destination.write_bytes(foreign)
                    return real_link(source, target, *args, **kwargs)

                with mock.patch.object(
                    schema_io_module.os, "link", side_effect=race_link
                ):
                    with self.assertRaises(FrozenArtifactError):
                        finalize_review(evaluation, review, output)

                self.assertTrue(run_dir.is_dir())
                self.assertTrue(evaluation.is_file())
                self.assertEqual(foreign_destination.read_bytes(), foreign)
                if foreign_destination == output:
                    self.assertFalse(sidecar.exists())
                else:
                    self.assertFalse(output.exists())

    def test_acceptance_link_then_directory_fsync_failure_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluation = self._evaluation(root)
            run_dir = evaluation.parent
            review = self._review(root)
            output = root / "acceptance-report.json"
            sidecar = root / "cleanup-report.json"
            before = {
                str(path.relative_to(run_dir)): path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            }
            real_fsync = schema_io_module.os.fsync
            calls = 0

            def fail_second_fsync(descriptor):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("acceptance directory fsync interrupted")
                return real_fsync(descriptor)

            with mock.patch.object(
                schema_io_module.os, "fsync", side_effect=fail_second_fsync
            ):
                with self.assertRaises(OSError):
                    finalize_review(evaluation, review, output)

            after = {
                str(path.relative_to(run_dir)): path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertFalse(output.exists())
            self.assertFalse(sidecar.exists())

            summary = finalize_review(evaluation, review, output)
            self.assertEqual(summary["exit_code"], ExitCode.ACCEPTED)
            self.assertTrue(output.is_file())
            self.assertTrue(sidecar.is_file())

    def test_cleanup_link_then_directory_fsync_failure_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluation = self._evaluation(root)
            run_dir = evaluation.parent
            review = self._review(root)
            output = root / "acceptance-report.json"
            sidecar = root / "cleanup-report.json"
            before = {
                str(path.relative_to(run_dir)): path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            }
            real_fsync = schema_io_module.os.fsync
            calls = 0

            def fail_fifth_fsync(descriptor):
                nonlocal calls
                calls += 1
                if calls == 5:
                    raise OSError("cleanup directory fsync interrupted")
                return real_fsync(descriptor)

            with mock.patch.object(
                schema_io_module.os, "fsync", side_effect=fail_fifth_fsync
            ):
                with self.assertRaises(OSError):
                    finalize_review(evaluation, review, output)

            after = {
                str(path.relative_to(run_dir)): path.read_bytes()
                for path in run_dir.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertFalse(output.exists())
            self.assertFalse(sidecar.exists())

            summary = finalize_review(evaluation, review, output)
            self.assertEqual(summary["exit_code"], ExitCode.ACCEPTED)
            self.assertTrue(output.is_file())
            self.assertTrue(sidecar.is_file())

    def test_concurrent_different_outputs_allow_exactly_one_coherent_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluation = self._evaluation(root)
            run_dir = evaluation.parent
            review = self._review(root)
            outputs = (
                root / "first" / "acceptance-report.json",
                root / "second" / "acceptance-report.json",
            )
            start = threading.Barrier(2)
            cleanup_barrier = threading.Barrier(2)
            outcomes: list[object] = []
            real_rmtree = pipeline_module.shutil.rmtree

            def synchronized_cleanup(path, *args, **kwargs):
                if Path(path) == run_dir:
                    try:
                        cleanup_barrier.wait(timeout=0.5)
                    except threading.BrokenBarrierError:
                        pass
                return real_rmtree(path, *args, **kwargs)

            def worker(output: Path) -> None:
                start.wait()
                try:
                    outcomes.append(finalize_review(evaluation, review, output))
                except Exception as error:
                    outcomes.append(error)

            with mock.patch.object(
                pipeline_module.shutil, "rmtree", side_effect=synchronized_cleanup
            ):
                threads = [threading.Thread(target=worker, args=(output,)) for output in outputs]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
            self.assertEqual(sum(output.is_file() for output in outputs), 1)
            coherent = [
                output for output in outputs
                if output.is_file() and (output.parent / "cleanup-report.json").is_file()
            ]
            self.assertEqual(len(coherent), 1)

    def test_semantic_evidence_rejects_unknown_artifact_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluation = self._evaluation(root)
            review = self._review(root)
            semantic = json.loads(review.read_text())
            semantic["gates"][0]["evidence"] = {
                "artifact_id": "does-not-exist",
                "sha256": "0" * 64,
            }
            review.write_text(json.dumps(semantic))

            with self.assertRaises(InvalidInputError):
                finalize_review(evaluation, review, root / "acceptance-report.json")

            self.assertTrue(evaluation.parent.is_dir())

    def test_semantic_evidence_rejects_mismatched_artifact_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluation = self._evaluation(root)
            review = self._review(root)
            semantic = json.loads(review.read_text())
            semantic["gates"][0]["evidence"] = {
                "artifact_id": "candidate-i00",
                "sha256": "0" * 64,
            }
            review.write_text(json.dumps(semantic))

            with self.assertRaises(InvalidInputError):
                finalize_review(evaluation, review, root / "acceptance-report.json")

            self.assertTrue(evaluation.parent.is_dir())

    def test_semantic_evidence_rejects_conflicting_duplicate_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluation = self._evaluation(root)
            evaluated = json.loads(evaluation.read_text())
            candidate_hash = evaluated["report"]["hashes"]["candidate"]
            review = self._review(root)
            semantic = json.loads(review.read_text())
            semantic["gates"][0]["evidence"] = {
                "artifact_id": "candidate-i00",
                "sha256": candidate_hash,
            }
            semantic["gates"][1]["evidence"] = {
                "artifact_id": "candidate-i00",
                "sha256": "0" * 64,
            }
            review.write_text(json.dumps(semantic))

            with self.assertRaises(InvalidInputError):
                finalize_review(evaluation, review, root / "acceptance-report.json")

            self.assertTrue(evaluation.parent.is_dir())

    def test_final_report_publish_failure_preserves_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluation = self._evaluation(root)
            run_dir = evaluation.parent
            review = self._review(root)
            real_write = pipeline_module.atomic_write_json

            def fail_output(path, document):
                if Path(path).name == "acceptance-report.json":
                    raise OSError("disk failure")
                return real_write(path, document)

            with mock.patch.object(pipeline_module, "atomic_write_json", side_effect=fail_output):
                with self.assertRaises(OSError):
                    finalize_review(evaluation, review, root / "acceptance-report.json")

            self.assertTrue(run_dir.exists())

    def test_final_report_preserves_normalization_and_stop_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluation = self._evaluation(root)
            review = self._review(root)
            output = root / "acceptance-report.json"

            finalize_review(evaluation, review, output)
            report = json.loads(output.read_text())

            self.assertIn("estimator", report["normalization"])
            self.assertIn("estimator_basis", report["normalization"])
            self.assertEqual(report["stop_reason"], "accepted")

    def test_explicit_normalization_override_and_reason_survive_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, draft_path = write_reference_inputs(root)
            draft = json.loads(draft_path.read_text())
            draft["normalization"]["estimator_basis"] = "explicit_override"
            draft["normalization"]["explicit_overrides"] = {
                "background_luminance": 1,
                "foreground_luminance": 0,
                "reason": "confirmed transparent-black foreground",
                "confirmed": True,
                "confirmed_at": "2026-08-26T00:00:00Z",
            }
            draft_path.write_text(json.dumps(draft))
            reference = root / "reference"
            prepare_reference(source, draft_path, reference)
            candidate = root / "candidate.svg"
            candidate.write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<rect id="mark" x="16" y="16" width="32" height="32" fill="currentColor"/>'
                "</svg>"
            )
            run_dir = root / "run"
            evaluate_candidate(
                reference / "reconstruction-map-r01.json",
                candidate,
                0,
                run_dir,
                renderer=fake_renderer,
                diagnostic_renderer=fake_diagnostics,
            )
            review = self._review(root)
            output = root / "acceptance-report.json"

            finalize_review(run_dir / "evaluation-i00.json", review, output)
            normalization = json.loads(output.read_text())["normalization"]

            self.assertEqual(normalization["estimator_basis"], "explicit_override")
            self.assertEqual(
                normalization["explicit_overrides"]["reason"],
                "confirmed transparent-black foreground",
            )

    def test_semantic_review_validation_and_merge_use_one_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evaluation = self._evaluation(root)
            review = self._review(root)
            real_validate = pipeline_module.validate_document

            def mutate_after_validate(document, schema_name):
                result = real_validate(document, schema_name)
                if schema_name == "semantic-review":
                    replacement = json.loads(review.read_text())
                    replacement["gates"][0]["state"] = "fail"
                    review.write_text(json.dumps(replacement))
                return result

            with mock.patch.object(
                pipeline_module, "validate_document", side_effect=mutate_after_validate
            ):
                summary = finalize_review(
                    evaluation, review, root / "acceptance-report.json"
                )

            self.assertEqual(summary["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
