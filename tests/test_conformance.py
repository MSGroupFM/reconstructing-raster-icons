"""Independent conformance and adversarial checks for acceptance model 1.0.0."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image
from jsonschema import ValidationError
from scipy.ndimage import binary_closing, binary_dilation, distance_transform_edt, label

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reconstructing_raster_icons.constants import AUTOMATIC_GATE_IDS, SEMANTIC_GATE_IDS, Status
from reconstructing_raster_icons.errors import InvalidInputError
from reconstructing_raster_icons.metrics import (
    MetricSet,
    component_layout_score,
    composite_score,
    contour_score,
    silhouette_score,
    topology_score,
)
from reconstructing_raster_icons.pipeline import evaluate_candidate, prepare_reference
from reconstructing_raster_icons.raster import canonical_size
from reconstructing_raster_icons.renderer import RenderResult, RendererEvidence
from reconstructing_raster_icons.reports import GateEvidence, GateResult, resolve_status
from reconstructing_raster_icons.schema_io import validate_document
import reconstructing_raster_icons.pipeline as pipeline_module


REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURES = REPOSITORY / "tests" / "fixtures"
CONFORMANCE = FIXTURES / "conformance"
CONTRACTS = FIXTURES / "contracts"
SECURITY = FIXTURES / "security"
GOLDEN = REPOSITORY / "tests" / "goldens" / "acceptance-model-1.0.0.json"
FIXED_TIME = "2026-08-26T12:00:00Z"


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _mask(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        image.load()
        return np.asarray(image.convert("L"), dtype=np.uint8) < 128


def _source_normalization(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Independent dark-on-white coverage and uncertainty for corpus rasters."""
    with Image.open(path) as image:
        values = np.asarray(image.convert("L"), dtype=np.float64) / 255.0
    linear = np.where(
        values <= 0.04045,
        values / 12.92,
        ((values + 0.055) / 1.055) ** 2.4,
    )
    coverage = 1.0 - linear
    reference = coverage >= 0.5
    delta = max(1, math.floor(0.001 * math.hypot(reference.shape[1], reference.shape[0]) + 0.5))
    disk = _disk(delta)
    uncertainty = binary_dilation((coverage > 0.10) & (coverage < 0.90), structure=disk)
    return reference, np.asarray(uncertainty, dtype=bool)


def _disk(radius: int) -> np.ndarray:
    axis = np.arange(-radius, radius + 1, dtype=np.int64)
    yy, xx = np.meshgrid(axis, axis, indexing="ij")
    return (xx * xx + yy * yy) <= radius * radius


def _reference_boundary(mask: np.ndarray) -> np.ndarray:
    """Independent 4-neighbour boundary extraction with off-canvas background."""
    values = np.asarray(mask, dtype=bool)
    padded = np.pad(values, 1, mode="constant", constant_values=False)
    interior = padded[1:-1, 1:-1]
    surrounded = (
        padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
    )
    return interior & ~surrounded


def _reference_silhouette(
    reference_full: np.ndarray,
    candidate_full: np.ndarray,
    uncertainty: np.ndarray,
) -> float:
    """Formula-only tolerant silhouette implementation, separate from production."""
    reference = np.asarray(reference_full, dtype=bool) & ~uncertainty
    candidate = np.asarray(candidate_full, dtype=bool) & ~uncertainty
    if not reference.any() and not candidate.any():
        return 100.0
    if not reference.any() or not candidate.any():
        return 0.0
    diagonal = math.hypot(reference.shape[1], reference.shape[0])
    delta = max(1, math.floor(0.001 * diagonal + 0.5))
    footprint = _disk(delta)
    dilated_reference = binary_dilation(reference, structure=footprint) & ~uncertainty
    dilated_candidate = binary_dilation(candidate, structure=footprint) & ~uncertainty
    precision = np.count_nonzero(candidate & dilated_reference) / np.count_nonzero(candidate)
    recall = np.count_nonzero(reference & dilated_candidate) / np.count_nonzero(reference)
    if precision + recall == 0:
        return 0.0
    return 100.0 * 2.0 * precision * recall / (precision + recall)


