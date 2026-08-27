#!/usr/bin/env python3
"""Build the original synthetic conformance and adversarial fixture corpus."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw


HERE = Path(__file__).resolve().parent
STAMP = "2026-08-26T00:00:00Z"


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _png(path: Path, pixels: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.asarray(pixels, dtype=np.uint8)).save(
        path,
        format="PNG",
        optimize=False,
        compress_level=9,
    )


def _luma(mask: np.ndarray) -> np.ndarray:
    return np.where(mask, 0, 255).astype(np.uint8)


def _rect(shape: tuple[int, int], box: tuple[int, int, int, int]) -> np.ndarray:
    result = np.zeros(shape, dtype=bool)
    left, top, right, bottom = box
    result[top:bottom, left:right] = True
    return result


def _ring(
    shape: tuple[int, int],
    outer: tuple[int, int, int, int],
    inner: tuple[int, int, int, int],
) -> np.ndarray:
    result = _rect(shape, outer)
    left, top, right, bottom = inner
    result[top:bottom, left:right] = False
    return result


def _polygon(shape: tuple[int, int], points: list[tuple[float, float]]) -> np.ndarray:
    canvas = Image.new("L", (shape[1], shape[0]), 0)
    ImageDraw.Draw(canvas).polygon(points, fill=255)
    return np.asarray(canvas, dtype=np.uint8) >= 128


def _stroke(shape: tuple[int, int]) -> np.ndarray:
    scale = shape[1] / 64.0
    points = [(12 * scale, 44 * scale), (24 * scale, 20 * scale),
              (40 * scale, 44 * scale), (52 * scale, 20 * scale)]
    width = round(4 * scale)
    canvas = Image.new("L", (shape[1], shape[0]), 0)
    draw = ImageDraw.Draw(canvas)
    draw.line(points, fill=255, width=width, joint="curve")
    radius = width / 2
    for x, y in (points[0], points[-1]):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)
    return np.asarray(canvas, dtype=np.uint8) >= 128


def _organic(shape: tuple[int, int]) -> np.ndarray:
    height, width = shape
    points: list[tuple[float, float]] = []
    for index in range(96):
        angle = 2.0 * math.pi * index / 96.0
        radius = 0.285 + 0.025 * math.sin(3.0 * angle) + 0.012 * math.cos(5.0 * angle)
        points.append(
            (
                width * (0.50 + radius * math.cos(angle)),
                height * (0.50 + radius * math.sin(angle)),
            )
        )
    return _polygon(shape, points)


def _component(
    component_id: str,
    *,
    paint_type: str = "fill",
    geometry: str = "polygon",
    holes: int = 0,
    weight: float = 1.0,
) -> dict[str, object]:
    return {
        "component_id": component_id,
        "svg_id": component_id,
        "paint_type": paint_type,
        "mandatory": True,
        "weight": weight,
        "source_mask_path": f"masks/{component_id}.png",
        "geometry": geometry,
        "expected_hole_count": holes,
        "applicable_gates": [
            "auto.components.present",
            "semantic.components.complete",
        ],
    }


def _draft(
    *,
    source_sha256: str,
    components: list[dict[str, object]],
    ratio: str = "1:1",
    view_box: tuple[int, int] = (64, 64),
    raster: tuple[int, int] = (1024, 1024),
    target: float = 98.0,
    topology: list[dict[str, str]] | None = None,
    strokes: list[dict[str, object]] | None = None,
    intentional_intersections: list[dict[str, str]] | None = None,
    source_color_scope: dict[str, object] | None = None,
) -> dict[str, object]:
    draft: dict[str, object] = {
        "schema_kind": "reconstruction-map-draft",
        "schema_version": "1.0.0",
        "map_revision": 1,
        "created_at": STAMP,
        "source_sha256": source_sha256,
        "accuracy_target": target,
        "accuracy_confirmed": True,
        "foreground_color": "currentColor",
        "normalization": {
            "foreground_polarity": "dark",
            "background_decision": "opaque",
            "threshold": 0.5,
            "estimator_basis": "automatic",
            "estimator": {
                "background_luminance": 1,
                "dark_foreground_luminance": 0,
                "light_foreground_luminance": 1,
                "border_variance": 0,
                "dark_contrast": 1,
                "light_contrast": 0,
                "contrast": 1,
            },
        },
        "viewport": {
            "aspect_ratio": ratio,
            "grid": 64,
            "view_box": [0, 0, view_box[0], view_box[1]],
            "fit_mode": "contain",
            "alignment": "center",
        },
        "canonical_canvas": {
            "width": view_box[0],
            "height": view_box[1],
            "raster_width": raster[0],
            "raster_height": raster[1],
        },
        "target_sizes": [128, 64, 32, 24],
        "components": components,
        "topology_facts": topology or [],
        "semantic_facts": [],
        "geometry_constraints": {
            "lines": [],
            "orthogonality": [],
            "parallelism": [],
            "endpoints": [],
            "radial": [],
            "symmetry": [],
            "strokes": strokes or [],
            "intentional_intersections": intentional_intersections or [],
            "minimum_intentional_gaps": [],
        },
        "applicable_gates": [
            "auto.components.present",
            "semantic.components.complete",
        ],
        "ambiguities": [],
        "user_confirmations": [
            {
                "decision": "accuracy target",
                "confirmed": True,
                "confirmed_at": STAMP,
            }
        ],
        "refinement_limit": 8,
    }
    if source_color_scope is not None:
        draft["source_color_scope"] = source_color_scope
    return draft


def _write_case(
    root: Path,
    name: str,
    *,
    source_luma: np.ndarray,
    reference_components: dict[str, np.ndarray],
    svg: str,
    components: list[dict[str, object]],
    ratio: str = "1:1",
    view_box: tuple[int, int] = (64, 64),
    target: float = 98.0,
    topology: list[dict[str, str]] | None = None,
    strokes: list[dict[str, object]] | None = None,
    intentional_intersections: list[dict[str, str]] | None = None,
    source_color_scope: dict[str, object] | None = None,
    iterations: int = 1,
) -> dict[str, object]:
    case = root / "conformance" / name
    source = case / "source.png"
    _png(source, source_luma)
    for component_id, mask in reference_components.items():
        _png(case / "masks" / f"{component_id}.png", _luma(mask))
    (case / "candidate.svg").write_text(svg + "\n", encoding="utf-8")
    height, width = source_luma.shape[:2]
    draft = _draft(
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        components=components,
        ratio=ratio,
        view_box=view_box,
        raster=(width, height),
        target=target,
        topology=topology,
        strokes=strokes,
        intentional_intersections=intentional_intersections,
        source_color_scope=source_color_scope,
    )
    _json(case / "draft.json", draft)
    return {
        "name": name,
        "path": f"conformance/{name}",
        "iterations": iterations,
    }


def _pipeline_cases(root: Path) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    square = (1024, 1024)
    rectangle = _rect(square, (256, 256, 768, 768))
    cases.append(
        _write_case(
            root,
            "analytic-fill",
            source_luma=_luma(rectangle),
            reference_components={"mark": rectangle},
            svg=(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<rect id="mark" x="16" y="16" width="32" height="32" '
                'fill="currentColor"/></svg>'
            ),
            components=[_component("mark")],
        )
    )

    stroke = _stroke(square)
    cases.append(
        _write_case(
            root,
            "open-stroke",
            source_luma=_luma(stroke),
            reference_components={"stroke": stroke},
            svg=(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<path id="stroke" d="M12 44 L24 20 L40 44 L52 20" fill="none" '
                'stroke="currentColor" stroke-width="4" stroke-linecap="round" '
                'stroke-linejoin="round"/></svg>'
            ),
            components=[_component("stroke", paint_type="stroke", geometry="organic")],
            strokes=[
                {
                    "component_id": "stroke",
                    "expected_width": 64,
                    "cap": "round",
                    "join": "round",
                }
            ],
        )
    )

    ring = _ring(square, (192, 192, 832, 832), (320, 320, 704, 704))
    inner = _rect(square, (448, 448, 576, 576))
    combined_ring = ring | inner
    cases.append(
        _write_case(
            root,
            "ring-hole",
            source_luma=_luma(combined_ring),
            reference_components={"ring": ring, "inner": inner},
            svg=(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<path id="ring" fill="currentColor" fill-rule="evenodd" '
                'd="M12 12 H52 V52 H12 Z M20 20 V44 H44 V20 Z"/>'
                '<rect id="inner" x="28" y="28" width="8" height="8" '
                'fill="currentColor"/></svg>'
            ),
            components=[_component("ring", holes=1), _component("inner")],
            topology=[{"relation": "contains", "subject": "ring", "object": "inner"}],
        )
    )

    organic = _organic(square)
    cases.append(
        _write_case(
            root,
            "organic-curve",
            source_luma=_luma(organic),
            reference_components={"organic": organic},
            svg=(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<path id="organic" fill="currentColor" d="M15 31 C14 20 23 13 33 15 '
                'C45 13 52 23 49 34 C52 45 42 52 31 49 C20 52 12 42 15 31 Z"/></svg>'
            ),
            components=[_component("organic", geometry="organic")],
        )
    )

    solid = _rect(square, (256, 256, 768, 768))
    noisy = _luma(solid)
    noisy[255, 256:768] = 187
    noisy[768, 256:768] = 187
    noisy[256:768, 255] = 187
    noisy[256:768, 768] = 187
    cases.append(
        _write_case(
            root,
            "noisy-antialias",
            source_luma=noisy,
            reference_components={"mark": solid},
            svg=(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<rect id="mark" x="16" y="16" width="32" height="32" '
                'fill="currentColor"/></svg>'
            ),
            components=[_component("mark")],
        )
    )

    landscape_shape = (576, 1024)
    landscape = _rect(landscape_shape, (256, 144, 768, 432))
    cases.append(
        _write_case(
            root,
            "widescreen-16x9",
            source_luma=_luma(landscape),
            reference_components={"mark": landscape},
            svg=(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 36">'
                '<rect id="mark" x="16" y="9" width="32" height="18" '
                'fill="currentColor"/></svg>'
            ),
            components=[_component("mark")],
            ratio="16:9",
            view_box=(64, 36),
        )
    )

    cases.append(
        _write_case(
            root,
            "impossible-target",
            source_luma=_luma(rectangle),
            reference_components={"mark": rectangle},
            svg=(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<rect id="mark" x="17" y="16" width="32" height="32" '
                'fill="currentColor"/></svg>'
            ),
            components=[_component("mark")],
            target=100,
            iterations=4,
        )
    )

    multicolor_source = np.full((1024, 1024, 4), 255, dtype=np.uint8)
    multicolor_source[rectangle, 0] = 255
    multicolor_source[rectangle, 1] = 0
    multicolor_source[rectangle, 2] = 0
    _write_case(
        root,
        "multicolor-rejection",
            source_luma=multicolor_source,
            reference_components={"mark": rectangle},
            svg=(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<g id="mark"><rect x="16" y="16" width="16" height="32" fill="#000000"/>'
                '<rect x="32" y="16" width="16" height="32" fill="#ff0000"/></g></svg>'
            ),
            components=[_component("mark", geometry="mixed")],
            source_color_scope={
                "classification": "meaningful_multicolor",
                "merge_to_monochrome": None,
            },
    )

    dot = _rect(square, (800, 800, 816, 816))
    reference_with_dot = rectangle | dot
    cases.append(
        _write_case(
            root,
            "missing-component",
            source_luma=_luma(reference_with_dot),
            reference_components={"mark": rectangle, "dot": dot},
            svg=(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<rect id="mark" x="16" y="16" width="32" height="32" '
                'fill="currentColor"/><g id="dot" fill="currentColor"/></svg>'
            ),
            components=[_component("mark"), _component("dot")],
        )
    )

    lower_full = _rect(square, (192, 256, 640, 704))
    upper = _rect(square, (448, 384, 832, 768))
    lower_visible = lower_full & ~upper
    overlap_source = lower_full | upper
    cases.append(
        _write_case(
            root,
            "occlusion-overlap",
            source_luma=_luma(overlap_source),
            reference_components={"lower": lower_visible, "upper": upper},
            svg=(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<rect id="lower" x="12" y="16" width="28" height="28" fill="currentColor"/>'
                '<rect id="upper" x="28" y="24" width="24" height="24" fill="currentColor"/>'
                '</svg>'
            ),
            components=[_component("lower"), _component("upper")],
            topology=[
                {"relation": "overlaps", "subject": "lower", "object": "upper"},
                {"relation": "touches", "subject": "lower", "object": "upper"},
                {"relation": "paint_order", "subject": "lower", "object": "upper"},
            ],
            intentional_intersections=[{"first": "lower", "second": "upper"}],
        )
    )
    return cases


def _metric_cases(root: Path) -> list[dict[str, object]]:
    directory = root / "conformance" / "metrics"
    shape = (64, 64)
    identity = _rect(shape, (16, 16, 48, 48))
    shifted = _rect(shape, (18, 17, 50, 49))
    organic = _organic(shape)
    simplified = _polygon(
        shape,
        [(14, 31), (17, 20), (27, 14), (39, 16), (49, 26), (48, 39),
         (38, 50), (25, 48), (15, 40)],
    )
    cases = {
        "analytic-fill-identity": (identity, identity),
        "analytic-fill-shifted": (identity, shifted),
        "organic-cleaned": (organic, simplified),
    }
    result: list[dict[str, object]] = []
    for name, (reference, candidate) in cases.items():
        _png(directory / f"{name}-reference.png", _luma(reference))
        _png(directory / f"{name}-candidate.png", _luma(candidate))
        _png(directory / f"{name}-uncertainty.png", _luma(np.zeros(shape, dtype=bool)))
        result.append(
            {
                "name": name,
                "reference": f"conformance/metrics/{name}-reference.png",
                "candidate": f"conformance/metrics/{name}-candidate.png",
                "uncertainty": f"conformance/metrics/{name}-uncertainty.png",
            }
        )
    return result


def _contract_variants(root: Path) -> None:
    baseline = json.loads((HERE / "contracts" / "valid-draft.json").read_text(encoding="utf-8"))
    variants: dict[str, tuple[str, list[int], list[int]]] = {
        "conformance-valid-ratio-16x9-draft.json": ("16:9", [0, 0, 64, 36], [64, 36, 1024, 576]),
        "conformance-valid-ratio-16x1-draft.json": ("16:1", [0, 0, 64, 4], [64, 4, 1024, 64]),
        "conformance-valid-ratio-1x16-draft.json": ("1:16", [0, 0, 4, 64], [4, 64, 64, 1024]),
        "conformance-invalid-ratio-17x1-draft.json": ("17:1", [0, 0, 64, 3.764706], [64, 3.764706, 1024, 60]),
        "conformance-invalid-ratio-1x17-draft.json": ("1:17", [0, 0, 3.764706, 64], [3.764706, 64, 60, 1024]),
    }
    for name, (ratio, view_box, canvas) in variants.items():
        document = copy.deepcopy(baseline)
        document["viewport"]["aspect_ratio"] = ratio
        document["viewport"]["view_box"] = view_box
        document["canonical_canvas"] = {
            "width": canvas[0],
            "height": canvas[1],
            "raster_width": canvas[2],
            "raster_height": canvas[3],
        }
        _json(root / "contracts" / name, document)


def _security_fixtures(root: Path) -> None:
    security = root / "security"
    security.mkdir(parents=True, exist_ok=True)
    fixtures = {
        "entity.svg": '<!ENTITY payload "boom"><svg xmlns="http://www.w3.org/2000/svg"/>\n',
        "processing-instruction.svg": (
            '<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg">'
            '<?run network?><rect width="1" height="1"/></svg>\n'
        ),
        "script.svg": (
            '<svg xmlns="http://www.w3.org/2000/svg"><script>fetch("https://example.invalid")'
            '</script></svg>\n'
        ),
    }
    for name, payload in fixtures.items():
        (security / name).write_text(payload, encoding="utf-8")
    _png(security / "oversize-side.png", np.full((1, 8193), 255, dtype=np.uint8))
    bomb = Image.new("1", (4001, 4001), color=1)
    bomb.save(
        security / "decompression-bomb.png",
        format="PNG",
        optimize=False,
        compress_level=9,
    )


def _status_cases(root: Path) -> None:
    _json(
        root / "conformance" / "status-precedence.json",
        {
            "cases": [
                {"name": "invalid-outranks-all", "invalid_input": True, "runtime_error": True,
                 "canonical": False, "score": 0, "target": 100, "gate": "fail",
                 "status": "invalid_input", "exit_code": 2},
                {"name": "runtime-outranks-noncanonical", "invalid_input": False,
                 "runtime_error": True, "canonical": False, "score": 0, "target": 100,
                 "gate": "fail", "status": "runtime_error", "exit_code": 7},
                {"name": "noncanonical-outranks-gate", "invalid_input": False,
                 "runtime_error": False, "canonical": False, "score": 0, "target": 100,
                 "gate": "fail", "status": "non_canonical", "exit_code": 6},
                {"name": "gate-outranks-score", "invalid_input": False,
                 "runtime_error": False, "canonical": True, "score": 0, "target": 100,
                 "gate": "fail", "status": "not_accepted", "exit_code": 4},
                {"name": "score-outranks-semantic-pending", "invalid_input": False,
                 "runtime_error": False, "canonical": True, "score": 99, "target": 100,
                 "gate": "pass", "status": "not_accepted", "exit_code": 3},
                {"name": "semantic-pending", "invalid_input": False,
                 "runtime_error": False, "canonical": True, "score": 100, "target": 100,
                 "gate": "pass", "status": "incomplete", "exit_code": 5},
            ]
        },
    )


def build(root: Path) -> None:
    root = root.resolve()
    pipeline_cases = _pipeline_cases(root)
    metric_cases = _metric_cases(root)
    _contract_variants(root)
    _security_fixtures(root)
    _status_cases(root)
    _json(
        root / "conformance" / "manifest.json",
        {
            "corpus_version": "1.0.0",
            "provenance": "synthetic-original",
            "renderer_mode": "pinned-node22-resvg-wasm-2.6.2-pixel-oracle",
            "fixture_classes": [
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
            ],
            "pipeline_cases": pipeline_cases,
            "metric_cases": metric_cases,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=HERE,
        help="fixture root (defaults to tests/fixtures)",
    )
    arguments = parser.parse_args()
    build(arguments.root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
