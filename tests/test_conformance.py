"""Independent conformance and adversarial checks for acceptance model 1.0.3."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import math
import os
import platform
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from xml.etree import ElementTree
import zlib

import numpy as np
from PIL import Image, PngImagePlugin
from jsonschema import ValidationError
from scipy.ndimage import binary_closing, binary_dilation, distance_transform_edt, label

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reconstructing_raster_icons.constants import (
    ACCEPTANCE_MODEL_VERSION,
    AUTOMATIC_GATE_IDS,
    SEMANTIC_GATE_IDS,
    ExitCode,
    Status,
)
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
from reconstructing_raster_icons.renderer import load_renderer_lock, resolve_canonical_node
from reconstructing_raster_icons.reports import GateEvidence, GateResult, resolve_status
from reconstructing_raster_icons.schema_io import validate_document
import reconstructing_raster_icons.pipeline as pipeline_module
import reconstructing_raster_icons.renderer as renderer_module


REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURES = REPOSITORY / "tests" / "fixtures"
CONFORMANCE = FIXTURES / "conformance"
CONTRACTS = FIXTURES / "contracts"
SECURITY = FIXTURES / "security"
GOLDEN = REPOSITORY / "tests" / "goldens" / "acceptance-model-1.0.3.json"
FIXED_TIME = "2026-08-26T12:00:00Z"
PINNED_LOADER_SHA256 = "10170d02d816f02ec76f9bc095b01d9becf536e7b1e12e5aa616652c84b237a1"
PINNED_WASM_SHA256 = "22bf6e9f9a100d972da0411a69c5ba504367fc1fa87b3b64e3f35e53926d2d70"
PINNED_RUNNER_SHA256 = "11b08e3fda461c2cc2bd7f03bbf6e0d21bcaf634e2d0ad0626cd71e0921b1af1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PNG_FILTERED_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class PixelOracleResult:
    authority: str
    production_canonical_environment: bool
    png_bytes: bytes
    sha256: str
    size: tuple[int, int]
    attestation: dict[str, object]


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_generated_fixture_matches_committed(
    generated: Path,
    committed: Path,
    relative: Path,
) -> None:
    if generated.suffix.lower() == ".png":
        _assert_png_fixture_matches(generated, committed, relative)
        return
    generated_source = generated.with_name("source.png")
    committed_source = committed.with_name("source.png")
    if (
        generated.name == "draft.json"
        and generated_source.is_file()
        and committed_source.is_file()
    ):
        generated_document = _verified_relational_draft(generated, generated_source, relative)
        committed_document = _verified_relational_draft(committed, committed_source, relative)
        generated_document["source_sha256"] = "<tree-source-sha256>"
        committed_document["source_sha256"] = "<tree-source-sha256>"
        if _canonical_json(generated_document) != _canonical_json(committed_document):
            raise AssertionError(f"fixture mismatch at {relative}: decoded draft JSON differs")
        return
    if generated.read_bytes() != committed.read_bytes():
        raise AssertionError(f"fixture mismatch at {relative}: raw bytes differ")


def _verified_relational_draft(path: Path, source: Path, relative: Path) -> dict[str, object]:
    document = _read_json_without_duplicates(path, relative)
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    if document.get("source_sha256") != expected:
        raise AssertionError(
            f"fixture mismatch at {relative}: source_sha256 does not match sibling source.png"
        )
    return document


def _read_json_without_duplicates(path: Path, relative: Path) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AssertionError(f"fixture mismatch at {relative}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise AssertionError(f"fixture mismatch at {relative}: invalid JSON") from error
    if not isinstance(document, dict):
        raise AssertionError(f"fixture mismatch at {relative}: draft JSON must be an object")
    return document


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _expected_png_filtered_bytes(ihdr: bytes) -> int:
    if len(ihdr) != 13:
        raise AssertionError("PNG IHDR must contain exactly 13 bytes")
    width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    if width == 0 or height == 0 or width > 0x7FFFFFFF or height > 0x7FFFFFFF:
        raise AssertionError("PNG IHDR dimensions are invalid")
    legal_bit_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    if color_type not in legal_bit_depths or bit_depth not in legal_bit_depths[color_type]:
        raise AssertionError("PNG IHDR color type and bit depth are incompatible")
    if compression != 0 or filtering != 0 or interlace not in {0, 1}:
        raise AssertionError("PNG IHDR compression, filter, or interlace method is invalid")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    bits_per_pixel = channels * bit_depth

    def pass_bytes(
        column_start: int,
        row_start: int,
        column_step: int,
        row_step: int,
    ) -> int:
        pass_width = (
            0
            if width <= column_start
            else (width - column_start + column_step - 1) // column_step
        )
        pass_height = (
            0
            if height <= row_start
            else (height - row_start + row_step - 1) // row_step
        )
        if pass_width == 0 or pass_height == 0:
            return 0
        scanline_bytes = (pass_width * bits_per_pixel + 7) // 8
        return pass_height * (1 + scanline_bytes)

    if interlace == 0:
        expected = pass_bytes(0, 0, 1, 1)
    else:
        expected = sum(
            pass_bytes(*adam7_pass)
            for adam7_pass in (
                (0, 0, 8, 8),
                (4, 0, 8, 8),
                (0, 4, 4, 8),
                (2, 0, 4, 4),
                (0, 2, 2, 4),
                (1, 0, 2, 2),
                (0, 1, 1, 2),
            )
        )
    if expected > MAX_PNG_FILTERED_BYTES:
        raise AssertionError("PNG filtered scanlines exceed decompression bound")
    return expected


def _validate_png_idat_stream(payload: bytes, expected_filtered_bytes: int) -> None:
    decompressor = zlib.decompressobj()
    try:
        filtered = decompressor.decompress(payload, expected_filtered_bytes + 1)
        if len(filtered) > expected_filtered_bytes or decompressor.unconsumed_tail:
            raise AssertionError("PNG filtered scanline length does not match IHDR")
        filtered += decompressor.flush(expected_filtered_bytes + 1 - len(filtered))
    except zlib.error as error:
        raise AssertionError("PNG IDAT zlib stream is corrupt") from error
    if len(filtered) > expected_filtered_bytes or decompressor.unconsumed_tail:
        raise AssertionError("PNG filtered scanline length does not match IHDR")
    if not decompressor.eof:
        raise AssertionError("PNG IDAT zlib stream is incomplete")
    if decompressor.unused_data:
        raise AssertionError("PNG IDAT zlib stream has trailing data or multiple streams")
    if len(filtered) != expected_filtered_bytes:
        raise AssertionError("PNG filtered scanline length does not match IHDR")


def _png_normalized_chunks(path: Path) -> tuple[tuple[bytes, bytes], ...]:
    payload = path.read_bytes()
    if not payload.startswith(PNG_SIGNATURE):
        raise AssertionError("invalid PNG signature")
    offset = len(PNG_SIGNATURE)
    chunks: list[tuple[bytes, bytes]] = []
    idat_payloads: list[bytes] = []
    ihdr: bytes | None = None
    saw_idat = False
    closed_idat = False
    while offset < len(payload):
        if len(payload) - offset < 12:
            raise AssertionError("truncated PNG chunk")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(payload):
            raise AssertionError("PNG chunk exceeds payload bounds")
        chunk_data = payload[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", payload[offset + 8 + length : chunk_end])[0]
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
            raise AssertionError("PNG chunk CRC mismatch")
        if not chunks and not saw_idat and chunk_type != b"IHDR":
            raise AssertionError("PNG does not begin with IHDR")
        if chunk_type == b"IHDR":
            if ihdr is not None:
                raise AssertionError("PNG contains multiple IHDR chunks")
            ihdr = chunk_data
        if chunk_type == b"IDAT":
            if closed_idat:
                raise AssertionError("PNG IDAT chunks are not contiguous")
            if not saw_idat:
                chunks.append((b"IDAT", b"<normalized-payload>"))
            idat_payloads.append(chunk_data)
            saw_idat = True
        else:
            if saw_idat:
                closed_idat = True
            chunks.append((chunk_type, chunk_data))
        offset = chunk_end
    if not saw_idat:
        raise AssertionError("PNG has no IDAT chunk")
    if ihdr is None:
        raise AssertionError("PNG has no IHDR chunk")
    _validate_png_idat_stream(
        b"".join(idat_payloads),
        _expected_png_filtered_bytes(ihdr),
    )
    return tuple(chunks)


def _png_pillow_contract(path: Path, relative: Path, stage: str) -> dict[str, object]:
    try:
        with Image.open(path) as image:
            image.load()
            rgba_bytes = image.convert("RGBA").tobytes()
            return {
                "format": image.format,
                "mode": image.mode,
                "bands": image.getbands(),
                "size": image.size,
                "n_frames": getattr(image, "n_frames", 1),
                "info": copy.deepcopy(image.info),
                "decoded_rgba": rgba_bytes,
                "decoded_rgba_sha256": hashlib.sha256(rgba_bytes).hexdigest(),
            }
    except (Image.DecompressionBombError, OSError, SyntaxError, ValueError) as error:
        raise AssertionError(
            f"fixture mismatch at {relative}: PNG {stage} Pillow decode failed: {error}"
        ) from error


def _assert_png_fixture_matches(generated: Path, committed: Path, relative: Path) -> None:
    try:
        generated_chunks = _png_normalized_chunks(generated)
        committed_chunks = _png_normalized_chunks(committed)
    except AssertionError as error:
        raise AssertionError(f"fixture mismatch at {relative}: {error}") from error
    if generated_chunks != committed_chunks:
        raise AssertionError(
            f"fixture mismatch at {relative}: normalized IDAT position or "
            "ordered non-IDAT chunks differ"
        )
    generated_contract = _png_pillow_contract(generated, relative, "generated")
    committed_contract = _png_pillow_contract(committed, relative, "committed")
    for field in (
        "format", "mode", "bands", "size", "n_frames", "info",
        "decoded_rgba", "decoded_rgba_sha256",
    ):
        if generated_contract[field] != committed_contract[field]:
            raise AssertionError(f"fixture mismatch at {relative}: PNG {field} differs")


def _assert_raw_fixture_trees_equal(first: Path, second: Path) -> None:
    first_files = {path.relative_to(first) for path in first.rglob("*") if path.is_file()}
    second_files = {path.relative_to(second) for path in second.rglob("*") if path.is_file()}
    if first_files != second_files:
        relative = min(first_files ^ second_files)
        raise AssertionError(f"same-host fixture trees first differ at {relative}: file presence")
    for relative in sorted(first_files):
        if (first / relative).read_bytes() != (second / relative).read_bytes():
            raise AssertionError(f"same-host fixture trees first differ at {relative}: raw bytes")


def _declared_generated_inventory(root: Path) -> tuple[Path, ...]:
    manifest = _read_json_without_duplicates(
        root / "conformance" / "manifest.json",
        Path("conformance/manifest.json"),
    )
    inventory = manifest.get("generated_files")
    if not isinstance(inventory, dict) or set(inventory) != {"version", "paths"}:
        raise AssertionError("generated_files inventory shape is invalid")
    if inventory["version"] != "1.0.0" or not isinstance(inventory["paths"], list):
        raise AssertionError("generated_files inventory version or paths are invalid")
    raw_paths = inventory["paths"]
    if any(not isinstance(value, str) for value in raw_paths):
        raise AssertionError("generated_files inventory paths must be strings")
    if raw_paths != sorted(set(raw_paths)):
        raise AssertionError("generated_files inventory paths must be sorted and unique")
    declared = tuple(Path(value) for value in raw_paths)
    if any(
        path.is_absolute()
        or ".." in path.parts
        or path.as_posix() != raw
        for path, raw in zip(declared, raw_paths, strict=True)
    ):
        raise AssertionError("generated_files inventory contains an unsafe path")
    return declared


def _assert_generated_inventory(root: Path) -> tuple[Path, ...]:
    declared = _declared_generated_inventory(root)
    declared_set = set(declared)
    actual = {path.relative_to(root) for path in root.rglob("*") if path.is_file()}
    if actual != declared_set:
        relative = min(actual ^ declared_set)
        detail = "undeclared generated file" if relative in actual else "declared file is missing"
        raise AssertionError(f"generated inventory mismatch at {relative}: {detail}")
    return declared


COMMITTED_GENERATED_FILES = _declared_generated_inventory(FIXTURES)


def _replace_first_idat_payload(payload: bytes, replacement: bytes) -> bytes:
    idat_type_offset = payload.index(b"IDAT")
    chunk_offset = idat_type_offset - 4
    idat_length = struct.unpack(">I", payload[chunk_offset:idat_type_offset])[0]
    chunk_end = idat_type_offset + 8 + idat_length
    crc = zlib.crc32(b"IDAT" + replacement) & 0xFFFFFFFF
    chunk = (
        struct.pack(">I", len(replacement))
        + b"IDAT"
        + replacement
        + struct.pack(">I", crc)
    )
    return payload[:chunk_offset] + chunk + payload[chunk_end:]


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


def _verify_test_renderer_contract() -> tuple[Path, Path, Path, Path]:
    lock = load_renderer_lock(REPOSITORY / "canonical-renderer.lock")
    platform_key = renderer_module._platform_key()
    node = resolve_canonical_node(lock, platform_key).source
    loader = REPOSITORY / "node_modules" / "@resvg" / "resvg-wasm" / "index.mjs"
    wasm = REPOSITORY / "node_modules" / "@resvg" / "resvg-wasm" / "index_bg.wasm"
    runner = REPOSITORY / "scripts" / "render_svg.mjs"
    expected = {
        loader: PINNED_LOADER_SHA256,
        wasm: PINNED_WASM_SHA256,
        runner: PINNED_RUNNER_SHA256,
    }
    mismatches = [path.name for path, digest in expected.items() if _sha256(path) != digest]
    if mismatches:
        raise AssertionError(f"pinned test renderer hash mismatch: {', '.join(mismatches)}")
    lock_document = _read_json(REPOSITORY / "canonical-renderer.lock")
    if (
        lock_document.get("lock_version") != 2
        or lock_document.get("acceptance_model_version") != "1.0.3"
        or lock_document.get("resource_controls")
        != {
            "wall_timeout_seconds": 15,
            "v8_old_space_mib": 512,
            "wasm_trap_handler_disabled": True,
        }
        or
        lock_document.get("node_version") != "22.14.0"
        or lock_document.get("package_version") != "2.6.2"
        or lock_document.get("render_options")
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
    return node, loader, wasm, runner


def _pixel_oracle(
    calls: list[tuple[str, tuple[int, int], str]],
):
    node, loader, wasm, runner = _verify_test_renderer_contract()

    def render(document, size: tuple[int, int], workspace: Path) -> PixelOracleResult:
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
                    str(node),
                    "--max-old-space-size=512",
                    "--disable-wasm-trap-handler",
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
                    "PATH": str(node.parent),
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
        return PixelOracleResult(
            authority="pixel_oracle",
            production_canonical_environment=False,
            png_bytes=payload,
            sha256=output_hash,
            size=size,
            attestation=dict(attestation),
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
        png_bytes: dict[str, bytes] = {}
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
                if (
                    result.authority != "pixel_oracle"
                    or result.production_canonical_environment is not False
                    or not result.png_bytes
                ):
                    raise AssertionError(f"{kind} test diagnostic render failed")
                destination[component_id] = _white_component_mask(result.png_bytes, size)
                hashes[f"{component_id}-{kind}"] = result.sha256
                png_bytes[f"{component_id}-{kind}"] = result.png_bytes
        capture.clear()
        capture.update(
            {
                "visible": {name: mask.copy() for name, mask in visible.items()},
                "isolated": {name: mask.copy() for name, mask in isolated.items()},
                "hashes": dict(hashes),
                "png_bytes": dict(png_bytes),
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
            "missing independently checked golden: tests/goldens/acceptance-model-1.0.3.json"
        )
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise AssertionError(f"duplicate JSON key in acceptance golden: {key}")
            value[key] = item
        return value

    return json.loads(GOLDEN.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)


def _pixel_oracle_iteration(
    case: Path,
    candidate_path: Path,
    iteration: int,
    run_dir: Path,
    calls: list[tuple[str, tuple[int, int], str]],
) -> tuple[dict[str, object], dict[str, object]]:
    """Render SVG pixels and record independent metrics without pipeline decisions."""
    run_dir.mkdir(parents=True, exist_ok=True)
    draft = _read_json(case / "draft.json")
    components = draft["components"]
    component_ids = [str(item["component_id"]) for item in components]
    size = (
        int(draft["canonical_canvas"]["raster_width"]),
        int(draft["canonical_canvas"]["raster_height"]),
    )
    document = pipeline_module._validate_svg_snapshot(candidate_path.read_bytes())
    oracle = _pixel_oracle(calls)
    rendered = oracle(document, size, run_dir)
    if (
        rendered.authority != "pixel_oracle"
        or rendered.production_canonical_environment is not False
    ):
        raise AssertionError("pixel oracle cannot carry production canonical authority")
    candidate = _alpha_mask(rendered.png_bytes, size)
    diagnostic_capture: dict[str, object] = {}
    _derived_diagnostics(oracle, diagnostic_capture)(document, components, size, run_dir)

    reference, uncertainty = _source_normalization(case / "source.png")
    reference_components = {
        component_id: _mask(case / "masks" / f"{component_id}.png")
        for component_id in component_ids
    }
    visible = diagnostic_capture["visible"]
    isolated = diagnostic_capture["isolated"]
    independent = _reference_metrics(
        reference,
        candidate,
        uncertainty,
        reference_components,
        visible,
        isolated,
        components,
        draft["topology_facts"],
    )
    metrics = {name: round(float(value), 6) for name, value in independent.items()}

    suffix = f"i{iteration:02d}"
    png_artifacts = {
        f"preview-{suffix}.png": rendered.png_bytes,
        **{
            f"diagnostic-{name}-{suffix}.png": payload
            for name, payload in diagnostic_capture["png_bytes"].items()
        },
    }
    for name, payload in png_artifacts.items():
        (run_dir / name).write_bytes(payload)
    record = {
        "record_kind": "pixel-oracle-conformance",
        "record_version": "1.0.0",
        "authority": "pixel_oracle",
        "acceptance_authority": False,
        "production_canonical_environment": False,
        "case": case.name,
        "iteration": iteration,
        "metrics": metrics,
        "renderer_contract": {
            "authority": "pixel_oracle",
            "runtime_version": "22.14.0",
            "renderer_package": "@resvg/resvg-wasm",
            "renderer_version": "2.6.2",
            "loader_sha256": PINNED_LOADER_SHA256,
            "wasm_sha256": PINNED_WASM_SHA256,
            "runner_sha256": PINNED_RUNNER_SHA256,
        },
        "candidate_sha256": hashlib.sha256(document.xml_bytes).hexdigest(),
        "artifacts": {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in sorted(png_artifacts.items())
        },
    }
    record_bytes = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    (run_dir / f"pixel-oracle-{suffix}.json").write_bytes(record_bytes)
    measurements = {
        "reference": reference,
        "candidate": candidate,
        "uncertainty": uncertainty,
        "reference_components": reference_components,
        "visible": visible,
        "isolated": isolated,
        "diagnostic_hashes": diagnostic_capture["hashes"],
    }
    return record, measurements


def _run_pixel_oracle_case(
    case: Path,
    candidate_path: Path,
    iterations: int,
    run_dir: Path,
    calls: list[tuple[str, tuple[int, int], str]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    records: list[dict[str, object]] = []
    measurements: list[dict[str, object]] = []
    for iteration in range(iterations):
        record, measured = _pixel_oracle_iteration(
            case, candidate_path, iteration, run_dir, calls
        )
        records.append(record)
        measurements.append(measured)
    return records, measurements


CANONICAL_CASE_NAMES = (
    "analytic-fill",
    "impossible-target",
    "missing-component",
    "noisy-antialias",
    "occlusion-overlap",
    "open-stroke",
    "organic-curve",
    "ring-hole",
    "widescreen-16x9",
)


def _canonical_platform_skip_reason(
    *, system: str | None = None, architecture: str | None = None
) -> str | None:
    selected_system = sys.platform if system is None else system
    selected_architecture = platform.machine().lower() if architecture is None else architecture.lower()
    if selected_system == "linux" and selected_architecture in {"x86_64", "amd64"}:
        return None
    if selected_system == "darwin" and selected_architecture in {"arm64", "aarch64"}:
        return None
    raise AssertionError(
        f"canonical-platform conformance has no supported contract for "
        f"{selected_system}-{selected_architecture}"
    )


def _run_canonical_pipeline_case(
    case: Path,
    iterations: int,
    root: Path,
) -> dict[str, object]:
    """Run the public production pipeline twice against one frozen reference map."""
    reference_output = root / "reference"
    with mock.patch.object(pipeline_module, "_utc_now", return_value=FIXED_TIME):
        prepare_summary = pipeline_module.prepare_reference(
            case / "source.png", case / "draft.json", reference_output
        )
        map_path = reference_output / "reconstruction-map-r01.json"
        runs: dict[str, dict[str, object]] = {}
        for run_name in ("run-a", "run-b"):
            run_dir = root / run_name
            summaries: list[dict[str, object]] = []
            evaluations: list[dict[str, object]] = []
            for iteration in range(iterations):
                summaries.append(
                    pipeline_module.evaluate_candidate(
                        map_path,
                        case / "candidate.svg",
                        iteration,
                        run_dir,
                    )
                )
                evaluations.append(_read_json(run_dir / f"evaluation-i{iteration:02d}.json"))
            runs[run_name] = {
                "directory": run_dir,
                "summaries": summaries,
                "evaluations": evaluations,
            }
    return {
        "prepare_summary": prepare_summary,
        "reference_directory": reference_output,
        "map_path": map_path,
        "runs": runs,
    }


def _canonical_report_artifact_paths(
    case: Path,
    reference_directory: Path,
    run_directory: Path,
    report: dict[str, object],
) -> dict[str, Path]:
    """Resolve every report artifact ID to the public pipeline file it binds."""
    iteration = int(report["iteration"])
    suffix = f"i{iteration:02d}"
    map_path = reference_directory / "reconstruction-map-r01.json"
    frozen_map = _read_json(map_path)
    revision = f"r{int(frozen_map['map_revision']):02d}"
    paths = {
        f"source-{revision}": case / "source.png",
        f"reconstruction-map-{revision}": map_path,
        f"map-snapshot-{suffix}": run_directory / f"map-snapshot-{suffix}.json",
        str(frozen_map["reference_mask"]["logical_id"]): (
            reference_directory / f"reference-{revision}" / "reference-mask.png"
        ),
        str(frozen_map["uncertainty_mask"]["logical_id"]): (
            reference_directory / f"reference-{revision}" / "uncertainty-mask.png"
        ),
        f"candidate-{suffix}": run_directory / f"candidate-{suffix}.svg",
        f"preview-{suffix}": run_directory / f"preview-{suffix}.png",
        f"overlay-{suffix}": run_directory / f"overlay-{suffix}.png",
        f"diff-{suffix}": run_directory / f"diff-{suffix}.png",
        f"diagnostics-{suffix}": run_directory / f"diagnostics-{suffix}.json",
    }
    for component in frozen_map["components"]:
        component_id = str(component["component_id"])
        logical_id = str(component["reference_mask"]["logical_id"])
        paths[logical_id] = (
            reference_directory / f"reference-{revision}" / f"component-{component_id}.png"
        )
    return paths


class FixtureGeneratorTests(unittest.TestCase):
    def test_cross_platform_png_contract_accepts_equivalent_reencoding(self) -> None:
        committed = CONFORMANCE / "metrics" / "organic-cleaned-candidate.png"
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / committed.name
            with Image.open(committed) as image:
                image.save(generated, format="PNG", optimize=False, compress_level=0)

            self.assertNotEqual(generated.read_bytes(), committed.read_bytes())
            _assert_generated_fixture_matches_committed(
                generated,
                committed,
                Path("conformance/metrics/organic-cleaned-candidate.png"),
            )

    def test_cross_platform_png_contract_rejects_pixel_mutation(self) -> None:
        committed = CONFORMANCE / "metrics" / "organic-cleaned-candidate.png"
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / committed.name
            with Image.open(committed) as image:
                mutated = image.copy()
                original = mutated.getpixel((0, 0))
                mutated.putpixel((0, 0), 0 if original != 0 else 255)
                mutated.save(generated, format="PNG", optimize=False, compress_level=0)
            with self.assertRaisesRegex(AssertionError, "decoded_rgba"):
                _assert_generated_fixture_matches_committed(
                    generated,
                    committed,
                    Path("conformance/metrics/organic-cleaned-candidate.png"),
                )

    def test_cross_platform_png_contract_rejects_added_text_metadata(self) -> None:
        committed = CONFORMANCE / "metrics" / "organic-cleaned-candidate.png"
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / committed.name
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text("Comment", "not canonical fixture metadata")
            with Image.open(committed) as image:
                image.save(
                    generated,
                    format="PNG",
                    optimize=False,
                    compress_level=0,
                    pnginfo=metadata,
                )
            with self.assertRaisesRegex(AssertionError, "ordered non-IDAT chunks differ"):
                _assert_generated_fixture_matches_committed(
                    generated,
                    committed,
                    Path("conformance/metrics/organic-cleaned-candidate.png"),
                )

    def test_cross_platform_png_contract_rejects_bad_crc_and_noncontiguous_idat(
        self,
    ) -> None:
        committed = CONFORMANCE / "metrics" / "organic-cleaned-candidate.png"
        payload = committed.read_bytes()
        idat_type_offset = payload.index(b"IDAT")
        chunk_offset = idat_type_offset - 4
        idat_length = struct.unpack(">I", payload[chunk_offset:idat_type_offset])[0]
        idat_data = payload[idat_type_offset + 4 : idat_type_offset + 4 + idat_length]
        crc_offset = idat_type_offset + 4 + idat_length
        chunk_end = crc_offset + 4

        def png_chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
            crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
            return (
                struct.pack(">I", len(chunk_data))
                + chunk_type
                + chunk_data
                + struct.pack(">I", crc)
            )

        with tempfile.TemporaryDirectory() as directory:
            bad_crc = bytearray(payload)
            bad_crc[crc_offset] ^= 0x01
            bad_crc_path = Path(directory) / "bad-crc.png"
            bad_crc_path.write_bytes(bad_crc)
            with self.assertRaisesRegex(AssertionError, "CRC mismatch"):
                _assert_generated_fixture_matches_committed(
                    bad_crc_path,
                    committed,
                    Path("conformance/metrics/organic-cleaned-candidate.png"),
                )

            split = len(idat_data) // 2
            separated_idat = (
                png_chunk(b"IDAT", idat_data[:split])
                + png_chunk(b"tEXt", b"gap\x00between-idat")
                + png_chunk(b"IDAT", idat_data[split:])
            )
            noncontiguous_path = Path(directory) / "noncontiguous-idat.png"
            noncontiguous_path.write_bytes(
                payload[:chunk_offset] + separated_idat + payload[chunk_end:]
            )
            with self.assertRaisesRegex(AssertionError, "IDAT chunks are not contiguous"):
                _assert_generated_fixture_matches_committed(
                    noncontiguous_path,
                    committed,
                    Path("conformance/metrics/organic-cleaned-candidate.png"),
                )

    def test_cross_platform_png_contract_rejects_trailing_idat_data(self) -> None:
        committed = CONFORMANCE / "metrics" / "organic-cleaned-candidate.png"
        payload = committed.read_bytes()
        idat_type_offset = payload.index(b"IDAT")
        idat_length = struct.unpack(">I", payload[idat_type_offset - 4:idat_type_offset])[0]
        idat_data = payload[idat_type_offset + 4 : idat_type_offset + 4 + idat_length]
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / committed.name
            generated.write_bytes(
                _replace_first_idat_payload(payload, idat_data + b"SECRET-IDAT-TRAILER")
            )
            with self.assertRaisesRegex(AssertionError, "trailing data"):
                _assert_generated_fixture_matches_committed(
                    generated,
                    committed,
                    Path("conformance/metrics/organic-cleaned-candidate.png"),
                )

    def test_cross_platform_png_contract_rejects_invalid_zlib_stream_shapes(self) -> None:
        committed = CONFORMANCE / "metrics" / "organic-cleaned-candidate.png"
        payload = committed.read_bytes()
        idat_type_offset = payload.index(b"IDAT")
        idat_length = struct.unpack(">I", payload[idat_type_offset - 4:idat_type_offset])[0]
        idat_data = payload[idat_type_offset + 4 : idat_type_offset + 4 + idat_length]
        variants = (
            ("corrupt", bytes([idat_data[0] ^ 0x01]) + idat_data[1:], "corrupt"),
            ("incomplete", idat_data[:-1], "incomplete"),
            ("multiple", idat_data + zlib.compress(b"second-stream"), "multiple streams"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for name, replacement, diagnostic in variants:
                with self.subTest(name=name):
                    generated = Path(directory) / f"{name}.png"
                    generated.write_bytes(_replace_first_idat_payload(payload, replacement))
                    with self.assertRaisesRegex(AssertionError, diagnostic):
                        _assert_generated_fixture_matches_committed(
                            generated,
                            committed,
                            Path("conformance/metrics/organic-cleaned-candidate.png"),
                        )

    def test_cross_platform_png_contract_rejects_extra_filtered_scanline_bytes(self) -> None:
        committed = CONFORMANCE / "metrics" / "organic-cleaned-candidate.png"
        payload = committed.read_bytes()
        idat_type_offset = payload.index(b"IDAT")
        idat_length = struct.unpack(">I", payload[idat_type_offset - 4:idat_type_offset])[0]
        idat_data = payload[idat_type_offset + 4 : idat_type_offset + 4 + idat_length]
        original_filtered = zlib.decompress(idat_data)
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / committed.name
            generated.write_bytes(
                _replace_first_idat_payload(
                    payload,
                    zlib.compress(original_filtered + b"EXTRA"),
                )
            )
            with self.assertRaisesRegex(AssertionError, "filtered scanline length"):
                _assert_generated_fixture_matches_committed(
                    generated,
                    committed,
                    Path("conformance/metrics/organic-cleaned-candidate.png"),
                )

    def test_png_filtered_length_validates_ihdr_and_adam7_geometry(self) -> None:
        def ihdr(
            width: int,
            height: int,
            bit_depth: int,
            color_type: int,
            compression: int = 0,
            filtering: int = 0,
            interlace: int = 0,
        ) -> bytes:
            return struct.pack(
                ">IIBBBBB",
                width,
                height,
                bit_depth,
                color_type,
                compression,
                filtering,
                interlace,
            )

        self.assertEqual(_expected_png_filtered_bytes(ihdr(8, 8, 1, 0)), 16)
        self.assertEqual(_expected_png_filtered_bytes(ihdr(8, 8, 1, 0, interlace=1)), 30)
        self.assertEqual(_expected_png_filtered_bytes(ihdr(8, 8, 8, 6, interlace=1)), 271)

        invalid_headers = (
            b"short",
            ihdr(0, 8, 8, 6),
            ihdr(8, 8, 16, 3),
            ihdr(8, 8, 8, 1),
            ihdr(8, 8, 8, 6, compression=1),
            ihdr(8, 8, 8, 6, filtering=1),
            ihdr(8, 8, 8, 6, interlace=2),
        )
        for invalid_header in invalid_headers:
            with self.subTest(ihdr=invalid_header):
                with self.assertRaisesRegex(AssertionError, "PNG IHDR"):
                    _expected_png_filtered_bytes(invalid_header)

    def test_cross_platform_png_contract_wraps_pillow_decode_errors(self) -> None:
        committed = CONFORMANCE / "metrics" / "organic-cleaned-candidate.png"
        payload = committed.read_bytes()
        idat_type_offset = payload.index(b"IDAT")
        idat_length = struct.unpack(">I", payload[idat_type_offset - 4:idat_type_offset])[0]
        idat_data = payload[idat_type_offset + 4 : idat_type_offset + 4 + idat_length]
        invalid_filtered = bytearray(zlib.decompress(idat_data))
        invalid_filtered[0] = 5
        with tempfile.TemporaryDirectory() as directory:
            generated = Path(directory) / committed.name
            generated.write_bytes(
                _replace_first_idat_payload(payload, zlib.compress(invalid_filtered))
            )
            with self.assertRaisesRegex(
                AssertionError,
                "conformance/metrics/organic-cleaned-candidate.png: PNG generated Pillow decode",
            ):
                _assert_generated_fixture_matches_committed(
                    generated,
                    committed,
                    Path("conformance/metrics/organic-cleaned-candidate.png"),
                )

    def test_cross_platform_draft_contract_normalizes_only_verified_source_hash(
        self,
    ) -> None:
        committed_case = CONFORMANCE / "analytic-fill"
        with tempfile.TemporaryDirectory() as directory:
            generated_case = Path(directory) / "analytic-fill"
            generated_case.mkdir()
            generated_source = generated_case / "source.png"
            with Image.open(committed_case / "source.png") as image:
                image.save(generated_source, format="PNG", optimize=False, compress_level=0)
            generated_draft = _read_json(committed_case / "draft.json")
            generated_draft["source_sha256"] = hashlib.sha256(
                generated_source.read_bytes()
            ).hexdigest()
            generated_draft_path = generated_case / "draft.json"
            generated_draft_path.write_text(
                json.dumps(generated_draft, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            self.assertNotEqual(
                generated_draft_path.read_bytes(),
                (committed_case / "draft.json").read_bytes(),
            )
            _assert_generated_fixture_matches_committed(
                generated_draft_path,
                committed_case / "draft.json",
                Path("conformance/analytic-fill/draft.json"),
            )

            generated_draft["accuracy_confirmed"] = 1
            generated_draft_path.write_text(json.dumps(generated_draft), encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "decoded draft JSON differs"):
                _assert_generated_fixture_matches_committed(
                    generated_draft_path,
                    committed_case / "draft.json",
                    Path("conformance/analytic-fill/draft.json"),
                )

            generated_draft["accuracy_confirmed"] = True
            generated_draft["source_sha256"] = "0" * 64
            generated_draft_path.write_text(json.dumps(generated_draft), encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "source_sha256"):
                _assert_generated_fixture_matches_committed(
                    generated_draft_path,
                    committed_case / "draft.json",
                    Path("conformance/analytic-fill/draft.json"),
                )

    def test_cross_platform_draft_contract_rejects_duplicate_json_keys(self) -> None:
        committed_case = CONFORMANCE / "analytic-fill"
        with tempfile.TemporaryDirectory() as directory:
            generated_case = Path(directory) / "analytic-fill"
            generated_case.mkdir()
            shutil.copyfile(committed_case / "source.png", generated_case / "source.png")
            generated_draft = (committed_case / "draft.json").read_text(encoding="utf-8")
            generated_draft = generated_draft.replace(
                '  "accuracy_confirmed": true,',
                '  "accuracy_confirmed": true,\n  "accuracy_confirmed": true,',
                1,
            )
            generated_draft_path = generated_case / "draft.json"
            generated_draft_path.write_text(generated_draft, encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "duplicate JSON key"):
                _assert_generated_fixture_matches_committed(
                    generated_draft_path,
                    committed_case / "draft.json",
                    Path("conformance/analytic-fill/draft.json"),
                )

    def test_generator_is_raw_byte_deterministic_on_same_host_for_full_corpus(
        self,
    ) -> None:
        script = FIXTURES / "build_fixtures.py"
        with (
            tempfile.TemporaryDirectory() as first_directory,
            tempfile.TemporaryDirectory() as second_directory,
        ):
            first = Path(first_directory)
            second = Path(second_directory)
            subprocess.run([sys.executable, str(script), "--root", str(first)], check=True)
            subprocess.run([sys.executable, str(script), "--root", str(second)], check=True)
            first_inventory = _assert_generated_inventory(first)
            second_inventory = _assert_generated_inventory(second)
            self.assertEqual(first_inventory, COMMITTED_GENERATED_FILES)
            self.assertEqual(second_inventory, COMMITTED_GENERATED_FILES)
            _assert_raw_fixture_trees_equal(first, second)

    def test_generated_corpus_matches_committed_cross_platform_contract(self) -> None:
        script = FIXTURES / "build_fixtures.py"
        with tempfile.TemporaryDirectory() as directory:
            generated_root = Path(directory)
            subprocess.run([sys.executable, str(script), "--root", str(generated_root)], check=True)
            generated_inventory = _assert_generated_inventory(generated_root)
            self.assertEqual(generated_inventory, COMMITTED_GENERATED_FILES)
            generated_files = set(generated_inventory)
            self.assertNotIn(Path("contracts/valid-draft.json"), generated_files)
            self.assertNotIn(Path("contracts/valid-map.json"), generated_files)
            self.assertNotIn(Path("security/doctype.svg"), generated_files)
            self.assertNotIn(Path("security/external-image.svg"), generated_files)
            for relative in sorted(generated_files):
                committed = FIXTURES / relative
                if not committed.is_file():
                    self.fail(f"fixture mismatch at {relative}: committed file is missing")
                _assert_generated_fixture_matches_committed(
                    generated_root / relative,
                    committed,
                    relative,
                )

    def test_generated_inventory_rejects_omitted_declared_file(self) -> None:
        script = FIXTURES / "build_fixtures.py"
        with tempfile.TemporaryDirectory() as directory:
            generated_root = Path(directory)
            subprocess.run([sys.executable, str(script), "--root", str(generated_root)], check=True)
            omitted = Path("conformance/analytic-fill/candidate.svg")
            (generated_root / omitted).unlink()
            with self.assertRaisesRegex(
                AssertionError,
                rf"generated inventory mismatch at {omitted}: declared file is missing",
            ):
                _assert_generated_inventory(generated_root)

    def test_manifest_declares_every_required_synthetic_fixture_class(self) -> None:
        manifest = _read_json(CONFORMANCE / "manifest.json")
        self.assertEqual(manifest["corpus_version"], "1.0.1")
        self.assertEqual(manifest["generated_files"]["version"], "1.0.0")
        self.assertIn("conformance/manifest.json", manifest["generated_files"]["paths"])
        self.assertEqual(
            manifest["png_fixture_contract"],
            {
                "version": "1.0.0",
                "idat_encoding": "non-authoritative",
                "non_idat_chunks": "ordered-exact",
                "decoded_rgba": "byte-exact",
                "pillow_properties": [
                    "format", "mode", "bands", "size", "n_frames", "info",
                ],
            },
        )
        self.assertEqual(manifest["provenance"], "synthetic-original")
        self.assertEqual(
            manifest["renderer_mode"],
            "pinned-node22-resvg-wasm-2.6.2-pixel-oracle",
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
    def test_golden_revision_matches_acceptance_model(self) -> None:
        self.assertEqual(_golden()["model_version"], ACCEPTANCE_MODEL_VERSION)

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
    def test_pixel_oracle_record_excludes_normative_pipeline_decisions(self) -> None:
        case = CONFORMANCE / "analytic-fill"
        calls: list[tuple[str, tuple[int, int], str]] = []
        with tempfile.TemporaryDirectory() as directory:
            records, _ = _run_pixel_oracle_case(
                case,
                case / "candidate.svg",
                1,
                Path(directory),
                calls,
            )

        forbidden = {
            "accuracy_target",
            "automatic_gates",
            "limit_state",
            "oracle_state",
            "semantic_gates",
            "status",
            "stop_reason",
            "target_met",
        }
        self.assertTrue(
            forbidden.isdisjoint(records[0]),
            f"pixel oracle reconstructed normative pipeline fields: {sorted(forbidden & records[0].keys())}",
        )

    def test_canonical_suite_dispatches_to_the_public_pipeline_and_propagates_mutation(self) -> None:
        dispatch = globals().get("_run_canonical_pipeline_case")
        self.assertIsNotNone(dispatch, "canonical-platform pipeline dispatch is missing")
        case = CONFORMANCE / "analytic-fill"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(
                pipeline_module,
                "evaluate_candidate",
                side_effect=RuntimeError("mutated evaluate_candidate"),
            ):
                with self.assertRaisesRegex(RuntimeError, "mutated evaluate_candidate"):
                    dispatch(case, 1, root)

    def test_repository_declares_and_selects_the_platform_pinned_node(self) -> None:
        package = _read_json(REPOSITORY / "package.json")
        package_lock = _read_json(REPOSITORY / "package-lock.json")
        repository_node = REPOSITORY / "node_modules" / "node" / "bin" / "node"

        self.assertEqual(package["dependencies"].get("node"), "22.14.0")
        self.assertEqual(
            package_lock["packages"][""]["dependencies"],
            {"@resvg/resvg-wasm": "2.6.2", "node": "22.14.0"},
        )
        expected_platform_packages = {
            "node-bin-darwin-arm64": "22.14.0",
            "node-linux-x64": "22.14.0",
        }
        self.assertEqual(package.get("optionalDependencies"), expected_platform_packages)
        self.assertEqual(
            package_lock["packages"][""].get("optionalDependencies"),
            expected_platform_packages,
        )
        self.assertEqual(
            package_lock["packages"]["node_modules/node-bin-darwin-arm64"]["integrity"],
            "sha512-vXh85M8hpgFnaX/q8fBhsH+oNH5FtN6sEczeR0vDel87NDHjF3mF+9Ffx60SAQnI9Akq93WFkmEp8FQR8YbHQQ==",
        )
        self.assertEqual(
            package_lock["packages"]["node_modules/node-linux-x64"]["integrity"],
            "sha512-R9k0h0zCZkX4/rlJbwS2c/CaOlmbAz3FkcQnQTJneQgJFaMntb8GVT64oArZEvrnzSyck8tGpcss6u3nT7hqxg==",
        )
        self.assertTrue(repository_node.is_file())
        lock = load_renderer_lock(REPOSITORY / "canonical-renderer.lock")
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                renderer_module.shutil,
                "which",
                side_effect=AssertionError("system Node fallback must not be consulted"),
            ),
        ):
            selected = resolve_canonical_node(lock, renderer_module._platform_key())
        self.assertEqual(selected.source, repository_node.resolve())

        renderer_lock = _read_json(REPOSITORY / "canonical-renderer.lock")
        self.assertEqual(
            renderer_lock["node_binaries"]["linux-x64"],
            {
                "package": "node-linux-x64",
                "package_version": "22.14.0",
                "package_integrity": "sha512-R9k0h0zCZkX4/rlJbwS2c/CaOlmbAz3FkcQnQTJneQgJFaMntb8GVT64oArZEvrnzSyck8tGpcss6u3nT7hqxg==",
                "executable_sha256": "1abce2374a485bddae3c27b17a3e3143e2780232026e627c4fe74ddde3f380a1",
            },
        )
        self.assertEqual(
            renderer_lock["node_binaries"]["darwin-arm64"]["executable_sha256"],
            "e2d4915d03eda6a2f00a09920e7eeb7a04ad123f9aaad61b1481179fe1bf50e0",
        )
        self.assertEqual(renderer_lock["runner_sha256"], PINNED_RUNNER_SHA256)
        self.assertEqual(
            renderer_lock["resource_controls"],
            {
                "wall_timeout_seconds": 15,
                "v8_old_space_mib": 512,
                "wasm_trap_handler_disabled": True,
            },
        )
        self.assertNotIn("disable_wasm_trap_handler", renderer_lock["render_options"])

    def test_pixel_oracle_never_publishes_canonical_acceptance_evidence(self) -> None:
        case = CONFORMANCE / "analytic-fill"
        calls: list[tuple[str, tuple[int, int], str]] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records, _ = _run_pixel_oracle_case(
                case,
                case / "candidate.svg",
                1,
                root / "oracle",
                calls,
            )
            record_bytes = (root / "oracle" / "pixel-oracle-i00.json").read_bytes()
            acceptance_evaluation_exists = (root / "oracle" / "evaluation-i00.json").exists()

        self.assertEqual(records[0]["authority"], "pixel_oracle")
        self.assertFalse(records[0]["acceptance_authority"])
        self.assertFalse(records[0]["production_canonical_environment"])
        self.assertNotIn(b'"canonical_environment":true', record_bytes)
        self.assertFalse(acceptance_evaluation_exists)

    def test_positive_renderer_contract_changes_when_candidate_geometry_changes(self) -> None:
        case = CONFORMANCE / "analytic-fill"
        original = pipeline_module._validate_svg_snapshot((case / "candidate.svg").read_bytes())
        mutated = pipeline_module._validate_svg_snapshot(
            (case / "candidate.svg").read_bytes().replace(b'width="32"', b'width="24"')
        )
        calls: list[tuple[str, tuple[int, int], str]] = []
        renderer = _pixel_oracle(calls)
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            first = renderer(original, (1024, 1024), workspace)
            second = renderer(mutated, (1024, 1024), workspace)
            mutated_path = workspace / "mutated.svg"
            mutated_path.write_bytes(mutated.xml_bytes)
            original_records, original_measurements = _run_pixel_oracle_case(
                case, case / "candidate.svg", 1, workspace / "original-run", calls
            )
            mutated_records, mutated_measurements = _run_pixel_oracle_case(
                case, mutated_path, 1, workspace / "mutated-run", calls
            )

        self.assertNotEqual(first.sha256, second.sha256)
        self.assertNotEqual(first.png_bytes, second.png_bytes)
        self.assertNotEqual(
            original_measurements[0]["diagnostic_hashes"],
            mutated_measurements[0]["diagnostic_hashes"],
        )
        original_metrics = original_records[0]["metrics"]
        mutated_metrics = mutated_records[0]["metrics"]
        self.assertNotEqual(original_metrics["silhouette"], mutated_metrics["silhouette"])
        self.assertNotEqual(original_metrics["contour"], mutated_metrics["contour"])
        expected = _golden()["pipeline_cases"]["analytic-fill"]["metrics"]
        tolerance = float(_golden()["tolerance"])
        self.assertTrue(
            any(
                abs(float(mutated_metrics[name]) - float(value)) > tolerance
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
                self.assertIn(expected["status"], {status.value for status in Status})
                self.assertIn("stop_reason", expected)

    def test_canonical_suite_lists_all_cases_and_is_mandatory_on_supported_platforms(self) -> None:
        manifest = _read_json(CONFORMANCE / "manifest.json")
        manifest_names = tuple(sorted(str(item["name"]) for item in manifest["pipeline_cases"]))
        self.assertEqual(CANONICAL_CASE_NAMES, manifest_names)
        self.assertIsNone(
            _canonical_platform_skip_reason(system="linux", architecture="x86_64")
        )
        self.assertIsNone(
            _canonical_platform_skip_reason(system="darwin", architecture="arm64")
        )
        self.assertTrue(
            issubclass(CanonicalPlatformConformanceTests, unittest.TestCase)
        )

    def test_meaningful_multicolor_source_stops_before_freeze(self) -> None:
        case = CONFORMANCE / "multicolor-rejection"
        with Image.open(case / "source.png") as source:
            colors = {tuple(color) for count, color in source.convert("RGB").getcolors(2_000_000) or [] if count}
        self.assertIn((0, 0, 0), colors)
        self.assertIn((255, 0, 0), colors)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(InvalidInputError, "merge-to-monochrome"):
                prepare_reference(case / "source.png", case / "draft.json", Path(directory) / "reference")

    def test_every_pixel_oracle_record_is_repeatable_and_matches_independent_goldens(self) -> None:
        manifest = _read_json(CONFORMANCE / "manifest.json")
        golden = _golden()
        tolerance = float(golden["tolerance"])
        self.assertEqual(golden["authority"], "pixel_oracle")
        self.assertFalse(golden["production_canonical_environment"])
        for case_record in manifest["pipeline_cases"]:
            name = str(case_record["name"])
            case = FIXTURES / str(case_record["path"])
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                calls: list[tuple[str, tuple[int, int], str]] = []
                draft = _read_json(case / "draft.json")
                components = draft["components"]
                component_ids = [str(item["component_id"]) for item in components]
                records: dict[str, list[dict[str, object]]] = {}
                measurements: dict[str, list[dict[str, object]]] = {}
                for run_name in ("run-a", "run-b"):
                    records[run_name], measurements[run_name] = _run_pixel_oracle_case(
                        case,
                        case / "candidate.svg",
                        int(case_record["iterations"]),
                        root / run_name,
                        calls,
                    )

                self.assertEqual(
                    len(calls),
                    2 * int(case_record["iterations"]) * (1 + 2 * len(component_ids)),
                )
                for iteration in range(int(case_record["iterations"])):
                    first = records["run-a"][iteration]
                    second = records["run-b"][iteration]
                    self.assertEqual(_normalized_bytes(first), _normalized_bytes(second))
                    self.assertEqual(first["authority"], "pixel_oracle")
                    self.assertFalse(first["acceptance_authority"])
                    self.assertFalse(first["production_canonical_environment"])
                    self.assertNotIn(
                        b'"canonical_environment":true', _normalized_bytes(first)
                    )
                first_pngs = {
                    path.relative_to(root / "run-a"): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (root / "run-a").glob("*.png")
                }
                second_pngs = {
                    path.relative_to(root / "run-b"): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (root / "run-b").glob("*.png")
                }
                self.assertEqual(first_pngs, second_pngs)

                expected = golden["pipeline_cases"][name]
                for metric_name, expected_value in expected["metrics"].items():
                    self.assertEqual(records["run-a"][-1]["metrics"][metric_name], expected_value)
                final_record = records["run-a"][-1]
                independent_metrics = final_record["metrics"]
                for metric_name, expected_value in expected["metrics"].items():
                    if expected.get("identity"):
                        self.assertEqual(independent_metrics[metric_name], 100.0)
                    self.assertLessEqual(
                        abs(float(independent_metrics[metric_name]) - expected_value), tolerance
                    )

                if name == "missing-component":
                    self.assertGreater(float(independent_metrics["silhouette"]), 99.9)
                elif name == "widescreen-16x9":
                    with Image.open(root / "run-a" / "preview-i00.png") as preview:
                        self.assertEqual(preview.size, (1024, 576))
                elif name == "noisy-antialias":
                    uncertainty = measurements["run-a"][-1]["uncertainty"]
                    self.assertGreater(int(np.count_nonzero(uncertainty)), 0)
                    self.assertEqual(independent_metrics["silhouette"], 100.0)
                    self.assertEqual(independent_metrics["contour"], 100.0)


class CanonicalPlatformConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reason = _canonical_platform_skip_reason()
        if reason is not None:
            raise unittest.SkipTest(reason)

    def test_public_pipeline_matches_all_normative_goldens_twice(self) -> None:
        manifest = _read_json(CONFORMANCE / "manifest.json")
        cases = {str(item["name"]): item for item in manifest["pipeline_cases"]}
        self.assertEqual(tuple(sorted(cases)), CANONICAL_CASE_NAMES)
        golden = _golden()
        tolerance = float(golden["tolerance"])

        for name in CANONICAL_CASE_NAMES:
            case_record = cases[name]
            case = FIXTURES / str(case_record["path"])
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                result = _run_canonical_pipeline_case(
                    case,
                    int(case_record["iterations"]),
                    Path(directory),
                )
                runs = result["runs"]
                for iteration in range(int(case_record["iterations"])):
                    first_evaluation = runs["run-a"]["evaluations"][iteration]
                    second_evaluation = runs["run-b"]["evaluations"][iteration]
                    first_report = first_evaluation["report"]
                    second_report = second_evaluation["report"]
                    self.assertTrue(
                        first_report["canonical_environment"],
                        first_report.get("warnings"),
                    )
                    self.assertTrue(
                        second_report["canonical_environment"],
                        second_report.get("warnings"),
                    )
                    self.assertEqual(
                        _normalized_bytes(first_report),
                        _normalized_bytes(second_report),
                    )
                    for image_name in ("preview", "overlay", "diff"):
                        filename = f"{image_name}-i{iteration:02d}.png"
                        self.assertEqual(
                            _sha256(runs["run-a"]["directory"] / filename),
                            _sha256(runs["run-b"]["directory"] / filename),
                        )
                    for run_name in ("run-a", "run-b"):
                        report = runs[run_name]["evaluations"][iteration]["report"]
                        run_directory = runs[run_name]["directory"]
                        declared_artifacts = report["artifacts"]
                        declared_ids = [str(item["logical_id"]) for item in declared_artifacts]
                        self.assertEqual(len(declared_ids), len(set(declared_ids)))
                        artifact_paths = _canonical_report_artifact_paths(
                            case,
                            result["reference_directory"],
                            run_directory,
                            report,
                        )
                        self.assertEqual(set(declared_ids), set(artifact_paths))
                        declared_hashes = {
                            str(item["logical_id"]): str(item["sha256"])
                            for item in declared_artifacts
                        }
                        for logical_id, artifact_path in artifact_paths.items():
                            self.assertTrue(artifact_path.is_file(), logical_id)
                            self.assertEqual(_sha256(artifact_path), declared_hashes[logical_id])

                expected = golden["pipeline_cases"][name]
                final_iteration = int(case_record["iterations"]) - 1
                final_report = runs["run-a"]["evaluations"][final_iteration]["report"]
                final_summary = runs["run-a"]["summaries"][final_iteration]
                self.assertTrue(final_report["canonical_environment"])
                self.assertEqual(final_report["status"], expected["status"])
                self.assertEqual(final_summary["status"], expected["status"])
                self.assertEqual(final_summary["exit_code"], expected["exit_code"])
                self.assertEqual(final_report["limit_state"], expected["limit_state"])
                self.assertEqual(final_report["stop_reason"], expected["stop_reason"])
                self.assertEqual(
                    final_report["viewport"]["aspect_ratio"], expected["aspect_ratio"]
                )
                for metric_name, expected_value in expected["metrics"].items():
                    actual = float(final_report["metrics"][f"{metric_name}_raw"])
                    if expected.get("identity"):
                        self.assertEqual(actual, 100.0)
                    self.assertLessEqual(abs(actual - float(expected_value)), tolerance)
                automatic_gates = [
                    item for item in final_report["gates"] if item["kind"] == "automatic"
                ]
                self.assertEqual(
                    [item["gate_id"] for item in automatic_gates],
                    list(AUTOMATIC_GATE_IDS),
                )
                self.assertEqual(
                    {item["gate_id"]: item["state"] for item in automatic_gates},
                    expected["gates"],
                )
                semantic_gates = [
                    item for item in final_report["gates"] if item["kind"] == "semantic"
                ]
                self.assertEqual(
                    [item["gate_id"] for item in semantic_gates],
                    list(SEMANTIC_GATE_IDS),
                )
                self.assertEqual(
                    {item["gate_id"]: item["state"] for item in semantic_gates},
                    golden["semantic_gate_defaults"],
                )


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
