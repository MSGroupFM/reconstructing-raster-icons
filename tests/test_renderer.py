from __future__ import annotations

import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
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
CANONICAL_RUNNER_SHA256 = "16011161fad6c9b585ce477aeff2d811abafbd767eee26612055259c610b8e5a"


class RendererTests(unittest.TestCase):
    @unittest.skipUnless(EXACT_NODE.is_file(), "exact Node 22.14.0 fixture is unavailable")
    def test_runner_gates_every_isolation_mismatch_before_wasm_and_output(self) -> None:
        harness = REPOSITORY / "tests" / "fixtures" / "renderer" / "runner_isolation_harness.mjs"
        cases = (
            "permission_type",
            "allowed_read_capability",
            "denied_read_capability",
            "allowed_write_capability",
            "child_capability",
            "worker_capability",
            "network_capability",
            "filesystem_allowed",
            "filesystem_denial",
            "subprocess_denial",
            "network_denial",
            "probe_exception",
        )
        for case in cases:
            with self.subTest(case=case), TemporaryDirectory() as temporary_directory:
                completed = subprocess.run(
                    [str(EXACT_NODE), str(harness), case, temporary_directory],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": str(EXACT_NODE.parent), "TZ": "UTC"},
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual(result["result"], 1)
                self.assertEqual(result["emitted"]["render_status"], "isolation_failure")
                self.assertEqual(result["emitted"]["isolation_failure"], case)
                self.assertFalse(result["render_called"])
                self.assertFalse(result["output_exists"])
                self.assertEqual(list(Path(temporary_directory).iterdir()), [])

    @unittest.skipUnless(EXACT_NODE.is_file(), "exact Node 22.14.0 fixture is unavailable")
    def test_adapter_accepts_minimal_isolation_failure_evidence_and_removes_private_artifacts(self) -> None:
        fixture = validate_svg(REPOSITORY / "tests" / "fixtures" / "renderer" / "square.svg")

        def fail_isolation(command: list[str], **kwargs: object) -> object:
            runner_index = next(
                index
                for index, value in enumerate(command)
                if not value.startswith("--") and value.endswith("scripts/render_svg.mjs")
            )
            private_node = Path(command[0])
            output = Path(command[runner_index + 2])
            nonce = command[runner_index + 6]
            output.write_bytes(b"unexpected renderer artifact")
            evidence = {
                "nonce": nonce,
                "exec_path": str(private_node),
                "node_version": CANONICAL_NODE_VERSION,
                "release_name": "node",
                "platform": "darwin",
                "architecture": "arm64",
                "render_status": "isolation_failure",
                "isolation_failure": "child_capability",
            }
            return renderer_module.subprocess.CompletedProcess(
                command,
                1,
                json.dumps(evidence).encode("utf-8") + b"\n",
                b"",
            )

        with TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            with (
                patch.object(renderer_module, "_probe_memory_preexec"),
                patch.object(renderer_module, "_memory_preexec", return_value=lambda: None),
                patch.object(renderer_module.subprocess, "run", side_effect=fail_isolation),
                patch.dict(os.environ, {"RECONSTRUCTING_RASTER_ICONS_NODE": str(EXACT_NODE)}),
            ):
                result = render_canonical(fixture, (128, 128), workspace)
            inventory = list(workspace.iterdir())

        self.assertEqual(result.status, Status.NON_CANONICAL)
        self.assertEqual(result.png_bytes, b"")
        self.assertIsNotNone(result.attestation)
        if result.attestation is not None:
            self.assertEqual(result.attestation["render_status"], "isolation_failure")
            self.assertEqual(result.attestation["isolation_failure"], "child_capability")
        self.assertEqual(inventory, [])

    def test_lock_matches_the_binding_renderer_contract(self) -> None:
        raw_lock = json.loads((REPOSITORY / "canonical-renderer.lock").read_text(encoding="utf-8"))
        lock = load_renderer_lock(REPOSITORY / "canonical-renderer.lock")
        self.assertEqual(lock.node_version, CANONICAL_NODE_VERSION)
        self.assertEqual(lock.package_integrity, CANONICAL_NPM_INTEGRITY)
        self.assertEqual(lock.wasm_sha256, CANONICAL_WASM_SHA256)
        self.assertEqual(lock.loader_sha256, CANONICAL_LOADER_SHA256)
        self.assertEqual(raw_lock.get("runner_sha256"), CANONICAL_RUNNER_SHA256)
        self.assertEqual(
            raw_lock.get("node_binaries"),
            {
                "darwin-arm64": {
                    "package": "node-bin-darwin-arm64",
                    "package_version": "22.14.0",
                    "package_integrity": "sha512-vXh85M8hpgFnaX/q8fBhsH+oNH5FtN6sEczeR0vDel87NDHjF3mF+9Ffx60SAQnI9Akq93WFkmEp8FQR8YbHQQ==",
                    "executable_sha256": "e2d4915d03eda6a2f00a09920e7eeb7a04ad123f9aaad61b1481179fe1bf50e0",
                },
                "linux-x64": {
                    "package": "node-linux-x64",
                    "package_version": "22.14.0",
                    "package_integrity": "sha512-R9k0h0zCZkX4/rlJbwS2c/CaOlmbAz3FkcQnQTJneQgJFaMntb8GVT64oArZEvrnzSyck8tGpcss6u3nT7hqxg==",
                    "executable_sha256": "1abce2374a485bddae3c27b17a3e3143e2780232026e627c4fe74ddde3f380a1",
                },
            },
        )
        self.assertEqual(getattr(lock, "node_binaries", None), raw_lock["node_binaries"])

    def test_lock_rejects_nested_node_binary_change(self) -> None:
        canonical = json.loads((REPOSITORY / "canonical-renderer.lock").read_text(encoding="utf-8"))
        self.assertIn("node_binaries", canonical)
        if "node_binaries" not in canonical:
            return
        with TemporaryDirectory() as temporary_directory:
            mutated = json.loads(json.dumps(canonical))
            digest = mutated["node_binaries"]["linux-x64"]["executable_sha256"]
            mutated["node_binaries"]["linux-x64"]["executable_sha256"] = digest[:-1] + "0"
            path = Path(temporary_directory) / "renderer.lock"
            path.write_text(json.dumps(mutated), encoding="utf-8")
            with self.assertRaises(RendererLockError):
                load_renderer_lock(path)

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
        memory_isolation_supported = True
        try:
            renderer_module._probe_memory_preexec(renderer_module._memory_preexec())
        except RendererLockError:
            memory_isolation_supported = False
        with patch.dict(os.environ, {"RECONSTRUCTING_RASTER_ICONS_NODE": str(EXACT_NODE)}):
            for fixture_name, size in fixtures:
                with self.subTest(fixture=fixture_name), TemporaryDirectory() as temporary_directory:
                    document = validate_svg(REPOSITORY / "tests" / "fixtures" / "renderer" / fixture_name)
                    workspace = Path(temporary_directory)
                    first = render_canonical(document, size, workspace)
                    second = render_canonical(document, size, workspace)

                    if not memory_isolation_supported:
                        self.assertEqual(first.status, Status.NON_CANONICAL)
                        self.assertEqual(second.status, Status.NON_CANONICAL)
                        self.assertIn("memory isolation", first.diagnostic)
                        self.assertEqual(first.png_bytes, b"")
                        self.assertEqual(list(workspace.iterdir()), [])
                        self.assertEqual(first.observed.platform, "darwin-arm64")
                        self.assertEqual(first.observed.node_package, "node-bin-darwin-arm64")
                        self.assertEqual(first.observed.node_package_version, CANONICAL_NODE_VERSION)
                        self.assertEqual(first.observed.node_sha256, first.expected.node_sha256)
                        self.assertIsNone(first.observed.node_version)
                        self.assertEqual(
                            first.expected.node_package_integrity,
                            renderer_module.CANONICAL_NODE_BINARIES["darwin-arm64"]["package_integrity"],
                        )
                        continue
                    self.assertEqual(first.status, Status.ACCEPTED, first.diagnostic)
                    self.assertEqual(second.status, Status.ACCEPTED, second.diagnostic)
                    self.assertEqual(first.sha256, second.sha256)
                    self.assertEqual(first.sha256, hashlib.sha256(first.png_bytes).hexdigest())
                    attestation = getattr(first, "attestation", None)
                    self.assertIsNotNone(attestation)
                    if attestation is not None:
                        self.assertNotEqual(attestation["exec_path"], str(EXACT_NODE))
                        self.assertTrue(attestation["exec_path"].endswith("/bin/node"))
                        self.assertEqual(attestation["node_version"], "22.14.0")
                        self.assertEqual(attestation["release_name"], "node")
                        self.assertEqual(attestation["platform"], "linux")
                        self.assertEqual(attestation["architecture"], "x64")
                        self.assertEqual(attestation["filesystem_denial"], "ERR_ACCESS_DENIED")
                        self.assertEqual(attestation["network_denial"], "EPERM")
                        self.assertEqual(attestation["subprocess_denial"], "ERR_ACCESS_DENIED")
                        self.assertEqual(attestation["executable_magic"], "7f454c46")
                        self.assertEqual(attestation["executable_mode"], "0o500")
                    self.assertEqual(first.observed.node_version, first.expected.node_version)
                    self.assertEqual(first.observed.node_package, first.expected.node_package)
                    self.assertEqual(first.observed.node_package_version, first.expected.node_package_version)
                    self.assertEqual(first.observed.node_sha256, first.expected.node_sha256)
                    self.assertEqual(first.observed.runner_sha256, first.expected.runner_sha256)
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
        self.assertIn("canonical Node executable", result.diagnostic)

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

    def test_darwin_memory_preexec_sets_data_and_resident_limits(self) -> None:
        applied: dict[int, tuple[int, int]] = {}

        def setrlimit(kind: int, limits: tuple[int, int]) -> None:
            applied[kind] = limits

        fake_resource = SimpleNamespace(
            RLIMIT_DATA=2,
            RLIMIT_RSS=5,
            setrlimit=setrlimit,
            getrlimit=lambda kind: applied.get(kind, (-1, -1)),
        )
        with (
            patch.object(renderer_module.sys, "platform", "darwin"),
            patch.dict(sys.modules, {"resource": fake_resource}),
        ):
            preexec = renderer_module._memory_preexec()
            self.assertTrue(callable(preexec))
            preexec()

        expected = (renderer_module.MEMORY_LIMIT_BYTES, renderer_module.MEMORY_LIMIT_BYTES)
        self.assertEqual(applied, {2: expected, 5: expected})

    @unittest.skipUnless(os.name == "posix", "pre-exec limits apply only to POSIX")
    def test_memory_preexec_error_fails_capability_probe_closed(self) -> None:
        def broken_limit() -> None:
            raise OSError("setrlimit failed")

        probe = getattr(renderer_module, "_probe_memory_preexec", None)
        self.assertTrue(callable(probe))
        if probe is not None:
            with self.assertRaises(RendererLockError):
                probe(broken_limit)

    def test_non_posix_platform_is_rejected_before_renderer_artifacts(self) -> None:
        platform_key = getattr(renderer_module, "_platform_key", None)
        self.assertTrue(callable(platform_key))
        if platform_key is not None:
            with patch.object(renderer_module.os, "name", "nt"):
                with self.assertRaises(RendererLockError):
                    platform_key()

    def test_non_posix_render_rejects_before_artifact_verification(self) -> None:
        fixture = validate_svg(REPOSITORY / "tests" / "fixtures" / "renderer" / "square.svg")
        with TemporaryDirectory() as temporary_directory:
            with (
                patch.object(
                    renderer_module,
                    "_platform_key",
                    side_effect=RendererLockError("unsupported platform"),
                ),
                patch.object(
                    renderer_module,
                    "_verify_install",
                    side_effect=AssertionError("artifacts must not be touched"),
                ),
            ):
                result = render_canonical(fixture, (128, 128), Path(temporary_directory))
        self.assertEqual(result.status, Status.NON_CANONICAL)
        self.assertIn("unsupported platform", result.diagnostic)

    def test_native_looking_wrong_hash_node_is_rejected_without_execution(self) -> None:
        fixture = validate_svg(REPOSITORY / "tests" / "fixtures" / "renderer" / "square.svg")
        with TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            fake_node = temporary / "node"
            fake_node.write_bytes(b"\xcf\xfa\xed\xfe" + b"not the canonical binary")
            fake_node.chmod(0o700)
            fake_digest = hashlib.sha256(fake_node.read_bytes()).hexdigest()
            with (
                patch.dict(os.environ, {"RECONSTRUCTING_RASTER_ICONS_NODE": str(fake_node)}),
                patch.object(
                    renderer_module.subprocess,
                    "run",
                    side_effect=AssertionError("wrong-hash Node must not execute"),
                ),
            ):
                result = render_canonical(fixture, (128, 128), temporary)

        self.assertEqual(result.status, Status.NON_CANONICAL)
        observed = getattr(result, "observed", None)
        expected = getattr(result, "expected", None)
        self.assertIsNotNone(observed)
        self.assertIsNotNone(expected)
        if observed is not None and expected is not None:
            self.assertEqual(observed.node_sha256, fake_digest)
            self.assertIsNone(observed.node_version)
            self.assertNotEqual(observed.node_sha256, expected.node_sha256)
            self.assertEqual(expected.node_version, CANONICAL_NODE_VERSION)

    @unittest.skipUnless(EXACT_NODE.is_file(), "exact Node 22.14.0 fixture is unavailable")
    def test_failure_evidence_separates_observed_and_expected_runner_hashes(self) -> None:
        fixture = validate_svg(REPOSITORY / "tests" / "fixtures" / "renderer" / "square.svg")
        with TemporaryDirectory(dir=REPOSITORY) as temporary_directory:
            temporary = Path(temporary_directory)
            changed_runner = temporary / "render_svg.mjs"
            changed_runner.write_bytes((REPOSITORY / "scripts" / "render_svg.mjs").read_bytes() + b"\n// changed\n")
            changed_digest = hashlib.sha256(changed_runner.read_bytes()).hexdigest()
            with (
                patch.object(renderer_module, "_RUNNER_PATH", changed_runner),
                patch.dict(os.environ, {"RECONSTRUCTING_RASTER_ICONS_NODE": str(EXACT_NODE)}),
            ):
                result = render_canonical(fixture, (128, 128), temporary)

        self.assertEqual(result.status, Status.NON_CANONICAL)
        observed = getattr(result, "observed", None)
        expected = getattr(result, "expected", None)
        self.assertIsNotNone(observed)
        self.assertIsNotNone(expected)
        if observed is not None and expected is not None:
            self.assertEqual(observed.runner_sha256, changed_digest)
            self.assertEqual(expected.runner_sha256, CANONICAL_RUNNER_SHA256)
            self.assertNotEqual(observed.runner_sha256, expected.runner_sha256)

    @unittest.skipUnless(EXACT_NODE.is_file(), "exact Node 22.14.0 fixture is unavailable")
    def test_single_process_executes_private_verified_copies_after_source_replacement(self) -> None:
        fixture = validate_svg(REPOSITORY / "tests" / "fixtures" / "renderer" / "square.svg")
        calls: list[list[str]] = []
        with TemporaryDirectory(dir=REPOSITORY) as temporary_directory:
            temporary = Path(temporary_directory)
            source_runner = temporary / "render_svg.mjs"
            source_runner.write_bytes((REPOSITORY / "scripts" / "render_svg.mjs").read_bytes())

            def run_combined(command: list[str], **kwargs: object) -> object:
                calls.append(command)
                if command[-1] == "--version":
                    return renderer_module.subprocess.CompletedProcess(command, 0, "v22.14.0\n", "")
                self.assertNotIn("--eval", command)
                runner_index = next(
                    index
                    for index, value in enumerate(command)
                    if not value.startswith("--") and value.endswith("scripts/render_svg.mjs")
                )
                private_node = Path(command[0])
                private_runner = Path(command[runner_index])
                candidate = Path(command[runner_index + 1])
                output = Path(command[runner_index + 2])
                private_wasm = Path(command[runner_index + 3])
                private_loader = (
                    private_runner.parent.parent / "node_modules" / "@resvg" / "resvg-wasm" / "index.mjs"
                )
                nonce = command[runner_index + 6]
                denied_path = command[runner_index + 7]

                replacement = source_runner.with_suffix(".replacement")
                replacement.write_text("throw new Error('substituted source');\n", encoding="utf-8")
                os.replace(replacement, source_runner)

                self.assertNotEqual(private_node, EXACT_NODE)
                self.assertNotEqual(private_runner, source_runner)
                self.assertEqual(hashlib.sha256(private_runner.read_bytes()).hexdigest(), CANONICAL_RUNNER_SHA256)
                self.assertEqual(hashlib.sha256(private_node.read_bytes()).hexdigest(), renderer_module.CANONICAL_NODE_BINARIES[renderer_module._platform_key()]["executable_sha256"])
                self.assertEqual(private_node.stat().st_mode & 0o777, 0o500)
                self.assertEqual(private_runner.stat().st_mode & 0o777, 0o500)
                self.assertEqual(candidate.stat().st_mode & 0o777, 0o400)
                self.assertEqual(private_loader.stat().st_mode & 0o777, 0o400)
                self.assertEqual(private_wasm.stat().st_mode & 0o777, 0o400)

                buffer = BytesIO()
                Image.new("RGBA", (128, 128), (0, 0, 0, 0)).save(buffer, format="PNG")
                output.write_bytes(buffer.getvalue())
                evidence = {
                    "nonce": nonce,
                    "exec_path": str(private_node),
                    "node_version": CANONICAL_NODE_VERSION,
                    "release_name": "node",
                    "platform": "darwin",
                    "architecture": "arm64",
                    "permission_type": "object",
                    "allowed_read_capability": True,
                    "denied_read_capability": False,
                    "allowed_write_capability": True,
                    "child_capability": False,
                    "worker_capability": False,
                    "network_capability": False,
                    "filesystem_allowed": True,
                    "filesystem_denial": "ERR_ACCESS_DENIED",
                    "subprocess_denial": "ERR_ACCESS_DENIED",
                    "network_denial": "EPERM",
                    "render_status": "ok",
                    "render_error": None,
                    "denied_path": denied_path,
                }
                return renderer_module.subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(evidence).encode("utf-8") + b"\n",
                    b"",
                )

            with (
                patch.object(renderer_module, "_RUNNER_PATH", source_runner),
                patch.object(renderer_module, "_probe_memory_preexec"),
                patch.object(renderer_module, "_memory_preexec", return_value=lambda: None),
                patch.object(renderer_module.subprocess, "run", side_effect=run_combined),
                patch.dict(os.environ, {"RECONSTRUCTING_RASTER_ICONS_NODE": str(EXACT_NODE)}),
            ):
                result = render_canonical(fixture, (128, 128), temporary)
            source_was_replaced = "substituted source" in source_runner.read_text(encoding="utf-8")

        self.assertEqual(result.status, Status.ACCEPTED, result.diagnostic)
        self.assertEqual(len(calls), 1)
        self.assertTrue(source_was_replaced)


if __name__ == "__main__":
    unittest.main()
