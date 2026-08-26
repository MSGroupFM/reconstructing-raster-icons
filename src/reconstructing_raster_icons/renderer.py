"""Hash-pinned canonical SVG rendering through the isolated resvg WASM runner."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
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
CANONICAL_PACKAGE = "@resvg/resvg-wasm"
CANONICAL_PACKAGE_VERSION = "2.6.2"
CANONICAL_NPM_INTEGRITY = "sha512-FqALmHI8D4o6lk/LRWDnhw95z5eO+eAa6ORjVg09YRR7BkcM6oPHU9uyC0gtQG5vpFLvgpeU4+zEAz2H8APHNw=="
CANONICAL_WASM_SHA256 = "22bf6e9f9a100d972da0411a69c5ba504367fc1fa87b3b64e3f35e53926d2d70"
CANONICAL_LOADER_SHA256 = "10170d02d816f02ec76f9bc095b01d9becf536e7b1e12e5aa616652c84b237a1"
CANONICAL_RUNNER_SHA256 = "12c6d7f3d44702be7070913097dcb138959a9511f99c20dc51710f5b272525da"
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
class RenderResult:
    status: Status
    path: Path | None
    png_bytes: bytes
    sha256: str
    size: tuple[int, int]
    diagnostic: str
    node_version: str
    package_integrity: str
    wasm_sha256: str
    loader_sha256: str
    runner_sha256: str
    attestation: dict[str, Any] | None


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
    """Load only the exact renderer lock published for acceptance model 1.0.0."""
    value = _load_json(Path(path))
    expected = {
        "lock_version": 1,
        "acceptance_model_version": ACCEPTANCE_MODEL_VERSION,
        "node_version": CANONICAL_NODE_VERSION,
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise RendererLockError(f"cannot hash renderer artifact {path.name}") from error
    return digest.hexdigest()


def _artifact_path(relative: str) -> Path:
    try:
        path = (_REPOSITORY / relative).resolve(strict=True)
        repository = _REPOSITORY.resolve(strict=True)
    except OSError as error:
        raise RendererLockError(f"renderer artifact is missing: {relative}") from error
    if repository not in path.parents or not path.is_file() or path.is_symlink():
        raise RendererLockError(f"renderer artifact escapes the repository: {relative}")
    return path


def _verify_install(lock: RendererLock) -> tuple[Path, Path, Path]:
    package_lock = _load_json(_PACKAGE_LOCK_PATH)
    try:
        root_record = package_lock["packages"][""]
        package_record = package_lock["packages"][f"node_modules/{CANONICAL_PACKAGE}"]
    except (KeyError, TypeError) as error:
        raise RendererLockError("package-lock does not contain the canonical renderer") from error
    if (
        package_lock.get("lockfileVersion") != 3
        or package_lock.get("requires") is not True
        or not isinstance(root_record, dict)
        or root_record.get("dependencies") != {CANONICAL_PACKAGE: CANONICAL_PACKAGE_VERSION}
        or root_record.get("engines") != {"node": CANONICAL_NODE_VERSION}
    ):
        raise RendererLockError("package-lock root contract is not canonical")
    expected_record = {
        "version": CANONICAL_PACKAGE_VERSION,
        "integrity": CANONICAL_NPM_INTEGRITY,
        "license": CANONICAL_LICENSE,
    }
    if not isinstance(package_record, dict) or any(package_record.get(key) != value for key, value in expected_record.items()):
        raise RendererLockError("package-lock renderer version, integrity, or license mismatch")
    loader = _artifact_path(lock.loader_file)
    wasm = _artifact_path(lock.wasm_file)
    try:
        runner = _RUNNER_PATH.resolve(strict=True)
        repository = _REPOSITORY.resolve(strict=True)
    except OSError as error:
        raise RendererLockError("canonical renderer runner is missing") from error
    if repository not in runner.parents or not runner.is_file() or _RUNNER_PATH.is_symlink():
        raise RendererLockError("canonical renderer runner is missing or unsafe")
    if _sha256(loader) != lock.loader_sha256:
        raise RendererLockError("installed renderer loader hash mismatch")
    if _sha256(wasm) != lock.wasm_sha256:
        raise RendererLockError("installed renderer WASM hash mismatch")
    if _sha256(runner) != lock.runner_sha256:
        raise RendererLockError("canonical renderer runner hash mismatch")
    return loader, wasm, runner


def _minimal_environment(node: Path) -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NODE_NO_WARNINGS": "1",
        "PATH": str(node.parent),
        "TZ": "UTC",
    }


def _native_magic(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            return stream.read(4)
    except OSError as error:
        raise RendererLockError("canonical Node executable cannot be inspected") from error


def _is_native_executable(path: Path) -> bool:
    magic = _native_magic(path)
    return magic in _NATIVE_MAGICS or magic[:2] == b"MZ"


def _node_binary() -> Path:
    configured = os.environ.get("RECONSTRUCTING_RASTER_ICONS_NODE")
    selected = configured or shutil.which("node")
    if not selected:
        raise RendererLockError("canonical Node executable is unavailable")
    selected_path = Path(selected)
    if not selected_path.is_absolute():
        selected_path = Path.cwd() / selected_path
    try:
        selected_stat = selected_path.lstat()
    except OSError as error:
        raise RendererLockError("canonical Node executable cannot be resolved") from error
    if stat.S_ISLNK(selected_stat.st_mode) or not stat.S_ISREG(selected_stat.st_mode):
        raise RendererLockError("canonical Node executable is not a safe executable file")
    if selected_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RendererLockError("canonical Node executable is group or world writable")
    if not os.access(selected_path, os.X_OK) or not _is_native_executable(selected_path):
        raise RendererLockError("canonical Node executable is not a native executable")
    try:
        node = selected_path.resolve(strict=True)
    except OSError as error:
        raise RendererLockError("canonical Node executable cannot be resolved") from error
    try:
        completed = subprocess.run(
            [str(node), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=_minimal_environment(node),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RendererLockError("canonical Node runtime could not be verified") from error
    actual = completed.stdout.strip()
    if completed.returncode or actual != f"v{CANONICAL_NODE_VERSION}":
        raise RendererLockError(f"Node {CANONICAL_NODE_VERSION} is required; found {actual or 'unknown'}")
    return node


def _permission_command(node: Path, read_paths: tuple[Path, ...], write_directory: Path) -> list[str]:
    command = [str(node), "--max-old-space-size=512", "--permission"]
    command.extend(f"--allow-fs-read={path}" for path in read_paths)
    command.append(f"--allow-fs-write={write_directory}")
    return command


def _attest_node(
    node: Path,
    read_paths: tuple[Path, ...],
    write_directory: Path,
    candidate: Path,
    preexec: Any,
) -> dict[str, Any]:
    nonce = secrets.token_hex(32)
    denied_path = _REPOSITORY / "package.json"
    script = f"""
