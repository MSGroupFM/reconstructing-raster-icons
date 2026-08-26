#!/usr/bin/env python3
"""Freeze one confirmed reference revision and its immutable masks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jsonschema import ValidationError

from reconstructing_raster_icons.constants import ExitCode
from reconstructing_raster_icons.errors import FrozenArtifactError, InvalidInputError
from reconstructing_raster_icons.pipeline import prepare_reference


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--freeze", action="store_true", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        summary = prepare_reference(args.source, args.draft, args.output)
    except (InvalidInputError, FrozenArtifactError, ValidationError, json.JSONDecodeError) as error:
        print(f"prepare_reference: {error}", file=sys.stderr)
        summary = {"ok": False, "stage": "prepare_reference", "status": "invalid_input", "exit_code": 2}
    except (OSError, RuntimeError, ValueError, TypeError) as error:
        print(f"prepare_reference: {error}", file=sys.stderr)
        summary = {"ok": False, "stage": "prepare_reference", "status": "runtime_error", "exit_code": 7}
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
    return int(summary.get("exit_code", ExitCode.RUNTIME_ERROR))


if __name__ == "__main__":
    raise SystemExit(main())
