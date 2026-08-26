"""Typed hard-gate evidence and normative final-status resolution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from typing import NamedTuple

from .constants import (
    AUTOMATIC_GATE_IDS,
    MANDATORY_GATE_IDS,
    SEMANTIC_GATE_IDS,
    ExitCode,
    Status,
)
from .geometry import within_tolerance


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ArtifactEvidence:
    logical_id: str
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.logical_id, str) or not self.logical_id:
            raise ValueError("artifact logical_id must be nonempty")
        if not isinstance(self.sha256, str) or not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("artifact sha256 must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True)
class MeasurementEvidence:
    name: str
    measured: float
    tolerance: float
    unit: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not isinstance(self.unit, str) or not self.name or not self.unit:
            raise ValueError("measurement name and unit must be nonempty")
        if (
            isinstance(self.measured, bool)
            or not isinstance(self.measured, (int, float))
            or isinstance(self.tolerance, bool)
            or not isinstance(self.tolerance, (int, float))
        ):
            raise TypeError("measurement values must be real numbers")
        if (
            not math.isfinite(self.measured)
            or self.measured < 0.0
            or not math.isfinite(self.tolerance)
            or self.tolerance < 0.0
        ):
            raise ValueError("measurement values must be finite and non-negative")
        object.__setattr__(self, "measured", float(self.measured))
        object.__setattr__(self, "tolerance", float(self.tolerance))


@dataclass(frozen=True)
class GateEvidence:
    artifacts: tuple[ArtifactEvidence, ...] = ()
    measurements: tuple[MeasurementEvidence, ...] = ()
    basis: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "measurements", tuple(self.measurements))
        if any(not isinstance(artifact, ArtifactEvidence) for artifact in self.artifacts):
            raise TypeError("gate artifacts must contain only ArtifactEvidence records")
        if any(not isinstance(measurement, MeasurementEvidence) for measurement in self.measurements):
            raise TypeError("gate measurements must contain only MeasurementEvidence records")
        if not isinstance(self.basis, str):
            raise TypeError("gate evidence basis must be a string")
        if not self.artifacts and not self.measurements and not self.basis:
            raise ValueError("gate evidence must contain an artifact, measurement, or basis")

    def to_report_dict(self) -> dict[str, str]:
        """Serialize rich typed evidence into the versioned schema's compact form."""
        result: dict[str, str] = {}
        basis_parts = [self.basis] if self.basis else []
        basis_parts.extend(
            f"{measurement.name}: measured={measurement.measured:.17g} "
            f"tolerance={measurement.tolerance:.17g} {measurement.unit}"
            for measurement in self.measurements
        )
        if self.artifacts:
            result["artifact_id"] = self.artifacts[0].logical_id
            result["sha256"] = self.artifacts[0].sha256
            basis_parts.extend(
                f"artifact {artifact.logical_id} sha256={artifact.sha256}"
                for artifact in self.artifacts[1:]
            )
        if basis_parts:
            result["basis"] = "; ".join(basis_parts)
        return result


@dataclass(frozen=True)
class GateResult:
    gate_id: str
    kind: str
    state: str
    evidence: GateEvidence
    evaluator: str
    timestamp: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.gate_id, str):
            raise TypeError("gate_id must be a string")
        if self.gate_id not in MANDATORY_GATE_IDS:
            raise ValueError(f"unknown mandatory gate ID: {self.gate_id}")
        expected_kind = "automatic" if self.gate_id in AUTOMATIC_GATE_IDS else "semantic"
        if not isinstance(self.kind, str):
            raise TypeError("gate kind must be a string")
        if self.kind != expected_kind:
            raise ValueError(f"{self.gate_id} must have kind {expected_kind}")
        allowed = {"pass", "fail"} if self.kind == "automatic" else {"pass", "fail", "not_evaluated"}
        if not isinstance(self.state, str):
            raise TypeError("gate state must be a string")
        if self.state not in allowed:
            if self.kind == "automatic":
                raise ValueError("automatic gate state must be pass or fail")
            raise ValueError("semantic gate state must be pass, fail, or not_evaluated")
        if not isinstance(self.evidence, GateEvidence):
            raise TypeError("gate evidence must be GateEvidence")
        if not isinstance(self.evaluator, str):
            raise TypeError("gate evaluator must be a string")
        if not self.evaluator:
            raise ValueError("gate evaluator must be nonempty")
        if not isinstance(self.timestamp, datetime):
            raise TypeError("gate timestamp must be a datetime")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() != timezone.utc.utcoffset(self.timestamp):
            raise ValueError("gate timestamp must be timezone-aware UTC")

    def to_dict(self) -> dict[str, object]:
        return {
            "gate_id": self.gate_id,
            "kind": self.kind,
            "state": self.state,
            "evidence": self.evidence.to_report_dict(),
            "evaluator": self.evaluator,
            "evaluated_at": self.timestamp.isoformat().replace("+00:00", "Z"),
        }


