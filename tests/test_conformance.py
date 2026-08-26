"""Independent conformance and adversarial checks for acceptance model 1.0.0."""

from __future__ import annotations

import copy
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
from xml.etree import ElementTree

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
EXACT_NODE = Path("/private/tmp/reconstructing-raster-icons-node/node_modules/node/bin/node")
PINNED_NODE_SHA256 = "e2d4915d03eda6a2f00a09920e7eeb7a04ad123f9aaad61b1481179fe1bf50e0"
PINNED_LOADER_SHA256 = "10170d02d816f02ec76f9bc095b01d9becf536e7b1e12e5aa616652c84b237a1"
PINNED_WASM_SHA256 = "22bf6e9f9a100d972da0411a69c5ba504367fc1fa87b3b64e3f35e53926d2d70"
PINNED_RUNNER_SHA256 = "16011161fad6c9b585ce477aeff2d811abafbd767eee26612055259c610b8e5a"


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_test_renderer_contract() -> tuple[Path, Path, Path]:
    if not EXACT_NODE.is_file():
        raise AssertionError(f"exact Node 22.14.0 test fixture is unavailable: {EXACT_NODE}")
    loader = REPOSITORY / "node_modules" / "@resvg" / "resvg-wasm" / "index.mjs"
    wasm = REPOSITORY / "node_modules" / "@resvg" / "resvg-wasm" / "index_bg.wasm"
    runner = REPOSITORY / "scripts" / "render_svg.mjs"
    expected = {
        EXACT_NODE: PINNED_NODE_SHA256,
        loader: PINNED_LOADER_SHA256,
        wasm: PINNED_WASM_SHA256,
        runner: PINNED_RUNNER_SHA256,
    }
    mismatches = [path.name for path, digest in expected.items() if _sha256(path) != digest]
    if mismatches:
        raise AssertionError(f"pinned test renderer hash mismatch: {', '.join(mismatches)}")
    lock = _read_json(REPOSITORY / "canonical-renderer.lock")
    if (
        lock.get("node_version") != "22.14.0"
        or lock.get("package_version") != "2.6.2"
        or lock.get("render_options")
        != {
            "background": None,
            "crop": None,
            "current_color": "#000000",
            "font_load_system_fonts": False,
            "shape_rendering": 2,
            "text_rendering": 2,
        }
    ):
        raise AssertionError("canonical-renderer.lock does not match the test render contract")
    return loader, wasm, runner


