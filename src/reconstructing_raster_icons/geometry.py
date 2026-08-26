"""Normative integer-grid morphology for acceptance masks."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

import numpy as np
from numpy.typing import NDArray


BoolMask = NDArray[np.bool_]


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
