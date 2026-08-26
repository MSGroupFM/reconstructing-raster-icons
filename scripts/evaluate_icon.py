#!/usr/bin/env python3
"""Evaluate one safe SVG candidate against a frozen reconstruction map."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jsonschema import ValidationError

from reconstructing_raster_icons.constants import ExitCode
from reconstructing_raster_icons.errors import FrozenArtifactError, InvalidInputError
from reconstructing_raster_icons.pipeline import evaluate_candidate


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", dest="map_path", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--iteration", type=int, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        summary = evaluate_candidate(args.map_path, args.candidate, args.iteration, args.run_dir)
    except (InvalidInputError, FrozenArtifactError, ValidationError, json.JSONDecodeError) as error:
        print(f"evaluate_icon: {error}", file=sys.stderr)
        summary = {"ok": False, "stage": "evaluate_candidate", "status": "invalid_input", "exit_code": 2}
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        print(f"evaluate_icon: {error}", file=sys.stderr)
        summary = {"ok": False, "stage": "evaluate_candidate", "status": "runtime_error", "exit_code": 7}
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return int(summary.get("exit_code", ExitCode.RUNTIME_ERROR))


if __name__ == "__main__":
    raise SystemExit(main())