import {{ readFileSync }} from "node:fs";
import {{ spawnSync }} from "node:child_process";
import net from "node:net";
const allowedPath = {json.dumps(str(candidate))};
const deniedPath = {json.dumps(str(denied_path))};
const writeDirectory = {json.dumps(str(write_directory))};
const evidence = {{
  nonce: {json.dumps(nonce)},
  exec_path: process.execPath,
  node_version: process.versions.node,
  release_name: process.release?.name,
  permission_type: typeof process.permission,
  allowed_read_capability: process.permission?.has("fs.read", allowedPath),
  denied_read_capability: process.permission?.has("fs.read", deniedPath),
  allowed_write_capability: process.permission?.has("fs.write", writeDirectory),
  child_capability: process.permission?.has("child"),
  worker_capability: process.permission?.has("worker"),
  network_capability: process.permission?.has("net"),
}};
try {{ readFileSync(allowedPath); evidence.filesystem_allowed = true; }}
catch (error) {{ evidence.filesystem_allowed = error?.code ?? "UNKNOWN"; }}
try {{ readFileSync(deniedPath); evidence.filesystem_denial = "ALLOWED"; }}
catch (error) {{ evidence.filesystem_denial = error?.code ?? "UNKNOWN"; }}
try {{ spawnSync(process.execPath, ["--version"]); evidence.subprocess_denial = "ALLOWED"; }}
catch (error) {{ evidence.subprocess_denial = error?.code ?? "UNKNOWN"; }}
evidence.network_denial = await new Promise((resolve) => {{
  let settled = false;
  let socket;
  let timer;
  const finish = (value) => {{
    if (!settled) {{ settled = true; clearTimeout(timer); socket?.destroy(); resolve(value); }}
  }};
  try {{
    socket = net.connect({{ host: "127.0.0.1", port: 1 }});
    socket.once("connect", () => finish("ALLOWED"));
    socket.once("error", (error) => finish(error?.code ?? "UNKNOWN"));
    timer = setTimeout(() => finish("TIMEOUT"), 1000);
  }} catch (error) {{ resolve(error?.code ?? "UNKNOWN"); }}
}});
console.log(JSON.stringify(evidence));
"""
    command = _permission_command(node, read_paths, write_directory)
    command.extend(["--input-type=module", "--eval", script])
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            timeout=5,
            env=_minimal_environment(node),
            cwd=write_directory,
            preexec_fn=preexec,
            start_new_session=True,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RendererLockError("Node permission attestation could not run") from error
    if completed.returncode or completed.stderr:
        raise RendererLockError("Node permission attestation did not complete cleanly")
    try:
        evidence = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RendererLockError("Node permission attestation returned invalid evidence") from error
    expected_keys = {
        "nonce",
        "exec_path",
        "node_version",
        "release_name",
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
    }
    if not isinstance(evidence, dict) or set(evidence) != expected_keys:
        raise RendererLockError("Node permission attestation evidence shape is invalid")
    exact = {
        "nonce": nonce,
        "exec_path": str(node),
        "node_version": CANONICAL_NODE_VERSION,
        "release_name": "node",
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
    }
    if any(evidence.get(key) != value for key, value in exact.items()):
        raise RendererLockError("Node permission attestation capability mismatch")
    if evidence["network_denial"] not in {"EPERM", "EACCES", "ERR_ACCESS_DENIED"}:
        raise RendererLockError("Node permission attestation did not deny network access")
    evidence["executable_magic"] = _native_magic(node).hex()
    evidence["executable_mode"] = oct(stat.S_IMODE(node.stat().st_mode))
    return evidence


def _memory_preexec() -> Any:
    """Return an enforceable Unix limiter where the platform implements RLIMIT_AS."""
    if os.name != "posix" or sys.platform == "darwin":
        # Darwin exposes RLIMIT_AS but rejects finite values with EINVAL. V8's
        # hard old-space cap remains active there; Linux receives the process cap.
        return None
    try:
        import resource

        limit_kind = resource.RLIMIT_AS
    except (ImportError, AttributeError) as error:
        raise RendererLockError("Unix process memory isolation is unavailable") from error

    def set_limit() -> None:
        resource.setrlimit(limit_kind, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))

    return set_limit


def _failure(
    diagnostic: str,
    size: tuple[int, int],
    lock: RendererLock | None = None,
    attestation: dict[str, Any] | None = None,
) -> RenderResult:
    return RenderResult(
        status=Status.NON_CANONICAL,
        path=None,
        png_bytes=b"",
        sha256="",
        size=size,
        diagnostic=diagnostic,
        node_version=CANONICAL_NODE_VERSION,
        package_integrity=lock.package_integrity if lock else CANONICAL_NPM_INTEGRITY,
        wasm_sha256=lock.wasm_sha256 if lock else CANONICAL_WASM_SHA256,
        loader_sha256=lock.loader_sha256 if lock else CANONICAL_LOADER_SHA256,
        runner_sha256=lock.runner_sha256 if lock else CANONICAL_RUNNER_SHA256,
        attestation=attestation,
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
    lock: RendererLock | None = None
    attestation: dict[str, Any] | None = None
    try:
        lock = load_renderer_lock(_LOCK_PATH)
        loader, wasm, runner = _verify_install(lock)
        node = _node_binary()
        preexec = _memory_preexec()
        workspace_path = Path(workspace)
        if workspace_path.is_symlink():
            raise RendererLockError("renderer workspace cannot be a symlink")
        workspace_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not workspace_path.is_dir():
            raise RendererLockError("renderer workspace is not a directory")
        run_directory = workspace_path / f".canonical-render-{secrets.token_hex(16)}"
        run_directory.mkdir(mode=0o700)
        os.chmod(run_directory, stat.S_IRWXU)
        candidate = run_directory / "candidate.svg"
        output = run_directory / "render.png"
        candidate.write_bytes(svg.xml_bytes)
        os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR)
        read_paths = (runner, loader, wasm, candidate)
        attestation = _attest_node(node, read_paths, run_directory, candidate, preexec)
        command = _permission_command(node, read_paths, run_directory)
        command.extend(
            [
                str(runner),
                str(candidate),
                str(output),
                str(wasm),
                str(dimensions[0]),
                str(dimensions[1]),
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
        if completed.returncode:
            diagnostic = completed.stderr.decode("utf-8", "replace").strip() or "canonical renderer failed"
            return _failure(diagnostic, dimensions, lock, attestation)
        if not output.is_file() or output.is_symlink():
            return _failure("canonical renderer did not create a safe PNG", dimensions, lock, attestation)
        if output.stat().st_size > MAX_PNG_BYTES:
            return _failure("canonical renderer PNG exceeds the output limit", dimensions, lock, attestation)
        png_bytes = output.read_bytes()
        _validate_png_payload(png_bytes, dimensions)
        return RenderResult(
            status=Status.ACCEPTED,
            path=output,
            png_bytes=png_bytes,
            sha256=hashlib.sha256(png_bytes).hexdigest(),
            size=dimensions,
            diagnostic="",
            node_version=lock.node_version,
            package_integrity=lock.package_integrity,
            wasm_sha256=lock.wasm_sha256,
            loader_sha256=lock.loader_sha256,
            runner_sha256=lock.runner_sha256,
            attestation=attestation,
        )
    except RendererLockError as error:
        return _failure(str(error), dimensions, lock, attestation)
    except subprocess.TimeoutExpired:
        return _failure("canonical renderer exceeded the 15 second timeout", dimensions, lock, attestation)
    except (OSError, ValueError) as error:
        return _failure(f"canonical isolation could not be established: {error}", dimensions, lock, attestation)
