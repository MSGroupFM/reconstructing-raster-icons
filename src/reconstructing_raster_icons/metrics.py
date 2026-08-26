"""Exact unrounded raster fidelity metrics for canonical icon evaluation."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
import math
from typing import TypeAlias

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.ndimage import distance_transform_edt

from .geometry import binary_boundary, component_enclosure, count_holes, dilate


BoolMask = NDArray[np.bool_]
NodeFact: TypeAlias = tuple[str, int]
EdgeFact: TypeAlias = tuple[str, str, str]
DiagnosticRaster: TypeAlias = tuple[ArrayLike, ArrayLike]
DiagnosticRenderer: TypeAlias = Callable[[str, dict[str, str]], ArrayLike | DiagnosticRaster]

_AUTOMATIC_RELATIONS = frozenset({"contains", "overlaps", "touches", "paint_order"})
_SYMMETRIC_RELATIONS = frozenset({"overlaps", "touches"})


@dataclass(frozen=True)
class MetricSet:
    """The four unrounded component scores used by the composite metric."""

    s: float
    c: float
    l: float
    t: float

    def __post_init__(self) -> None:
        for field_name in ("s", "c", "l", "t"):
            value = float(getattr(self, field_name))
            if not math.isfinite(value) or not 0.0 <= value <= 100.0:
                raise ValueError(f"{field_name} must be a finite score between 0 and 100")
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True)
class ComponentMetric:
    """One visible-contribution layout diagnostic."""

    component_id: str
    score: float
    weight: float
    present: bool
    degenerate: bool


@dataclass(frozen=True)
class ComponentLayoutEvaluation:
    """Layout score plus the independent mandatory-component gate state."""

    score: float
    components: tuple[ComponentMetric, ...]
    gate_pass: bool
    failed_mandatory: frozenset[str]


@dataclass(frozen=True)
class TopologyEvaluation:
    """Topology score, exact observed facts, and the independent hard gate."""

    score: float
    node_f1: float
    edge_f1: float
    gate_pass: bool
    observed_node_facts: frozenset[NodeFact]
    observed_edge_facts: frozenset[EdgeFact]
    missing_node_facts: frozenset[NodeFact]
    unexpected_node_facts: frozenset[NodeFact]
    missing_edge_facts: frozenset[EdgeFact]
    unexpected_edge_facts: frozenset[EdgeFact]


def _array(value: ArrayLike, name: str) -> NDArray[np.generic]:
    try:
        result = np.asarray(value)
    except (TypeError, ValueError) as error:
        raise TypeError(f"{name} must be a rectangular array") from error
    if result.ndim != 2:
        raise ValueError(f"{name} must be a two-dimensional array")
    if result.shape[0] == 0 or result.shape[1] == 0:
        raise ValueError(f"{name} dimensions must be non-zero")
    return result


def _coverage(value: ArrayLike, name: str) -> NDArray[np.float64]:
    result = _array(value, name)
    if np.issubdtype(result.dtype, np.bool_):
        return result.astype(np.float64)
    if not np.issubdtype(result.dtype, np.floating):
        raise TypeError(f"{name} must contain bool or floating-point coverage values")
    coverage = result.astype(np.float64, copy=False)
    if not np.all(np.isfinite(coverage)) or np.any(coverage < 0.0) or np.any(coverage > 1.0):
        raise ValueError(f"{name} coverage values must be finite and between 0 and 1")
    return coverage


def _mask(value: ArrayLike, name: str) -> BoolMask:
    return _coverage(value, name) >= np.float64(0.5)


def _same_shape(named_masks: Sequence[tuple[str, BoolMask]]) -> tuple[int, int]:
    first_name, first = named_masks[0]
    shape = first.shape
    for name, mask in named_masks[1:]:
        if mask.shape != shape:
            raise ValueError(f"{first_name}, {name}, and all masks must have the same shape")
    return shape


def _tolerances(shape: tuple[int, int]) -> tuple[float, int, float]:
    height, width = shape
    diagonal = float(np.hypot(np.float64(width), np.float64(height)))
    delta = max(1, int(math.floor(np.float64(0.001) * diagonal + np.float64(0.5))))
    tau = float(np.float64(0.02) * diagonal)
    return diagonal, delta, tau


def _bounded_score(value: np.float64 | float) -> float:
    return float(np.clip(np.float64(value), np.float64(0.0), np.float64(100.0)))


def silhouette_score(reference: ArrayLike, candidate: ArrayLike, uncertainty: ArrayLike) -> float:
    """Calculate exact tolerant silhouette F1 on confident pixels."""
    reference_full = _mask(reference, "reference")
    candidate_full = _mask(candidate, "candidate")
    excluded = _mask(uncertainty, "uncertainty")
    shape = _same_shape(
        (("reference", reference_full), ("candidate", candidate_full), ("uncertainty", excluded))
    )
    confident_reference = reference_full & ~excluded
    confident_candidate = candidate_full & ~excluded
    reference_area = np.float64(np.count_nonzero(confident_reference))
    candidate_area = np.float64(np.count_nonzero(confident_candidate))
    if reference_area == 0.0 and candidate_area == 0.0:
        return 100.0
    if reference_area == 0.0 or candidate_area == 0.0:
        return 0.0

    _, delta, _ = _tolerances(shape)
    dilated_reference = dilate(confident_reference, delta) & ~excluded
    dilated_candidate = dilate(confident_candidate, delta) & ~excluded
    precision = (
        np.float64(np.count_nonzero(confident_candidate & dilated_reference)) / candidate_area
    )
    recall = np.float64(np.count_nonzero(confident_reference & dilated_candidate)) / reference_area
    denominator = precision + recall
    if denominator == 0.0:
        return 0.0
    return _bounded_score(np.float64(100.0) * np.float64(2.0) * precision * recall / denominator)


def _directed_boundary_mean(
    source: BoolMask, target: BoolMask, delta: int, tau: float
) -> np.float64:
    distances = distance_transform_edt(~target).astype(np.float64, copy=False)
    directed = distances[source]
    normalized = np.minimum(
        np.maximum(directed - np.float64(delta), np.float64(0.0)),
        np.float64(tau),
    ) / np.float64(tau)
    return np.mean(normalized, dtype=np.float64)


def contour_score(reference: ArrayLike, candidate: ArrayLike, uncertainty: ArrayLike) -> float:
    """Calculate bidirectional contour proximity using Euclidean pixel-center EDT."""
    reference_full = _mask(reference, "reference")
    candidate_full = _mask(candidate, "candidate")
    excluded = _mask(uncertainty, "uncertainty")
    shape = _same_shape(
        (("reference", reference_full), ("candidate", candidate_full), ("uncertainty", excluded))
    )
    reference_boundary = binary_boundary(reference_full) & ~excluded
    candidate_boundary = binary_boundary(candidate_full) & ~excluded
    reference_empty = not np.any(reference_boundary)
    candidate_empty = not np.any(candidate_boundary)
    if reference_empty and candidate_empty:
        return 100.0
    if reference_empty or candidate_empty:
        return 0.0

    _, delta, tau = _tolerances(shape)
    reference_to_candidate = _directed_boundary_mean(
        reference_boundary, candidate_boundary, delta, tau
    )
    candidate_to_reference = _directed_boundary_mean(
        candidate_boundary, reference_boundary, delta, tau
    )
    symmetric = (reference_to_candidate + candidate_to_reference) / np.float64(2.0)
    return _bounded_score(np.float64(100.0) * (np.float64(1.0) - symmetric))


def visible_component_mask(alpha: ArrayLike, relative_luminance: ArrayLike) -> BoolMask:
    """Threshold a black/white diagnostic render into its visible contribution."""
    alpha_values = _coverage(alpha, "alpha")
    luminance_values = _coverage(relative_luminance, "relative_luminance")
    _same_shape((("alpha", alpha_values >= 0.5), ("relative_luminance", luminance_values >= 0.5)))
    return (alpha_values >= np.float64(0.5)) & (luminance_values >= np.float64(0.5))


def _mask_geometry(
    mask: BoolMask,
) -> tuple[np.float64, np.float64, np.float64, np.float64, np.float64]:
    rows, columns = np.nonzero(mask)
    area = np.float64(rows.size)
    centroid_x = np.mean(columns, dtype=np.float64)
    centroid_y = np.mean(rows, dtype=np.float64)
    width = np.float64(columns.max() - columns.min() + 1)
    height = np.float64(rows.max() - rows.min() + 1)
    return centroid_x, centroid_y, width, height, area


def _component_score(reference: BoolMask, candidate: BoolMask, diagonal: float) -> float:
    reference_geometry = _mask_geometry(reference)
    candidate_geometry = _mask_geometry(candidate)
    reference_x, reference_y, reference_width, reference_height, reference_area = reference_geometry
    candidate_x, candidate_y, candidate_width, candidate_height, candidate_area = candidate_geometry
    centroid_distance = np.hypot(candidate_x - reference_x, candidate_y - reference_y)
    center_error = min(
        np.float64(1.0), centroid_distance / (np.float64(0.05) * np.float64(diagonal))
    )
    width_error = min(
        np.float64(1.0),
        abs(np.log(candidate_width / reference_width)) / np.log(np.float64(1.25)),
    )
    height_error = min(
        np.float64(1.0),
        abs(np.log(candidate_height / reference_height)) / np.log(np.float64(1.25)),
    )
    area_error = min(
        np.float64(1.0),
        abs(np.log(candidate_area / reference_area)) / np.log(np.float64(1.50)),
    )
    error = (
        np.float64(0.40) * center_error
        + np.float64(0.20) * width_error
        + np.float64(0.20) * height_error
        + np.float64(0.20) * area_error
    )
    return _bounded_score(np.float64(100.0) * (np.float64(1.0) - error))


def _candidate_component_mask(
    component_id: str,
    candidate: Mapping[str, ArrayLike] | DiagnosticRenderer,
    component_ids: tuple[str, ...],
) -> tuple[BoolMask | None, bool]:
    if isinstance(candidate, Mapping):
        if component_id not in candidate:
            return None, False
        return _mask(candidate[component_id], f"candidate component {component_id}"), True

    palette = {
        item: "#ffffff" if item == component_id else "#000000" for item in component_ids
    }
    try:
        rendered = candidate(component_id, palette)
    except KeyError:
        return None, False
    if isinstance(rendered, tuple) and len(rendered) == 2:
        return visible_component_mask(rendered[0], rendered[1]), True
    return _mask(rendered, f"candidate component {component_id}"), True


def component_layout_score(
    reference_masks: Mapping[str, ArrayLike],
    candidate_masks: Mapping[str, ArrayLike] | DiagnosticRenderer,
    *,
    weights: Mapping[str, float] | None = None,
    mandatory: Iterable[str] | None = None,
    component_ids: Sequence[str] | None = None,
) -> ComponentLayoutEvaluation:
    """Evaluate visible component masks without hiding mandatory failures."""
    if not isinstance(reference_masks, Mapping) or not reference_masks:
        raise ValueError("reference_masks must contain at least one component")
    reference_ids = tuple(reference_masks)
    all_component_ids = tuple(component_ids) if component_ids is not None else reference_ids
    if len(set(all_component_ids)) != len(all_component_ids) or not set(reference_ids).issubset(
        all_component_ids
    ):
        raise ValueError("component_ids must be unique and include every reference component")
    mandatory_ids = frozenset(reference_ids if mandatory is None else mandatory)
    if not mandatory_ids.issubset(reference_ids):
        raise ValueError("mandatory contains an unknown reference component")

    if weights is None:
        component_weights = {component_id: 1.0 for component_id in reference_ids}
    else:
        if set(weights) != set(reference_ids):
            raise ValueError("weights must declare every reference component exactly once")
        component_weights = {
            component_id: float(weights[component_id]) for component_id in reference_ids
        }
    if any(not math.isfinite(weight) or weight <= 0.0 for weight in component_weights.values()):
        raise ValueError("component weights must be finite and positive")

    normalized_reference = {
        component_id: _mask(mask, f"reference component {component_id}")
        for component_id, mask in reference_masks.items()
    }
    named_references = tuple(
        (f"reference component {component_id}", mask)
        for component_id, mask in normalized_reference.items()
    )
    shape = _same_shape(named_references)
    if any(not np.any(mask) for mask in normalized_reference.values()):
        raise ValueError("reference component masks must be non-degenerate")
    diagonal, _, _ = _tolerances(shape)

    diagnostics: list[ComponentMetric] = []
    failed: set[str] = set()
    for component_id in reference_ids:
        candidate_mask, present = _candidate_component_mask(
            component_id, candidate_masks, all_component_ids
        )
        degenerate = candidate_mask is None or not np.any(candidate_mask)
        if candidate_mask is not None and candidate_mask.shape != shape:
            raise ValueError("reference and candidate component masks must have the same shape")
        score = 0.0 if degenerate else _component_score(
            normalized_reference[component_id], candidate_mask, diagonal
        )
        if component_id in mandatory_ids and degenerate:
            failed.add(component_id)
        diagnostics.append(
            ComponentMetric(
                component_id=component_id,
                score=score,
                weight=component_weights[component_id],
                present=present,
                degenerate=degenerate,
            )
        )

    numerator = np.sum(
        np.asarray([item.score * item.weight for item in diagnostics], dtype=np.float64),
        dtype=np.float64,
    )
    denominator = np.sum(
        np.asarray([item.weight for item in diagnostics], dtype=np.float64), dtype=np.float64
    )
    score = _bounded_score(numerator / denominator)
    return ComponentLayoutEvaluation(
        score=score,
        components=tuple(diagnostics),
        gate_pass=not failed,
        failed_mandatory=frozenset(failed),
    )


def _node_facts(values: Iterable[NodeFact | Mapping[str, object]]) -> frozenset[NodeFact]:
    result: set[NodeFact] = set()
    for value in values:
        if isinstance(value, Mapping):
            component_id = value.get("component_id")
            holes = value.get("hole_count")
        else:
            try:
                component_id, holes = value
            except (TypeError, ValueError) as error:
                raise ValueError("node facts must contain component_id and hole_count") from error
        valid_hole_count = not isinstance(holes, bool) and isinstance(holes, int) and holes >= 0
        if not isinstance(component_id, str) or not valid_hole_count:
            raise ValueError("node facts require a string component_id and non-negative hole_count")
        result.add((component_id, holes))
    return frozenset(result)


def _edge_facts(values: Iterable[EdgeFact | Mapping[str, object]]) -> frozenset[EdgeFact]:
    result: set[EdgeFact] = set()
    for value in values:
        if isinstance(value, Mapping):
            relation = value.get("relation")
            subject = value.get("subject")
            object_id = value.get("object")
        else:
            try:
                relation, subject, object_id = value
            except (TypeError, ValueError) as error:
                raise ValueError("edge facts must contain relation, subject, and object") from error
        if relation == "connects":
            continue
        if relation not in _AUTOMATIC_RELATIONS:
            raise ValueError(f"unsupported automatic topology relation: {relation}")
        if not isinstance(subject, str) or not isinstance(object_id, str) or subject == object_id:
            raise ValueError("edge facts require two distinct string component IDs")
        if relation in _SYMMETRIC_RELATIONS and object_id < subject:
            subject, object_id = object_id, subject
        result.add((relation, subject, object_id))
    return frozenset(result)


def _fact_f1(expected: frozenset[object], observed: frozenset[object]) -> float:
    if not expected and not observed:
        return 1.0
    if not expected or not observed:
        return 0.0
    intersection = np.float64(len(expected & observed))
    precision = intersection / np.float64(len(observed))
    recall = intersection / np.float64(len(expected))
    if precision + recall == 0.0:
        return 0.0
    return float(np.float64(2.0) * precision * recall / (precision + recall))


def _minimum_distance(first: BoolMask, second: BoolMask) -> float:
    distances = distance_transform_edt(~second).astype(np.float64, copy=False)
    return float(np.min(distances[first]))


def topology_score(
    expected_node_facts: Iterable[NodeFact | Mapping[str, object]],
    expected_edge_facts: Iterable[EdgeFact | Mapping[str, object]],
    *,
    visible_masks: Mapping[str, ArrayLike],
    isolated_masks: Mapping[str, ArrayLike],
    paint_order: Sequence[str],
    uncertainty: ArrayLike | None = None,
) -> TopologyEvaluation:
    """Derive automatic topology facts and retain the exact hard-gate state."""
    expected_nodes = _node_facts(expected_node_facts)
    expected_edges = _edge_facts(expected_edge_facts)
    isolated = {
        component_id: _mask(mask, f"isolated component {component_id}")
        for component_id, mask in isolated_masks.items()
    }
    visible = {
        component_id: _mask(mask, f"visible component {component_id}")
        for component_id, mask in visible_masks.items()
    }
    all_named_masks = tuple(
        [(f"isolated component {component_id}", mask) for component_id, mask in isolated.items()]
        + [(f"visible component {component_id}", mask) for component_id, mask in visible.items()]
    )
    if all_named_masks:
        shape = _same_shape(all_named_masks)
        if uncertainty is None:
            excluded = np.zeros(shape, dtype=bool)
        else:
            excluded = _mask(uncertainty, "uncertainty")
            if excluded.shape != shape:
                raise ValueError("uncertainty and component masks must have the same shape")
        _, delta, _ = _tolerances(shape)
    else:
        if uncertainty is not None:
            _mask(uncertainty, "uncertainty")
        excluded = None
        delta = 1

    order = tuple(paint_order)
    if len(set(order)) != len(order):
        raise ValueError("paint_order component IDs must be unique")
    order_index = {component_id: index for index, component_id in enumerate(order)}
    observed_nodes = frozenset(
        (component_id, count_holes(mask)) for component_id, mask in isolated.items()
    )
    observed_edges: set[EdgeFact] = set()
    component_ids = tuple(isolated)
    enclosures = {
        component_id: component_enclosure(mask, delta)
        for component_id, mask in isolated.items()
        if np.any(mask)
    }
    declared_paint_pairs = {
        frozenset((subject, object_id))
        for relation, subject, object_id in expected_edges
        if relation == "paint_order"
    }

    for index, first_id in enumerate(component_ids):
        first_isolated = isolated[first_id]
        for second_id in component_ids[index + 1 :]:
            second_isolated = isolated[second_id]
            if np.any(first_isolated) and np.any(second_isolated):
                if np.count_nonzero(second_isolated & enclosures[first_id]) / np.float64(
                    np.count_nonzero(second_isolated)
                ) >= np.float64(0.99):
                    observed_edges.add(("contains", first_id, second_id))
                if np.count_nonzero(first_isolated & enclosures[second_id]) / np.float64(
                    np.count_nonzero(first_isolated)
                ) >= np.float64(0.99):
                    observed_edges.add(("contains", second_id, first_id))

            overlap = bool(np.any(first_isolated & second_isolated))
            symmetric_ids = tuple(sorted((first_id, second_id)))
            if overlap:
                observed_edges.add(("overlaps", symmetric_ids[0], symmetric_ids[1]))

            first_visible = visible.get(first_id)
            second_visible = visible.get(second_id)
            if first_visible is not None and second_visible is not None and excluded is not None:
                first_confident = first_visible & ~excluded
                second_confident = second_visible & ~excluded
                if (
                    np.any(first_confident)
                    and np.any(second_confident)
                    and not np.any(first_confident & second_confident)
                    and _minimum_distance(first_confident, second_confident) <= delta
                ):
                    observed_edges.add(("touches", symmetric_ids[0], symmetric_ids[1]))

            pair = frozenset((first_id, second_id))
            if overlap or pair in declared_paint_pairs:
                if first_id not in order_index or second_id not in order_index:
                    continue
                if order_index[first_id] < order_index[second_id]:
                    observed_edges.add(("paint_order", first_id, second_id))
                else:
                    observed_edges.add(("paint_order", second_id, first_id))

    normalized_observed_edges = _edge_facts(observed_edges)
    node_f1 = _fact_f1(expected_nodes, observed_nodes)
    edge_f1 = _fact_f1(expected_edges, normalized_observed_edges)
    score = _bounded_score(
        np.float64(50.0) * np.float64(node_f1) + np.float64(50.0) * np.float64(edge_f1)
    )
    return TopologyEvaluation(
        score=score,
        node_f1=node_f1,
        edge_f1=edge_f1,
        gate_pass=expected_nodes == observed_nodes and expected_edges == normalized_observed_edges,
        observed_node_facts=observed_nodes,
        observed_edge_facts=normalized_observed_edges,
        missing_node_facts=expected_nodes - observed_nodes,
        unexpected_node_facts=observed_nodes - expected_nodes,
        missing_edge_facts=expected_edges - normalized_observed_edges,
        unexpected_edge_facts=normalized_observed_edges - expected_edges,
    )


def composite_score(metrics: MetricSet) -> float:
    """Return the exact float64-style weighted sum without report rounding."""
    if not isinstance(metrics, MetricSet):
        raise TypeError("metrics must be a MetricSet")
    return 0.45 * metrics.s + 0.30 * metrics.c + 0.15 * metrics.l + 0.10 * metrics.t
