"""Hash-pinned canonical SVG rendering through the isolated resvg WASM runner."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import stat
import struct
import subprocess
import sys
from typing import Any
import zlib

from PIL import Image, UnidentifiedImageError

from .constants import ACCEPTANCE_MODEL_VERSION, Status
from .safe_svg import SafeSvgDocument


CANONICAL_NODE_VERSION = "22.14.0"
CANONICAL_NODE_BINARIES = {
    "darwin-arm64": {
        "package": "node-bin-darwin-arm64",
        "package_version": CANONICAL_NODE_VERSION,
        "package_integrity": "sha512-vXh85M8hpgFnaX/q8fBhsH+oNH5FtN6sEczeR0vDel87NDHjF3mF+9Ffx60SAQnI9Akq93WFkmEp8FQR8YbHQQ==",
        "executable_sha256": "e2d4915d03eda6a2f00a09920e7eeb7a04ad123f9aaad61b1481179fe1bf50e0",
    },
    "linux-x64": {
        "package": "node-linux-x64",
        "package_version": CANONICAL_NODE_VERSION,
        "package_integrity": "sha512-R9k0h0zCZkX4/rlJbwS2c/CaOlmbAz3FkcQnQTJneQgJFaMntb8GVT64oArZEvrnzSyck8tGpcss6u3nT7hqxg==",
        "executable_sha256": "1abce2374a485bddae3c27b17a3e3143e2780232026e627c4fe74ddde3f380a1",
    },
}
CANONICAL_PACKAGE = "@resvg/resvg-wasm"
CANONICAL_PACKAGE_VERSION = "2.6.2"
CANONICAL_NPM_INTEGRITY = "sha512-FqALmHI8D4o6lk/LRWDnhw95z5eO+eAa6ORjVg09YRR7BkcM6oPHU9uyC0gtQG5vpFLvgpeU4+zEAz2H8APHNw=="
CANONICAL_WASM_SHA256 = "22bf6e9f9a100d972da0411a69c5ba504367fc1fa87b3b64e3f35e53926d2d70"
CANONICAL_LOADER_SHA256 = "10170d02d816f02ec76f9bc095b01d9becf536e7b1e12e5aa616652c84b237a1"
CANONICAL_RUNNER_SHA256 = "6fbfe4d1b7b6c67aba48b7162e4c43456920325c025eb3c77835290d693ee16a"
CANONICAL_LICENSE = "MPL-2.0"
RENDER_TIMEOUT_SECONDS = 15
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
MAX_CANONICAL_SIDE = 1024
MIN_CANONICAL_SIDE = 64
MAX_PNG_BYTES = 32 * 1024 * 1024

_NATIVE_MAGICS = frozenset(
    {
        b"\x7fELF",
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
    }
)

_REPOSITORY = Path(__file__).resolve().parents[2]
_LOCK_PATH = _REPOSITORY / "canonical-renderer.lock"
_PACKAGE_LOCK_PATH = _REPOSITORY / "package-lock.json"
_RUNNER_PATH = _REPOSITORY / "scripts" / "render_svg.mjs"
_RENDER_OPTIONS = {
    "background": None,
    "crop": None,
    "current_color": "#000000",
    "font_load_system_fonts": False,
    "shape_rendering": 2,
    "text_rendering": 2,
}


class RendererLockError(ValueError):
    """Raised when the canonical lock or an installed artifact is not exact."""


@dataclass(frozen=True)
class RendererLock:
    node_version: str
    node_binaries: dict[str, dict[str, str]]
    package: str
    package_version: str
    package_integrity: str
    loader_file: str
    loader_sha256: str
    runner_file: str
    runner_sha256: str
    wasm_file: str
    wasm_sha256: str
    license: str
    render_options: dict[str, Any]


@dataclass(frozen=True)
class RendererEvidence:
    platform: str | None = None
    node_version: str | None = None
    node_package: str | None = None
    node_package_version: str | None = None
    node_package_integrity: str | None = None
    node_sha256: str | None = None
    renderer_package: str | None = None
    renderer_package_version: str | None = None
    renderer_package_integrity: str | None = None
    loader_sha256: str | None = None
    runner_sha256: str | None = None
    wasm_sha256: str | None = None


@dataclass(frozen=True)
class RenderResult:
    status: Status
    path: Path | None
    png_bytes: bytes
    sha256: str
    size: tuple[int, int]
    diagnostic: str
    observed: RendererEvidence
    expected: RendererEvidence
    attestation: dict[str, Any] | None


@dataclass(frozen=True)
class OpenedArtifact:
    source: Path
    data: bytes
    sha256: str
    mode: int


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RendererLockError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file() or path.is_symlink():
            raise RendererLockError(f"{path.name} must be a regular, non-symlink file")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except RendererLockError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RendererLockError(f"{path.name} is not valid canonical JSON") from error
    if not isinstance(value, dict):
        raise RendererLockError(f"{path.name} must contain a JSON object")
    return value


def load_renderer_lock(path: Path) -> RendererLock:
    """Load only the exact renderer lock published for acceptance model 1.0.1."""
    value = _load_json(Path(path))
    expected = {
        "lock_version": 1,
        "acceptance_model_version": ACCEPTANCE_MODEL_VERSION,
        "node_version": CANONICAL_NODE_VERSION,
        "node_binaries": CANONICAL_NODE_BINARIES,
        "package": CANONICAL_PACKAGE,
        "package_version": CANONICAL_PACKAGE_VERSION,
        "package_integrity": CANONICAL_NPM_INTEGRITY,
        "loader_file": "node_modules/@resvg/resvg-wasm/index.mjs",
        "loader_sha256": CANONICAL_LOADER_SHA256,
        "runner_file": "scripts/render_svg.mjs",
        "runner_sha256": CANONICAL_RUNNER_SHA256,
        "wasm_file": "node_modules/@resvg/resvg-wasm/index_bg.wasm",
        "wasm_sha256": CANONICAL_WASM_SHA256,
        "license": CANONICAL_LICENSE,
        "render_options": _RENDER_OPTIONS,
    }
    if value != expected:
        differing = sorted(key for key in set(value) | set(expected) if value.get(key) != expected.get(key))
        raise RendererLockError(f"canonical renderer lock mismatch: {', '.join(differing) or 'unknown field'}")
    return RendererLock(
        node_version=value["node_version"],
        node_binaries={key: dict(record) for key, record in value["node_binaries"].items()},
        package=value["package"],
        package_version=value["package_version"],
        package_integrity=value["package_integrity"],
        loader_file=value["loader_file"],
        loader_sha256=value["loader_sha256"],
        runner_file=value["runner_file"],
        runner_sha256=value["runner_sha256"],
        wasm_file=value["wasm_file"],
        wasm_sha256=value["wasm_sha256"],
        license=value["license"],
        render_options=dict(value["render_options"]),
    )


def _platform_key() -> str:
    if os.name != "posix":
        raise RendererLockError("canonical renderer requires a supported POSIX platform")
    machine = platform.machine().lower()
    architecture = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "x64",
        "amd64": "x64",
    }.get(machine)
    system = "darwin" if sys.platform == "darwin" else "linux" if sys.platform.startswith("linux") else None
    key = f"{system}-{architecture}" if system and architecture else ""
    if key not in CANONICAL_NODE_BINARIES:
        raise RendererLockError("canonical Node binary is not pinned for this platform")
    return key


def _safe_open_bytes(
    path: Path,
    label: str,
    maximum: int,
    *,
    executable: bool = False,
) -> OpenedArtifact:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    if not no_follow or not nonblocking:
        raise RendererLockError("safe non-following renderer artifact open is unavailable")
    flags = os.O_RDONLY | no_follow | nonblocking | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    source = Path(os.path.abspath(path))
    try:
        descriptor = os.open(source, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RendererLockError(f"{label} must be a regular, non-symlink file")
        if metadata.st_size < 1 or metadata.st_size > maximum:
            raise RendererLockError(f"{label} has an unsafe size")
        if executable and (
            not metadata.st_mode & stat.S_IXUSR or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RendererLockError(f"{label} is not a safe executable file")
        if not getattr(os, "O_CLOEXEC", 0):
            os.set_inheritable(descriptor, False)
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            raise RendererLockError(f"{label} has an unsafe size")
        return OpenedArtifact(
            source=source,
            data=data,
            sha256=hashlib.sha256(data).hexdigest(),
            mode=stat.S_IMODE(metadata.st_mode),
        )
    except RendererLockError:
        raise
    except OSError as error:
        raise RendererLockError(f"{label} could not be opened safely") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _repository_artifact(relative: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise RendererLockError(f"renderer artifact escapes the repository: {relative}")
    return _REPOSITORY / relative_path


def _verify_install(lock: RendererLock, observed: dict[str, Any]) -> dict[str, OpenedArtifact]:
    package_lock = _load_json(_PACKAGE_LOCK_PATH)
    try:
        root_record = package_lock["packages"][""]
        package_record = package_lock["packages"][f"node_modules/{CANONICAL_PACKAGE}"]
    except (KeyError, TypeError) as error:
        raise RendererLockError("package-lock does not contain the canonical renderer") from error
    if isinstance(package_record, dict):
        observed["renderer_package"] = CANONICAL_PACKAGE
        observed["renderer_package_version"] = package_record.get("version")
        observed["renderer_package_integrity"] = package_record.get("integrity")
    if (
        package_lock.get("lockfileVersion") != 3
        or package_lock.get("requires") is not True
        or not isinstance(root_record, dict)
        or root_record.get("dependencies")
        != {CANONICAL_PACKAGE: CANONICAL_PACKAGE_VERSION, "node": CANONICAL_NODE_VERSION}
        or root_record.get("optionalDependencies")
        != {
            record["package"]: record["package_version"]
            for record in lock.node_binaries.values()
        }
        or root_record.get("engines") != {"node": CANONICAL_NODE_VERSION}
    ):
        raise RendererLockError("package-lock root contract is not canonical")
    expected_record = {
        "version": CANONICAL_PACKAGE_VERSION,
        "integrity": CANONICAL_NPM_INTEGRITY,
        "license": CANONICAL_LICENSE,
    }
    if not isinstance(package_record, dict) or any(
        package_record.get(key) != value for key, value in expected_record.items()
    ):
        raise RendererLockError("package-lock renderer version, integrity, or license mismatch")
    for platform_key, node_record in lock.node_binaries.items():
        locked_node = package_lock["packages"].get(f"node_modules/{node_record['package']}")
        system, architecture = platform_key.split("-", 1)
        if not isinstance(locked_node, dict) or any(
            locked_node.get(key) != value
            for key, value in {
                "version": node_record["package_version"],
                "integrity": node_record["package_integrity"],
                "cpu": architecture,
                "optional": True,
                "os": system,
                "bin": {"node": "bin/node"},
            }.items()
        ):
            raise RendererLockError("package-lock platform Node contract is not canonical")

    runner_source = Path(os.path.abspath(_RUNNER_PATH))
    repository = Path(os.path.abspath(_REPOSITORY))
    if repository not in runner_source.parents:
        raise RendererLockError("canonical renderer runner escapes the repository")
    artifacts = {
        "loader": _safe_open_bytes(
            _repository_artifact(lock.loader_file),
            "renderer loader",
            8 * 1024 * 1024,
        ),
        "wasm": _safe_open_bytes(
            _repository_artifact(lock.wasm_file),
            "renderer WASM",
            64 * 1024 * 1024,
        ),
        "runner": _safe_open_bytes(runner_source, "renderer runner", 1024 * 1024),
    }
    observed["loader_sha256"] = artifacts["loader"].sha256
    observed["wasm_sha256"] = artifacts["wasm"].sha256
    observed["runner_sha256"] = artifacts["runner"].sha256
    mismatches = [
        label
        for label, actual, expected in (
            ("loader", artifacts["loader"].sha256, lock.loader_sha256),
            ("WASM", artifacts["wasm"].sha256, lock.wasm_sha256),
            ("runner", artifacts["runner"].sha256, lock.runner_sha256),
        )
        if actual != expected
    ]
    if mismatches:
        raise RendererLockError(f"installed renderer {'/'.join(mismatches)} hash mismatch")
    return artifacts


def _minimal_environment(node: Path) -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NODE_NO_WARNINGS": "1",
        "PATH": str(node.parent),
        "TZ": "UTC",
    }


def _is_native_bytes(data: bytes) -> bool:
    magic = data[:4]
    return magic in _NATIVE_MAGICS or magic[:2] == b"MZ"


def _node_package_identity(
    node: OpenedArtifact,
    record: dict[str, str],
    observed: dict[str, Any],
) -> None:
    package_root = node.source.parent.parent
    if package_root.name == record["package"]:
        manifest_path = package_root / "package.json"
    else:
        manifest_path = package_root / "node_modules" / record["package"] / "package.json"
    manifest_artifact = _safe_open_bytes(manifest_path, "Node binary package manifest", 128 * 1024)
    try:
        manifest = json.loads(manifest_artifact.data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RendererLockError("Node binary package manifest is invalid") from error
    if not isinstance(manifest, dict):
        raise RendererLockError("Node binary package manifest is invalid")
    name = manifest.get("name")
    version = manifest.get("version")
    normalized_version = version.removeprefix("v") if isinstance(version, str) else version
    observed["node_package"] = name
    observed["node_package_version"] = normalized_version
    if name != record["package"] or normalized_version != record["package_version"]:
        raise RendererLockError("Node binary package identity mismatch")


def resolve_canonical_node(
    lock: RendererLock,
    platform_key: str,
    observed: dict[str, Any] | None = None,
) -> OpenedArtifact:
    """Resolve only the repo-provisioned or explicitly configured lock-matching Node."""
    evidence = observed if observed is not None else {}
    configured = os.environ.get("RECONSTRUCTING_RASTER_ICONS_NODE")
    selected = Path(configured) if configured else _REPOSITORY / "node_modules" / "node" / "bin" / "node"
    node = _safe_open_bytes(
        selected,
        "canonical Node executable",
        256 * 1024 * 1024,
        executable=True,
    )
    evidence["node_sha256"] = node.sha256
    record = lock.node_binaries[platform_key]
    if not _is_native_bytes(node.data):
        raise RendererLockError("canonical Node executable is not a native executable")
    if node.sha256 != record["executable_sha256"]:
        raise RendererLockError("canonical Node executable hash mismatch")
    _node_package_identity(node, record, evidence)
    return node


def _stage_bytes(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow:
        raise RendererLockError("safe private artifact creation is unavailable")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | no_follow | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, mode)
        position = 0
        while position < len(data):
            position += os.write(descriptor, data[position:])
        os.fchmod(descriptor, mode)
    except OSError as error:
        raise RendererLockError(f"private renderer artifact {path.name} could not be staged") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _permission_command(node: Path, read_paths: tuple[Path, ...], write_directory: Path) -> list[str]:
    command = [str(node), "--max-old-space-size=512", "--permission"]
    if any(write_directory not in path.parents for path in read_paths):
        raise RendererLockError("private renderer read allowlist escapes the run directory")
    command.append(f"--allow-fs-read={write_directory}")
    command.append(f"--allow-fs-write={write_directory}")
    return command


def _validate_combined_attestation(
    payload: bytes,
    *,
    nonce: str,
    node: Path,
    candidate: Path,
    run_directory: Path,
    denied_path: Path,
    platform_key: str,
) -> dict[str, Any]:
    if candidate.parent != run_directory or node.parent.parent != run_directory:
        raise RendererLockError("Node renderer attestation paths escape the private run directory")
    if len(payload) > 64 * 1024:
        raise RendererLockError("Node renderer attestation exceeds its output limit")
    try:
        evidence = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RendererLockError("Node renderer returned invalid attestation evidence") from error
    identity_keys = {
        "nonce", "exec_path", "node_version", "release_name", "platform", "architecture",
    }
    failure_keys = identity_keys | {"render_status", "isolation_failure"}
    full_keys = identity_keys | {
        "permission_type", "allowed_read_capability", "denied_read_capability",
        "allowed_write_capability", "child_capability", "worker_capability",
        "filesystem_allowed", "filesystem_denial", "subprocess_denial",
        "render_status", "render_error", "denied_path",
    }
    if not isinstance(evidence, dict):
        raise RendererLockError("Node renderer attestation evidence shape is invalid")
    evidence_keys = frozenset(evidence)
    if evidence_keys not in {frozenset(failure_keys), frozenset(full_keys)}:
        raise RendererLockError("Node renderer attestation evidence shape is invalid")
    expected_platform, expected_architecture = platform_key.split("-", 1)
    identity = {
        "nonce": nonce,
        "exec_path": str(node),
        "node_version": CANONICAL_NODE_VERSION,
        "release_name": "node",
        "platform": expected_platform,
        "architecture": expected_architecture,
    }
    if any(evidence.get(key) != value for key, value in identity.items()):
        raise RendererLockError("Node renderer attestation runtime mismatch")
    if evidence_keys == failure_keys:
        allowed_failures = {
            "permission_type", "allowed_read_capability", "denied_read_capability",
            "allowed_write_capability", "child_capability", "worker_capability",
            "filesystem_allowed", "filesystem_denial", "subprocess_denial", "probe_exception",
        }
        if (
            evidence["render_status"] != "isolation_failure"
            or evidence["isolation_failure"] not in allowed_failures
        ):
            raise RendererLockError("Node renderer isolation failure evidence is invalid")
        return evidence
    exact = {
        **identity,
        "permission_type": "object",
        "allowed_read_capability": True,
        "denied_read_capability": False,
        "allowed_write_capability": True,
        "child_capability": False,
        "worker_capability": False,
        "filesystem_allowed": True,
        "filesystem_denial": "ERR_ACCESS_DENIED",
        "subprocess_denial": "ERR_ACCESS_DENIED",
        "denied_path": str(denied_path),
    }
    if any(evidence.get(key) != value for key, value in exact.items()):
        raise RendererLockError("Node renderer attestation capability mismatch")
    if evidence["render_status"] not in {"ok", "error"}:
        raise RendererLockError("Node renderer attestation has an invalid render status")
    if evidence["render_status"] == "ok" and evidence["render_error"] is not None:
        raise RendererLockError("Node renderer attestation has contradictory render evidence")
    return evidence


def _memory_preexec() -> Any:
    """Return a verified-in-child 512 MiB Unix process-memory limiter."""
    if os.name != "posix":
        raise RendererLockError("canonical renderer memory isolation requires POSIX")
    try:
        import resource

        if sys.platform == "darwin":
            # Darwin aliases RLIMIT_RSS and RLIMIT_AS. RLIMIT_DATA adds a
            # distinct data-segment ceiling; both must be enforceable for a
            # canonical run on that host.
            limit_kinds = (resource.RLIMIT_DATA, resource.RLIMIT_RSS)
        else:
            limit_kinds = (resource.RLIMIT_AS,)
    except (ImportError, AttributeError) as error:
        raise RendererLockError("Unix process memory isolation is unavailable") from error

    def set_limit() -> None:
        exact_limit = (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES)
        for limit_kind in limit_kinds:
            resource.setrlimit(limit_kind, exact_limit)
        for limit_kind in limit_kinds:
            if resource.getrlimit(limit_kind) != exact_limit:
                raise OSError("process memory limit could not be verified")

    return set_limit


def _probe_memory_preexec(preexec: Any) -> None:
    """Prove the pre-exec memory limiter works before starting any Node code."""
    if preexec is None:
        raise RendererLockError("Unix process memory isolation callback is unavailable")
    try:
        completed = subprocess.run(
            ["/usr/bin/true"],
            check=False,
            capture_output=True,
            timeout=5,
            env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin", "TZ": "UTC"},
            preexec_fn=preexec,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RendererLockError("Unix process memory isolation could not be established") from error
    if completed.returncode:
        raise RendererLockError("Unix process memory isolation capability probe failed")


def _failure(
    diagnostic: str,
    size: tuple[int, int],
    observed: dict[str, Any],
    expected: RendererEvidence,
    attestation: dict[str, Any] | None = None,
) -> RenderResult:
    return RenderResult(
        status=Status.NON_CANONICAL,
        path=None,
        png_bytes=b"",
        sha256="",
        size=size,
        diagnostic=diagnostic,
        observed=RendererEvidence(**observed),
        expected=expected,
        attestation=attestation,
    )


def _expected_evidence(platform_key: str | None = None) -> RendererEvidence:
    record = CANONICAL_NODE_BINARIES.get(platform_key or "")
    return RendererEvidence(
        platform=platform_key if record else None,
        node_version=CANONICAL_NODE_VERSION,
        node_package=record["package"] if record else None,
        node_package_version=record["package_version"] if record else None,
        node_package_integrity=record["package_integrity"] if record else None,
        node_sha256=record["executable_sha256"] if record else None,
        renderer_package=CANONICAL_PACKAGE,
        renderer_package_version=CANONICAL_PACKAGE_VERSION,
        renderer_package_integrity=CANONICAL_NPM_INTEGRITY,
        loader_sha256=CANONICAL_LOADER_SHA256,
        runner_sha256=CANONICAL_RUNNER_SHA256,
        wasm_sha256=CANONICAL_WASM_SHA256,
    )


def _validate_png_payload(payload: bytes, size: tuple[int, int]) -> None:
    if not payload or len(payload) > MAX_PNG_BYTES or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RendererLockError("renderer output is not a bounded PNG")
    offset = 8
    saw_header = False
    saw_data = False
    saw_end = False
    while offset < len(payload):
        if len(payload) - offset < 12:
            raise RendererLockError("renderer PNG contains a truncated chunk")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(payload):
            raise RendererLockError("renderer PNG contains an invalid chunk length")
        chunk_data = payload[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", payload[offset + 8 + length : chunk_end])[0]
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
            raise RendererLockError("renderer PNG contains an invalid chunk checksum")
        if not saw_header:
            if chunk_type != b"IHDR" or length != 13:
                raise RendererLockError("renderer PNG does not begin with IHDR")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            if (width, height) != size or (bit_depth, color_type, compression, filtering, interlace) != (8, 6, 0, 0, 0):
                raise RendererLockError("renderer PNG dimensions or RGBA profile mismatch")
            saw_header = True
        elif chunk_type == b"IHDR":
            raise RendererLockError("renderer PNG contains multiple headers")
        if chunk_type == b"IDAT":
            saw_data = True
        if chunk_type == b"IEND":
            if length != 0 or chunk_end != len(payload):
                raise RendererLockError("renderer PNG contains trailing or malformed payload")
            saw_end = True
        offset = chunk_end
    if not (saw_header and saw_data and saw_end):
        raise RendererLockError("renderer PNG is incomplete")
    try:
        with Image.open(BytesIO(payload)) as image:
            if image.format != "PNG" or image.size != size or image.mode != "RGBA":
                raise RendererLockError("renderer output dimensions or mode mismatch")
            image.verify()
        with Image.open(BytesIO(payload)) as image:
            image.load()
    except RendererLockError:
        raise
    except (OSError, ValueError, UnidentifiedImageError) as error:
        raise RendererLockError("renderer PNG decoder verification failed") from error


def _validate_size(size: tuple[int, int]) -> tuple[int, int]:
    if (
        not isinstance(size, tuple)
        or len(size) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in size)
    ):
        raise ValueError("canonical render size must be a pair of integers")
    width, height = size
    if min(width, height) < MIN_CANONICAL_SIDE or max(width, height) > MAX_CANONICAL_SIDE:
        raise ValueError("canonical render dimensions must be between 64 and 1024 pixels")
    return width, height


def render_canonical(svg: SafeSvgDocument, size: tuple[int, int], workspace: Path) -> RenderResult:
    """Render validated SVG bytes only when every canonicality check succeeds."""
    dimensions = _validate_size(size)
    if not isinstance(svg, SafeSvgDocument):
        raise TypeError("svg must be a SafeSvgDocument returned by validate_svg")
    observed: dict[str, Any] = {}
    expected = _expected_evidence()
    attestation: dict[str, Any] | None = None
    run_directory: Path | None = None
    retain_run_directory = False
    try:
        platform_key = _platform_key()
        observed["platform"] = platform_key
        expected = _expected_evidence(platform_key)
        lock = load_renderer_lock(_LOCK_PATH)
        artifacts = _verify_install(lock, observed)
        node_source = resolve_canonical_node(lock, platform_key, observed)
        preexec = _memory_preexec()
        _probe_memory_preexec(preexec)
        workspace_path = Path(workspace)
        if workspace_path.is_symlink():
            raise RendererLockError("renderer workspace cannot be a symlink")
        workspace_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not workspace_path.is_dir():
            raise RendererLockError("renderer workspace is not a directory")
        workspace_path = workspace_path.resolve(strict=True)
        run_directory = workspace_path / f".canonical-render-{secrets.token_hex(16)}"
        run_directory.mkdir(mode=0o700)
        os.chmod(run_directory, stat.S_IRWXU)
        node = run_directory / "bin" / "node"
        runner = run_directory / lock.runner_file
        loader = run_directory / lock.loader_file
        wasm = run_directory / lock.wasm_file
        candidate = run_directory / "candidate.svg"
        output = run_directory / "render.png"
        _stage_bytes(node, node_source.data, 0o500)
        _stage_bytes(runner, artifacts["runner"].data, 0o500)
        _stage_bytes(loader, artifacts["loader"].data, 0o400)
        _stage_bytes(wasm, artifacts["wasm"].data, 0o400)
        _stage_bytes(candidate, svg.xml_bytes, 0o400)
        read_paths = (runner, loader, wasm, candidate)
        nonce = secrets.token_hex(32)
        denied_path = _REPOSITORY / "package.json"
        command = _permission_command(node, read_paths, run_directory)
        command.extend(
            [
                str(runner),
                str(candidate),
                str(output),
                str(wasm),
                str(dimensions[0]),
                str(dimensions[1]),
                nonce,
                str(denied_path),
            ]
        )
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=RENDER_TIMEOUT_SECONDS,
            env=_minimal_environment(node),
            cwd=run_directory,
            preexec_fn=preexec,
            start_new_session=True,
        )
        attestation = _validate_combined_attestation(
            completed.stdout,
            nonce=nonce,
            node=node,
            candidate=candidate,
            run_directory=run_directory,
            denied_path=denied_path,
            platform_key=platform_key,
        )
        observed["node_version"] = attestation["node_version"]
        attestation["executable_magic"] = node_source.data[:4].hex()
        attestation["executable_mode"] = oct(0o500)
        if completed.returncode or attestation["render_status"] != "ok" or completed.stderr:
            diagnostic = (
                attestation.get("isolation_failure")
                or attestation.get("render_error")
                or completed.stderr.decode("utf-8", "replace").strip()
            )
            return _failure(diagnostic or "canonical renderer failed", dimensions, observed, expected, attestation)
        png_artifact = _safe_open_bytes(output, "renderer PNG", MAX_PNG_BYTES)
        png_bytes = png_artifact.data
        _validate_png_payload(png_bytes, dimensions)
        retain_run_directory = True
        return RenderResult(
            status=Status.ACCEPTED,
            path=output,
            png_bytes=png_bytes,
            sha256=hashlib.sha256(png_bytes).hexdigest(),
            size=dimensions,
            diagnostic="",
            observed=RendererEvidence(**observed),
            expected=expected,
            attestation=attestation,
        )
    except RendererLockError as error:
        return _failure(str(error), dimensions, observed, expected, attestation)
    except subprocess.TimeoutExpired:
        return _failure("canonical renderer exceeded the 15 second timeout", dimensions, observed, expected, attestation)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return _failure(
            f"canonical isolation could not be established: {error}",
            dimensions,
            observed,
            expected,
            attestation,
        )
    finally:
        if run_directory is not None and not retain_run_directory:
            shutil.rmtree(run_directory, ignore_errors=True)
