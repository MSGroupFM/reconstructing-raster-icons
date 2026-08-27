#!/usr/bin/env python3
"""Build and revalidate a deterministic, manifest-scoped source release ZIP."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile


ARCHIVE_PREFIX = "reconstructing-raster-icons-v"
MANIFEST_NAME = "release-manifest.txt"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
MAX_SOURCE_FILE_SIZE = 64 * 1024 * 1024
MAX_RELEASE_SIZE = 512 * 1024 * 1024
READ_CHUNK_SIZE = 1024 * 1024
ALLOWED_ROOT_FILES = frozenset(
    {
        ".gitignore",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "NOTICE",
        "README.md",
        "SECURITY.md",
        "SKILL.md",
        "THIRD_PARTY_NOTICES.md",
        "canonical-renderer.lock",
        "package-lock.json",
        "package.json",
        "pyproject.toml",
        MANIFEST_NAME,
        "requirements-lock.txt",
        "requirements.txt",
    }
)
ALLOWED_ROOT_DIRECTORIES = frozenset(
    {".github", "agents", "docs", "references", "schemas", "scripts", "src", "tests"}
)
FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".idea",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "diagnostics",
        "dist",
        "node_modules",
        "run-workspaces",
    }
)
REQUIRED_RELEASE_FILES = frozenset(
    {
        ".github/workflows/ci.yml",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "NOTICE",
        "README.md",
        "SECURITY.md",
        "SKILL.md",
        "THIRD_PARTY_NOTICES.md",
        "agents/openai.yaml",
        "canonical-renderer.lock",
        "docs/provenance.md",
        "docs/releases/v0.1.0.md",
        "package-lock.json",
        "package.json",
        "pyproject.toml",
        MANIFEST_NAME,
        "references/acceptance-model.md",
        "references/reconstruction-workflow.md",
        "references/security-and-rendering.md",
        "references/vtracer-workflow.md",
        "requirements-lock.txt",
        "requirements.txt",
        "schemas/acceptance-report.schema.json",
        "scripts/build_release.py",
        "scripts/evaluate_icon.py",
        "scripts/finalize_review.py",
        "scripts/prepare_reference.py",
        "scripts/render_svg.mjs",
        "scripts/validate_schemas.py",
        "scripts/validate_skill.py",
        "src/reconstructing_raster_icons/__init__.py",
        "tests/goldens/acceptance-model-1.0.0.json",
        "tests/test_release_package.py",
        "tests/test_vtracer_workflow_contract.py",
    }
)


class ReleaseBuildError(RuntimeError):
    """Raised when a source, archive, or output violates the release contract."""


@dataclass(frozen=True, order=True)
class ReleaseEntry:
    """An immutable file snapshot ready for deterministic ZIP serialization."""

    relative: str
    data: bytes
    mode: int


def _directory_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _file_read_flags() -> int:
    flags = os.O_RDONLY | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _file_create_flags() -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _require_descriptor_primitives() -> None:
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    missing = [name for name in required if not hasattr(os, name)]
    if missing or os.open not in os.supports_dir_fd:
        details = ", ".join(missing or ("descriptor-relative os.open",))
        raise ReleaseBuildError(f"platform lacks required safe filesystem primitives: {details}")


def _absolute_path(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return Path(os.path.abspath(os.fspath(candidate)))


def _open_root_directory(path: Path, label: str) -> tuple[Path, int]:
    _require_descriptor_primitives()
    candidate = _absolute_path(path)
    if candidate == Path(candidate.anchor):
        raise ReleaseBuildError(f"filesystem root is not a valid {label}")
    try:
        descriptor = os.open(candidate, _directory_flags())
    except OSError as error:
        raise ReleaseBuildError(f"{label} is unavailable or unsafe: {candidate}") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise ReleaseBuildError(f"{label} must be a directory")
    return candidate, descriptor


def resolve_source_root(source: Path) -> Path:
    """Return an absolute source path after a no-follow directory open."""

    candidate, descriptor = _open_root_directory(source, "source root")
    os.close(descriptor)
    return candidate


def _same_or_nested(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((os.fspath(path), os.fspath(root))) == os.fspath(root)
    except ValueError:
        return False


def validate_output_root(source: Path, output: Path) -> Path:
    """Apply the lexical separation policy before opening the output directory."""

    source_absolute = _absolute_path(source)
    output_absolute = _absolute_path(output)
    if (
        output_absolute == Path(output_absolute.anchor)
        or _same_or_nested(output_absolute, source_absolute)
        or _same_or_nested(source_absolute, output_absolute)
    ):
        raise ReleaseBuildError("output root must be separate from the source tree")
    return output_absolute


def _descriptor_is_within(ancestor: os.stat_result, child_fd: int) -> bool:
    """Compare an opened directory to a child and each opened parent directory."""

    current = os.dup(child_fd)
    try:
        while True:
            metadata = os.fstat(current)
            if (metadata.st_dev, metadata.st_ino) == (ancestor.st_dev, ancestor.st_ino):
                return True
            parent = os.open("..", _directory_flags(), dir_fd=current)
            parent_metadata = os.fstat(parent)
            if (parent_metadata.st_dev, parent_metadata.st_ino) == (
                metadata.st_dev,
                metadata.st_ino,
            ):
                os.close(parent)
                return False
            os.close(current)
            current = parent
    finally:
        os.close(current)


def _validate_open_root_separation(source_fd: int, output_fd: int) -> None:
    if _descriptor_is_within(os.fstat(source_fd), output_fd) or _descriptor_is_within(
        os.fstat(output_fd), source_fd
    ):
        raise ReleaseBuildError("opened output root must be separate from the source tree")


def _safe_relative_path(name: str, context: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name or "\r" in name:
        raise ReleaseBuildError(f"unsafe {context} name: {name!r}")
    if name.startswith("/") or name.endswith("/") or "//" in name:
        raise ReleaseBuildError(f"unsafe {context} path: {name}")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ReleaseBuildError(f"unsafe {context} path: {name}")
    if ":" in parts[0]:
        raise ReleaseBuildError(f"unsafe {context} drive path: {name}")
    pure = PurePosixPath(*parts)
    if pure.as_posix() != name:
        raise ReleaseBuildError(f"non-canonical {context} path: {name}")
    return pure


def _collision_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _validate_allowed_manifest_path(path: PurePosixPath) -> None:
    if FORBIDDEN_PARTS.intersection(path.parts):
        raise ReleaseBuildError(f"manifest path uses a forbidden directory: {path}")
    if path.parts[:3] == ("tests", "behavioral", "evidence"):
        raise ReleaseBuildError(f"raw behavioral evidence is not releasable: {path}")
    if len(path.parts) == 1:
        if path.name not in ALLOWED_ROOT_FILES:
            raise ReleaseBuildError(f"manifest path is outside the root-file policy: {path}")
    elif path.parts[0] not in ALLOWED_ROOT_DIRECTORIES:
        raise ReleaseBuildError(f"manifest path is outside the directory policy: {path}")


def parse_release_manifest(payload: bytes) -> tuple[str, ...]:
    """Decode and validate the exact sorted allowlist used for packaging."""

    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ReleaseBuildError("release manifest must be valid UTF-8") from error
    if not text.endswith("\n"):
        raise ReleaseBuildError("release manifest must end with one LF")
    names = text[:-1].split("\n")
    if not names or any(not name for name in names):
        raise ReleaseBuildError("release manifest contains an empty entry")
    if names != sorted(names):
        raise ReleaseBuildError("release manifest entries must be sorted")
    if len(names) != len(set(names)):
        raise ReleaseBuildError("release manifest contains duplicate paths")

    collision_keys: dict[str, str] = {}
    for name in names:
        pure = _safe_relative_path(name, "manifest")
        _validate_allowed_manifest_path(pure)
        key = _collision_key(name)
        previous = collision_keys.get(key)
        if previous is not None:
            raise ReleaseBuildError(f"release manifest path collision: {previous!r} and {name!r}")
        collision_keys[key] = name

    missing = sorted(REQUIRED_RELEASE_FILES - set(names))
    if missing:
        raise ReleaseBuildError(f"release manifest is incomplete: {', '.join(missing)}")
    if names.count(MANIFEST_NAME) != 1:
        raise ReleaseBuildError("release manifest must include itself exactly once")
    return tuple(names)


def _open_directory_at(parent_fd: int, name: str, context: str) -> int:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError as error:
        raise ReleaseBuildError(f"unsafe or unavailable {context} directory: {name}") from error
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ReleaseBuildError(f"{context} entry is not a directory: {name}")
    return descriptor


def _read_regular_file_at(root_fd: int, relative: str) -> bytes:
    """Read a bounded regular file through a no-follow descriptor walk."""

    pure = _safe_relative_path(relative, "source")
    directory = os.dup(root_fd)
    try:
        for component in pure.parts[:-1]:
            next_directory = _open_directory_at(directory, component, f"source {relative}")
            os.close(directory)
            directory = next_directory
        try:
            descriptor = os.open(pure.name, _file_read_flags(), dir_fd=directory)
        except OSError as error:
            raise ReleaseBuildError(f"could not safely open source entry: {relative}") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ReleaseBuildError(f"release entry is not a regular file: {relative}")
            if metadata.st_nlink != 1:
                raise ReleaseBuildError(f"release entry must have exactly one link: {relative}")
            if metadata.st_size > MAX_SOURCE_FILE_SIZE:
                raise ReleaseBuildError(f"release entry exceeds the size limit: {relative}")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(READ_CHUNK_SIZE, MAX_SOURCE_FILE_SIZE + 1 - total))
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_SOURCE_FILE_SIZE:
                    raise ReleaseBuildError(f"release entry exceeds the size limit: {relative}")
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def _collect_entries_from_fd(source_fd: int) -> tuple[ReleaseEntry, ...]:
    manifest = _read_regular_file_at(source_fd, MANIFEST_NAME)
    names = parse_release_manifest(manifest)
    entries: list[ReleaseEntry] = []
    total_size = 0
    for relative in names:
        data = manifest if relative == MANIFEST_NAME else _read_regular_file_at(source_fd, relative)
        total_size += len(data)
        if total_size > MAX_RELEASE_SIZE:
            raise ReleaseBuildError("release payload exceeds the total size limit")
        mode = 0o755 if relative.startswith("scripts/") else 0o644
        entries.append(ReleaseEntry(relative=relative, data=data, mode=mode))
    return tuple(entries)


def collect_entries(source: Path) -> tuple[ReleaseEntry, ...]:
    """Snapshot only files declared in the committed release manifest."""

    _, source_fd = _open_root_directory(source, "source root")
    try:
        return _collect_entries_from_fd(source_fd)
    finally:
        os.close(source_fd)


def reject_absolute_leaks(entries: tuple[ReleaseEntry, ...], source: Path) -> None:
    """Reject machine-local source roots and common home-directory path leaks."""

    markers = (
        os.fspath(source).encode("utf-8"),
        b"/" + b"Users/",
        b"/" + b"home/",
        b":\\" + b"Users\\",
    )
    for entry in entries:
        for marker in markers:
            if marker and marker in entry.data:
                raise ReleaseBuildError(f"absolute path leak in release entry: {entry.relative}")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise ReleaseBuildError("short write while creating release artifact")
        view = view[written:]


def _create_transaction_file(output_fd: int, label: str) -> tuple[str, int]:
    for _ in range(32):
        name = f".release-{label}-{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(name, _file_create_flags(), 0o600, dir_fd=output_fd)
        except FileExistsError:
            continue
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            os.close(descriptor)
            raise ReleaseBuildError("transaction output is not a private regular file")
        return name, descriptor
    raise ReleaseBuildError("could not allocate a unique transaction output")


def _write_zip_fd(descriptor: int, entries: tuple[ReleaseEntry, ...]) -> None:
    with os.fdopen(os.dup(descriptor), "w+b") as stream:
        with zipfile.ZipFile(
            stream,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            for entry in entries:
                info = zipfile.ZipInfo(entry.relative, date_time=FIXED_ZIP_TIME)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | entry.mode) << 16
                archive.writestr(info, entry.data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        stream.flush()
        os.fsync(stream.fileno())
    os.fchmod(descriptor, 0o644)
    os.fsync(descriptor)


def _hash_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, READ_CHUNK_SIZE)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _safe_member_path(name: str) -> PurePosixPath:
    return _safe_relative_path(name, "ZIP member")


def _validated_zip_infos(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    infos = archive.infolist()
    seen: set[str] = set()
    collisions: dict[str, str] = {}
    total_size = 0
    for info in infos:
        _safe_member_path(info.filename)
        if info.filename in seen:
            raise ReleaseBuildError(f"duplicate ZIP member: {info.filename}")
        seen.add(info.filename)
        key = _collision_key(info.filename)
        previous = collisions.get(key)
        if previous is not None:
            raise ReleaseBuildError(f"ZIP member path collision: {previous!r} and {info.filename!r}")
        collisions[key] = info.filename
        member_mode = info.external_attr >> 16
        if info.is_dir() or stat.S_ISLNK(member_mode) or not stat.S_ISREG(member_mode):
            raise ReleaseBuildError(f"ZIP member is not a regular file: {info.filename}")
        if stat.S_IMODE(member_mode) not in {0o644, 0o755}:
            raise ReleaseBuildError(f"ZIP member has an unsupported mode: {info.filename}")
        if info.file_size > MAX_SOURCE_FILE_SIZE:
            raise ReleaseBuildError(f"ZIP member exceeds the size limit: {info.filename}")
        total_size += info.file_size
        if total_size > MAX_RELEASE_SIZE:
            raise ReleaseBuildError("ZIP payload exceeds the total size limit")
    return tuple(infos)


def _open_or_create_directory_at(parent_fd: int, name: str, context: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as error:
        raise ReleaseBuildError(f"could not create {context} directory: {name}") from error
    return _open_directory_at(parent_fd, name, context)


def _unlink_owned_at(parent_fd: int, name: str, owned: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
        os.unlink(name, dir_fd=parent_fd)


def _extract_archive_to_fd(archive: zipfile.ZipFile, root_fd: int) -> tuple[str, ...]:
    """Extract validated members through a destination root descriptor."""

    infos = _validated_zip_infos(archive)
    written: list[str] = []
    for info in infos:
        pure = _safe_member_path(info.filename)
        directory = os.dup(root_fd)
        descriptor: int | None = None
        owned: os.stat_result | None = None
        try:
            for component in pure.parts[:-1]:
                next_directory = _open_or_create_directory_at(
                    directory, component, f"ZIP member {info.filename}"
                )
                os.close(directory)
                directory = next_directory
            try:
                descriptor = os.open(pure.name, _file_create_flags(), 0o600, dir_fd=directory)
            except OSError as error:
                raise ReleaseBuildError(
                    f"ZIP member would overwrite or use an unsafe path: {info.filename}"
                ) from error
            owned = os.fstat(descriptor)
            if not stat.S_ISREG(owned.st_mode) or owned.st_nlink != 1:
                raise ReleaseBuildError(f"extracted ZIP target is not private: {info.filename}")
            remaining = info.file_size
            with archive.open(info, "r") as member:
                while True:
                    chunk = member.read(min(READ_CHUNK_SIZE, remaining + 1))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise ReleaseBuildError(f"ZIP member expanded beyond its declared size: {info.filename}")
                    _write_all(descriptor, chunk)
            if remaining != 0:
                raise ReleaseBuildError(f"ZIP member was truncated: {info.filename}")
            os.fchmod(descriptor, stat.S_IMODE(info.external_attr >> 16))
            os.fsync(descriptor)
            written.append(info.filename)
        except BaseException:
            if owned is not None:
                _unlink_owned_at(directory, pure.name, owned)
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(directory)
    return tuple(written)


def _open_zip_from_fd(descriptor: int) -> tuple[zipfile.ZipFile, object]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    stream = os.fdopen(os.dup(descriptor), "rb")
    try:
        archive = zipfile.ZipFile(stream)
    except BaseException:
        stream.close()
        raise
    return archive, stream


def safe_extract(archive_path: Path, destination: Path) -> tuple[Path, ...]:
    """Extract unique regular files without following destination path entries."""

    destination_absolute = _absolute_path(destination)
    try:
        os.makedirs(destination_absolute, mode=0o700, exist_ok=True)
    except OSError as error:
        raise ReleaseBuildError("could not create extraction destination") from error
    _, destination_fd = _open_root_directory(destination_absolute, "extraction destination")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            written = _extract_archive_to_fd(archive, destination_fd)
    finally:
        os.close(destination_fd)
    return tuple(destination_absolute.joinpath(*PurePosixPath(name).parts) for name in written)


def _run_validator(command: list[str], root: Path) -> None:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(root / "src"),
    }
    completed = subprocess.run(
        command,
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout).strip()
        raise ReleaseBuildError(f"extracted release validation failed: {diagnostic}")


def validate_extracted_release(archive_source: Path | int) -> None:
    """Safely extract and run repository-local skill and schema validators."""

    with tempfile.TemporaryDirectory(prefix="reconstructing-raster-icons-release-") as temporary:
        root = Path(temporary)
        _, root_fd = _open_root_directory(root, "validation extraction destination")
        try:
            if isinstance(archive_source, int):
                archive, stream = _open_zip_from_fd(archive_source)
                try:
                    _extract_archive_to_fd(archive, root_fd)
                finally:
                    archive.close()
                    stream.close()
            else:
                with zipfile.ZipFile(archive_source) as archive:
                    _extract_archive_to_fd(archive, root_fd)
        finally:
            os.close(root_fd)

        _run_validator([sys.executable, "scripts/validate_skill.py", "--path", "."], root)
        contract_root = root / "tests" / "fixtures" / "contracts"
        documents = sorted(contract_root.glob("valid-*.json"))
        documents.extend(sorted(contract_root.glob("conformance-valid-*.json")))
        if not documents:
            raise ReleaseBuildError("extracted release has no valid contract fixtures")
        _run_validator(
            [
                sys.executable,
                "scripts/validate_schemas.py",
                "--schemas",
                "schemas",
                "--documents",
                *(str(path.relative_to(root)) for path in documents),
            ],
            root,
        )


def _publish_no_clobber(
    output_fd: int,
    temporary_name: str,
    final_name: str,
    owned: os.stat_result,
) -> None:
    try:
        os.link(
            temporary_name,
            final_name,
            src_dir_fd=output_fd,
            dst_dir_fd=output_fd,
            follow_symlinks=False,
        )
    except FileExistsError as error:
        raise ReleaseBuildError(f"release output already exists: {final_name}") from error
    except OSError as error:
        raise ReleaseBuildError(f"could not safely publish release output: {final_name}") from error
    published = os.stat(final_name, dir_fd=output_fd, follow_symlinks=False)
    if (published.st_dev, published.st_ino) != (owned.st_dev, owned.st_ino):
        raise ReleaseBuildError(f"published release output changed unexpectedly: {final_name}")


def build_release(source: Path, output: Path, version: str) -> tuple[Path, Path, str, int]:
    """Build, hash, safely extract, validate, and atomically publish a release."""

    if VERSION_RE.fullmatch(version) is None:
        raise ReleaseBuildError("version must be a plain semantic version such as 0.1.0")
    source_root, source_fd = _open_root_directory(source, "source root")
    output_root = validate_output_root(source_root, output)
    try:
        os.makedirs(output_root, mode=0o700, exist_ok=True)
        _, output_fd = _open_root_directory(output_root, "output root")
    except BaseException:
        os.close(source_fd)
        raise

    archive_name = f"{ARCHIVE_PREFIX}{version}.zip"
    checksum_name = f"{archive_name}.sha256"
    archive_path = output_root / archive_name
    checksum_path = output_root / checksum_name
    archive_temporary = checksum_temporary = None
    archive_fd = checksum_fd = None
    archive_owned = checksum_owned = None
    published: list[tuple[str, os.stat_result]] = []
    try:
        _validate_open_root_separation(source_fd, output_fd)
        entries = _collect_entries_from_fd(source_fd)
        reject_absolute_leaks(entries, source_root)

        archive_temporary, archive_fd = _create_transaction_file(output_fd, "archive")
        _write_zip_fd(archive_fd, entries)
        archive_owned = os.fstat(archive_fd)
        digest = _hash_fd(archive_fd)

        checksum_temporary, checksum_fd = _create_transaction_file(output_fd, "checksum")
        checksum_payload = f"{digest}  {archive_name}\n".encode("ascii")
        _write_all(checksum_fd, checksum_payload)
        os.fchmod(checksum_fd, 0o644)
        os.fsync(checksum_fd)
        checksum_owned = os.fstat(checksum_fd)

        validate_extracted_release(archive_fd)

        _publish_no_clobber(
            output_fd, archive_temporary, archive_name, archive_owned
        )
        published.append((archive_name, archive_owned))
        _publish_no_clobber(
            output_fd, checksum_temporary, checksum_name, checksum_owned
        )
        published.append((checksum_name, checksum_owned))
        os.fsync(output_fd)

        _unlink_owned_at(output_fd, archive_temporary, archive_owned)
        archive_temporary = None
        _unlink_owned_at(output_fd, checksum_temporary, checksum_owned)
        checksum_temporary = None
        os.fsync(output_fd)
        return archive_path, checksum_path, digest, len(entries)
    except BaseException as error:
        for final_name, owned in reversed(published):
            _unlink_owned_at(output_fd, final_name, owned)
        if isinstance(error, (OSError, zipfile.BadZipFile)):
            raise ReleaseBuildError(f"release transaction failed: {error}") from error
        raise
    finally:
        if archive_owned is not None and archive_temporary is not None:
            _unlink_owned_at(output_fd, archive_temporary, archive_owned)
        if checksum_owned is not None and checksum_temporary is not None:
            _unlink_owned_at(output_fd, checksum_temporary, checksum_owned)
        if archive_fd is not None:
            os.close(archive_fd)
        if checksum_fd is not None:
            os.close(checksum_fd)
        os.close(output_fd)
        os.close(source_fd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        archive, checksum, digest, count = build_release(args.source, args.output, args.version)
    except (OSError, ReleaseBuildError, zipfile.BadZipFile) as error:
        print(f"release build failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "archive": str(archive),
                "checksum": str(checksum),
                "entries": count,
                "sha256": digest,
                "validated_after_extraction": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
