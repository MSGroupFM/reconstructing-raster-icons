"""Versioned constants shared by reconstruction contract consumers."""

from __future__ import annotations

from enum import Enum, IntEnum


SCHEMA_VERSION = "1.0.0"
ACCEPTANCE_MODEL_VERSION = "1.0.2"
SCHEMA_KINDS = frozenset(
    {
        "reconstruction-map-draft",
        "reconstruction-map",
        "semantic-review",
        "acceptance-report",
    }
)


class Status(str, Enum):
    """Final status values, ordered by the acceptance-model precedence."""

    INVALID_INPUT = "invalid_input"
    RUNTIME_ERROR = "runtime_error"
    NON_CANONICAL = "non_canonical"
    NOT_ACCEPTED = "not_accepted"
    INCOMPLETE = "incomplete"
    ACCEPTED = "accepted"


class ExitCode(IntEnum):
    ACCEPTED = 0
    INVALID_INPUT = 2
    SCORE_BELOW_TARGET = 3
    GATE_FAILED = 4
    INCOMPLETE_REVIEW = 5
    NON_CANONICAL = 6
    RUNTIME_ERROR = 7


AUTOMATIC_GATE_IDS = (
    "auto.svg.safe_subset",
    "auto.svg.render",
    "auto.integrity.hashes",
    "auto.components.present",
    "auto.topology.facts",
    "auto.viewport.geometry",
    "auto.primitives.constraints",
    "auto.paths.integrity",
    "auto.style.monochrome",
)
SEMANTIC_GATE_IDS = (
    "semantic.components.complete",
    "semantic.connectivity",
    "semantic.editability",
    "semantic.visual_meaning",
    "semantic.target_sizes",
    "semantic.overlay_diff",
    "semantic.ambiguities",
)
MANDATORY_GATE_IDS = AUTOMATIC_GATE_IDS + SEMANTIC_GATE_IDS

MIN_REFINEMENT_LIMIT = 1
MAX_REFINEMENT_LIMIT = 20