class StatusResolution(NamedTuple):
    status: Status
    exit_code: ExitCode


def evaluate_automatic_gates(
    checks: Mapping[str, Mapping[str, object]], *, evaluator: str, timestamp: datetime
) -> tuple[GateResult, ...]:
    """Turn a complete keyed automatic check set into ordered gate records."""
    if set(checks) != set(AUTOMATIC_GATE_IDS) or len(checks) != len(AUTOMATIC_GATE_IDS):
        raise ValueError("checks must contain the exact automatic gate catalog once")
    results: list[GateResult] = []
    for gate_id in AUTOMATIC_GATE_IDS:
        check = checks[gate_id]
        if set(check) != {"passed", "evidence"} or not isinstance(check["passed"], bool):
            raise ValueError(f"{gate_id} check must contain boolean passed and typed evidence")
        gate_evidence = check["evidence"]
        if not isinstance(gate_evidence, GateEvidence):
            raise TypeError(f"{gate_id} evidence must be GateEvidence")
        if gate_id in {"auto.primitives.constraints", "auto.paths.integrity"} and not gate_evidence.measurements:
            raise ValueError(f"{gate_id} evidence must include measured deviation and tolerance")
        if gate_id not in {"auto.primitives.constraints", "auto.paths.integrity"} and not gate_evidence.artifacts:
            raise ValueError(f"{gate_id} static evidence must include logical artifact ID and SHA-256")
        numeric_passed = all(
            within_tolerance(measurement.measured, measurement.tolerance)
            for measurement in gate_evidence.measurements
        )
        results.append(
            GateResult(
                gate_id=gate_id,
                kind="automatic",
                state="pass" if check["passed"] and numeric_passed else "fail",
                evidence=gate_evidence,
                evaluator=evaluator,
                timestamp=timestamp,
            )
        )
    return tuple(results)


def resolve_status(
    *,
    score: float | None,
    target: float | None,
    gates: Sequence[GateResult],
    invalid_input: bool = False,
    runtime_error: bool = False,
    canonical_environment: bool = True,
) -> StatusResolution:
    """Apply status and exit-code precedence without rounding the score."""
    if invalid_input:
        return StatusResolution(Status.INVALID_INPUT, ExitCode.INVALID_INPUT)
    gate_ids = [gate.gate_id for gate in gates]
    if len(gate_ids) != len(set(gate_ids)) or any(gate_id not in MANDATORY_GATE_IDS for gate_id in gate_ids):
        return StatusResolution(Status.INVALID_INPUT, ExitCode.INVALID_INPUT)
    if score is not None:
        if not math.isfinite(score) or not 0.0 <= score <= 100.0:
            return StatusResolution(Status.INVALID_INPUT, ExitCode.INVALID_INPUT)
    if target is not None:
        if (
            not math.isfinite(target)
            or not 0.0 < target <= 100.0
        ):
            return StatusResolution(Status.INVALID_INPUT, ExitCode.INVALID_INPUT)
    present = set(gate_ids)
    missing_automatic = set(AUTOMATIC_GATE_IDS) - present
    if runtime_error or missing_automatic or score is None or target is None:
        return StatusResolution(Status.RUNTIME_ERROR, ExitCode.RUNTIME_ERROR)
    if not canonical_environment:
        return StatusResolution(Status.NON_CANONICAL, ExitCode.NON_CANONICAL)
    if any(gate.state == "fail" for gate in gates):
        return StatusResolution(Status.NOT_ACCEPTED, ExitCode.GATE_FAILED)
    if score < target:
        return StatusResolution(Status.NOT_ACCEPTED, ExitCode.SCORE_BELOW_TARGET)
    missing_semantic = set(SEMANTIC_GATE_IDS) - present
    if missing_semantic or any(gate.kind == "semantic" and gate.state == "not_evaluated" for gate in gates):
        return StatusResolution(Status.INCOMPLETE, ExitCode.INCOMPLETE_REVIEW)
    return StatusResolution(Status.ACCEPTED, ExitCode.ACCEPTED)
