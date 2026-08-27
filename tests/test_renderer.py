from __future__ import annotations

import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image

from reconstructing_raster_icons.constants import ACCEPTANCE_MODEL_VERSION, Status
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
EXACT_NODE = REPOSITORY / "node_modules" / "node" / "bin" / "node"
CANONICAL_RUNNER_SHA256 = "11b08e3fda461c2cc2bd7f03bbf6e0d21bcaf634e2d0ad0626cd71e0921b1af1"


class RendererTests(unittest.TestCase):
    @unittest.skipUnless(
        sys.platform.startswith("linux")
        and platform.machine().lower() in {"x86_64", "amd64"},
        "requires canonical Linux x64",
    )
    def test_linux_x64_bundled_node_v8_starts_under_canonical_old_space_contract(
        self,
    ) -> None:
        self.assertTrue(EXACT_NODE.is_file(), "npm ci did not provision the exact bundled Node")
        fixture = validate_svg(REPOSITORY / "tests" / "fixtures" / "renderer" / "square.svg")
        real_run = subprocess.run
        javascript = (
            'process.stdout.write(JSON.stringify({'
            'phase:"javascript",'
            'exec_path:process.execPath,'
            'node_version:process.versions.node,'
            'platform:process.platform,'
            'architecture:process.arch,'
            'max_old_space_size_512:process.execArgv.includes("--max-old-space-size=512"),'
            'wasm_trap_handler_disabled:process.execArgv.includes("--disable-wasm-trap-handler")'
            '})+"\\n")'
        )

        class StartupProbeCompleted(RuntimeError):
            def __init__(
                self,
                completed: subprocess.CompletedProcess[bytes],
                command: list[str],
            ) -> None:
                super().__init__("canonical V8 startup probe completed")
                self.completed = completed
                self.command = command

        def run_startup_probe(command: list[str], **kwargs: object) -> object:
            runner_index = next(
                (
                    index
                    for index, value in enumerate(command)
                    if not value.startswith("--") and value.endswith("scripts/render_svg.mjs")
                ),
                None,
            )
            if runner_index is None:
                return real_run(command, **kwargs)
            probe_command = [*command[:runner_index], "--eval", javascript]
            completed = real_run(probe_command, **kwargs)
            raise StartupProbeCompleted(completed, probe_command)

        with TemporaryDirectory() as temporary_directory:
            with (
                patch.object(renderer_module.subprocess, "run", side_effect=run_startup_probe),
                self.assertRaises(StartupProbeCompleted) as raised,
            ):
                render_canonical(fixture, (128, 128), Path(temporary_directory))

        completed = raised.exception.completed
        diagnostic = (
            f"returncode={completed.returncode}, "
            f"stdout={completed.stdout[:4096]!r}, "
            f"stderr={completed.stderr[:4096]!r}"
        )
        self.assertEqual(completed.returncode, 0, diagnostic)
        self.assertEqual(completed.stderr, b"", diagnostic)
        self.assertLessEqual(len(completed.stdout), 4096, diagnostic)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "phase": "javascript",
                "exec_path": raised.exception.command[0],
                "node_version": "22.14.0",
                "platform": "linux",
                "architecture": "x64",
                "max_old_space_size_512": True,
                "wasm_trap_handler_disabled": True,
            },
        )

    def test_node_22_runner_does_not_claim_unsupported_network_isolation(self) -> None:
        source = (REPOSITORY / "scripts" / "render_svg.mjs").read_text(encoding="utf-8")
        self.assertNotIn('from "node:net"', source)
        self.assertNotIn("network_capability", source)
        self.assertNotIn("network_denial", source)

    @unittest.skipUnless(EXACT_NODE.is_file(), "exact Node 22.14.0 fixture is unavailable")
    def test_runner_reports_every_runtime_control_mismatch_before_wasm_and_output(self) -> None:
        harness = REPOSITORY / "tests" / "fixtures" / "renderer" / "runner_runtime_controls_harness.mjs"
        cases = (
            "v8_old_space_mib",
            "wasm_trap_handler_disabled",
            "permission_type",
            "allowed_read_capability",
            "denied_read_capability",
            "allowed_write_capability",
            "child_capability",
            "worker_capability",
            "filesystem_allowed",
            "filesystem_denial",
            "subprocess_denial",
            "probe_exception",
        )
        for case in cases:
            with self.subTest(case=case), TemporaryDirectory() as temporary_directory:
                resource_flags = (
                    ["--max-old-space-size=511", "--disable-wasm-trap-handler"]
                    if case == "v8_old_space_mib"
                    else ["--max-old-space-size=512"]
                    if case == "wasm_trap_handler_disabled"
                    else ["--max-old-space-size=512", "--disable-wasm-trap-handler"]
                )
                completed = subprocess.run(
                    [str(EXACT_NODE), *resource_flags, str(harness), case, temporary_directory],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": str(EXACT_NODE.parent), "TZ": "UTC"},
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertEqual(result["result"], 1)
                self.assertEqual(result["emitted"]["render_status"], "runtime_control_failure")
                self.assertEqual(result["emitted"]["runtime_control_failure"], case)
                self.assertFalse(result["render_called"])
                self.assertFalse(result["output_exists"])
                self.assertEqual(list(Path(temporary_directory).iterdir()), [])

    @unittest.skipUnless(EXACT_NODE.is_file(), "exact Node 22.14.0 fixture is unavailable")
    def test_adapter_accepts_minimal_runtime_control_failure_evidence_and_removes_private_artifacts(self) -> None:
        fixture = validate_svg(REPOSITORY / "tests" / "fixtures" / "renderer" / "square.svg")
        cases = (
            ("child_capability", 512, True),
            ("v8_old_space_mib", 511, True),
            ("wasm_trap_handler_disabled", 512, False),
        )
        for failure, v8_old_space_mib, wasm_trap_handler_disabled in cases:
            with self.subTest(failure=failure), TemporaryDirectory() as temporary_directory:
                def fail_runtime_control(command: list[str], **kwargs: object) -> object:
                    runner_index = next(
                        index
                        for index, value in enumerate(command)
                        if not value.startswith("--") and value.endswith("scripts/render_svg.mjs")
                    )
                    private_node = Path(command[0])
                    output = Path(command[runner_index + 2])
                    nonce = command[runner_index + 6]
                    expected_platform, expected_architecture = renderer_module._platform_key().split("-", 1)
                    output.write_bytes(b"unexpected renderer artifact")
                    evidence = {
                        "nonce": nonce,
                        "exec_path": str(private_node),
                        "node_version": CANONICAL_NODE_VERSION,
                        "release_name": "node",
                        "platform": expected_platform,
                        "architecture": expected_architecture,
                        "v8_old_space_mib": v8_old_space_mib,
                        "wasm_trap_handler_disabled": wasm_trap_handler_disabled,
                        "render_status": "runtime_control_failure",
                        "runtime_control_failure": failure,
                    }
                    return renderer_module.subprocess.CompletedProcess(
                        command,
                        1,
                        json.dumps(evidence).encode("utf-8") + b"\n",
                        b"",
                    )

                workspace = Path(temporary_directory)
                with (
                    patch.object(renderer_module.subprocess, "run", side_effect=fail_runtime_control),
                    patch.dict(os.environ, {"RECONSTRUCTING_RASTER_ICONS_NODE": str(EXACT_NODE)}),
                ):
                    result = render_canonical(fixture, (128, 128), workspace)
                inventory = list(workspace.iterdir())

                self.assertEqual(result.status, Status.NON_CANONICAL)
                self.assertEqual(result.png_bytes, b"")
                self.assertIsNotNone(result.attestation)
                if result.attestation is not None:
                    self.assertEqual(result.attestation["render_status"], "runtime_control_failure")
                    self.assertEqual(result.attestation["runtime_control_failure"], failure)
                self.assertEqual(inventory, [])

    def test_adapter_enforces_first_runtime_control_mismatch_and_exact_success_flags(self) -> None:
        validate_attestation = getattr(renderer_module, "_validate_combined_attestation", None)
        self.assertTrue(callable(validate_attestation))
        run_directory = Path("/private/run")
        node = run_directory / "bin" / "node"
        candidate = run_directory / "candidate.svg"
        denied_path = Path("/private/denied")
        platform_key = renderer_module._platform_key()
        expected_platform, expected_architecture = platform_key.split("-", 1)
        stable_identity = {
            "nonce": "nonce",
            "exec_path": str(node),
            "node_version": CANONICAL_NODE_VERSION,
            "release_name": "node",
            "platform": expected_platform,
            "architecture": expected_architecture,
        }

        invalid_failures = (
            ("v8_old_space_mib", 512, False),
            ("wasm_trap_handler_disabled", 511, False),
            ("child_capability", 511, True),
            ("probe_exception", 512, False),
        )
        for failure, v8_old_space_mib, wasm_trap_handler_disabled in invalid_failures:
            evidence = {
                **stable_identity,
                "v8_old_space_mib": v8_old_space_mib,
                "wasm_trap_handler_disabled": wasm_trap_handler_disabled,
                "render_status": "runtime_control_failure",
                "runtime_control_failure": failure,
            }
            with self.subTest(failure=failure), self.assertRaisesRegex(
                RendererLockError, "runtime-control failure evidence is invalid"
            ):
                validate_attestation(
                    json.dumps(evidence).encode("utf-8"),
                    stderr=b"",
                    nonce="nonce",
                    node=node,
                    candidate=candidate,
                    run_directory=run_directory,
                    denied_path=denied_path,
                    platform_key=platform_key,
                )

        valid_resource_failure = {
            **stable_identity,
            "v8_old_space_mib": 511,
            "wasm_trap_handler_disabled": True,
            "render_status": "runtime_control_failure",
            "runtime_control_failure": "v8_old_space_mib",
        }
        identity_mutations = (
            ("nonce", "forged"),
            ("exec_path", "/private/other/node"),
            ("node_version", "22.13.0"),
            ("platform", "forged"),
            ("architecture", "forged"),
        )
        for field, value in identity_mutations:
            evidence = {**valid_resource_failure, field: value}
            with self.subTest(identity_field=field), self.assertRaisesRegex(
                RendererLockError, "runtime mismatch"
            ):
                validate_attestation(
                    json.dumps(evidence).encode("utf-8"),
                    stderr=b"",
                    nonce="nonce",
                    node=node,
                    candidate=candidate,
                    run_directory=run_directory,
                    denied_path=denied_path,
                    platform_key=platform_key,
                )

        full_evidence = {
            **stable_identity,
            "v8_old_space_mib": 512,
            "wasm_trap_handler_disabled": True,
            "permission_type": "object",
            "allowed_read_capability": True,
            "denied_read_capability": False,
            "allowed_write_capability": True,
            "child_capability": False,
            "worker_capability": False,
            "filesystem_allowed": True,
            "filesystem_denial": "ERR_ACCESS_DENIED",
            "subprocess_denial": "ERR_ACCESS_DENIED",
            "render_status": "ok",
            "render_error": None,
            "denied_path": str(denied_path),
        }
        for field, value in (("v8_old_space_mib", 511), ("wasm_trap_handler_disabled", False)):
            evidence = {**full_evidence, field: value}
            with self.subTest(full_field=field), self.assertRaisesRegex(
                RendererLockError, "resource controls mismatch"
            ):
                validate_attestation(
                    json.dumps(evidence).encode("utf-8"),
                    stderr=b"",
                    nonce="nonce",
                    node=node,
                    candidate=candidate,
                    run_directory=run_directory,
                    denied_path=denied_path,
                    platform_key=platform_key,
                )

    def test_lock_matches_the_binding_renderer_contract(self) -> None:
        raw_lock = json.loads((REPOSITORY / "canonical-renderer.lock").read_text(encoding="utf-8"))
        lock = load_renderer_lock(REPOSITORY / "canonical-renderer.lock")
        self.assertEqual(raw_lock.get("lock_version"), 2)
        self.assertEqual(raw_lock.get("acceptance_model_version"), ACCEPTANCE_MODEL_VERSION)
        self.assertEqual(lock.node_version, CANONICAL_NODE_VERSION)
        self.assertEqual(lock.package_integrity, CANONICAL_NPM_INTEGRITY)
        self.assertEqual(lock.wasm_sha256, CANONICAL_WASM_SHA256)
        self.assertEqual(lock.loader_sha256, CANONICAL_LOADER_SHA256)
        self.assertEqual(raw_lock.get("runner_sha256"), CANONICAL_RUNNER_SHA256)
        self.assertEqual(
            raw_lock.get("resource_controls"),
            {
                "wall_timeout_seconds": 15,
                "v8_old_space_mib": 512,
                "wasm_trap_handler_disabled": True,
            },
        )
        self.assertEqual(getattr(lock, "resource_controls", None), raw_lock["resource_controls"])
        self.assertNotIn("disable_wasm_trap_handler", raw_lock.get("render_options", {}))
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

    def test_lock_rejects_resource_control_changes(self) -> None:
        canonical = json.loads((REPOSITORY / "canonical-renderer.lock").read_text(encoding="utf-8"))
        mutations = (
            ("wall_timeout_seconds", 14),
            ("v8_old_space_mib", 511),
            ("wasm_trap_handler_disabled", False),
        )
        for field, value in mutations:
            with self.subTest(field=field), TemporaryDirectory() as temporary_directory:
                mutated = json.loads(json.dumps(canonical))
                mutated["resource_controls"][field] = value
                path = Path(temporary_directory) / "renderer.lock"
                path.write_text(json.dumps(mutated), encoding="utf-8")
                with self.assertRaises(RendererLockError):
                    load_renderer_lock(path)

    def test_permission_command_uses_only_portable_resource_flags(self) -> None:
        run_directory = Path("/private/run")
        command = renderer_module._permission_command(
            Path("/private/run/bin/node"),
            (Path("/private/run/scripts/render_svg.mjs"),),
            run_directory,
        )
        self.assertEqual(command[1:3], ["--max-old-space-size=512", "--disable-wasm-trap-handler"])
        source = (REPOSITORY / "src" / "reconstructing_raster_icons" / "renderer.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("preexec_fn", source)
        self.assertNotIn("RLIMIT_", source)

    @unittest.skipUnless(EXACT_NODE.is_file(), "exact Node 22.14.0 fixture is unavailable")
    def test_renders_canonical_dimensions_and_repeatable_hashes(self) -> None:
        fixtures = (
            ("square.svg", (128, 128)),
            ("landscape.svg", (1024, 576)),
        )
        expected_platform, expected_architecture = renderer_module._platform_key().split("-", 1)
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
                        self.assertNotEqual(attestation["exec_path"], str(EXACT_NODE))
                        self.assertTrue(attestation["exec_path"].endswith("/bin/node"))
                        self.assertEqual(attestation["node_version"], "22.14.0")
                        self.assertEqual(attestation["release_name"], "node")
                        self.assertEqual(attestation["platform"], expected_platform)
                        self.assertEqual(attestation["architecture"], expected_architecture)
                        self.assertEqual(attestation["v8_old_space_mib"], 512)
                        self.assertTrue(attestation["wasm_trap_handler_disabled"])
                        self.assertEqual(attestation["filesystem_denial"], "ERR_ACCESS_DENIED")
                        self.assertEqual(attestation["subprocess_denial"], "ERR_ACCESS_DENIED")
                        self.assertEqual(attestation["executable_magic"], EXACT_NODE.read_bytes()[:4].hex())
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
    def test_nonexact_platform_node_lock_contract_is_noncanonical(self) -> None:
        package_lock = json.loads((REPOSITORY / "package-lock.json").read_text(encoding="utf-8"))
        package_lock["packages"][""]["optionalDependencies"]["node-linux-x64"] = "^22.14.0"
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

    def test_invalid_attestation_reports_bounded_renderer_stderr(self) -> None:
        diagnostic = getattr(renderer_module, "_invalid_attestation_diagnostic", None)
        self.assertTrue(callable(diagnostic))
        if diagnostic is not None:
            message = diagnostic(b"", b"Fatal process out of memory\n")
            self.assertEqual(
                message,
                "Node renderer returned invalid attestation evidence: Fatal process out of memory",
            )

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
                    "platform": renderer_module._platform_key().split("-", 1)[0],
                    "architecture": renderer_module._platform_key().split("-", 1)[1],
                    "v8_old_space_mib": 512,
                    "wasm_trap_handler_disabled": True,
                    "permission_type": "object",
                    "allowed_read_capability": True,
                    "denied_read_capability": False,
                    "allowed_write_capability": True,
                    "child_capability": False,
                    "worker_capability": False,
                    "filesystem_allowed": True,
                    "filesystem_denial": "ERR_ACCESS_DENIED",
                    "subprocess_denial": "ERR_ACCESS_DENIED",
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