def _reference_contour(
    reference_full: np.ndarray,
    candidate_full: np.ndarray,
    uncertainty: np.ndarray,
) -> float:
    """Formula-only bidirectional contour implementation, separate from production."""
    reference = _reference_boundary(reference_full) & ~uncertainty
    candidate = _reference_boundary(candidate_full) & ~uncertainty
    if not reference.any() and not candidate.any():
        return 100.0
    if not reference.any() or not candidate.any():
        return 0.0
    diagonal = math.hypot(reference.shape[1], reference.shape[0])
    delta = max(1, math.floor(0.001 * diagonal + 0.5))
    tau = 0.02 * diagonal

    def directed(source: np.ndarray, target: np.ndarray) -> float:
        distances = distance_transform_edt(~target)[source]
        normalized = np.minimum(np.maximum(distances - delta, 0.0), tau) / tau
        return float(np.mean(normalized, dtype=np.float64))

    symmetric = 0.5 * (directed(reference, candidate) + directed(candidate, reference))
    return max(0.0, min(100.0, 100.0 * (1.0 - symmetric)))


def _geometry(mask: np.ndarray) -> tuple[float, float, float, float, float] | None:
    rows, columns = np.nonzero(mask)
    if rows.size == 0:
        return None
    return (
        float(columns.mean()),
        float(rows.mean()),
        float(columns.max() - columns.min() + 1),
        float(rows.max() - rows.min() + 1),
        float(rows.size),
    )


def _reference_component_score(
    reference: np.ndarray,
    candidate: np.ndarray,
    diagonal: float,
) -> float:
    reference_geometry = _geometry(reference)
    candidate_geometry = _geometry(candidate)
    if reference_geometry is None:
        raise AssertionError("reference component fixtures must be non-degenerate")
    if candidate_geometry is None:
        return 0.0
    rx, ry, rw, rh, ra = reference_geometry
    cx, cy, cw, ch, ca = candidate_geometry
    center = min(1.0, math.hypot(cx - rx, cy - ry) / (0.05 * diagonal))
    width = min(1.0, abs(math.log(cw / rw)) / math.log(1.25))
    height = min(1.0, abs(math.log(ch / rh)) / math.log(1.25))
    area = min(1.0, abs(math.log(ca / ra)) / math.log(1.50))
    return 100.0 * (1.0 - (0.40 * center + 0.20 * width + 0.20 * height + 0.20 * area))


def _reference_layout(
    references: dict[str, np.ndarray],
    candidates: dict[str, np.ndarray],
    weights: dict[str, float],
) -> float:
    shape = next(iter(references.values())).shape
    diagonal = math.hypot(shape[1], shape[0])
    numerator = sum(
        _reference_component_score(references[name], candidates[name], diagonal) * weights[name]
        for name in references
    )
    return numerator / sum(weights.values())


def _holes(mask: np.ndarray) -> int:
    background = ~np.asarray(mask, dtype=bool)
    labels, count = label(
        background,
        structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8),
    )
    edge = set(labels[0, :]) | set(labels[-1, :]) | set(labels[:, 0]) | set(labels[:, -1])
    return sum(index not in edge for index in range(1, count + 1))


def _enclosure(mask: np.ndarray, delta: int) -> np.ndarray:
    closed = binary_closing(mask, structure=_disk(delta), border_value=0)
    background = ~closed
    labels, count = label(
        background,
        structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8),
    )
    edge = set(labels[0, :]) | set(labels[-1, :]) | set(labels[:, 0]) | set(labels[:, -1])
    enclosed_background = np.zeros_like(closed, dtype=bool)
    for index in range(1, count + 1):
        if index not in edge:
            enclosed_background |= labels == index
    return np.asarray(closed | enclosed_background, dtype=bool)


def _fact_f1(expected: set[tuple[object, ...]], observed: set[tuple[object, ...]]) -> float:
    if not expected and not observed:
        return 1.0
    if not expected or not observed:
        return 0.0
    shared = len(expected & observed)
    precision = shared / len(observed)
    recall = shared / len(expected)
    return 2.0 * precision * recall / (precision + recall)