def _renderer_contract(
    calls: list[tuple[str, tuple[int, int], str]],
):
    loader, wasm, runner = _verify_test_renderer_contract()

    def render(document, size: tuple[int, int], workspace: Path) -> RenderResult:
        candidate_hash = hashlib.sha256(document.xml_bytes).hexdigest()
        workspace.mkdir(parents=True, exist_ok=True)
        resolved_workspace = workspace.resolve()
        with tempfile.TemporaryDirectory(
            prefix=".pinned-test-render-", dir=resolved_workspace
        ) as directory:
            private = Path(directory).resolve()
            private_runner = private / "scripts" / "render_svg.mjs"
            private_loader = private / "node_modules" / "@resvg" / "resvg-wasm" / "index.mjs"
            private_wasm = private / "node_modules" / "@resvg" / "resvg-wasm" / "index_bg.wasm"
            candidate = private / "candidate.svg"
            output = private / "render.png"
            private_runner.parent.mkdir(parents=True)
            private_loader.parent.mkdir(parents=True)
            shutil.copyfile(runner, private_runner)
            shutil.copyfile(loader, private_loader)
            shutil.copyfile(wasm, private_wasm)
            candidate.write_bytes(document.xml_bytes)
            nonce = hashlib.sha256(
                document.xml_bytes + f"{size[0]}x{size[1]}".encode("ascii")
            ).hexdigest()
            completed = subprocess.run(
                [
                    str(EXACT_NODE),
                    "--max-old-space-size=512",
                    "--permission",
                    f"--allow-fs-read={private}",
                    f"--allow-fs-write={private}",
                    str(private_runner),
                    str(candidate),
                    str(output),
                    str(private_wasm),
                    str(size[0]),
                    str(size[1]),
                    nonce,
                    str(REPOSITORY / "package.json"),
                ],
                check=False,
                capture_output=True,
                timeout=15,
                cwd=private,
                env={
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "NODE_NO_WARNINGS": "1",
                    "PATH": str(EXACT_NODE.parent),
                    "TZ": "UTC",
                },
            )
            try:
                attestation = json.loads(completed.stdout.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as error:
                raise AssertionError(
                    "pinned test renderer returned invalid attestation: "
                    f"stdout={completed.stdout!r}, stderr={completed.stderr!r}"
                ) from error
            if (
                completed.returncode != 0
                or completed.stderr
                or attestation.get("render_status") != "ok"
                or attestation.get("nonce") != nonce
                or attestation.get("node_version") != "22.14.0"
            ):
                raise AssertionError(
                    "pinned test renderer failed: "
                    f"returncode={completed.returncode}, stdout={completed.stdout!r}, "
                    f"stderr={completed.stderr!r}"
                )
            payload = output.read_bytes()
        with Image.open(BytesIO(payload)) as image:
            image.load()
            if image.mode != "RGBA" or image.size != size:
                raise AssertionError("pinned test renderer returned the wrong PNG contract")
        output_hash = hashlib.sha256(payload).hexdigest()
        calls.append((candidate_hash, size, output_hash))
        evidence = RendererEvidence(
            platform="darwin-arm64-test-contract",
            node_version="22.14.0",
            renderer_package="@resvg/resvg-wasm",
            renderer_package_version="2.6.2",
            loader_sha256=PINNED_LOADER_SHA256,
            runner_sha256=PINNED_RUNNER_SHA256,
            wasm_sha256=PINNED_WASM_SHA256,
        )
        return RenderResult(
            status=Status.ACCEPTED,
            path=None,
            png_bytes=payload,
            sha256=output_hash,
            size=size,
            diagnostic=(
                "pinned Node 22.14.0/resvg-wasm 2.6.2 test contract render; "
                "not a live Darwin canonical-environment claim"
            ),
            observed=evidence,
            expected=evidence,
            attestation=attestation,
        )

    return render


def _component_variant(
    document,
    components: list[dict[str, object]],
    selected_id: str,
    *,
    isolated: bool,
) -> bytes:
    root = copy.deepcopy(document.root)
    roots = {element.attrib.get("id"): element for element in root.iter() if element.attrib.get("id")}
    for component in components:
        component_id = str(component["component_id"])
        svg_id = str(component["svg_id"])
        element = roots.get(svg_id)
        if element is None:
            raise AssertionError(f"test diagnostic component {svg_id!r} is absent")
        selected = component_id == selected_id
        color = "#ffffff" if selected else ("none" if isolated else "#000000")
        paint_type = str(component["paint_type"])
        for descendant in element.iter():
            if descendant.attrib.get("fill") != "none" and (
                "fill" in descendant.attrib or paint_type in {"fill", "mixed"}
            ):
                descendant.set("fill", color)
            if descendant.attrib.get("stroke") != "none" and (
                "stroke" in descendant.attrib or paint_type in {"stroke", "mixed"}
            ):
                descendant.set("stroke", color)
    ElementTree.register_namespace("", "http://www.w3.org/2000/svg")
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=False)


def _white_component_mask(payload: bytes, size: tuple[int, int]) -> np.ndarray:
    with Image.open(BytesIO(payload)) as image:
        image.load()
        if image.mode != "RGBA" or image.size != size:
            raise AssertionError("diagnostic PNG has the wrong contract")
        rgba = np.asarray(image, dtype=np.uint8)
    luminance = (
        0.2126 * rgba[..., 0].astype(np.float64)
        + 0.7152 * rgba[..., 1].astype(np.float64)
        + 0.0722 * rgba[..., 2].astype(np.float64)
    )
    return np.asarray((rgba[..., 3] >= 128) & (luminance >= 127.5), dtype=bool)


def _alpha_mask(payload: bytes, size: tuple[int, int]) -> np.ndarray:
    with Image.open(BytesIO(payload)) as image:
        image.load()
        if image.mode != "RGBA" or image.size != size:
            raise AssertionError("candidate PNG has the wrong contract")
        return np.asarray(image, dtype=np.uint8)[..., 3] >= 128


