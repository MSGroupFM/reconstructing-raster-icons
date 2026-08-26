#!/usr/bin/env python3
"""Validate contract schemas and one or more JSON documents."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jsonschema import ValidationError

from reconstructing_raster_icons.constants import SCHEMA_KINDS
from reconstructing_raster_icons.schema_io import load_schema, validate_instance


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schemas", type=Path, required=True)
    parser.add_argument("--documents", type=Path, nargs="+", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    schemas = {name: load_schema(name, args.schemas) for name in sorted(SCHEMA_KINDS)}
    for document_path in args.documents:
        try:
            with document_path.open(encoding="utf-8") as source:
                document = json.load(source)
            if not isinstance(document, dict):
                raise ValidationError("document root must be an object")
            schema_kind = document.get("schema_kind")
            if schema_kind not in schemas:
                raise ValidationError(f"unknown schema_kind: {schema_kind!r}")
            validate_instance(document, schemas[schema_kind])
        except (OSError, json.JSONDecodeError, ValidationError) as error:
            print(json.dumps({"document": str(document_path), "valid": False, "error": str(error)}))
            return 2
        print(json.dumps({"document": str(document_path), "schema_kind": schema_kind, "valid": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
