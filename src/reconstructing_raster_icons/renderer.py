"""Hash-pinned canonical SVG rendering through the isolated resvg WASM runner."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
from typing import Any

from .constants import ACCEPTANCE_MODEL_VERSION, Status
from .safe_svg import SafeSvgDocument


CANONICAL_NODE_VERSION = "22.14.0"
CANONICAL_PACKAGE = "@resvg/resvg-wasm"
CANONICAL_PACKAGE_VERSION = "2.6.2"
CANONICAL_NPM_INTEGRITY = "sha512-FqALmHI8D4o6lk/LRWDnhw95z5eO+eAa6ORjVg09YRR7BkcM6oPHU9uyC0gtQG5vpFLvgpeU4+zEAz2H8APHNw=="
CANONICAL_WASM_SHA256 = "22bf6e9f9a100d972da0411a69c5ba504367fc1fa87b3b64e3f35e53926d2d70"
CANONICAL_LOADER_SHA256 = "10170d02d816f02ec76f9bc095b01d9becf536e7b1e12e5aa616652c84b237a1"
CANONICAL_LICENSE = "MPL-2.0"
RENDER_TIMEOUT_SECONDS = 15
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
MAX_CANONICAL_SIDE = 1024
MIN_CANONICAL_SIDE = 64

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


def _verify_install(lock: RendererLock) -> tuple[Path, Path]:
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
    if _sha256(loader) != lock.loader_sha256:
        raise RendererLockError("installed renderer loader hash mismatch")
    if _sha256(wasm) != lock.wasm_sha256:
        raise RendererLockError("installed renderer WASM hash mismatch")
    if not _RUNNER_PATH.is_file() or _RUNNER_PATH.is_symlink():
        raise RendererLockError("canonical renderer runner is missing or unsafe")
    return loader, wasm


def _minimal_environment(node: Path) -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NODE_NO_WARNINGS": "1",
        "PATH": str(node.parent),
        "TZ": "UTC",
    }


def _node_binary() -> Path:
    configured = os.environ.get("RECONSTRUCTING_RASTER_ICONS_NODE")
    selected = configured or shutil.which("node")
    if not selected:
        raise RendererLockError("canonical Node executable is unavailable")
    try:
        node = Path(selected).resolve(strict=True)
    except OSError as error:
        raise RendererLockError("canonical Node executable cannot be resolved") from error
    if not node.is_file() or node.is_symlink() or not os.access(node, os.X_OK):
        raise RendererLockError("canonical Node executable is not a safe executable file")
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


def _failure(diagnostic: str, size: tuple[int, int], lock: RendererLock | None = None) -> RenderResult:
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
    )


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
    try:
        lock = load_renderer_lock(_LOCK_PATH)
        loader, wasm = _verify_install(lock)
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
        read_paths = (_RUNNER_PATH.resolve(), loader, wasm, candidate)
        command = [str(node), "--max-old-space-size=512", "--permission"]
        command.extend(f"--allow-fs-read={path}" for path in read_paths)
        command.append(f"--allow-fs-write={run_directory}")
        command.extend(
            [
                str(_RUNNER_PATH.resolve()),
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
            return _failure(diagnostic, dimensions, lock)
        if not output.is_file() or output.is_symlink():
            return _failure("canonical renderer did not create a safe PNG", dimensions, lock)
        png_bytes = output.read_bytes()
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
        )
    except RendererLockError as error:
        return _failure(str(error), dimensions, lock)
    except subprocess.TimeoutExpired:
        return _failure("canonical renderer exceeded the 15 second timeout", dimensions, lock)
    except (OSError, ValueError) as error:
        return _failure(f"canonical isolation could not be established: {error}", dimensions, lock)
