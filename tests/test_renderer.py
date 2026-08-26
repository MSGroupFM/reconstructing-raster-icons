from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from reconstructing_raster_icons.constants import Status
import reconstructing_raster_icons.renderer as renderer_module
from reconstructing_raster_icons.renderer import (
    CANONICAL_LOADER_SHA256,
    CANONICAL_NPM_INTEGRITY,
    CANONICAL_NODE_VERSION,
    CANONICAL_WASM_SHA256,
    RendererLockError,
    load_renderer_lock,
    render_canonical,
)
from reconstructing_raster_icons.safe_svg import validate_svg


REPOSITORY = Path(__file__).resolve().parents[1]
EXACT_NODE = Path("/private/tmp/reconstructing-raster-icons-node/node_modules/node/bin/node")


class RendererTests(unittest.TestCase):
    def test_lock_matches_the_binding_renderer_contract(self) -> None:
        lock = load_renderer_lock(REPOSITORY / "canonical-renderer.lock")
        self.assertEqual(lock.node_version, CANONICAL_NODE_VERSION)
        self.assertEqual(lock.package_integrity, CANONICAL_NPM_INTEGRITY)
        self.assertEqual(lock.wasm_sha256, CANONICAL_WASM_SHA256)
        self.assertEqual(lock.loader_sha256, CANONICAL_LOADER_SHA256)

    def test_lock_rejects_one_character_change_to_every_pinned_value(self) -> None:
        canonical = json.loads((REPOSITORY / "canonical-renderer.lock").read_text(encoding="utf-8"))
        fields = (
            ("node_version", CANONICAL_NODE_VERSION),
            ("package_integrity", CANONICAL_NPM_INTEGRITY),
            ("wasm_sha256", CANONICAL_WASM_SHA256),
            ("loader_sha256", CANONICAL_LOADER_SHA256),
        )
        for field, expected in fields:
            with self.subTest(field=field), TemporaryDirectory() as temporary_directory:
                mutated = dict(canonical)
                mutated[field] = expected[:-1] + ("0" if expected[-1] != "0" else "1")
                path = Path(temporary_directory) / "renderer.lock"
                path.write_text(json.dumps(mutated), encoding="utf-8")
                with self.assertRaises(RendererLockError):
                    load_renderer_lock(path)

    @unittest.skipUnless(EXACT_NODE.is_file(), "exact Node 22.14.0 fixture is unavailable")
    def test_renders_canonical_dimensions_and_repeatable_hashes(self) -> None:
        fixtures = (
            ("square.svg", (128, 128)),
            ("landscape.svg", (1024, 576)),
        )
        with patch.dict(os.environ, {"RECONSTRUCTING_RASTER_ICONS_NODE": str(EXACT_NODE)}):
            for fixture_name, size in fixtures:
                with self.subTest(fixture=fixture_name), TemporaryDirectory() as temporary_directory:
                    document = validate_svg(REPOSITORY / "tests" / "fixtures" / "renderer" / fixture_name)
                    workspace = Path(temporary_directory)
                    first = render_canonical(document, size, workspace)
                    second = render_canonical(document, size, workspace)

                    self.assertEqual(first.status, Status.ACCEPTED, first.diagnostic)
                    self.assertEqual(second.status, Status.ACCEPTED, second.diagnostic)
                    self.assertEqual(first.sha256, second.sha256)
                    self.assertEqual(first.sha256, hashlib.sha256(first.png_bytes).hexdigest())
                    with Image.open(first.path) as rendered:
                        self.assertEqual(rendered.size, size)
                        self.assertEqual(rendered.mode, "RGBA")

    def test_noncanonical_node_is_rejected_before_rendering(self) -> None:
        system_node = Path("/opt/homebrew/bin/node")
        if not system_node.is_file():
            self.skipTest("system Node negative fixture is unavailable")
        fixture = validate_svg(REPOSITORY / "tests" / "fixtures" / "renderer" / "square.svg")
        with TemporaryDirectory() as temporary_directory:
            with patch.dict(os.environ, {"RECONSTRUCTING_RASTER_ICONS_NODE": str(system_node)}):
                result = render_canonical(fixture, (128, 128), Path(temporary_directory))
        self.assertEqual(result.status, Status.NON_CANONICAL)
        self.assertEqual(result.png_bytes, b"")
        self.assertIn("22.14.0", result.diagnostic)

    @unittest.skipUnless(EXACT_NODE.is_file(), "exact Node 22.14.0 fixture is unavailable")
    def test_nonexact_root_package_lock_contract_is_noncanonical(self) -> None:
        package_lock = json.loads((REPOSITORY / "package-lock.json").read_text(encoding="utf-8"))
        package_lock["packages"][""]["dependencies"]["@resvg/resvg-wasm"] = "^2.6.2"
        fixture = validate_svg(REPOSITORY / "tests" / "fixtures" / "renderer" / "square.svg")
        with TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            mutated_lock = temporary / "package-lock.json"
            mutated_lock.write_text(json.dumps(package_lock), encoding="utf-8")
            with (
                patch.object(renderer_module, "_PACKAGE_LOCK_PATH", mutated_lock),
                patch.dict(os.environ, {"RECONSTRUCTING_RASTER_ICONS_NODE": str(EXACT_NODE)}),
            ):
                result = render_canonical(fixture, (128, 128), temporary)
        self.assertEqual(result.status, Status.NON_CANONICAL)
        self.assertIn("package-lock", result.diagnostic)


if __name__ == "__main__":
    unittest.main()
