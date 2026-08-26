#!/usr/bin/env python3
"""Merge semantic evidence and publish one immutable acceptance report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jsonschema import ValidationError

from reconstructing_raster_icons.constants import ExitCode
from reconstructing_raster_icons.errors import FrozenArtifactError, InvalidInputError
from reconstructing_raster_icons.pipeline import finalize_review, write_failure_report


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        print(json.dumps({"ok": False, "stage": "finalize_review", "status": "invalid_input", "exit_code": 2}, separators=(",", ":")))
        raise SystemExit(2)


def parse_args() -> argparse.Namespace:
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--semantic-review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        summary = finalize_review(args.evaluation, args.semantic_review, args.output)
    except (InvalidInputError, FrozenArtifactError, ValidationError, json.JSONDecodeError) as error:
        print(f"finalize_review: {error}", file=sys.stderr)
        summary = {"ok": False, "stage": "finalize_review", "status": "invalid_input", "exit_code": 2}
        write_failure_report(args.output.parent, stage="finalize_review", status="invalid_input", exit_code=2, error=error)
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        print(f"finalize_review: {error}", file=sys.stderr)
        summary = {"ok": False, "stage": "finalize_review", "status": "runtime_error", "exit_code": 7}
        write_failure_report(args.output.parent, stage="finalize_review", status="runtime_error", exit_code=7, error=error)
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return int(summary.get("exit_code", ExitCode.RUNTIME_ERROR))


if __name__ == "__main__":
    raise SystemExit(main())
