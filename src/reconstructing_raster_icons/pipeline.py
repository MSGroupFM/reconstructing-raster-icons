"""Immutable prepare, evaluate, and finalize stages for acceptance model 1.0.0."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import copy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any
from xml.etree import ElementTree

import numpy as np
from numpy.typing import ArrayLike, NDArray
from PIL import Image, UnidentifiedImageError
from .constants import (
    ACCEPTANCE_MODEL_VERSION,
    AUTOMATIC_GATE_IDS,
    SCHEMA_VERSION,
    SEMANTIC_GATE_IDS,
    ExitCode,
    Status,
)
from .errors import FrozenArtifactError, InvalidInputError
from .geometry import evaluate_geometry_constraints, flatten_svg_path
from .metrics import (
    MetricSet,
    component_layout_score,
    composite_score,
    contour_score,
    silhouette_score,
    topology_score,
)
from .raster import (
    FrozenPlacement,
    NormalizationDecision,
    apply_frozen_placement,
    build_uncertainty,
    canonical_size,
    load_raster,
    normalize_with_decision,
    place_raster,
)
from .renderer import (
    CANONICAL_LOADER_SHA256,
    CANONICAL_NODE_VERSION,
    CANONICAL_NPM_INTEGRITY,
    CANONICAL_PACKAGE,
    CANONICAL_PACKAGE_VERSION,
    CANONICAL_WASM_SHA256,
    RenderResult,
    render_canonical,
)
from .reports import ArtifactEvidence, GateEvidence, GateResult, resolve_status
from .safe_svg import SVG_NAMESPACE, SafeSvgDocument, validate_svg
from .schema_io import atomic_write_json, validate_document


Renderer = Callable[[SafeSvgDocument, tuple[int, int], Path], RenderResult]
DiagnosticRenderer = Callable[
    [SafeSvgDocument, Sequence[Mapping[str, object]], tuple[int, int], Path],
    Mapping[str, Mapping[str, ArrayLike]],
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(document: object) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, object]:
    source = Path(path)
    try:
        if not source.is_file() or source.is_symlink():
            raise InvalidInputError(f"{label} must be a regular, non-symlink JSON file")

        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise InvalidInputError(f"{label} contains duplicate JSON key {key!r}")
                result[key] = value
            return result

        value = json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except InvalidInputError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InvalidInputError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise InvalidInputError(f"{label} root must be an object")
    return value


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    destination = Path(path)
    if destination.exists():
        raise FrozenArtifactError(f"refusing to overwrite frozen artifact: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise FrozenArtifactError(
                f"refusing to overwrite frozen artifact: {destination}"
            ) from error
        temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_replace_json(path: Path, document: object) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(_json_bytes(document))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _revision_suffix(revision: object) -> str:
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise InvalidInputError("map_revision must be a positive integer")
    return f"r{revision:02d}"


def _iteration_suffix(iteration: object) -> str:
    if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 0:
        raise InvalidInputError("iteration must be a non-negative integer")
    return f"i{iteration:02d}"


def _mask_png(mask: ArrayLike) -> bytes:
    values = np.asarray(mask, dtype=bool)
    if values.ndim != 2 or not values.size:
        raise InvalidInputError("mask must be a nonempty two-dimensional raster")
    output = BytesIO()
    Image.fromarray(np.where(values, 0, 255).astype(np.uint8), mode="L").save(
        output, format="PNG", optimize=False
    )
    return output.getvalue()


def _read_mask(path: Path, expected_hash: str) -> NDArray[np.bool_]:
    source = Path(path)
    try:
        payload = source.read_bytes()
    except OSError as error:
        raise InvalidInputError(f"frozen mask is unavailable: {source.name}") from error
    if _sha256(payload) != expected_hash:
        raise InvalidInputError(f"frozen mask hash mismatch: {source.name}")
    try:
        with Image.open(BytesIO(payload)) as image:
            image.load()
            if image.format != "PNG":
                raise InvalidInputError(f"frozen mask is not PNG: {source.name}")
            return np.asarray(image.convert("L"), dtype=np.uint8) < 128
    except InvalidInputError:
        raise
    except (OSError, UnidentifiedImageError) as error:
        raise InvalidInputError(f"frozen mask cannot be decoded: {source.name}") from error


def _mask_geometry(mask: NDArray[np.bool_]) -> tuple[list[int], list[float], int]:
    rows, columns = np.nonzero(mask)
    if not rows.size:
        raise InvalidInputError("component source mask must have visible area")
    left = int(columns.min())
    top = int(rows.min())
    width = int(columns.max()) - left + 1
    height = int(rows.max()) - top + 1
    return [left, top, width, height], [float(columns.mean()), float(rows.mean())], int(rows.size)


def _normalization_decision(draft: Mapping[str, object]) -> NormalizationDecision:
    normalization = draft["normalization"]
    if not isinstance(normalization, Mapping):
        raise InvalidInputError("normalization must be an object")
    polarity = normalization["foreground_polarity"]
    estimator = normalization["estimator"]
    if not isinstance(estimator, Mapping) or polarity not in {"dark", "light"}:
        raise InvalidInputError("normalization estimator is invalid")
    overrides = normalization.get("explicit_overrides")
    values = overrides if normalization.get("estimator_basis") == "explicit_override" else estimator
    if not isinstance(values, Mapping):
        raise InvalidInputError("explicit normalization values are missing")
    background = values.get("background_luminance")
    foreground_key = (
        "dark_foreground_luminance" if polarity == "dark" else "light_foreground_luminance"
    )
    foreground = values.get("foreground_luminance", values.get(foreground_key))
    try:
        return NormalizationDecision(float(background), float(foreground), polarity)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise InvalidInputError("normalization luminance values are invalid") from error


def _round_fraction(value: Fraction) -> int:
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def _aligned_placement(
    image: Image.Image,
    ratio: Fraction,
    *,
    fit_mode: str,
    alignment: str,
    confirmed: bool,
) -> FrozenPlacement:
    if alignment == "center":
        return place_raster(
            image,
            ratio,
            fit_mode=fit_mode,  # type: ignore[arg-type]
            confirmed=confirmed,
        )
    if alignment not in {
        "top",
        "bottom",
        "left",
        "right",
        "top-left",
        "top-right",
        "bottom-left",
        "bottom-right",
    }:
        raise InvalidInputError("alignment is not supported")
    if fit_mode not in {"contain", "cover", "stretch"}:
        raise InvalidInputError("fit mode must be contain, cover, or stretch")
    if fit_mode != "contain" and not confirmed:
        raise InvalidInputError("cover and stretch require an explicit confirmed decision")
    source_width, source_height = image.size
    canvas_width, canvas_height = canonical_size(ratio)
    if fit_mode == "stretch":
        scale_x = Fraction(canvas_width, source_width)
        scale_y = Fraction(canvas_height, source_height)
        resampled_width, resampled_height = canvas_width, canvas_height
    else:
        width_scale = Fraction(canvas_width, source_width)
        height_scale = Fraction(canvas_height, source_height)
        scale = min(width_scale, height_scale) if fit_mode == "contain" else max(width_scale, height_scale)
        scale_x = scale_y = scale
        resampled_width = _round_fraction(Fraction(source_width) * scale)
        resampled_height = _round_fraction(Fraction(source_height) * scale)
        if fit_mode == "contain":
            resampled_width = min(resampled_width, canvas_width)
            resampled_height = min(resampled_height, canvas_height)
        else:
            resampled_width = max(resampled_width, canvas_width)
            resampled_height = max(resampled_height, canvas_height)

    if "left" in alignment:
        offset_x = 0
    elif "right" in alignment:
        offset_x = canvas_width - resampled_width
    else:
        offset_x = (canvas_width - resampled_width) // 2
    if "top" in alignment:
        offset_y = 0
    elif "bottom" in alignment:
        offset_y = canvas_height - resampled_height
    else:
        offset_y = (canvas_height - resampled_height) // 2

    resampled = image.convert("RGBA").resize(
        (resampled_width, resampled_height), Image.Resampling.LANCZOS
    )
    canvas = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    source_left = max(0, -offset_x)
    source_top = max(0, -offset_y)
    source_right = min(resampled_width, canvas_width - offset_x)
    source_bottom = min(resampled_height, canvas_height - offset_y)
    if source_left < source_right and source_top < source_bottom:
        visible = resampled.crop((source_left, source_top, source_right, source_bottom))
        canvas.alpha_composite(visible, (max(offset_x, 0), max(offset_y, 0)))
    return FrozenPlacement(
        image=canvas,
        source_size=image.size,
        canvas_size=(canvas_width, canvas_height),
        resampled_size=(resampled_width, resampled_height),
        scale_x=float(scale_x),
        scale_y=float(scale_y),
        offset_x=offset_x,
        offset_y=offset_y,
        fit_mode=fit_mode,  # type: ignore[arg-type]
    )


def _component_mask(image: Image.Image, polarity: str) -> NDArray[np.bool_]:
    rgba = np.asarray(image.convert("RGBA"), dtype=np.float64) / 255.0
    alpha = rgba[..., 3]
    luminance = 0.2126 * rgba[..., 0] + 0.7152 * rgba[..., 1] + 0.0722 * rgba[..., 2]
    foreground = luminance < 0.5 if polarity == "dark" else luminance >= 0.5
    return (alpha >= 0.5) & foreground


def prepare_reference(source: Path, draft: Path, output: Path) -> dict[str, object]:
    """Validate and freeze one reconstruction-map revision and all reference masks."""
    source_path = Path(source)
    draft_path = Path(draft)
    output_path = Path(output)
    document = _load_json(draft_path, "reconstruction map draft")
    validate_document(document, "reconstruction-map-draft")
    if document.get("accuracy_confirmed") is not True:
        raise InvalidInputError("accuracy target must be explicitly confirmed")

    try:
        source_payload = source_path.read_bytes()
    except OSError as error:
        raise InvalidInputError("source raster cannot be read") from error
    source_hash = _sha256(source_payload)
    if document.get("source_sha256") != source_hash:
        raise InvalidInputError("source SHA-256 does not match the confirmed draft")

    suffix = _revision_suffix(document["map_revision"])
    map_path = output_path / f"reconstruction-map-{suffix}.json"
    reference_directory = output_path / f"reference-{suffix}"
    report_path = output_path / f"reference-stage-report-{suffix}.json"
    destinations = [
        map_path,
        report_path,
        reference_directory / "reference-mask.png",
        reference_directory / "uncertainty-mask.png",
    ]
    components = document["components"]
    if not isinstance(components, list):
        raise InvalidInputError("components must be an array")
    destinations.extend(
        reference_directory / f"component-{component['component_id']}.png"
        for component in components
        if isinstance(component, Mapping)
    )
    existing = next((path for path in destinations if path.exists()), None)
    if existing is not None:
        raise FrozenArtifactError(f"refusing to overwrite frozen artifact: {existing}")

    source_image = load_raster(source_path)
    ratio_width, ratio_height = str(document["viewport"]["aspect_ratio"]).split(":")  # type: ignore[index]
    ratio = Fraction(int(ratio_width), int(ratio_height))
    fit_mode = str(document["viewport"]["fit_mode"])  # type: ignore[index]
    alignment = str(document["viewport"]["alignment"])  # type: ignore[index]
    placement = _aligned_placement(
        source_image,
        ratio,
        fit_mode=fit_mode,  # type: ignore[arg-type]
        alignment=alignment,
        confirmed=fit_mode == "contain" or any(
            isinstance(item, Mapping)
            and item.get("confirmed") is True
            and "fit" in str(item.get("decision", "")).lower()
            for item in document["user_confirmations"]  # type: ignore[union-attr]
        ),
    )
    decision = _normalization_decision(document)
    normalized = normalize_with_decision(placement.image, decision)
    diagonal = math.hypot(*placement.canvas_size)
    delta = max(1, math.floor(0.001 * diagonal + 0.5))
    uncertainty = build_uncertainty(normalized.coverage, delta)

    reference_payload = _mask_png(normalized.mask)
    uncertainty_payload = _mask_png(uncertainty)
    frozen_components: list[dict[str, object]] = []
    component_payloads: list[tuple[Path, bytes]] = []
    for component in components:
        if not isinstance(component, Mapping):
            raise InvalidInputError("component must be an object")
        source_mask_path = Path(str(component["source_mask_path"]))
        if source_mask_path.is_absolute() or ".." in source_mask_path.parts:
            raise InvalidInputError("component source mask path must stay relative to the draft")
        mask_image = load_raster(draft_path.parent / source_mask_path)
        placed_mask = apply_frozen_placement(mask_image, placement)
        mask = _component_mask(placed_mask, decision.polarity)
        bbox, centroid, area = _mask_geometry(mask)
        payload = _mask_png(mask)
        component_id = str(component["component_id"])
        component_path = reference_directory / f"component-{component_id}.png"
        component_payloads.append((component_path, payload))
        frozen_component = {key: copy.deepcopy(value) for key, value in component.items() if key != "source_mask_path"}
        frozen_component.update(
            {
                "reference_mask": {
                    "logical_id": f"reference-component-{component_id}-{suffix}",
                    "sha256": _sha256(payload),
                },
                "bbox": bbox,
                "centroid": centroid,
                "area": area,
            }
        )
        frozen_components.append(frozen_component)

    frozen_map = {
        key: copy.deepcopy(value)
        for key, value in document.items()
        if key not in {"accuracy_confirmed", "user_confirmations", "components"}
    }
    frozen_map.update(
        {
            "schema_kind": "reconstruction-map",
            "frozen_at": _utc_now(),
            "components": frozen_components,
            "reference_mask": {
                "logical_id": f"reference-mask-{suffix}",
                "sha256": _sha256(reference_payload),
            },
            "uncertainty_mask": {
                "logical_id": f"uncertainty-mask-{suffix}",
                "sha256": _sha256(uncertainty_payload),
            },
        }
    )
    validate_document(frozen_map, "reconstruction-map")
    map_payload = _json_bytes(frozen_map)
    stage_report = {
        "stage": "prepare_reference",
        "stage_version": SCHEMA_VERSION,
        "created_at": _utc_now(),
        "map_revision": document["map_revision"],
        "frozen": True,
        "normalization_runs": 1,
        "artifacts": [
            {"logical_id": f"reconstruction-map-{suffix}", "sha256": _sha256(map_payload)},
            frozen_map["reference_mask"],
            frozen_map["uncertainty_mask"],
            *(component["reference_mask"] for component in frozen_components),
        ],
    }

    _atomic_write_bytes(reference_directory / "reference-mask.png", reference_payload)
    _atomic_write_bytes(reference_directory / "uncertainty-mask.png", uncertainty_payload)
    for component_path, payload in component_payloads:
        _atomic_write_bytes(component_path, payload)
    _atomic_write_bytes(map_path, map_payload)
    atomic_write_json(report_path, stage_report)
    return {
        "ok": True,
        "stage": "prepare_reference",
        "artifact_id": f"reconstruction-map-{suffix}",
        "sha256": _sha256(map_payload),
        "frozen": True,
        "exit_code": int(ExitCode.ACCEPTED),
    }


def is_stalled(
    history: Sequence[float], gate_improvements: Sequence[bool]
) -> bool:
    """Apply the exact three-refinement stall window without float subtraction."""
    if len(history) < 4 or len(gate_improvements) < 3:
        return False
    if any(not isinstance(value, bool) for value in gate_improvements[-3:]):
        raise TypeError("gate improvements must be booleans")
    scores: list[Decimal] = []
    for value in history:
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise TypeError("score history must contain real numbers")
        score = Decimal(str(value))
        if not score.is_finite() or not Decimal("0") <= score <= Decimal("100"):
            raise ValueError("score history values must be finite and within 0..100")
        scores.append(score)
    current_best = max(scores)
    previous_best = max(scores[:-3])
    return current_best - previous_best < Decimal("0.10") and not any(gate_improvements[-3:])


def _canonical_renderer_report() -> dict[str, object]:
    return {
        "renderer_name": CANONICAL_PACKAGE,
        "renderer_version": CANONICAL_PACKAGE_VERSION,
        "runtime_name": "node",
        "runtime_version": CANONICAL_NODE_VERSION,
        "npm_integrity": CANONICAL_NPM_INTEGRITY,
        "wasm_sha256": CANONICAL_WASM_SHA256,
        "loader_sha256": CANONICAL_LOADER_SHA256,
        "profile": {
            "color_space": "sRGB",
            "background": "transparent",
            "current_color": "#000000",
            "shape_rendering": 2,
            "text_rendering": 2,
            "load_system_fonts": False,
            "fit_to": "max_side",
            "crop": False,
        },
    }


def _render_mask(payload: bytes, size: tuple[int, int]) -> NDArray[np.bool_]:
    try:
        with Image.open(BytesIO(payload)) as image:
            image.load()
            if image.size != size or image.mode != "RGBA":
                raise InvalidInputError("renderer output has the wrong size or mode")
            return np.asarray(image, dtype=np.uint8)[..., 3] >= 128
    except InvalidInputError:
        raise
    except (OSError, UnidentifiedImageError) as error:
        raise InvalidInputError("renderer output is not a valid RGBA PNG") from error


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _paint_variant(
    document: SafeSvgDocument,
    components: Sequence[Mapping[str, object]],
    selected_id: str,
    *,
    isolated: bool,
) -> bytes:
    root = copy.deepcopy(document.root)
    id_to_component = {str(item["svg_id"]): str(item["component_id"]) for item in components}
    selected_svg_id = next(
        str(item["svg_id"]) for item in components if str(item["component_id"]) == selected_id
    )
    located = False
    for element in root.iter():
        element_id = element.attrib.get("id")
        if element_id not in id_to_component:
            continue
        located = located or element_id == selected_svg_id
        selected = element_id == selected_svg_id
        color = "#ffffff" if selected else ("none" if isolated else "#000000")
        element_name = _local_name(element.tag)
        paint_type = next(
            str(item["paint_type"]) for item in components if str(item["svg_id"]) == element_id
        )
        if paint_type in {"fill", "mixed"} or element_name in {"g", "path", "rect", "circle", "ellipse", "polygon"}:
            if element.attrib.get("fill") != "none" or paint_type in {"fill", "mixed"}:
                element.set("fill", color)
        if paint_type in {"stroke", "mixed"} or "stroke" in element.attrib:
            if element.attrib.get("stroke") != "none" or paint_type in {"stroke", "mixed"}:
                element.set("stroke", color)
    if not located:
        raise InvalidInputError(f"mandatory SVG component {selected_svg_id!r} is missing")
    ElementTree.register_namespace("", SVG_NAMESPACE)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=False)


def _default_diagnostics(
    document: SafeSvgDocument,
    components: Sequence[Mapping[str, object]],
    size: tuple[int, int],
    workspace: Path,
    renderer: Renderer,
) -> Mapping[str, Mapping[str, ArrayLike]]:
    visible: dict[str, NDArray[np.bool_]] = {}
    isolated: dict[str, NDArray[np.bool_]] = {}
    for component in components:
        component_id = str(component["component_id"])
        for kind, destination in (("visible", visible), ("isolated", isolated)):
            payload = _paint_variant(
                document, components, component_id, isolated=kind == "isolated"
            )
            variant_path = workspace / f".{kind}-{component_id}.svg"
            _atomic_write_bytes(variant_path, payload)
            try:
                variant = validate_svg(variant_path)
                result = renderer(variant, size, workspace)
            finally:
                variant_path.unlink(missing_ok=True)
            if result.status != Status.ACCEPTED or not result.png_bytes:
                raise RuntimeError(
                    result.diagnostic or f"{kind} component diagnostic render was non-canonical"
                )
            destination[component_id] = _render_mask(result.png_bytes, size)
    return {"visible": visible, "isolated": isolated}


def _viewbox_matches(document: SafeSvgDocument, expected: Sequence[object]) -> bool:
    value = document.root.attrib.get("viewBox")
    if not value:
        return False
    try:
        actual = [Decimal(item) for item in value.replace(",", " ").split()]
        wanted = [Decimal(str(item)) for item in expected]
    except Exception:
        return False
    return actual == wanted


def _monochrome(document: SafeSvgDocument) -> bool:
    colors: set[str] = set()
    for element in document.root.iter():
        for name in ("fill", "stroke"):
            value = element.attrib.get(name)
            if value and value != "none":
                colors.add("#000000" if value == "currentColor" else value.lower())
    return len(colors) <= 1


def _candidate_geometry(
    document: SafeSvgDocument,
    components: Sequence[Mapping[str, object]],
    size: tuple[int, int],
    delta: float,
) -> dict[str, dict[str, object]]:
    view_box = document.root.attrib.get("viewBox", "").replace(",", " ").split()
    if len(view_box) != 4:
        raise InvalidInputError("candidate viewBox is required for geometry constraints")
    origin_x, origin_y, view_width, view_height = (float(value) for value in view_box)
    if view_width <= 0.0 or view_height <= 0.0:
        raise InvalidInputError("candidate viewBox dimensions must be positive")
    scale_x = size[0] / view_width
    scale_y = size[1] / view_height

    def point(x: float, y: float) -> tuple[float, float]:
        return (x - origin_x) * scale_x, (y - origin_y) * scale_y

    def number(element: ElementTree.Element, name: str, default: float = 0.0) -> float:
        return float(element.attrib.get(name, default))

    def element_points(element: ElementTree.Element) -> list[tuple[float, float]]:
        name = _local_name(element.tag)
        if name == "line":
            return [
                point(number(element, "x1"), number(element, "y1")),
                point(number(element, "x2"), number(element, "y2")),
            ]
        if name == "rect":
            x, y = number(element, "x"), number(element, "y")
            width, height = number(element, "width"), number(element, "height")
            return [
                point(x, y),
                point(x + width, y),
                point(x + width, y + height),
                point(x, y + height),
                point(x, y),
            ]
        if name in {"polyline", "polygon"}:
            values = [float(value) for value in element.attrib["points"].replace(",", " ").split()]
            points = [point(values[index], values[index + 1]) for index in range(0, len(values), 2)]
            if name == "polygon" and points[0] != points[-1]:
                points.append(points[0])
            return points
        if name in {"circle", "ellipse"}:
            center_x, center_y = number(element, "cx"), number(element, "cy")
            radius_x = number(element, "r") if name == "circle" else number(element, "rx")
            radius_y = number(element, "r") if name == "circle" else number(element, "ry")
            return [
                point(
                    center_x + radius_x * math.cos(2.0 * math.pi * index / 64.0),
                    center_y + radius_y * math.sin(2.0 * math.pi * index / 64.0),
                )
                for index in range(65)
            ]
        if name == "path":
            view_delta = max(delta / max(scale_x, scale_y), 1e-9)
            return [
                point(x, y)
                for subpath in flatten_svg_path(element.attrib["d"], view_delta)
                for x, y in subpath.points
            ]
        return []

    by_id = {element.attrib["id"]: element for element in document.root.iter() if "id" in element.attrib}
    result: dict[str, dict[str, object]] = {}
    for component in components:
        component_id = str(component["component_id"])
        svg_id = str(component["svg_id"])
        root = by_id.get(svg_id)
        if root is None:
            raise InvalidInputError(f"SVG component {svg_id!r} is missing")
        points = [candidate_point for element in root.iter() for candidate_point in element_points(element)]
        if len(points) < 2:
            raise InvalidInputError(f"SVG component {svg_id!r} has no measurable geometry")
        result[component_id] = {
            "points": points,
            "stroke_width": number(root, "stroke-width") * max(scale_x, scale_y),
            "cap": root.attrib.get("stroke-linecap", "butt"),
            "join": root.attrib.get("stroke-linejoin", "miter"),
        }
    return result


def _artifact_gate(
    gate_id: str, passed: bool, logical_id: str, digest: str, timestamp: str
) -> dict[str, object]:
    return {
        "gate_id": gate_id,
        "kind": "automatic",
        "state": "pass" if passed else "fail",
        "evidence": {"artifact_id": logical_id, "sha256": digest},
        "evaluator": "acceptance-model-1.0.0",
        "evaluated_at": timestamp,
    }


def _measurement_gate(
    gate_id: str, passed: bool, measured: float, tolerance: float, timestamp: str
) -> dict[str, object]:
    return {
        "gate_id": gate_id,
        "kind": "automatic",
        "state": "pass" if passed else "fail",
        "evidence": {
            "basis": f"maximum measured deviation={measured:.17g}; tolerance={tolerance:.17g} canonical pixels"
        },
        "evaluator": "acceptance-model-1.0.0",
        "evaluated_at": timestamp,
    }


def _pending_semantic_gates(timestamp: str) -> list[dict[str, object]]:
    return [
        {
            "gate_id": gate_id,
            "kind": "semantic",
            "state": "not_evaluated",
            "evidence": {"basis": "semantic review is required before finalization"},
            "evaluator": "pending-review",
            "evaluated_at": timestamp,
        }
        for gate_id in SEMANTIC_GATE_IDS
    ]


def _gate_result(gate: Mapping[str, object]) -> GateResult:
    evidence = gate["evidence"]
    if not isinstance(evidence, Mapping):
        raise InvalidInputError("gate evidence must be an object")
    artifacts: tuple[ArtifactEvidence, ...] = ()
    if "artifact_id" in evidence or "sha256" in evidence:
        artifacts = (
            ArtifactEvidence(str(evidence.get("artifact_id", "")), str(evidence.get("sha256", ""))),
        )
    basis = str(evidence.get("basis", ""))
    try:
        timestamp = datetime.fromisoformat(str(gate["evaluated_at"]).replace("Z", "+00:00"))
        return GateResult(
            gate_id=str(gate["gate_id"]),
            kind=str(gate["kind"]),
            state=str(gate["state"]),
            evidence=GateEvidence(artifacts=artifacts, basis=basis),
            evaluator=str(gate["evaluator"]),
            timestamp=timestamp,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise InvalidInputError("evaluation contains malformed gate evidence") from error


def _round_score(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _renderer_is_canonical(result: RenderResult) -> bool:
    return result.status == Status.ACCEPTED and bool(result.png_bytes) and bool(result.sha256)


def _read_prior_evaluations(run_dir: Path, iteration: int) -> list[dict[str, object]]:
    prior: list[dict[str, object]] = []
    for index in range(iteration):
        path = run_dir / f"evaluation-i{index:02d}.json"
        if not path.is_file():
            raise InvalidInputError("evaluation iterations must be contiguous from baseline i00")
        prior.append(_load_json(path, f"evaluation i{index:02d}"))
    if iteration == 0 and any(run_dir.glob("evaluation-i*.json")):
        raise FrozenArtifactError("baseline evaluation already exists")
    if prior and prior[-1].get("stop_reason") in {"stalled", "iteration_limit", "accepted"}:
        raise InvalidInputError("the prior evaluation already stopped this run")
    return prior


def evaluate_candidate(
    map_path: Path,
    candidate: Path,
    iteration: int,
    run_dir: Path,
    *,
    renderer: Renderer = render_canonical,
    diagnostic_renderer: DiagnosticRenderer | None = None,
) -> dict[str, object]:
    """Safely evaluate one immutable candidate iteration against one frozen map."""
    map_source = Path(map_path)
    candidate_source = Path(candidate)
    workspace = Path(run_dir)
    resolved_workspace = workspace.resolve()
    if map_source.resolve().is_relative_to(resolved_workspace) or candidate_source.resolve().is_relative_to(
        resolved_workspace
    ):
        raise InvalidInputError(
            "disposable run directory cannot contain the frozen map or candidate input"
        )
    frozen_map = _load_json(map_source, "reconstruction map")
    validate_document(frozen_map, "reconstruction-map")
    suffix = _iteration_suffix(iteration)
    refinement_limit = frozen_map["refinement_limit"]
    if not isinstance(refinement_limit, int) or iteration > refinement_limit:
        raise InvalidInputError("iteration exceeds the frozen refinement_limit")

    evaluation_path = workspace / f"evaluation-{suffix}.json"
    diagnostics_path = workspace / f"diagnostics-{suffix}.json"
    candidate_path = workspace / f"candidate-{suffix}.svg"
    preview_path = workspace / f"preview-{suffix}.png"
    for path in (evaluation_path, diagnostics_path, candidate_path, preview_path):
        if path.exists():
            raise FrozenArtifactError(f"refusing to overwrite frozen artifact: {path}")
    prior = _read_prior_evaluations(workspace, iteration)

    map_payload = map_source.read_bytes()
    map_hash = _sha256(map_payload)
    document = validate_svg(candidate_source)
    candidate_hash = _sha256(document.xml_bytes)
    map_revision = int(frozen_map["map_revision"])
    revision = _revision_suffix(map_revision)
    reference_directory = map_source.parent / f"reference-{revision}"
    reference_record = frozen_map["reference_mask"]
    uncertainty_record = frozen_map["uncertainty_mask"]
    if not isinstance(reference_record, Mapping) or not isinstance(uncertainty_record, Mapping):
        raise InvalidInputError("frozen map mask records are malformed")
    reference = _read_mask(
        reference_directory / "reference-mask.png", str(reference_record["sha256"])
    )
    uncertainty = _read_mask(
        reference_directory / "uncertainty-mask.png", str(uncertainty_record["sha256"])
    )
    components = frozen_map["components"]
    if not isinstance(components, list) or any(not isinstance(item, Mapping) for item in components):
        raise InvalidInputError("frozen map components are malformed")
    reference_components: dict[str, NDArray[np.bool_]] = {}
    for component in components:
        component_id = str(component["component_id"])
        record = component["reference_mask"]
        if not isinstance(record, Mapping):
            raise InvalidInputError("component mask record is malformed")
        reference_components[component_id] = _read_mask(
            reference_directory / f"component-{component_id}.png", str(record["sha256"])
        )

    size = (
        int(frozen_map["canonical_canvas"]["raster_width"]),  # type: ignore[index]
        int(frozen_map["canonical_canvas"]["raster_height"]),  # type: ignore[index]
    )
    if reference.shape != (size[1], size[0]):
        raise InvalidInputError("frozen reference mask dimensions do not match the map")
    render_result = renderer(document, size, workspace)
    canonical = _renderer_is_canonical(render_result)
    timestamp = _utc_now()
    candidate_mask = np.zeros_like(reference)
    visible_masks: Mapping[str, ArrayLike] = {}
    isolated_masks: Mapping[str, ArrayLike] = {}
    diagnostic_error = ""
    if canonical:
        candidate_mask = _render_mask(render_result.png_bytes, size)
        try:
            diagnostic_data = (
                diagnostic_renderer(document, components, size, workspace)
                if diagnostic_renderer is not None
                else _default_diagnostics(document, components, size, workspace, renderer)
            )
            visible_masks = diagnostic_data["visible"]
            isolated_masks = diagnostic_data["isolated"]
        except Exception as error:
            canonical = False
            diagnostic_error = str(error)

    if canonical:
        layout = component_layout_score(
            reference_components,
            visible_masks,
            weights={str(item["component_id"]): float(item["weight"]) for item in components},
            mandatory={str(item["component_id"]) for item in components if item["mandatory"] is True},
        )
        expected_nodes = [
            (str(item["component_id"]), int(item["expected_hole_count"])) for item in components
        ]
        topology = topology_score(
            expected_nodes,
            frozen_map["topology_facts"],  # type: ignore[arg-type]
            visible_masks=visible_masks,
            isolated_masks=isolated_masks,
            paint_order=tuple(str(item["component_id"]) for item in components),
            uncertainty=uncertainty,
        )
        scores = MetricSet(
            silhouette_score(reference, candidate_mask, uncertainty),
            contour_score(reference, candidate_mask, uncertainty),
            layout.score,
            topology.score,
        )
        raw_composite = composite_score(scores)
    else:
        class EmptyLayout:
            score = 0.0
            gate_pass = False
            components: tuple[object, ...] = ()

        class EmptyTopology:
            score = 0.0
            gate_pass = False
            observed_node_facts: frozenset[tuple[str, int]] = frozenset()
            observed_edge_facts: frozenset[tuple[str, str, str]] = frozenset()

        layout = EmptyLayout()
        topology = EmptyTopology()
        scores = MetricSet(0.0, 0.0, 0.0, 0.0)
        raw_composite = 0.0

    integrity_ok = all(
        _SHA256_RE.fullmatch(str(value))
        for value in (
            frozen_map["source_sha256"],
            map_hash,
            reference_record["sha256"],
            uncertainty_record["sha256"],
            candidate_hash,
        )
    )
    viewport_ok = _viewbox_matches(document, frozen_map["viewport"]["view_box"])  # type: ignore[index]
    style_ok = _monochrome(document)
    delta = max(1.0, math.floor(0.001 * math.hypot(*size) + 0.5))
    geometry_measured = 0.0
    geometry_tolerance = delta
    geometry_ok = False
    if canonical:
        try:
            geometry = evaluate_geometry_constraints(
                _candidate_geometry(document, components, size, delta),
                frozen_map["geometry_constraints"],  # type: ignore[arg-type]
                delta=delta,
                canonical_canvas=(float(size[0]), float(size[1])),
            )
            geometry_ok = geometry.passed
            if geometry.measurements:
                worst = max(
                    geometry.measurements,
                    key=lambda measurement: measurement.measured_deviation
                    / max(measurement.tolerance, 1e-300),
                )
                geometry_measured = worst.measured_deviation
                geometry_tolerance = worst.tolerance
        except (KeyError, TypeError, ValueError):
            geometry_measured = delta + 1.0
            geometry_tolerance = delta
    safe_hash = candidate_hash
    automatic_gates = [
        _artifact_gate("auto.svg.safe_subset", True, f"candidate-{suffix}", safe_hash, timestamp),
        _artifact_gate(
            "auto.svg.render",
            canonical,
            f"preview-{suffix}" if render_result.sha256 else f"candidate-{suffix}",
            render_result.sha256 or candidate_hash,
            timestamp,
        ),
        _artifact_gate("auto.integrity.hashes", integrity_ok, f"reconstruction-map-{revision}", map_hash, timestamp),
        _artifact_gate("auto.components.present", bool(layout.gate_pass), f"candidate-{suffix}", candidate_hash, timestamp),
        _artifact_gate("auto.topology.facts", bool(topology.gate_pass), f"candidate-{suffix}", candidate_hash, timestamp),
        _artifact_gate("auto.viewport.geometry", viewport_ok, f"candidate-{suffix}", candidate_hash, timestamp),
        _measurement_gate(
            "auto.primitives.constraints",
            geometry_ok,
            geometry_measured if canonical else delta + 1.0,
            geometry_tolerance,
            timestamp,
        ),
        _measurement_gate("auto.paths.integrity", canonical, 0.0 if canonical else delta + 1.0, delta, timestamp),
        _artifact_gate("auto.style.monochrome", style_ok, f"candidate-{suffix}", candidate_hash, timestamp),
    ]
    if tuple(gate["gate_id"] for gate in automatic_gates) != AUTOMATIC_GATE_IDS:
        raise RuntimeError("automatic gate catalog order drifted")

    metrics = {
        "silhouette_raw": scores.s,
        "silhouette": _round_score(scores.s),
        "contour_raw": scores.c,
        "contour": _round_score(scores.c),
        "layout_raw": scores.l,
        "layout": _round_score(scores.l),
        "topology_raw": scores.t,
        "topology": _round_score(scores.t),
        "composite_raw": raw_composite,
        "composite": _round_score(raw_composite),
    }
    diagnostics = {
        "stage": "evaluate_candidate",
        "stage_version": SCHEMA_VERSION,
        "iteration": iteration,
        "map_sha256": map_hash,
        "candidate_sha256": candidate_hash,
        "renderer_diagnostic": render_result.diagnostic,
        "component_diagnostic": diagnostic_error,
        "metrics": metrics,
        "automatic_gates": automatic_gates,
    }
    diagnostics_payload = _json_bytes(diagnostics)
    diagnostics_hash = _sha256(diagnostics_payload)
    semantic_gates = _pending_semantic_gates(timestamp)
    provisional = resolve_status(
        score=raw_composite,
        target=float(frozen_map["accuracy_target"]),
        gates=tuple(_gate_result(gate) for gate in automatic_gates + semantic_gates),
        canonical_environment=canonical,
    )
    previous_scores = [float(item["report"]["metrics"]["composite_raw"]) for item in prior]  # type: ignore[index]
    previous_improvements = [bool(item.get("gate_improvement", False)) for item in prior[1:]]
    current_improvement = False
    if prior:
        prior_states = {
            gate["gate_id"]: gate["state"]
            for gate in prior[-1]["report"]["gates"]  # type: ignore[index]
            if gate["kind"] == "automatic"
        }
        current_improvement = any(
            prior_states.get(str(gate["gate_id"])) != "pass" and gate["state"] == "pass"
            for gate in automatic_gates
        )
    score_history = previous_scores + [raw_composite]
    improvement_history = previous_improvements + ([current_improvement] if prior else [])
    stalled = is_stalled(score_history, improvement_history)
    limit_state = "stalled" if stalled else "reached" if iteration == refinement_limit else "active"
    stop_reason = "stalled" if stalled else "iteration_limit" if iteration == refinement_limit else None
    uncertainty_pixels = int(np.count_nonzero(uncertainty))
    reference_rows, reference_columns = np.nonzero(reference)
    bbox_area = (
        (int(reference_rows.max()) - int(reference_rows.min()) + 1)
        * (int(reference_columns.max()) - int(reference_columns.min()) + 1)
        if reference_rows.size
        else reference.size
    )
    report = {
        "schema_kind": "acceptance-report",
        "schema_version": SCHEMA_VERSION,
        "model_version": ACCEPTANCE_MODEL_VERSION,
        "run_id": f"run-{map_revision}",
        "created_at": timestamp,
        "status": provisional.status.value,
        "accuracy_target": frozen_map["accuracy_target"],
        "iteration": iteration,
        "limit_state": limit_state,
        "target_met": raw_composite >= float(frozen_map["accuracy_target"]),
        "canonical_environment": canonical,
        "canonical_renderer": _canonical_renderer_report(),
        "hashes": {
            "source": frozen_map["source_sha256"],
            "map": map_hash,
            "reference_mask": reference_record["sha256"],
            "uncertainty_mask": uncertainty_record["sha256"],
            "candidate": candidate_hash,
            "diagnostics": diagnostics_hash,
        },
        "normalization": {
            key: frozen_map["normalization"][key]  # type: ignore[index]
            for key in ("foreground_polarity", "background_decision", "threshold")
        },
        "viewport": {**copy.deepcopy(frozen_map["viewport"]), "canonical_canvas": copy.deepcopy(frozen_map["canonical_canvas"])},
        "metrics": metrics,
        "components": [
            {
                "component_id": str(item["component_id"]),
                "layout_score": next(
                    (float(metric.score) for metric in layout.components if metric.component_id == item["component_id"]),
                    0.0,
                ),
                "expected_hole_count": int(item["expected_hole_count"]),
            }
            for item in components
        ],
        "topology_nodes": [
            {"component_id": component_id, "hole_count": hole_count}
            for component_id, hole_count in sorted(topology.observed_node_facts)
        ] if topology.observed_node_facts else [
            {"component_id": str(item["component_id"]), "hole_count": 0} for item in components
        ],
        "topology_facts": [
            {"relation": relation, "subject": subject, "object": object_id}
            for relation, subject, object_id in sorted(topology.observed_edge_facts)
        ],
        "uncertainty": {
            "pixels": uncertainty_pixels,
            "canvas_fraction": uncertainty_pixels / float(uncertainty.size),
            "bbox_fraction": min(1.0, uncertainty_pixels / float(max(1, bbox_area))),
        },
        "gates": automatic_gates + semantic_gates,
        "warnings": [item for item in (render_result.diagnostic, diagnostic_error) if item],
        "limitations": [] if canonical else ["canonical renderer/runtime was not confirmed"],
        "artifacts": [
            {"logical_id": f"reconstruction-map-{revision}", "sha256": map_hash, "retention": "retained"},
            {"logical_id": str(reference_record["logical_id"]), "sha256": str(reference_record["sha256"]), "retention": "retained"},
            {"logical_id": str(uncertainty_record["logical_id"]), "sha256": str(uncertainty_record["sha256"]), "retention": "retained"},
            {"logical_id": f"candidate-{suffix}", "sha256": candidate_hash, "retention": "retained"},
            {"logical_id": f"diagnostics-{suffix}", "sha256": diagnostics_hash, "retention": "retained"},
        ],
    }
    validate_document(report, "acceptance-report")
    evaluation = {
        "stage": "evaluate_candidate",
        "stage_version": SCHEMA_VERSION,
        "artifact_id": f"evaluation-{suffix}",
        "created_at": timestamp,
        "map_revision": map_revision,
        "map_sha256": map_hash,
        "iteration": iteration,
        "refinement_limit": refinement_limit,
        "score_history": score_history,
        "gate_improvement": current_improvement,
        "limit_state": limit_state,
        "stop_reason": stop_reason,
        "cleanup_logical_ids": [f"candidate-{suffix}", f"diagnostics-{suffix}", f"preview-{suffix}"],
        "report": report,
    }

    workspace.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(candidate_path, document.xml_bytes)
    if render_result.png_bytes:
        _atomic_write_bytes(preview_path, render_result.png_bytes)
    _atomic_write_bytes(diagnostics_path, diagnostics_payload)
    atomic_write_json(evaluation_path, evaluation)
    _atomic_replace_json(
        workspace / "run-state.json",
        {
            "run_id": report["run_id"],
            "map_revision": map_revision,
            "latest_iteration": iteration,
            "latest_evaluation": f"evaluation-{suffix}",
            "score_history": score_history,
            "limit_state": limit_state,
            "stop_reason": stop_reason,
        },
    )
    if map_source.read_bytes() != map_payload:
        raise RuntimeError("frozen reconstruction map changed during evaluation")
    return {
        "ok": True,
        "stage": "evaluate_candidate",
        "artifact_id": f"evaluation-{suffix}",
        "status": report["status"],
        "score": metrics["composite"],
        "limit_state": limit_state,
        "exit_code": int(provisional.exit_code),
    }


def _ensure_logical_evidence(document: Mapping[str, object]) -> None:
    def walk(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in {"logical_id", "artifact_id"}:
                    text = str(item)
                    if Path(text).is_absolute() or text.startswith(("file:", "http:", "https:")):
                        raise InvalidInputError("semantic evidence must use logical artifact IDs")
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(document)


def finalize_review(
    evaluation: Path, semantic_review: Path, output: Path
) -> dict[str, object]:
    """Merge validated semantic evidence, resolve precedence, report, then inventory cleanup."""
    evaluation_path = Path(evaluation)
    review_path = Path(semantic_review)
    output_path = Path(output)
    cleanup_inventory_path = output_path.parent / "cleanup-inventory.json"
    if output_path.exists() or cleanup_inventory_path.exists():
        existing = output_path if output_path.exists() else cleanup_inventory_path
        raise FrozenArtifactError(f"refusing to overwrite frozen artifact: {existing}")
    evaluated = _load_json(evaluation_path, "evaluation")
    review = _load_json(review_path, "semantic review")
    validate_document(review, "semantic-review")
    _ensure_logical_evidence(review)
    if evaluated.get("stage") != "evaluate_candidate" or not isinstance(evaluated.get("report"), Mapping):
        raise InvalidInputError("evaluation artifact has an unknown stage contract")
    base_report = copy.deepcopy(evaluated["report"])
    validate_document(base_report, "acceptance-report")
    if review.get("run_id") != base_report.get("run_id"):
        raise InvalidInputError("semantic review run_id does not match the evaluation")
    if output_path.resolve().is_relative_to(evaluation_path.parent.resolve()):
        raise InvalidInputError("final report output must be outside the disposable run workspace")

    automatic_gates = [gate for gate in base_report["gates"] if gate["kind"] == "automatic"]
    semantic_gates = review["gates"]
    gates = automatic_gates + semantic_gates
    resolution = resolve_status(
        score=float(base_report["metrics"]["composite_raw"]),
        target=float(base_report["accuracy_target"]),
        gates=tuple(_gate_result(gate) for gate in gates),
        canonical_environment=bool(base_report["canonical_environment"]),
    )
    final_report = base_report
    final_report["created_at"] = _utc_now()
    final_report["status"] = resolution.status.value
    final_report["target_met"] = (
        float(final_report["metrics"]["composite_raw"]) >= float(final_report["accuracy_target"])
    )
    final_report["gates"] = gates
    final_report["warnings"] = list(final_report["warnings"]) + list(review["warnings"])
    cleanup_ids = frozenset(str(item) for item in evaluated.get("cleanup_logical_ids", []))
    for artifact in final_report["artifacts"]:
        if artifact["logical_id"] in cleanup_ids and str(artifact["logical_id"]).startswith("diagnostics-"):
            artifact["retention"] = "deleted"
    validate_document(final_report, "acceptance-report")
    _ensure_logical_evidence(final_report)

    run_workspace = evaluation_path.parent
    cleanup_started = _utc_now()
    cleanup_records: list[dict[str, object]] = []
    if run_workspace.exists():
        resolved_workspace = run_workspace.resolve()
        if resolved_workspace == resolved_workspace.parent or resolved_workspace == Path("/"):
            raise InvalidInputError("refusing unsafe run workspace cleanup")
        cleanup_records = [
            {"logical_id": str(item), "retention": "deleted"}
            for item in sorted(cleanup_ids)
        ]
        shutil.rmtree(run_workspace)
    atomic_write_json(output_path, final_report)
    atomic_write_json(
        cleanup_inventory_path,
        {
            "stage": "finalize_cleanup",
            "stage_version": SCHEMA_VERSION,
            "run_id": final_report["run_id"],
            "started_at": cleanup_started,
            "recorded_at": _utc_now(),
            "after_report": output_path.name,
            "artifacts": cleanup_records,
        },
    )
    return {
        "ok": resolution.exit_code == ExitCode.ACCEPTED,
        "stage": "finalize_review",
        "artifact_id": "acceptance-report",
        "status": resolution.status.value,
        "score": final_report["metrics"]["composite"],
        "exit_code": int(resolution.exit_code),
    }
