"""Bounded, fail-closed validation for untrusted canonical SVG candidates."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree as DefusedElementTree
from defusedxml.common import DefusedXmlException
from svgpathtools import parse_path

from .errors import InvalidInputError


MAX_SVG_BYTES = 5 * 1024 * 1024
MAX_ELEMENTS = 10_000
MAX_PATH_DATA_CHARACTERS = 2_000_000
MAX_NESTING = 64
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"

_ALLOWED_ELEMENTS = frozenset(
    {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "title", "desc"}
)
_CONTAINER_ELEMENTS = frozenset({"svg", "g"})
_TEXT_ELEMENTS = frozenset({"title", "desc"})
_PRESENTATION_ATTRIBUTES = frozenset(
    {
        "fill",
        "fill-opacity",
        "fill-rule",
        "opacity",
        "paint-order",
        "shape-rendering",
        "stroke",
        "stroke-dasharray",
        "stroke-dashoffset",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-miterlimit",
        "stroke-opacity",
        "stroke-width",
        "vector-effect",
    }
)
_ACCESSIBILITY_ATTRIBUTES = frozenset(
    {"aria-describedby", "aria-hidden", "aria-label", "aria-labelledby", "role", f"{{{XML_NAMESPACE}}}lang"}
)
_GLOBAL_ATTRIBUTES = frozenset({"id"}) | _PRESENTATION_ATTRIBUTES | _ACCESSIBILITY_ATTRIBUTES
_ELEMENT_ATTRIBUTES = {
    "svg": frozenset({"viewBox", "width", "height", "preserveAspectRatio"}) | _GLOBAL_ATTRIBUTES,
    "g": _GLOBAL_ATTRIBUTES,
    "path": frozenset({"d", "pathLength"}) | _GLOBAL_ATTRIBUTES,
    "rect": frozenset({"x", "y", "width", "height", "rx", "ry", "pathLength"}) | _GLOBAL_ATTRIBUTES,
    "circle": frozenset({"cx", "cy", "r", "pathLength"}) | _GLOBAL_ATTRIBUTES,
    "ellipse": frozenset({"cx", "cy", "rx", "ry", "pathLength"}) | _GLOBAL_ATTRIBUTES,
    "line": frozenset({"x1", "y1", "x2", "y2", "pathLength"}) | _GLOBAL_ATTRIBUTES,
    "polyline": frozenset({"points", "pathLength"}) | _GLOBAL_ATTRIBUTES,
    "polygon": frozenset({"points", "pathLength"}) | _GLOBAL_ATTRIBUTES,
    "title": frozenset({"id", f"{{{XML_NAMESPACE}}}lang"}),
    "desc": frozenset({"id", f"{{{XML_NAMESPACE}}}lang"}),
}

_XML_DECLARATION = re.compile(
    r'\A<\?xml\s+version\s*=\s*(["\'])1\.0\1'
    r'(?:\s+encoding\s*=\s*(["\'])[Uu][Tt][Ff]-8\2)?'
    r'(?:\s+standalone\s*=\s*(["\'])(?:yes|no)\3)?\s*\?>',
    re.ASCII,
)
_NUMBER_TEXT = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_NUMBER = re.compile(rf"\A{_NUMBER_TEXT}\Z", re.ASCII)
_NUMBER_LIST = re.compile(rf"\A\s*{_NUMBER_TEXT}(?:[\s,]+{_NUMBER_TEXT})*\s*\Z", re.ASCII)
_PATH_CHARACTERS = re.compile(r"\A[MmZzLlHhVvCcSsQqTtAaEe0-9.,+\-\s]*\Z", re.ASCII)
_ID = re.compile(r"\A[A-Za-z_][A-Za-z0-9_.:-]*\Z", re.ASCII)
_ID_REFERENCES = re.compile(r"\A[A-Za-z_][A-Za-z0-9_.:-]*(?:\s+[A-Za-z_][A-Za-z0-9_.:-]*)*\Z", re.ASCII)
_LANGUAGE = re.compile(r"\A[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*\Z", re.ASCII)
_COLOR = re.compile(r"\A(?:none|currentColor|#[0-9A-Fa-f]{3}|#[0-9A-Fa-f]{6})\Z", re.ASCII)
_FORBIDDEN_RAW_MARKUP = (re.compile(br"<!DOCTYPE", re.IGNORECASE), re.compile(br"<!ENTITY", re.IGNORECASE))
_FORBIDDEN_SCHEME = re.compile(br"(?:data|https?|file|ftp|javascript|vbscript|blob):", re.IGNORECASE)
_SVG_NAMESPACE_BYTES = SVG_NAMESPACE.encode("ascii")


class SecurityViolation(InvalidInputError):
    """Raised when an SVG cannot be proven to belong to the safe subset."""


@dataclass(frozen=True)
class SafeSvgDocument:
    """A byte-exact SVG candidate that has passed every safe-subset gate."""

    source: Path
    xml_bytes: bytes
    root: Element
    element_count: int
    path_data_characters: int


def _read_bounded(path: Path) -> bytes:
    source = Path(path)
    try:
        if not source.is_file() or source.is_symlink():
            raise SecurityViolation("SVG input must be a regular, non-symlink file")
        if source.stat().st_size > MAX_SVG_BYTES:
            raise SecurityViolation("SVG input exceeds 5 MiB")
        with source.open("rb") as stream:
            data = stream.read(MAX_SVG_BYTES)
            if stream.read(1):
                raise SecurityViolation("SVG input exceeds 5 MiB")
    except SecurityViolation:
        raise
    except OSError as error:
        raise SecurityViolation("SVG input could not be read safely") from error
    if not data:
        raise SecurityViolation("SVG input is empty")
    return data


def _is_namespace_scheme(data: bytes, start: int) -> bool:
    if data[start : start + len(_SVG_NAMESPACE_BYTES)].lower() != _SVG_NAMESPACE_BYTES:
        return False
    prefix = data[max(0, start - 24) : start]
    return re.search(br"xmlns\s*=\s*[\"']\Z", prefix, re.IGNORECASE) is not None


def _raw_prescan(data: bytes) -> str:
    if b"\x00" in data:
        raise SecurityViolation("NUL is forbidden in SVG input")
    for token in _FORBIDDEN_RAW_MARKUP:
        if token.search(data):
            raise SecurityViolation("DTD and entity declarations are forbidden")
    for match in _FORBIDDEN_SCHEME.finditer(data):
        if not _is_namespace_scheme(data, match.start()):
            raise SecurityViolation("URI schemes are forbidden in SVG input")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SecurityViolation("SVG input must be well-formed UTF-8") from error
    declaration = _XML_DECLARATION.match(text)
    remainder = text[declaration.end() :] if declaration else text
    if "<?" in remainder:
        raise SecurityViolation("processing instructions are forbidden")
    if not declaration and "<?" in text:
        raise SecurityViolation("the XML declaration must be the first construct")
    return text


def _local_name(name: str) -> str:
    if name.startswith("{"):
        namespace, separator, local = name[1:].partition("}")
        if not separator or namespace != SVG_NAMESPACE:
            raise SecurityViolation("foreign XML namespaces are forbidden")
        return local
    if ":" in name:
        raise SecurityViolation("prefixed element names are forbidden")
    return name


def _number(value: str, *, nonnegative: bool = False, positive: bool = False) -> float:
    if not _NUMBER.fullmatch(value):
        raise SecurityViolation("geometry values must be finite unitless numbers")
    parsed = float(value)
    if not math.isfinite(parsed) or (nonnegative and parsed < 0) or (positive and parsed <= 0):
        raise SecurityViolation("geometry value is outside the safe range")
    return parsed


def _number_sequence(value: str) -> list[float]:
    if not _NUMBER_LIST.fullmatch(value):
        raise SecurityViolation("geometry lists may contain only finite unitless numbers")
    values = [float(item) for item in re.findall(_NUMBER_TEXT, value, re.ASCII)]
    if not all(math.isfinite(item) for item in values):
        raise SecurityViolation("geometry list contains a non-finite number")
    return values


def _validate_presentation(name: str, value: str) -> None:
    if name in {"fill", "stroke"}:
        if not _COLOR.fullmatch(value):
            raise SecurityViolation("paint must be none, currentColor, or a hex color")
    elif name in {"fill-opacity", "opacity", "stroke-opacity"}:
        parsed = _number(value, nonnegative=True)
        if parsed > 1:
            raise SecurityViolation("opacity must be between zero and one")
    elif name in {"stroke-width", "stroke-dashoffset"}:
        _number(value, nonnegative=name == "stroke-width")
    elif name == "stroke-miterlimit":
        if _number(value, nonnegative=True) < 1:
            raise SecurityViolation("stroke miter limit must be at least one")
    elif name == "stroke-dasharray":
        if value != "none" and any(item < 0 for item in _number_sequence(value)):
            raise SecurityViolation("stroke dash values must be non-negative")
    elif name == "fill-rule" and value not in {"nonzero", "evenodd"}:
        raise SecurityViolation("fill rule is not allowed")
    elif name == "stroke-linecap" and value not in {"butt", "round", "square"}:
        raise SecurityViolation("stroke line cap is not allowed")
    elif name == "stroke-linejoin" and value not in {"miter", "round", "bevel"}:
        raise SecurityViolation("stroke line join is not allowed")
    elif name == "vector-effect" and value not in {"none", "non-scaling-stroke"}:
        raise SecurityViolation("vector effect is not allowed")
    elif name == "shape-rendering" and value not in {
        "auto",
        "optimizeSpeed",
        "crispEdges",
        "geometricPrecision",
    }:
        raise SecurityViolation("shape rendering value is not allowed")
    elif name == "paint-order":
        tokens = value.split()
        if value != "normal" and (not tokens or len(tokens) != len(set(tokens)) or not set(tokens) <= {"fill", "stroke"}):
            raise SecurityViolation("paint order is not allowed")


def _validate_attribute(element: str, name: str, value: str) -> None:
    if name.lower().startswith("on") or name in {"style", "transform", "href", "xlink:href"}:
        raise SecurityViolation("active, linked, styled, and transformed content is forbidden")
    if name not in _ELEMENT_ATTRIBUTES[element]:
        raise SecurityViolation(f"attribute {name!r} is not allowed on {element}")
    if any(character == "\x00" or ord(character) < 0x20 and character not in "\t\n\r" for character in value):
        raise SecurityViolation("control characters are forbidden in attribute values")
    lowered = value.lower()
    if "url(" in lowered or _FORBIDDEN_SCHEME.search(value.encode("utf-8")):
        raise SecurityViolation("linked attribute values are forbidden")
    if name == "id":
        if not _ID.fullmatch(value):
            raise SecurityViolation("id is not a safe XML identifier")
    elif name in _PRESENTATION_ATTRIBUTES:
        _validate_presentation(name, value)
    elif name in {"aria-labelledby", "aria-describedby"}:
        if not _ID_REFERENCES.fullmatch(value):
            raise SecurityViolation("ARIA references must be local IDs")
    elif name == "aria-hidden":
        if value not in {"true", "false"}:
            raise SecurityViolation("aria-hidden must be true or false")
    elif name == "aria-label":
        if not value.strip():
            raise SecurityViolation("aria-label cannot be empty")
    elif name == "role":
        if value not in {"img", "presentation", "none", "group", "graphics-symbol"}:
            raise SecurityViolation("role is not allowed")
    elif name == f"{{{XML_NAMESPACE}}}lang":
        if not _LANGUAGE.fullmatch(value):
            raise SecurityViolation("xml:lang is malformed")
    elif name == "viewBox":
        values = _number_sequence(value)
        if len(values) != 4 or values[2] <= 0 or values[3] <= 0:
            raise SecurityViolation("viewBox must contain four numbers and positive dimensions")
    elif name in {"width", "height", "r", "rx", "ry", "pathLength"}:
        _number(value, positive=name in {"width", "height", "r", "pathLength"}, nonnegative=True)
    elif name in {"x", "y", "cx", "cy", "x1", "y1", "x2", "y2"}:
        _number(value)
    elif name == "preserveAspectRatio":
        if not re.fullmatch(r"(?:none|x(?:Min|Mid|Max)Y(?:Min|Mid|Max)(?:\s+(?:meet|slice))?)", value):
            raise SecurityViolation("preserveAspectRatio is not allowed")
    elif name == "d":
        if not value.strip() or not _PATH_CHARACTERS.fullmatch(value):
            raise SecurityViolation("path data contains forbidden characters")
        try:
            parse_path(value)
        except (ValueError, TypeError, IndexError) as error:
            raise SecurityViolation("path data is malformed") from error
    elif name == "points":
        values = _number_sequence(value)
        minimum = 6 if element == "polygon" else 4
        if len(values) < minimum or len(values) % 2:
            raise SecurityViolation("points must contain complete coordinate pairs")


def _validate_tree(root: Element) -> tuple[int, int]:
    if _local_name(root.tag) != "svg":
        raise SecurityViolation("the root element must be svg")
    element_count = 0
    path_characters = 0
    stack: list[tuple[Element, int]] = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_NESTING:
            raise SecurityViolation("SVG nesting exceeds 64 elements")
        element_count += 1
        if element_count > MAX_ELEMENTS:
            raise SecurityViolation("SVG contains more than 10000 elements")
        element = _local_name(node.tag)
        if element not in _ALLOWED_ELEMENTS:
            raise SecurityViolation(f"element {element!r} is forbidden")
        children = list(node)
        if children and element not in _CONTAINER_ELEMENTS:
            raise SecurityViolation(f"element {element!r} cannot contain elements")
        if element not in _TEXT_ELEMENTS and node.text and node.text.strip():
            raise SecurityViolation("visible text is forbidden outside title and desc")
        if node.tail and node.tail.strip():
            raise SecurityViolation("visible tail text is forbidden")
        for attribute, value in node.attrib.items():
            _validate_attribute(element, attribute, value)
            if attribute in {"d", "points"}:
                path_characters += len(value)
                if path_characters > MAX_PATH_DATA_CHARACTERS:
                    raise SecurityViolation("path and points data exceed 2000000 characters")
        stack.extend((child, depth + 1) for child in reversed(children))
    return element_count, path_characters


def validate_svg(path: Path) -> SafeSvgDocument:
    """Validate an untrusted SVG with a raw pre-scan before defused XML parsing."""
    source = Path(path)
    data = _read_bounded(source)
    _raw_prescan(data)
    try:
        root = DefusedElementTree.fromstring(data)
    except (ParseError, DefusedXmlException, ValueError) as error:
        raise SecurityViolation("SVG XML is malformed or unsafe") from error
    element_count, path_characters = _validate_tree(root)
    return SafeSvgDocument(
        source=source,
        xml_bytes=data,
        root=root,
        element_count=element_count,
        path_data_characters=path_characters,
    )