def _reference_topology(
    components: list[dict[str, object]],
    expected_edges: list[dict[str, str]],
    visible: dict[str, np.ndarray],
    isolated: dict[str, np.ndarray],
    uncertainty: np.ndarray,
) -> tuple[float, bool]:
    expected_nodes = {
        (str(component["component_id"]), int(component["expected_hole_count"]))
        for component in components
    }
    observed_nodes = {(name, _holes(mask)) for name, mask in isolated.items()}
    expected = {
        (item["relation"], item["subject"], item["object"])
        for item in expected_edges
    }
    observed: set[tuple[str, str, str]] = set()
    order = {str(component["component_id"]): index for index, component in enumerate(components)}
    ids = list(isolated)
    shape = next(iter(isolated.values())).shape
    delta = max(1, math.floor(0.001 * math.hypot(shape[1], shape[0]) + 0.5))
    declared_paint = {
        frozenset((subject, object_id))
        for relation, subject, object_id in expected
        if relation == "paint_order"
    }
    enclosures = {name: _enclosure(mask, delta) for name, mask in isolated.items() if mask.any()}
    for index, first in enumerate(ids):
        for second in ids[index + 1:]:
            first_mask, second_mask = isolated[first], isolated[second]
            if first_mask.any() and second_mask.any():
                if np.count_nonzero(second_mask & enclosures[first]) / np.count_nonzero(second_mask) >= 0.99:
                    observed.add(("contains", first, second))
                if np.count_nonzero(first_mask & enclosures[second]) / np.count_nonzero(first_mask) >= 0.99:
                    observed.add(("contains", second, first))
            overlap = bool(np.any(first_mask & second_mask))
            ordered_pair = tuple(sorted((first, second)))
            if overlap:
                observed.add(("overlaps", ordered_pair[0], ordered_pair[1]))
            first_visible = visible[first] & ~uncertainty
            second_visible = visible[second] & ~uncertainty
            if (
                first_visible.any()
                and second_visible.any()
                and not np.any(first_visible & second_visible)
                and float(np.min(distance_transform_edt(~second_visible)[first_visible])) <= delta
            ):
                observed.add(("touches", ordered_pair[0], ordered_pair[1]))
            if overlap or frozenset((first, second)) in declared_paint:
                before, after = (first, second) if order[first] < order[second] else (second, first)
                observed.add(("paint_order", before, after))
    node_f1 = _fact_f1(set(expected_nodes), set(observed_nodes))
    edge_f1 = _fact_f1(set(expected), set(observed))
    score = 50.0 * node_f1 + 50.0 * edge_f1
    return score, expected_nodes == observed_nodes and expected == observed


def _reference_metrics(
    reference: np.ndarray,
    candidate: np.ndarray,
    uncertainty: np.ndarray,
    reference_components: dict[str, np.ndarray],
    candidate_visible: dict[str, np.ndarray],
    candidate_isolated: dict[str, np.ndarray],
    component_records: list[dict[str, object]],
    expected_edges: list[dict[str, str]],
) -> dict[str, float]:
    weights = {
        str(component["component_id"]): float(component["weight"])
        for component in component_records
    }
    silhouette = _reference_silhouette(reference, candidate, uncertainty)
    contour = _reference_contour(reference, candidate, uncertainty)
    layout = _reference_layout(reference_components, candidate_visible, weights)
    topology, _ = _reference_topology(
        component_records,
        expected_edges,
        candidate_visible,
        candidate_isolated,
        uncertainty,
    )
    score = 0.45 * silhouette + 0.30 * contour + 0.15 * layout + 0.10 * topology
    return {
        "silhouette": silhouette,
        "contour": contour,
        "layout": layout,
        "topology": topology,
        "composite": score,
    }


