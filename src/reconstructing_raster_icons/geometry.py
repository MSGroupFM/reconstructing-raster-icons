"""Normative mask morphology and editable SVG path geometry."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import math
import re

import numpy as np
from numpy.typing import NDArray


BoolMask = NDArray[np.bool_]
Point = tuple[float, float]


class PathIntegrityError(ValueError):
    """Raised when a path cannot be flattened within the normative bound."""


@dataclass(frozen=True)
class PolylineSubpath:
    """One independently editable, ordered piecewise-linear subpath."""

    points: tuple[Point, ...]
    closed: bool

    def __post_init__(self) -> None:
        points = tuple((float(point[0]), float(point[1])) for point in self.points)
        if len(points) < 2:
            raise ValueError("a polyline subpath must contain at least two points")
        if any(not math.isfinite(value) for point in points for value in point):
            raise ValueError("polyline coordinates must be finite")
        if self.closed and points[0] != points[-1]:
            raise ValueError("a closed polyline subpath must repeat its first point")
        object.__setattr__(self, "points", points)

    @property
    def signed_area(self) -> float:
        if not self.closed:
            return 0.0
        return 0.5 * sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(self.points, self.points[1:])
        )


@dataclass(frozen=True)
class ConstraintMeasurement:
    constraint_kind: str
    subject: str
    measured_deviation: float
    tolerance: float
    passed: bool
    details: Mapping[str, object]


@dataclass(frozen=True)
class GeometryConstraintEvaluation:
    passed: bool
    measurements: tuple[ConstraintMeasurement, ...]


_PATH_TOKEN_RE = re.compile(r"^[\s,0-9eE.+\-AaCcHhLlMmQqSsTtVvZz]*$")
_PATH_VALUE_RE = re.compile(r"[AaCcHhLlMmQqSsTtVvZz]|[-+]?(?:\d+\.?(?:\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


@dataclass(frozen=True)
class _LineSegment:
    start: Point
    end: Point


@dataclass(frozen=True)
class _QuadraticSegment:
    start: Point
    control: Point
    end: Point


@dataclass(frozen=True)
class _CubicSegment:
    start: Point
    control1: Point
    control2: Point
    end: Point


@dataclass(frozen=True)
class _ArcSegment:
    start: Point
    radius_x: float
    radius_y: float
    rotation: float
    large_arc: bool
    sweep: bool
    end: Point


_PathSegment = _LineSegment | _QuadraticSegment | _CubicSegment | _ArcSegment


def _explicit_path_parse(path_data: str) -> tuple[tuple[tuple[_PathSegment, ...], bool], ...]:
    """Parse path grammar without invoking geometry decisions in a library."""
    matches = list(_PATH_VALUE_RE.finditer(path_data))
    position = 0
    for match in matches:
        if path_data[position : match.start()].strip(" ,\t\r\n"):
            raise PathIntegrityError("invalid SVG path token")
        position = match.end()
    if path_data[position:].strip(" ,\t\r\n"):
        raise PathIntegrityError("invalid SVG path token")
    tokens = [match.group(0) for match in matches]
    arities = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7}
    subpaths: list[tuple[tuple[_PathSegment, ...], bool]] = []
    segments: list[_PathSegment] = []
    current = (0.0, 0.0)
    subpath_start: Point | None = None
    previous_command = ""
    cubic_control: Point | None = None
    quadratic_control: Point | None = None
    command: str | None = None
    index = 0

    def coordinate_pair(x: float, y: float, relative: bool) -> Point:
        return (current[0] + x, current[1] + y) if relative else (x, y)

    def finish(closed: bool = False) -> None:
        nonlocal segments
        if segments:
            subpaths.append((tuple(segments), closed))
            segments = []

    while index < len(tokens):
        token = tokens[index]
        if token.isalpha():
            command = token
            index += 1
            if command.upper() == "Z":
                if subpath_start is None:
                    raise PathIntegrityError("close command has no open subpath")
                if current != subpath_start:
                    segments.append(_LineSegment(current, subpath_start))
                current = subpath_start
                finish(True)
                previous_command = "Z"
                cubic_control = quadratic_control = None
                command = None
                continue
        if command is None:
            raise PathIntegrityError("path parameters must follow a command")
        upper = command.upper()
        if upper == "Z":
            raise PathIntegrityError("close command cannot have parameters")
        arity = arities[upper]
        if index + arity > len(tokens) or any(value.isalpha() for value in tokens[index : index + arity]):
            raise PathIntegrityError(f"{command} command has incomplete parameters")
        values = [float(value) for value in tokens[index : index + arity]]
        if not all(math.isfinite(value) for value in values):
            raise PathIntegrityError("path coordinates must be finite")
        index += arity
        relative = command.islower()

        if upper == "M":
            endpoint = coordinate_pair(values[0], values[1], relative)
            finish(False)
            subpath_start = endpoint
            current = endpoint
            previous_command = "M"
            command = "l" if relative else "L"
            cubic_control = quadratic_control = None
            continue
        if subpath_start is None:
            raise PathIntegrityError("path must begin with a move command")

        if upper == "L":
            endpoint = coordinate_pair(values[0], values[1], relative)
            segment: _PathSegment = _LineSegment(current, endpoint)
        elif upper == "H":
            endpoint = (current[0] + values[0], current[1]) if relative else (values[0], current[1])
            segment = _LineSegment(current, endpoint)
        elif upper == "V":
            endpoint = (current[0], current[1] + values[0]) if relative else (current[0], values[0])
            segment = _LineSegment(current, endpoint)
        elif upper == "C":
            first_control = coordinate_pair(values[0], values[1], relative)
            second_control = coordinate_pair(values[2], values[3], relative)
            endpoint = coordinate_pair(values[4], values[5], relative)
            segment = _CubicSegment(current, first_control, second_control, endpoint)
            cubic_control = second_control
        elif upper == "S":
            first_control = (
                (2.0 * current[0] - cubic_control[0], 2.0 * current[1] - cubic_control[1])
                if previous_command in {"C", "S"} and cubic_control is not None
                else current
            )
            second_control = coordinate_pair(values[0], values[1], relative)
            endpoint = coordinate_pair(values[2], values[3], relative)
            segment = _CubicSegment(current, first_control, second_control, endpoint)
            cubic_control = second_control
        elif upper == "Q":
            control = coordinate_pair(values[0], values[1], relative)
            endpoint = coordinate_pair(values[2], values[3], relative)
            segment = _QuadraticSegment(current, control, endpoint)
            quadratic_control = control
        elif upper == "T":
            control = (
                (2.0 * current[0] - quadratic_control[0], 2.0 * current[1] - quadratic_control[1])
                if previous_command in {"Q", "T"} and quadratic_control is not None
                else current
            )
            endpoint = coordinate_pair(values[0], values[1], relative)
            segment = _QuadraticSegment(current, control, endpoint)
            quadratic_control = control
        else:
            if values[3] not in (0.0, 1.0) or values[4] not in (0.0, 1.0):
                raise PathIntegrityError("arc flags must be zero or one")
            endpoint = coordinate_pair(values[5], values[6], relative)
            segment = _ArcSegment(
                current,
                abs(values[0]),
                abs(values[1]),
                values[2],
                bool(values[3]),
                bool(values[4]),
                endpoint,
            )
        segments.append(segment)
        current = endpoint
        if upper not in {"C", "S"}:
            cubic_control = None
        if upper not in {"Q", "T"}:
            quadratic_control = None
        previous_command = upper
    finish(False)
    return tuple(subpaths)


def _point(value: complex | Sequence[float]) -> Point:
    if isinstance(value, complex):
        result = (float(value.real), float(value.imag))
    else:
        if len(value) != 2:
            raise ValueError("a point must have exactly two coordinates")
        result = (float(value[0]), float(value[1]))
    if not all(math.isfinite(coordinate) for coordinate in result):
        raise PathIntegrityError("path coordinates must be finite")
    return result


def _distance(first: Point, second: Point) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


def _point_segment_distance(point: Point, start: Point, end: Point) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    denominator = dx * dx + dy * dy
    if denominator == 0.0:
        return _distance(point, start)
    projection = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / denominator
    projection = min(1.0, max(0.0, projection))
    return math.hypot(point[0] - (start[0] + projection * dx), point[1] - (start[1] + projection * dy))


def _flat_enough(control_points: tuple[Point, ...], tolerance: float) -> bool:
    start, end = control_points[0], control_points[-1]
    if start == end:
        hull_diameter = max(
            _distance(first, second)
            for index, first in enumerate(control_points)
            for second in control_points[index + 1 :]
        )
        return hull_diameter <= tolerance
    return max(
        (_point_segment_distance(point, start, end) for point in control_points[1:-1]),
        default=0.0,
    ) <= tolerance


def _split_midpoint(control_points: tuple[Point, ...]) -> tuple[tuple[Point, ...], tuple[Point, ...]]:
    levels = [control_points]
    while len(levels[-1]) > 1:
        previous = levels[-1]
        levels.append(
            tuple(
                ((first[0] + second[0]) * 0.5, (first[1] + second[1]) * 0.5)
                for first, second in zip(previous, previous[1:])
            )
        )
    left = tuple(level[0] for level in levels)
    right = tuple(level[-1] for level in reversed(levels))
    return left, right


def _flatten_bezier(
    control_points: tuple[Point, ...], tolerance: float, *, depth: int = 0
) -> tuple[Point, ...]:
    if _flat_enough(control_points, tolerance):
        return (control_points[0], control_points[-1])
    if depth >= 32:
        raise PathIntegrityError("adaptive De Casteljau subdivision exceeded maximum depth 32")
    left, right = _split_midpoint(control_points)
    flattened_left = _flatten_bezier(left, tolerance, depth=depth + 1)
    flattened_right = _flatten_bezier(right, tolerance, depth=depth + 1)
    return flattened_left[:-1] + flattened_right


def _ellipse_point(
    center: Point, radius_x: float, radius_y: float, rotation: float, theta: float
) -> Point:
    cosine, sine = math.cos(rotation), math.sin(rotation)
    x = radius_x * math.cos(theta)
    y = radius_y * math.sin(theta)
    return center[0] + cosine * x - sine * y, center[1] + sine * x + cosine * y


def _ellipse_tangent(radius_x: float, radius_y: float, rotation: float, theta: float) -> Point:
    cosine, sine = math.cos(rotation), math.sin(rotation)
    dx = -radius_x * math.sin(theta)
    dy = radius_y * math.cos(theta)
    return cosine * dx - sine * dy, sine * dx + cosine * dy


def _vector_angle(first: Point, second: Point) -> float:
    return math.atan2(first[0] * second[1] - first[1] * second[0], first[0] * second[0] + first[1] * second[1])


def _arc_to_cubics(segment: _ArcSegment) -> tuple[tuple[Point, Point, Point, Point], ...]:
    """Apply the SVG 2 endpoint-to-center algorithm, then split at 90 degrees."""
    start, end = _point(segment.start), _point(segment.end)
    if start == end:
        return ()
    radius_x, radius_y = segment.radius_x, segment.radius_y
    if radius_x == 0.0 or radius_y == 0.0:
        return ((start, start, end, end),)
    rotation = math.radians(segment.rotation % 360.0)
    cosine, sine = math.cos(rotation), math.sin(rotation)
    midpoint_x = (start[0] - end[0]) * 0.5
    midpoint_y = (start[1] - end[1]) * 0.5
    x_prime = cosine * midpoint_x + sine * midpoint_y
    y_prime = -sine * midpoint_x + cosine * midpoint_y

    scale = x_prime * x_prime / (radius_x * radius_x) + y_prime * y_prime / (radius_y * radius_y)
    if scale > 1.0:
        correction = math.sqrt(scale)
        radius_x *= correction
        radius_y *= correction

    numerator = max(
        0.0,
        radius_x * radius_x * radius_y * radius_y
        - radius_x * radius_x * y_prime * y_prime
        - radius_y * radius_y * x_prime * x_prime,
    )
    denominator = radius_x * radius_x * y_prime * y_prime + radius_y * radius_y * x_prime * x_prime
    coefficient = 0.0 if denominator == 0.0 else math.sqrt(numerator / denominator)
    if segment.large_arc == segment.sweep:
        coefficient = -coefficient
    center_prime_x = coefficient * radius_x * y_prime / radius_y
    center_prime_y = coefficient * -radius_y * x_prime / radius_x
    center = (
        cosine * center_prime_x - sine * center_prime_y + (start[0] + end[0]) * 0.5,
        sine * center_prime_x + cosine * center_prime_y + (start[1] + end[1]) * 0.5,
    )

    unit_start = ((x_prime - center_prime_x) / radius_x, (y_prime - center_prime_y) / radius_y)
    unit_end = ((-x_prime - center_prime_x) / radius_x, (-y_prime - center_prime_y) / radius_y)
    theta = _vector_angle((1.0, 0.0), unit_start)
    sweep_angle = _vector_angle(unit_start, unit_end)
    if not segment.sweep and sweep_angle > 0.0:
        sweep_angle -= math.tau
    elif segment.sweep and sweep_angle < 0.0:
        sweep_angle += math.tau

    part_count = max(1, math.ceil(abs(sweep_angle) / (math.pi * 0.5)))
    part_sweep = sweep_angle / part_count
    cubics: list[tuple[Point, Point, Point, Point]] = []
    for index in range(part_count):
        first_theta = theta + index * part_sweep
        second_theta = first_theta + part_sweep
        first = _ellipse_point(center, radius_x, radius_y, rotation, first_theta)
        fourth = _ellipse_point(center, radius_x, radius_y, rotation, second_theta)
        k = (4.0 / 3.0) * math.tan(abs(part_sweep) / 4.0)
        signed_k = math.copysign(k, part_sweep)
        first_tangent = _ellipse_tangent(radius_x, radius_y, rotation, first_theta)
        second_tangent = _ellipse_tangent(radius_x, radius_y, rotation, second_theta)
        second = (first[0] + signed_k * first_tangent[0], first[1] + signed_k * first_tangent[1])
        third = (fourth[0] - signed_k * second_tangent[0], fourth[1] - signed_k * second_tangent[1])
        cubics.append((first, second, third, fourth))
    cubics[0] = (start,) + cubics[0][1:]
    cubics[-1] = cubics[-1][:-1] + (end,)
    return tuple(cubics)


def flatten_svg_path(path_data: str, delta: float) -> tuple[PolylineSubpath, ...]:
    """Parse path data and flatten every curve with the normative decisions."""
    if not isinstance(path_data, str) or not path_data.strip():
        raise ValueError("path_data must be a nonempty string")
    if not _PATH_TOKEN_RE.fullmatch(path_data):
        raise PathIntegrityError("path data contains unsupported tokens")
    delta = float(delta)
    if not math.isfinite(delta) or delta <= 0.0:
        raise ValueError("delta must be finite and positive")
    result: list[PolylineSubpath] = []
    for parsed_segments, closed in _explicit_path_parse(path_data):
        points: list[Point] = []
        for segment in parsed_segments:
            flattened: tuple[Point, ...]
            if isinstance(segment, _LineSegment):
                flattened = (segment.start, segment.end)
                if flattened[0] == flattened[1]:
                    raise PathIntegrityError("path contains a zero-length segment")
            elif isinstance(segment, _QuadraticSegment):
                flattened = _flatten_bezier(
                    (segment.start, segment.control, segment.end), delta / 8.0
                )
            elif isinstance(segment, _CubicSegment):
                flattened = _flatten_bezier(
                    (segment.start, segment.control1, segment.control2, segment.end),
                    delta / 8.0,
                )
            elif isinstance(segment, _ArcSegment):
                arc_points: list[Point] = []
                for cubic in _arc_to_cubics(segment):
                    flattened_cubic = _flatten_bezier(cubic, delta / 8.0)
                    arc_points.extend(flattened_cubic if not arc_points else flattened_cubic[1:])
                flattened = tuple(arc_points) if arc_points else (segment.start, segment.end)
            if all(point == flattened[0] for point in flattened[1:]):
                raise PathIntegrityError("path contains a zero-length segment")
            points.extend(flattened if not points else flattened[1:])
        if not points:
            continue
        if closed and points[-1] != points[0]:
            points.append(points[0])
        result.append(PolylineSubpath(tuple(points), closed))
    if not result:
        raise PathIntegrityError("path contains no drawable subpaths")
    return tuple(result)


def _segments(subpaths: Sequence[PolylineSubpath]) -> tuple[tuple[Point, Point], ...]:
    return tuple(
        (start, end)
        for subpath in subpaths
        for start, end in zip(subpath.points, subpath.points[1:])
        if start != end
    )


def _quadratic_for_point_to_segment(
    source_start: Point, source_vector: Point, target_start: Point, target_end: Point, sample_t: float
) -> tuple[float, float, float]:
    target_vector = (target_end[0] - target_start[0], target_end[1] - target_start[1])
    target_length_squared = target_vector[0] ** 2 + target_vector[1] ** 2
    difference = (source_start[0] - target_start[0], source_start[1] - target_start[1])
    if target_length_squared == 0.0:
        projected = None
    else:
        projected = (
            (difference[0] + sample_t * source_vector[0]) * target_vector[0]
            + (difference[1] + sample_t * source_vector[1]) * target_vector[1]
        ) / target_length_squared
    endpoint = target_start if projected is None or projected <= 0.0 else target_end if projected >= 1.0 else None
    if endpoint is not None:
        offset = (source_start[0] - endpoint[0], source_start[1] - endpoint[1])
        return (
            source_vector[0] ** 2 + source_vector[1] ** 2,
            2.0 * (offset[0] * source_vector[0] + offset[1] * source_vector[1]),
            offset[0] ** 2 + offset[1] ** 2,
        )
    dot_difference = difference[0] * target_vector[0] + difference[1] * target_vector[1]
    dot_source = source_vector[0] * target_vector[0] + source_vector[1] * target_vector[1]
    return (
        source_vector[0] ** 2 + source_vector[1] ** 2 - dot_source**2 / target_length_squared,
        2.0
        * (
            difference[0] * source_vector[0]
            + difference[1] * source_vector[1]
            - dot_difference * dot_source / target_length_squared
        ),
        difference[0] ** 2 + difference[1] ** 2 - dot_difference**2 / target_length_squared,
    )


def _polynomial_roots_between(
    first: tuple[float, float, float], second: tuple[float, float, float], low: float, high: float
) -> tuple[float, ...]:
    a, b, c = (first[index] - second[index] for index in range(3))
    epsilon = 1e-14
    roots: list[float] = []
    if abs(a) <= epsilon:
        if abs(b) > epsilon:
            roots.append(-c / b)
    else:
        discriminant = b * b - 4.0 * a * c
        if discriminant >= -epsilon:
            square_root = math.sqrt(max(0.0, discriminant))
            roots.extend(((-b - square_root) / (2.0 * a), (-b + square_root) / (2.0 * a)))
    return tuple(root for root in roots if low < root < high)


def _directed_segment_hausdorff(source: tuple[Point, Point], targets: Sequence[tuple[Point, Point]]) -> float:
    source_start, source_end = source
    source_vector = (source_end[0] - source_start[0], source_end[1] - source_start[1])
    breakpoints = {0.0, 1.0}
    for target_start, target_end in targets:
        target_vector = (target_end[0] - target_start[0], target_end[1] - target_start[1])
        target_length_squared = target_vector[0] ** 2 + target_vector[1] ** 2
        if target_length_squared == 0.0:
            continue
        u0 = (
            (source_start[0] - target_start[0]) * target_vector[0]
            + (source_start[1] - target_start[1]) * target_vector[1]
        ) / target_length_squared
        u1 = (source_vector[0] * target_vector[0] + source_vector[1] * target_vector[1]) / target_length_squared
        if u1 != 0.0:
            breakpoints.update(value for value in (-u0 / u1, (1.0 - u0) / u1) if 0.0 < value < 1.0)
    ordered = sorted(breakpoints)
    candidates = set(ordered)
    for low, high in zip(ordered, ordered[1:]):
        sample = (low + high) * 0.5
        polynomials = [
            _quadratic_for_point_to_segment(source_start, source_vector, target_start, target_end, sample)
            for target_start, target_end in targets
        ]
        for index, first in enumerate(polynomials):
            for second in polynomials[index + 1 :]:
                candidates.update(_polynomial_roots_between(first, second, low, high))

    maximum_squared = 0.0
    for parameter in candidates:
        point = (
            source_start[0] + parameter * source_vector[0],
            source_start[1] + parameter * source_vector[1],
        )
        distance_squared = min(_point_segment_distance(point, *target) ** 2 for target in targets)
        maximum_squared = max(maximum_squared, distance_squared)
    return math.sqrt(maximum_squared)


def symmetric_hausdorff(
    first: Sequence[PolylineSubpath], second: Sequence[PolylineSubpath]
) -> float:
    """Return exact symmetric Hausdorff distance between continuous segments."""
    first_segments, second_segments = _segments(first), _segments(second)
    if not first_segments or not second_segments:
        raise ValueError("both polyline collections must contain non-degenerate segments")
    first_to_second = max(_directed_segment_hausdorff(segment, second_segments) for segment in first_segments)
    second_to_first = max(_directed_segment_hausdorff(segment, first_segments) for segment in second_segments)
    return max(first_to_second, second_to_first)


def _douglas_peucker(points: tuple[Point, ...], tolerance: float) -> tuple[Point, ...]:
    if len(points) <= 2:
        return points
    distances = tuple(_point_segment_distance(point, points[0], points[-1]) for point in points[1:-1])
    maximum = max(distances, default=0.0)
    if maximum <= tolerance:
        return points[0], points[-1]
    split = 1 + distances.index(maximum)
    left = _douglas_peucker(points[: split + 1], tolerance)
    right = _douglas_peucker(points[split:], tolerance)
    return left[:-1] + right


def _canonical_closed(subpath: PolylineSubpath) -> PolylineSubpath:
    vertices = list(subpath.points[:-1])
    start_index = min(range(len(vertices)), key=lambda index: vertices[index])
    rotated = vertices[start_index:] + vertices[:start_index]
    return PolylineSubpath(tuple(rotated + [rotated[0]]), True)


def _simplify_closed(subpath: PolylineSubpath, tolerance: float) -> PolylineSubpath:
    canonical = _canonical_closed(subpath)
    vertices = canonical.points[:-1]
    if len(vertices) <= 3:
        return canonical
    split = max(range(1, len(vertices)), key=lambda index: _distance(vertices[0], vertices[index]))
    first_half = _douglas_peucker(vertices[: split + 1], tolerance)
    second_half = _douglas_peucker(vertices[split:] + (vertices[0],), tolerance)
    points = first_half[:-1] + second_half
    candidate = PolylineSubpath(points, True)
    if candidate.signed_area * canonical.signed_area <= 0.0 or len(candidate.points) < 4:
        return canonical
    return candidate


def _point_in_polygon(point: Point, polygon: PolylineSubpath) -> bool:
    inside = False
    x, y = point
    for first, second in zip(polygon.points, polygon.points[1:]):
        if (first[1] > y) != (second[1] > y):
            intersection_x = first[0] + (y - first[1]) * (second[0] - first[0]) / (second[1] - first[1])
            if x < intersection_x:
                inside = not inside
    return inside


def _containment_signature(subpaths: Sequence[PolylineSubpath]) -> tuple[tuple[int, int], ...]:
    closed = [(index, subpath) for index, subpath in enumerate(subpaths) if subpath.closed]
    return tuple(
        (inner_index, outer_index)
        for inner_index, inner in closed
        for outer_index, outer in closed
        if inner_index != outer_index and _point_in_polygon(inner.points[0], outer)
    )


def _intersection_signature(subpaths: Sequence[PolylineSubpath]) -> tuple[tuple[int, int], ...]:
    signature: list[tuple[int, int]] = []
    segment_sets = [tuple(zip(path.points, path.points[1:])) for path in subpaths]
    for first_index, first_segments in enumerate(segment_sets):
        for second_index in range(first_index, len(segment_sets)):
            second_segments = segment_sets[second_index]
            intersects = False
            for left_index, first_segment in enumerate(first_segments):
                for right_index, second_segment in enumerate(second_segments):
                    if first_index == second_index:
                        if abs(left_index - right_index) <= 1:
                            continue
                        if subpaths[first_index].closed and {left_index, right_index} == {0, len(first_segments) - 1}:
                            continue
                    if _segments_intersect(first_segment, second_segment):
                        intersects = True
                        break
                if intersects:
                    break
            if intersects:
                signature.append((first_index, second_index))
    return tuple(signature)


def simplify_subpaths(
    subpaths: Sequence[PolylineSubpath], delta: float
) -> tuple[PolylineSubpath, ...]:
    """Simplify independently while preserving winding, holes and topology."""
    delta = float(delta)
    if not math.isfinite(delta) or delta <= 0.0:
        raise ValueError("delta must be finite and positive")
    originals = tuple(_canonical_closed(path) if path.closed else path for path in subpaths)
    simplified = tuple(
        _simplify_closed(path, delta / 2.0)
        if path.closed
        else PolylineSubpath(_douglas_peucker(path.points, delta / 2.0), False)
        for path in originals
    )
    winding_preserved = all(
        not original.closed or original.signed_area * candidate.signed_area > 0.0
        for original, candidate in zip(originals, simplified, strict=True)
    )
    if (
        not winding_preserved
        or _containment_signature(originals) != _containment_signature(simplified)
        or _intersection_signature(originals) != _intersection_signature(simplified)
        or any(
            symmetric_hausdorff((original,), (candidate,)) > delta / 2.0 + 1e-12
            for original, candidate in zip(originals, simplified, strict=True)
        )
    ):
        return originals
    return simplified


def _component_points(component: object) -> tuple[Point, ...]:
    if isinstance(component, PolylineSubpath):
        return component.points
    if isinstance(component, Mapping):
        value = component.get("points")
    else:
        value = component
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("component geometry must provide a points sequence")
    return tuple(_point(point) for point in value)


def _direction(points: tuple[Point, ...]) -> Point:
    vector = (points[-1][0] - points[0][0], points[-1][1] - points[0][1])
    length = math.hypot(*vector)
    if length == 0.0:
        raise PathIntegrityError("constraint direction is degenerate")
    return vector[0] / length, vector[1] / length


def _minimum_polyline_distance(first: tuple[Point, ...], second: tuple[Point, ...]) -> float:
    first_segments = tuple(zip(first, first[1:]))
    second_segments = tuple(zip(second, second[1:]))
    return min(
        min(
            _point_segment_distance(a, c, d),
            _point_segment_distance(b, c, d),
            _point_segment_distance(c, a, b),
            _point_segment_distance(d, a, b),
        )
        for a, b in first_segments
        for c, d in second_segments
    )


def _segments_intersect(first: tuple[Point, Point], second: tuple[Point, Point]) -> bool:
    def orientation(a: Point, b: Point, c: Point) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    a, b = first
    c, d = second
    values = orientation(a, b, c), orientation(a, b, d), orientation(c, d, a), orientation(c, d, b)
    if values[0] * values[1] < 0.0 and values[2] * values[3] < 0.0:
        return True
    return _minimum_polyline_distance((a, b), (c, d)) <= 1e-12


def evaluate_geometry_constraints(
    components: Mapping[str, object],
    constraints: Mapping[str, object],
    *,
    delta: float,
    canonical_canvas: Point | None = None,
) -> GeometryConstraintEvaluation:
    """Evaluate universal numeric reconstruction-map constraints."""
    if delta <= 0.0 or not math.isfinite(delta):
        raise ValueError("delta must be finite and positive")
    if canonical_canvas is not None:
        canonical_canvas = _point(canonical_canvas)
        if canonical_canvas[0] <= 0.0 or canonical_canvas[1] <= 0.0:
            raise ValueError("canonical_canvas dimensions must be positive")

    def map_point(value: object) -> Point:
        result = _point(value)  # type: ignore[arg-type]
        if canonical_canvas is None:
            return result
        return result[0] * canonical_canvas[0], result[1] * canonical_canvas[1]

    points = {component_id: _component_points(component) for component_id, component in components.items()}
    measurements: list[ConstraintMeasurement] = []

    def add(kind: str, subject: str, measured: float, tolerance: float, details: Mapping[str, object], passed: bool | None = None) -> None:
        effective_passed = measured <= tolerance if passed is None else passed
        measurements.append(ConstraintMeasurement(kind, subject, float(measured), float(tolerance), effective_passed, details))

    for constraint in constraints.get("lines", []):
        component_id = str(constraint["component_id"])
        start, end = map_point(constraint["start"]), map_point(constraint["end"])
        measured = max(_point_segment_distance(point, start, end) for point in points[component_id])
        add("line", component_id, measured, float(constraint.get("tolerance", delta)), {"start": start, "end": end})

    for kind, formula in (("orthogonality", "dot"), ("parallelism", "cross")):
        for constraint in constraints.get(kind, []):
            first, second = str(constraint["first"]), str(constraint["second"])
            first_direction, second_direction = _direction(points[first]), _direction(points[second])
            measured = (
                abs(first_direction[0] * second_direction[0] + first_direction[1] * second_direction[1])
                if formula == "dot"
                else abs(first_direction[0] * second_direction[1] - first_direction[1] * second_direction[0])
            )
            add(kind, f"{first}:{second}", measured, float(constraint.get("tolerance", delta)), {})

    for constraint in constraints.get("endpoints", []):
        component_id = str(constraint["component_id"])
        expected_start, expected_end = map_point(constraint["start"]), map_point(constraint["end"])
        actual_start, actual_end = points[component_id][0], points[component_id][-1]
        measured = min(
            max(_distance(actual_start, expected_start), _distance(actual_end, expected_end)),
            max(_distance(actual_start, expected_end), _distance(actual_end, expected_start)),
        )
        add("endpoints", component_id, measured, float(constraint.get("tolerance", delta)), {"actual_start": actual_start, "actual_end": actual_end})

    for constraint in constraints.get("radial", []):
        component_id = str(constraint["component_id"])
        center = map_point(constraint["center"])
        if constraint["geometry"] == "circle":
            radius_x = radius_y = float(constraint["radius"])
        else:
            radius_x, radius_y = float(constraint["radius_x"]), float(constraint["radius_y"])
        deviations = []
        for x, y in points[component_id]:
            normalized_radius = math.hypot((x - center[0]) / radius_x, (y - center[1]) / radius_y)
            deviations.append(abs(normalized_radius - 1.0) * max(radius_x, radius_y))
        add("radial", component_id, max(deviations), float(constraint.get("tolerance", delta)), {"center": center})

    for constraint in constraints.get("symmetry", []):
        component_id = str(constraint["component_id"])
        axis_start, axis_end = map_point(constraint["axis_start"]), map_point(constraint["axis_end"])
        axis_dx, axis_dy = axis_end[0] - axis_start[0], axis_end[1] - axis_start[1]
        axis_squared = axis_dx**2 + axis_dy**2
        if axis_squared == 0.0:
            raise PathIntegrityError("symmetry axis is degenerate")
        reflected = []
        for point in points[component_id]:
            projection = ((point[0] - axis_start[0]) * axis_dx + (point[1] - axis_start[1]) * axis_dy) / axis_squared
            on_axis = (axis_start[0] + projection * axis_dx, axis_start[1] + projection * axis_dy)
            reflected.append((2.0 * on_axis[0] - point[0], 2.0 * on_axis[1] - point[1]))
        original_path = PolylineSubpath(points[component_id], points[component_id][0] == points[component_id][-1])
        reflected_path = PolylineSubpath(tuple(reflected), reflected[0] == reflected[-1])
        measured = symmetric_hausdorff((original_path,), (reflected_path,))
        add("symmetry", component_id, measured, float(constraint.get("tolerance", delta)), {"axis_start": axis_start, "axis_end": axis_end})

    for constraint in constraints.get("strokes", []):
        component_id = str(constraint["component_id"])
        component = components[component_id]
        if not isinstance(component, Mapping):
            raise ValueError("stroke constraints require component style metadata")
        tolerance = float(constraint.get("tolerance", delta))
        measured = abs(float(component.get("stroke_width", 0.0)) - float(constraint["expected_width"]))
        style_matches = component.get("cap") == constraint["cap"] and component.get("join") == constraint["join"]
        add("stroke", component_id, measured, tolerance, {"cap": component.get("cap"), "join": component.get("join")}, measured <= tolerance and style_matches)

    allowed_intersections: set[frozenset[str]] = set()
    for constraint in constraints.get("intentional_intersections", []):
        first, second = str(constraint["first"]), str(constraint["second"])
        allowed_intersections.add(frozenset((first, second)))
        intersects = any(
            _segments_intersect(first_segment, second_segment)
            for first_segment in zip(points[first], points[first][1:])
            for second_segment in zip(points[second], points[second][1:])
        )
        add("intersection", f"{first}:{second}", 0.0 if intersects else 1.0, 0.0, {}, intersects)
    component_ids = tuple(points)
    for first_index, first in enumerate(component_ids):
        for second in component_ids[first_index + 1 :]:
            pair = frozenset((first, second))
            if pair in allowed_intersections:
                continue
            intersects = any(
                _segments_intersect(first_segment, second_segment)
                for first_segment in zip(points[first], points[first][1:])
                for second_segment in zip(points[second], points[second][1:])
            )
            if intersects:
                add("intersection", f"{first}:{second}", 1.0, 0.0, {"unexpected": True}, False)

    for constraint in constraints.get("minimum_intentional_gaps", []):
        first, second = str(constraint["first"]), str(constraint["second"])
        expected = float(constraint["minimum_gap"])
        observed = _minimum_polyline_distance(points[first], points[second])
        add("gap", f"{first}:{second}", max(0.0, expected - observed), 0.0, {"observed_gap": observed, "minimum_gap": expected}, observed >= expected)

    return GeometryConstraintEvaluation(all(measurement.passed for measurement in measurements), tuple(measurements))


def _as_mask(mask: NDArray[np.bool_] | Iterable[Iterable[bool]]) -> BoolMask:
    result = np.asarray(mask)
    if result.ndim != 2:
        raise ValueError("mask must be a two-dimensional array")
    if np.issubdtype(result.dtype, np.bool_):
        return result.astype(bool, copy=False)
    if not np.issubdtype(result.dtype, np.floating):
        raise TypeError("mask must contain bool or floating-point coverage values")
    coverage = result.astype(np.float64, copy=False)
    if not np.all(np.isfinite(coverage)) or np.any(coverage < 0.0) or np.any(coverage > 1.0):
        raise ValueError("mask coverage values must be finite and between 0 and 1")
    return coverage >= np.float64(0.5)


def euclidean_disk(radius: int) -> BoolMask:
    """Return the exact set of integer offsets at Euclidean distance ``<= r``."""
    if isinstance(radius, bool) or not isinstance(radius, (int, np.integer)):
        raise TypeError("radius must be an integer")
    if radius < 0:
        raise ValueError("radius must be non-negative")
    offsets = np.arange(-radius, radius + 1, dtype=np.int64)
    dx, dy = np.meshgrid(offsets, offsets)
    return (dx * dx + dy * dy) <= radius * radius


def _disk_offsets(radius: int) -> Iterable[tuple[int, int]]:
    disk = euclidean_disk(radius)
    center = radius
    for y, x in np.argwhere(disk):
        yield int(y - center), int(x - center)


def dilate(mask: NDArray[np.bool_] | Iterable[Iterable[bool]], radius: int) -> BoolMask:
    """Dilate on the finite canvas, treating outside pixels as background."""
    source = _as_mask(mask)
    height, width = source.shape
    result = np.zeros_like(source)
    for dy, dx in _disk_offsets(radius):
        destination_y = max(0, dy)
        destination_x = max(0, dx)
        source_y = max(0, -dy)
        source_x = max(0, -dx)
        rows = height - abs(dy)
        columns = width - abs(dx)
        if rows and columns:
            result[destination_y : destination_y + rows, destination_x : destination_x + columns] |= source[
                source_y : source_y + rows, source_x : source_x + columns
            ]
    return result


def erode(mask: NDArray[np.bool_] | Iterable[Iterable[bool]], radius: int) -> BoolMask:
    """Erode on the finite canvas, treating outside pixels as background."""
    source = _as_mask(mask)
    height, width = source.shape
    result = np.ones_like(source)
    for dy, dx in _disk_offsets(radius):
        shifted = np.zeros_like(source)
        destination_y = max(0, -dy)
        destination_x = max(0, -dx)
        source_y = max(0, dy)
        source_x = max(0, dx)
        rows = height - abs(dy)
        columns = width - abs(dx)
        if rows and columns:
            shifted[destination_y : destination_y + rows, destination_x : destination_x + columns] = source[
                source_y : source_y + rows, source_x : source_x + columns
            ]
        result &= shifted
    return result


def closing(mask: NDArray[np.bool_] | Iterable[Iterable[bool]], radius: int) -> BoolMask:
    """Apply normative Euclidean dilation followed by erosion."""
    return erode(dilate(mask, radius), radius)


def binary_boundary(mask: NDArray[np.bool_] | Iterable[Iterable[bool]]) -> BoolMask:
    """Return foreground pixels with a 4-connected background neighbour."""
    source = _as_mask(mask)
    padded = np.pad(source, 1, mode="constant", constant_values=False)
    interior = padded[1:-1, 1:-1]
    all_four_neighbours_are_foreground = (
        padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
    )
    return interior & ~all_four_neighbours_are_foreground


def masked_boundary(
    mask: NDArray[np.bool_] | Iterable[Iterable[bool]],
    uncertainty: NDArray[np.bool_] | Iterable[Iterable[bool]],
) -> BoolMask:
    """Remove uncertainty from a boundary extracted from the *full* mask."""
    source = _as_mask(mask)
    excluded = _as_mask(uncertainty)
    if source.shape != excluded.shape:
        raise ValueError("mask and uncertainty must have the same shape")
    return binary_boundary(source) & ~excluded


def connected_components(
    mask: NDArray[np.bool_] | Iterable[Iterable[bool]], *, connectivity: int = 8
) -> tuple[frozenset[tuple[int, int]], ...]:
    """Return components as immutable ``(row, column)`` pixel sets."""
    source = _as_mask(mask)
    if connectivity == 4:
        neighbours = ((-1, 0), (0, -1), (0, 1), (1, 0))
    elif connectivity == 8:
        neighbours = tuple(
            (dy, dx)
            for dy in (-1, 0, 1)
            for dx in (-1, 0, 1)
            if (dy, dx) != (0, 0)
        )
    else:
        raise ValueError("connectivity must be 4 or 8")

    height, width = source.shape
    unseen = source.copy()
    components: list[frozenset[tuple[int, int]]] = []
    for start_y, start_x in np.argwhere(unseen):
        y, x = int(start_y), int(start_x)
        if not unseen[y, x]:
            continue
        unseen[y, x] = False
        queue: deque[tuple[int, int]] = deque([(y, x)])
        pixels: set[tuple[int, int]] = {(y, x)}
        while queue:
            current_y, current_x = queue.popleft()
            for dy, dx in neighbours:
                next_y, next_x = current_y + dy, current_x + dx
                if 0 <= next_y < height and 0 <= next_x < width and unseen[next_y, next_x]:
                    unseen[next_y, next_x] = False
                    pixels.add((next_y, next_x))
                    queue.append((next_y, next_x))
        components.append(frozenset(pixels))
    return tuple(components)


def count_holes(mask: NDArray[np.bool_] | Iterable[Iterable[bool]]) -> int:
    """Count 4-connected background regions not connected to the canvas edge."""
    source = _as_mask(mask)
    height, width = source.shape
    holes = 0
    for component in connected_components(~source, connectivity=4):
        if not any(y in (0, height - 1) or x in (0, width - 1) for y, x in component):
            holes += 1
    return holes


def component_enclosure(
    mask: NDArray[np.bool_] | Iterable[Iterable[bool]], radius: int
) -> BoolMask:
    """Return geometry and regions enclosed after closing boundary gaps.

    The boundary is taken from the isolated full component geometry.  Its
    normative Euclidean closing is then treated as an impenetrable wall for a
    4-connected flood fill from the canvas edge.
    """
    source = _as_mask(mask)
    if not source.shape[0] or not source.shape[1]:
        raise ValueError("mask dimensions must be non-zero")
    wall = closing(binary_boundary(source), radius)
    background = ~wall
    exterior = np.zeros_like(source)
    height, width = source.shape
    queue: deque[tuple[int, int]] = deque()

    for x in range(width):
        for y in (0, height - 1):
            if background[y, x] and not exterior[y, x]:
                exterior[y, x] = True
                queue.append((y, x))
    for y in range(height):
        for x in (0, width - 1):
            if background[y, x] and not exterior[y, x]:
                exterior[y, x] = True
                queue.append((y, x))

    while queue:
        y, x = queue.popleft()
        for dy, dx in ((-1, 0), (0, -1), (0, 1), (1, 0)):
            next_y, next_x = y + dy, x + dx
            if (
                0 <= next_y < height
                and 0 <= next_x < width
                and background[next_y, next_x]
                and not exterior[next_y, next_x]
            ):
                exterior[next_y, next_x] = True
                queue.append((next_y, next_x))
    return ~exterior
