from __future__ import annotations

import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
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
CANONICAL_RUNNER_SHA256 = "12c6d7f3d44702be7070913097dcb138959a9511f99c20dc51710f5b272525da"


class RendererTests(unittest.TestCase):
    def test_lock_matches_the_binding_renderer_contract(self) -> None:
        raw_lock = json.loads((REPOSITORY / "canonical-renderer.lock").read_text(encoding="utf-8"))
        lock = load_renderer_lock(REPOSITORY / "canonical-renderer.lock")
        self.assertEqual(lock.node_version, CANONICAL_NODE_VERSION)
        self.assertEqual(lock.package_integrity, CANONICAL_NPM_INTEGRITY)
        self.assertEqual(lock.wasm_sha256, CANONICAL_WASM_SHA256)
        self.assertEqual(lock.loader_sha256, CANONICAL_LOADER_SHA256)
        self.assertEqual(raw_lock.get("runner_sha256"), CANONICAL_RUNNER_SHA256)

    def test_lock_rejects_one_character_change_to_every_pinned_value(self) -> None:
        canonical = json.loads((REPOSITORY / "canonical-renderer.lock").read_text(encoding="utf-8"))
        fields = (
            ("node_version", CANONICAL_NODE_VERSION),
            ("package_integrity", CANONICAL_NPM_INTEGRITY),
            ("wasm_sha256", CANONICAL_WASM_SHA256),
            ("loader_sha256", CANONICAL_LOADER_SHA256),
            ("runner_sha256", CANONICAL_RUNNER_SHA256),
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
                    attestation = getattr(first, "attestation", None)
                    self.assertIsNotNone(attestation)
                    if attestation is not None:
                        self.assertEqual(attestation["exec_path"], str(EXACT_NODE.resolve()))
                        self.assertEqual(attestation["node_version"], "22.14.0")
                        self.assertEqual(attestation["release_name"], "node")
                        self.assertEqual(attestation["filesystem_denial"], "ERR_ACCESS_DENIED")
                        self.assertEqual(attestation["network_denial"], "EPERM")
                        self.assertEqual(attestation["subprocess_denial"], "ERR_ACCESS_DENIED")
                        self.assertEqual(attestation["executable_magic"], "cffaedfe")
                        self.assertEqual(attestation["executable_mode"], "0o755")
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
        self.assertIn("safe executable", result.diagnostic)

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

    @unittest.skipUnless(EXACT_NODE.is_file(), "exact Node 22.14.0 fixture is unavailable")
    def test_runner_source_mutation_is_noncanonical(self) -> None:
        fixture = validate_svg(REPOSITORY / "tests" / "fixtures" / "renderer" / "square.svg")
        with TemporaryDirectory(dir=REPOSITORY) as temporary_directory:
            temporary = Path(temporary_directory)
            mutated_runner = temporary / "render_svg.mjs"
            shutil.copyfile(REPOSITORY / "scripts" / "render_svg.mjs", mutated_runner)
            mutated_runner.write_text(
                mutated_runner.read_text(encoding="utf-8") + "\n// harmless mutation\n",
                encoding="utf-8",
            )
            with (
                patch.object(renderer_module, "_RUNNER_PATH", mutated_runner),
                patch.dict(os.environ, {"RECONSTRUCTING_RASTER_ICONS_NODE": str(EXACT_NODE)}),
            ):
                result = render_canonical(fixture, (128, 128), temporary)
        self.assertEqual(result.status, Status.NON_CANONICAL)
        self.assertIn("runner hash", result.diagnostic)

    def test_fake_version_executable_is_noncanonical(self) -> None:
        fixture = validate_svg(REPOSITORY / "tests" / "fixtures" / "renderer" / "square.svg")
        with TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            fake_node = temporary / "fake-node"
            fake_node.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"--version\" ]; then printf 'v22.14.0\\n'; exit 0; fi\n"
                "for value in \"$@\"; do\n"
                "  case \"$value\" in */render.png) printf 'arbitrary bytes' > \"$value\";; esac\n"
                "done\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_node.chmod(0o700)
            with patch.dict(os.environ, {"RECONSTRUCTING_RASTER_ICONS_NODE": str(fake_node)}):
                result = render_canonical(fixture, (128, 128), temporary)
        self.assertEqual(result.status, Status.NON_CANONICAL)
        self.assertEqual(result.png_bytes, b"")

    @unittest.skipUnless(EXACT_NODE.is_file(), "exact Node 22.14.0 fixture is unavailable")
    def test_configured_node_symlink_is_noncanonical(self) -> None:
        fixture = validate_svg(REPOSITORY / "tests" / "fixtures" / "renderer" / "square.svg")
        with TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            node_link = temporary / "node-link"
            node_link.symlink_to(EXACT_NODE)
            with patch.dict(os.environ, {"RECONSTRUCTING_RASTER_ICONS_NODE": str(node_link)}):
                result = render_canonical(fixture, (128, 128), temporary)
        self.assertEqual(result.status, Status.NON_CANONICAL)

    def test_invalid_renderer_bytes_are_not_a_png(self) -> None:
        validator = getattr(renderer_module, "_validate_png_payload", None)
        self.assertTrue(callable(validator))
        if validator is not None:
            with self.assertRaises(RendererLockError):
                validator(b"arbitrary bytes", (128, 128))

    def test_renderer_png_with_trailing_payload_is_rejected(self) -> None:
        validator = getattr(renderer_module, "_validate_png_payload", None)
        self.assertTrue(callable(validator))
        if validator is None:
            return
        buffer = BytesIO()
        Image.new("RGBA", (128, 128), (0, 0, 0, 0)).save(buffer, format="PNG")
        with self.assertRaises(RendererLockError):
            validator(buffer.getvalue() + b"trailing", (128, 128))

    def test_renderer_png_dimensions_and_mode_are_independently_checked(self) -> None:
        validator = getattr(renderer_module, "_validate_png_payload", None)
        self.assertTrue(callable(validator))
        if validator is None:
            return
        for mode, size in (("RGBA", (64, 64)), ("RGB", (128, 128))):
            with self.subTest(mode=mode, size=size):
                buffer = BytesIO()
                Image.new(mode, size).save(buffer, format="PNG")
                with self.assertRaises(RendererLockError):
                    validator(buffer.getvalue(), (128, 128))


if __name__ == "__main__":
    unittest.main()