def _png_bytes(mask: np.ndarray) -> bytes:
    rgba = np.zeros((*mask.shape, 4), dtype=np.uint8)
    rgba[mask, :3] = 0
    rgba[mask, 3] = 255
    output = BytesIO()
    Image.fromarray(rgba).save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _renderer_double(mask: np.ndarray, calls: list[tuple[int, int]]):
    payload = _png_bytes(mask)

    def render(document, size: tuple[int, int], workspace: Path) -> RenderResult:
        calls.append(size)
        if mask.shape != (size[1], size[0]):
            raise AssertionError("fixture renderer mask does not match the requested canvas")
        return RenderResult(
            status=Status.ACCEPTED,
            path=None,
            png_bytes=payload,
            sha256=hashlib.sha256(payload).hexdigest(),
            size=size,
            diagnostic="deterministic test contract double; not a live canonical render",
            observed=RendererEvidence(node_version="test-contract-double"),
            expected=RendererEvidence(node_version="test-contract-double"),
            attestation={"render_status": "test-contract-double"},
        )

    return render


def _diagnostics_double(case: Path, component_ids: list[str]):
    visible = {
        name: _mask(case / "candidate-components" / f"{name}-visible.png")
        for name in component_ids
    }
    isolated = {
        name: _mask(case / "candidate-components" / f"{name}-isolated.png")
        for name in component_ids
    }

    def diagnostics(document, components, size: tuple[int, int], workspace: Path):
        return {"visible": visible, "isolated": isolated}

    return diagnostics


def _normalize_report(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _normalize_report(item)
            for key, item in value.items()
            if key != "run_id" and not key.endswith("_at")
        }
    if isinstance(value, list):
        return [_normalize_report(item) for item in value]
    return value