def _derived_diagnostics(renderer, capture: dict[str, object]):

    def diagnostics(document, components, size: tuple[int, int], workspace: Path):
        visible: dict[str, np.ndarray] = {}
        isolated: dict[str, np.ndarray] = {}
        hashes: dict[str, str] = {}
        component_records = [dict(item) for item in components]
        for component in component_records:
            component_id = str(component["component_id"])
            for kind, destination in (("visible", visible), ("isolated", isolated)):
                payload = _component_variant(
                    document,
                    component_records,
                    component_id,
                    isolated=kind == "isolated",
                )
                variant = pipeline_module._validate_svg_snapshot(payload)
                result = renderer(variant, size, workspace)
                if result.status != Status.ACCEPTED or not result.png_bytes:
                    raise AssertionError(f"{kind} test diagnostic render failed")
                destination[component_id] = _white_component_mask(result.png_bytes, size)
                hashes[f"{component_id}-{kind}"] = result.sha256
        capture.clear()
        capture.update(
            {
                "visible": {name: mask.copy() for name, mask in visible.items()},
                "isolated": {name: mask.copy() for name, mask in isolated.items()},
                "hashes": dict(hashes),
            }
        )
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
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise AssertionError(f"duplicate JSON key in acceptance golden: {key}")
            value[key] = item
        return value

    return json.loads(GOLDEN.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)


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
        self.assertEqual(
            manifest["renderer_mode"],
            "pinned-node22-resvg-wasm-2.6.2-test-contract",
        )
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
    def test_positive_renderer_contract_changes_when_candidate_geometry_changes(self) -> None:
        case = CONFORMANCE / "analytic-fill"
        original = pipeline_module._validate_svg_snapshot((case / "candidate.svg").read_bytes())
        mutated = pipeline_module._validate_svg_snapshot(
            (case / "candidate.svg").read_bytes().replace(b'width="32"', b'width="24"')
        )
        calls: list[tuple[str, tuple[int, int], str]] = []
        renderer = _renderer_contract(calls)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first = renderer(original, (1024, 1024), workspace)
            second = renderer(mutated, (1024, 1024), workspace)

            prepare_reference(case / "source.png", case / "draft.json", workspace / "reference")
            map_path = workspace / "reference" / "reconstruction-map-r01.json"
            mutated_path = workspace / "mutated.svg"
            mutated_path.write_bytes(mutated.xml_bytes)
            original_capture: dict[str, object] = {}
            mutated_capture: dict[str, object] = {}
            with mock.patch.object(pipeline_module, "_utc_now", return_value=FIXED_TIME):
                evaluate_candidate(
                    map_path,
                    case / "candidate.svg",
                    0,
                    workspace / "original-run",
                    renderer=renderer,
                    diagnostic_renderer=_derived_diagnostics(renderer, original_capture),
                )
                evaluate_candidate(
                    map_path,
                    mutated_path,
                    0,
                    workspace / "mutated-run",
                    renderer=renderer,
                    diagnostic_renderer=_derived_diagnostics(renderer, mutated_capture),
                )
            original_report = _read_json(workspace / "original-run" / "evaluation-i00.json")[
                "report"
            ]
            mutated_report = _read_json(workspace / "mutated-run" / "evaluation-i00.json")["report"]

        self.assertNotEqual(first.sha256, second.sha256)
        self.assertNotEqual(first.png_bytes, second.png_bytes)
        self.assertNotEqual(original_capture["hashes"], mutated_capture["hashes"])
        original_metrics = original_report["metrics"]
        mutated_metrics = mutated_report["metrics"]
        self.assertNotEqual(original_metrics["silhouette_raw"], mutated_metrics["silhouette_raw"])
        self.assertNotEqual(original_metrics["contour_raw"], mutated_metrics["contour_raw"])
        expected = _golden()["pipeline_cases"]["analytic-fill"]["metrics"]
        tolerance = float(_golden()["tolerance"])
        self.assertTrue(
            any(
                abs(float(mutated_metrics[f"{name}_raw"]) - float(value)) > tolerance
                for name, value in expected.items()
            )
        )

    def test_every_pipeline_golden_has_the_exact_automatic_gate_catalog(self) -> None:
        expected_ids = set(AUTOMATIC_GATE_IDS)
        for name, expected in _golden()["pipeline_cases"].items():
            with self.subTest(case=name):
                gates = expected["gates"]
                self.assertEqual(len(gates), len(AUTOMATIC_GATE_IDS))
                self.assertEqual(set(gates), expected_ids)

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
                calls: list[tuple[str, tuple[int, int], str]] = []
                renderer = _renderer_contract(calls)
                draft = _read_json(case / "draft.json")
                components = draft["components"]
                component_ids = [str(item["component_id"]) for item in components]

                with mock.patch.object(pipeline_module, "_utc_now", return_value=FIXED_TIME):
                    prepare_reference(case / "source.png", case / "draft.json", reference_output)
                    map_path = reference_output / "reconstruction-map-r01.json"
                    reports: dict[str, list[dict[str, object]]] = {"run-a": [], "run-b": []}
                    diagnostic_captures: dict[str, list[dict[str, object]]] = {
                        "run-a": [],
                        "run-b": [],
                    }
                    for run_name in reports:
                        run_dir = root / run_name
                        for iteration in range(int(case_record["iterations"])):
                            diagnostic_capture: dict[str, object] = {}
                            summary = evaluate_candidate(
                                map_path,
                                case / "candidate.svg",
                                iteration,
                                run_dir,
                                renderer=renderer,
                                diagnostic_renderer=_derived_diagnostics(
                                    renderer, diagnostic_capture
                                ),
                            )
                            evaluation = _read_json(run_dir / f"evaluation-i{iteration:02d}.json")
                            report = evaluation["report"]
                            reports[run_name].append(report)
                            diagnostic_captures[run_name].append(diagnostic_capture)
                            self.assertEqual(summary["status"], report["status"])

                self.assertEqual(
                    len(calls),
                    2 * int(case_record["iterations"]) * (1 + 2 * len(component_ids)),
                )
                for iteration in range(int(case_record["iterations"])):
                    first = reports["run-a"][iteration]
                    second = reports["run-b"][iteration]
                    self.assertEqual(_normalized_bytes(first), _normalized_bytes(second))
                    self.assertEqual(
                        diagnostic_captures["run-a"][iteration]["hashes"],
                        diagnostic_captures["run-b"][iteration]["hashes"],
                    )
                    for image_name in ("preview", "overlay", "diff"):
                        first_hash = hashlib.sha256(
                            (root / "run-a" / f"{image_name}-i{iteration:02d}.png").read_bytes()
                        ).hexdigest()
                        second_hash = hashlib.sha256(
                            (root / "run-b" / f"{image_name}-i{iteration:02d}.png").read_bytes()
                        ).hexdigest()
                        self.assertEqual(first_hash, second_hash)

                reference, uncertainty = _source_normalization(case / "source.png")
                size = (
                    int(draft["canonical_canvas"]["raster_width"]),
                    int(draft["canonical_canvas"]["raster_height"]),
                )
                candidate_mask = _alpha_mask(
                    (root / "run-a" / f"preview-i{int(case_record['iterations']) - 1:02d}.png").read_bytes(),
                    size,
                )
                reference_components = {
                    component_id: _mask(case / "masks" / f"{component_id}.png")
                    for component_id in component_ids
                }
                final_capture = diagnostic_captures["run-a"][-1]
                visible = final_capture["visible"]
                isolated = final_capture["isolated"]
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
                automatic_gates = [
                    item for item in final_report["gates"] if item["kind"] == "automatic"
                ]
                self.assertEqual(len(automatic_gates), len(AUTOMATIC_GATE_IDS))
                self.assertEqual(
                    [item["gate_id"] for item in automatic_gates], list(AUTOMATIC_GATE_IDS)
                )
                states = {item["gate_id"]: item["state"] for item in automatic_gates}
                self.assertEqual(states, expected["gates"])
                semantic_gates = [
                    item for item in final_report["gates"] if item["kind"] == "semantic"
                ]
                self.assertEqual(
                    [item["gate_id"] for item in semantic_gates], list(SEMANTIC_GATE_IDS)
                )
                self.assertEqual(
                    {item["gate_id"]: item["state"] for item in semantic_gates},
                    golden["semantic_gate_defaults"],
                )

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