def _normalized_bytes(report: dict[str, object]) -> bytes:
    return (
        json.dumps(_normalize_report(report), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _golden() -> dict[str, object]:
    if not GOLDEN.is_file():
        raise AssertionError(
            "missing independently checked golden: tests/goldens/acceptance-model-1.0.0.json"
        )
    return _read_json(GOLDEN)


class FixtureGeneratorTests(unittest.TestCase):
    def test_generator_is_reproducible_and_preserves_task3_baseline_targets(self) -> None:
        script = FIXTURES / "build_fixtures.py"
        with tempfile.TemporaryDirectory() as first_directory, tempfile.TemporaryDirectory() as second_directory:
            first = Path(first_directory)
            second = Path(second_directory)
            subprocess.run([sys.executable, str(script), "--root", str(first)], check=True)
            subprocess.run([sys.executable, str(script), "--root", str(second)], check=True)
            first_files = {path.relative_to(first) for path in first.rglob("*") if path.is_file()}
            second_files = {path.relative_to(second) for path in second.rglob("*") if path.is_file()}

            self.assertEqual(first_files, second_files)
            self.assertNotIn(Path("contracts/valid-draft.json"), first_files)
            self.assertNotIn(Path("contracts/valid-map.json"), first_files)
            self.assertNotIn(Path("security/doctype.svg"), first_files)
            self.assertNotIn(Path("security/external-image.svg"), first_files)
            for relative in sorted(first_files):
                self.assertEqual((first / relative).read_bytes(), (second / relative).read_bytes())
                self.assertEqual((first / relative).read_bytes(), (FIXTURES / relative).read_bytes())

    def test_manifest_declares_every_required_synthetic_fixture_class(self) -> None:
        manifest = _read_json(CONFORMANCE / "manifest.json")
        self.assertEqual(manifest["provenance"], "synthetic-original")
        self.assertEqual(manifest["renderer_mode"], "deterministic-test-contract-double")
        self.assertEqual(
            set(manifest["fixture_classes"]),
            {
                "analytic-fill",
                "open-stroke-caps-joins",
                "ring-hole-nested-topology",
                "cleaned-organic-curve",
                "noisy-antialiasing",
                "non-square-16x9",
                "impossible-target-iteration-stall",
                "meaningful-multicolor-rejection",
                "missing-component-high-silhouette",
                "overlap-occlusion-enclosure",
                "uncertainty-boundary-no-artificial-contour",
                "status-exit-precedence",
                "ratio-extremes",
                "malicious-svg-raster",
            },
        )


class IndependentMetricGoldenTests(unittest.TestCase):
    def test_test_side_reference_formulas_and_production_match_six_decimal_goldens(self) -> None:
        manifest = _read_json(CONFORMANCE / "manifest.json")
        golden = _golden()
        tolerance = float(golden["tolerance"])
        for record in manifest["metric_cases"]:
            name = str(record["name"])
            with self.subTest(case=name):
                reference = _mask(FIXTURES / str(record["reference"]))
                candidate = _mask(FIXTURES / str(record["candidate"]))
                uncertainty = _mask(FIXTURES / str(record["uncertainty"]))
                independent = _reference_metrics(
                    reference,
                    candidate,
                    uncertainty,
                    {"mark": reference},
                    {"mark": candidate},
                    {"mark": candidate},
                    [{"component_id": "mark", "expected_hole_count": _holes(reference), "weight": 1}],
                    [],
                )
                expected = golden["metric_cases"][name]
                for metric_name, expected_value in expected["metrics"].items():
                    self.assertEqual(round(independent[metric_name], 6), expected_value)

                layout = component_layout_score({"mark": reference}, {"mark": candidate})
                topology = topology_score(
                    {("mark", _holes(reference))},
                    set(),
                    visible_masks={"mark": candidate},
                    isolated_masks={"mark": candidate},
                    paint_order=("mark",),
                    uncertainty=uncertainty,
                )
                production = {
                    "silhouette": silhouette_score(reference, candidate, uncertainty),
                    "contour": contour_score(reference, candidate, uncertainty),
                    "layout": layout.score,
                    "topology": topology.score,
                }
                production["composite"] = composite_score(
                    MetricSet(
                        production["silhouette"],
                        production["contour"],
                        production["layout"],
                        production["topology"],
                    )
                )
                for metric_name, expected_value in expected["metrics"].items():
                    if expected.get("identity"):
                        self.assertEqual(production[metric_name], 100.0)
                    self.assertLessEqual(abs(production[metric_name] - expected_value), tolerance)


class PipelineCorpusTests(unittest.TestCase):
    def test_every_evaluation_is_repeatable_and_matches_independent_goldens(self) -> None:
        manifest = _read_json(CONFORMANCE / "manifest.json")
        golden = _golden()
        tolerance = float(golden["tolerance"])
        for case_record in manifest["pipeline_cases"]:
            name = str(case_record["name"])
            case = FIXTURES / str(case_record["path"])
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                reference_output = root / "reference"
                calls: list[tuple[int, int]] = []
                candidate_mask = _mask(case / "candidate-mask.png")
                renderer = _renderer_double(candidate_mask, calls)
                draft = _read_json(case / "draft.json")
                components = draft["components"]
                component_ids = [str(item["component_id"]) for item in components]
                diagnostics = _diagnostics_double(case, component_ids)

                with mock.patch.object(pipeline_module, "_utc_now", return_value=FIXED_TIME):
                    prepare_reference(case / "source.png", case / "draft.json", reference_output)
                    map_path = reference_output / "reconstruction-map-r01.json"
                    reports: dict[str, list[dict[str, object]]] = {"run-a": [], "run-b": []}
                    for run_name in reports:
                        run_dir = root / run_name
                        for iteration in range(int(case_record["iterations"])):
                            summary = evaluate_candidate(
                                map_path,
                                case / "candidate.svg",
                                iteration,
                                run_dir,
                                renderer=renderer,
                                diagnostic_renderer=diagnostics,
                            )
                            evaluation = _read_json(run_dir / f"evaluation-i{iteration:02d}.json")
                            report = evaluation["report"]
                            reports[run_name].append(report)
                            self.assertEqual(summary["status"], report["status"])

                self.assertEqual(len(calls), 2 * int(case_record["iterations"]))
                for iteration in range(int(case_record["iterations"])):
                    first = reports["run-a"][iteration]
                    second = reports["run-b"][iteration]
                    self.assertEqual(_normalized_bytes(first), _normalized_bytes(second))
                    for image_name in ("preview", "overlay", "diff"):
                        first_hash = hashlib.sha256(
                            (root / "run-a" / f"{image_name}-i{iteration:02d}.png").read_bytes()
                        ).hexdigest()
                        second_hash = hashlib.sha256(
                            (root / "run-b" / f"{image_name}-i{iteration:02d}.png").read_bytes()
                        ).hexdigest()
                        self.assertEqual(first_hash, second_hash)

                reference, uncertainty = _source_normalization(case / "source.png")
                reference_components = {
                    component_id: _mask(case / "masks" / f"{component_id}.png")
                    for component_id in component_ids
                }
                visible = {
                    component_id: _mask(
                        case / "candidate-components" / f"{component_id}-visible.png"
                    )
                    for component_id in component_ids
                }
                isolated = {
                    component_id: _mask(
                        case / "candidate-components" / f"{component_id}-isolated.png"
                    )
                    for component_id in component_ids
                }
                independent = _reference_metrics(
                    reference,
                    candidate_mask,
                    uncertainty,
                    reference_components,
                    visible,
                    isolated,
                    components,
                    draft["topology_facts"],
                )
                expected = golden["pipeline_cases"][name]
                for metric_name, expected_value in expected["metrics"].items():
                    self.assertEqual(round(independent[metric_name], 6), expected_value)
                final_report = reports["run-a"][-1]
                report_metrics = final_report["metrics"]
                for metric_name, expected_value in expected["metrics"].items():
                    raw_name = f"{metric_name}_raw"
                    if expected.get("identity"):
                        self.assertEqual(report_metrics[raw_name], 100.0)
                    self.assertLessEqual(abs(float(report_metrics[raw_name]) - expected_value), tolerance)
                self.assertEqual(final_report["status"], expected["status"])
                self.assertEqual(final_report["limit_state"], expected["limit_state"])
                states = {item["gate_id"]: item["state"] for item in final_report["gates"]}
                for gate_id, state in expected["gates"].items():
                    self.assertEqual(states[gate_id], state)

                if name == "missing-component":
                    self.assertGreater(float(report_metrics["silhouette_raw"]), 99.9)
                    self.assertEqual(states["auto.components.present"], "fail")
                    self.assertEqual(final_report["status"], "not_accepted")
                elif name == "widescreen-16x9":
                    self.assertEqual(final_report["viewport"]["aspect_ratio"], "16:9")
                    self.assertEqual(
                        final_report["viewport"]["canonical_canvas"],
                        {"width": 64, "height": 36, "raster_width": 1024, "raster_height": 576},
                    )
                    with Image.open(root / "run-a" / "preview-i00.png") as preview:
                        self.assertEqual(preview.size, (1024, 576))
                elif name == "noisy-antialias":
                    self.assertGreater(final_report["uncertainty"]["pixels"], 0)
                    self.assertEqual(report_metrics["silhouette_raw"], 100.0)
                    self.assertEqual(report_metrics["contour_raw"], 100.0)
                elif name == "impossible-target":
                    self.assertEqual(final_report["limit_state"], "stalled")
                    self.assertEqual(final_report["stop_reason"], "stalled")
                elif name == "multicolor-rejection":
                    self.assertEqual(states["auto.style.monochrome"], "fail")
                elif name == "ring-hole":
                    self.assertEqual(states["auto.topology.facts"], "pass")
                    self.assertIn(
                        {"relation": "contains", "subject": "ring", "object": "inner"},
                        final_report["topology_facts"],
                    )
                elif name == "occlusion-overlap":
                    self.assertEqual(states["auto.topology.facts"], "pass")


class ContractAndStatusCorpusTests(unittest.TestCase):
    def test_ratio_extremes_and_16x9_contracts(self) -> None:
        for name in (
            "conformance-valid-ratio-16x9-draft.json",
            "conformance-valid-ratio-16x1-draft.json",
            "conformance-valid-ratio-1x16-draft.json",
        ):
            with self.subTest(fixture=name):
                validate_document(_read_json(CONTRACTS / name), "reconstruction-map-draft")
        for name in (
            "conformance-invalid-ratio-17x1-draft.json",
            "conformance-invalid-ratio-1x17-draft.json",
        ):
            with self.subTest(fixture=name):
                with self.assertRaises(ValidationError):
                    validate_document(_read_json(CONTRACTS / name), "reconstruction-map-draft")

        from fractions import Fraction

        self.assertEqual(canonical_size(Fraction(16, 1)), (1024, 64))
        self.assertEqual(canonical_size(Fraction(1, 16)), (64, 1024))
        with self.assertRaises(InvalidInputError):
            canonical_size(Fraction(17, 1))
        with self.assertRaises(InvalidInputError):
            canonical_size(Fraction(1, 17))

    def test_status_and_exit_code_precedence_fixture(self) -> None:
        timestamp = datetime(2026, 8, 26, tzinfo=timezone.utc)
        cases = _read_json(CONFORMANCE / "status-precedence.json")["cases"]
        for case in cases:
            with self.subTest(case=case["name"]):
                automatic = [
                    GateResult(
                        gate_id=gate_id,
                        kind="automatic",
                        state="fail" if case["gate"] == "fail" and index == 0 else "pass",
                        evidence=GateEvidence(basis="synthetic conformance fact"),
                        evaluator="independent-conformance",
                        timestamp=timestamp,
                    )
                    for index, gate_id in enumerate(AUTOMATIC_GATE_IDS)
                ]
                semantic = [
                    GateResult(
                        gate_id=gate_id,
                        kind="semantic",
                        state="not_evaluated",
                        evidence=GateEvidence(basis="review pending"),
                        evaluator="independent-conformance",
                        timestamp=timestamp,
                    )
                    for gate_id in SEMANTIC_GATE_IDS
                ]
                resolution = resolve_status(
                    score=float(case["score"]),
                    target=float(case["target"]),
                    gates=automatic + semantic,
                    invalid_input=bool(case["invalid_input"]),
                    runtime_error=bool(case["runtime_error"]),
                    canonical_environment=bool(case["canonical"]),
                )
                self.assertEqual(resolution.status.value, case["status"])
                self.assertEqual(int(resolution.exit_code), case["exit_code"])


class AdversarialCorpusTests(unittest.TestCase):
    def test_malicious_svg_stops_before_renderer_invocation(self) -> None:
        case = CONFORMANCE / "analytic-fill"
        malicious = (
            SECURITY / "doctype.svg",
            SECURITY / "external-image.svg",
            SECURITY / "entity.svg",
            SECURITY / "processing-instruction.svg",
            SECURITY / "script.svg",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(pipeline_module, "_utc_now", return_value=FIXED_TIME):
                prepare_reference(case / "source.png", case / "draft.json", root / "reference")
            map_path = root / "reference" / "reconstruction-map-r01.json"
            for candidate in malicious:
                renderer = mock.Mock(side_effect=AssertionError("renderer must not run"))
                run_dir = root / candidate.stem
                with self.subTest(candidate=candidate.name):
                    with self.assertRaises(InvalidInputError):
                        evaluate_candidate(map_path, candidate, 0, run_dir, renderer=renderer)
                    renderer.assert_not_called()
                    self.assertFalse((run_dir / "evaluation-i00.json").exists())
                    self.assertFalse((run_dir / "preview-i00.png").exists())

    def test_malicious_raster_stops_workflow_before_renderer_invocation(self) -> None:
        case = CONFORMANCE / "analytic-fill"
        for source in (SECURITY / "decompression-bomb.png", SECURITY / "oversize-side.png"):
            with self.subTest(source=source.name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                draft = _read_json(case / "draft.json")
                draft["source_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
                (root / "masks").mkdir()
                shutil.copyfile(case / "masks" / "mark.png", root / "masks" / "mark.png")
                draft_path = root / "draft.json"
                draft_path.write_text(json.dumps(draft), encoding="utf-8")
                renderer = mock.Mock(side_effect=AssertionError("renderer must not run"))

                def run_workflow() -> None:
                    prepare_reference(source, draft_path, root / "reference")
                    evaluate_candidate(
                        root / "reference" / "reconstruction-map-r01.json",
                        case / "candidate.svg",
                        0,
                        root / "run",
                        renderer=renderer,
                    )

                with self.assertRaises(InvalidInputError):
                    run_workflow()
                renderer.assert_not_called()
                self.assertFalse((root / "run" / "evaluation-i00.json").exists())


if __name__ == "__main__":
    unittest.main()
