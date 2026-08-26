#!/usr/bin/env python3
"""Build and revalidate a deterministic source release ZIP."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import zipfile


ARCHIVE_PREFIX = "reconstructing-raster-icons-v"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
VERSION_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".github-release",
        ".idea",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo", ".zip", ".sha256"})
DIAGNOSTIC_NAME_RE = re.compile(
    r"(?:preview|overlay|diff)-i[0-9]{2}\.png\Z|"
    r"component-.+-(?:visible|isolated)-i[0-9]{2}\.png\Z"
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
        "references/acceptance-model.md",
        "references/reconstruction-workflow.md",
        "references/security-and-rendering.md",
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
    }
)


class ReleaseBuildError(RuntimeError):
    """Raised when a source or archive violates the release contract."""


@dataclass(frozen=True, order=True)
class ReleaseEntry:
    """An immutable file snapshot ready for deterministic ZIP serialization."""

    relative: str
    data: bytes
    mode: int


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_source_root(source: Path) -> Path:
    """Resolve a real non-root directory without accepting a symlink entrypoint."""

    candidate = source.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        metadata = candidate.lstat()
    except OSError as error:
        raise ReleaseBuildError(f"source root is unavailable: {candidate}") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ReleaseBuildError("source root must not be a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseBuildError("source root must be a directory")
    resolved = candidate.resolve(strict=True)
    if resolved == Path(resolved.anchor):
        raise ReleaseBuildError("filesystem root is not a valid release source")
    return resolved


def validate_output_root(source: Path, output: Path) -> Path:
    """Require a dedicated output directory outside and below no source root."""

    source = source.resolve(strict=True)
    candidate = output.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if candidate.exists() and candidate.is_symlink():
        raise ReleaseBuildError("output root must not be a symlink")
    resolved = candidate.resolve(strict=False)
    if _is_relative_to(resolved, source) or _is_relative_to(source, resolved):
        raise ReleaseBuildError("output root must be separate from the source tree")
    return resolved


def _excluded(relative: PurePosixPath) -> bool:
    if EXCLUDED_PARTS.intersection(relative.parts):
        return True
    if relative.name == ".DS_Store" or relative.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    if relative.parts[:3] == ("tests", "behavioral", "evidence"):
        return True
    if "diagnostics" in relative.parts or "run-workspaces" in relative.parts:
        return True
    return DIAGNOSTIC_NAME_RE.fullmatch(relative.name) is not None


def _read_regular_file(path: Path, relative: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ReleaseBuildError(f"could not safely open source entry: {relative}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReleaseBuildError(f"release entry is not a regular file: {relative}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def collect_entries(source: Path) -> tuple[ReleaseEntry, ...]:
    """Snapshot included regular files while rejecting source-tree symlinks."""

    source = source.resolve(strict=True)
    entries: list[ReleaseEntry] = []
    for directory, directory_names, file_names in os.walk(source, topdown=True, followlinks=False):
        current = Path(directory)
        retained_directories: list[str] = []
        for name in sorted(directory_names):
            path = current / name
            relative = PurePosixPath(path.relative_to(source).as_posix())
            if _excluded(relative):
                continue
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ReleaseBuildError(f"release source contains a symlink: {relative}")
            if not stat.S_ISDIR(metadata.st_mode):
                raise ReleaseBuildError(f"release source entry is not a directory: {relative}")
            retained_directories.append(name)
        directory_names[:] = retained_directories

        for name in sorted(file_names):
            path = current / name
            relative_path = PurePosixPath(path.relative_to(source).as_posix())
            if _excluded(relative_path):
                continue
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ReleaseBuildError(f"release source contains a symlink: {relative_path}")
            relative = relative_path.as_posix()
            data = _read_regular_file(path, relative)
            mode = 0o755 if relative.startswith("scripts/") else 0o644
            entries.append(ReleaseEntry(relative=relative, data=data, mode=mode))

    entries.sort()
    names = [entry.relative for entry in entries]
    if len(names) != len(set(names)):
        raise ReleaseBuildError("release source contains duplicate archive names")
    missing = sorted(REQUIRED_RELEASE_FILES - set(names))
    if missing:
        raise ReleaseBuildError(f"release source is incomplete: {', '.join(missing)}")
    return tuple(entries)


def reject_absolute_leaks(entries: tuple[ReleaseEntry, ...], source: Path) -> None:
    """Reject machine-local source roots and common home-directory path leaks."""

    markers = (
        str(source).encode("utf-8"),
        b"/" + b"Users/",
        b"/" + b"home/",
        b":\\" + b"Users\\",
    )
    for entry in entries:
        for marker in markers:
            if marker and marker in entry.data:
                raise ReleaseBuildError(f"absolute path leak in release entry: {entry.relative}")


def _write_zip(archive_path: Path, entries: tuple[ReleaseEntry, ...]) -> None:
    temporary = archive_path.with_name(f".{archive_path.name}.tmp")
    try:
        with zipfile.ZipFile(
            temporary,
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
        os.replace(temporary, archive_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _safe_member_path(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise ReleaseBuildError(f"unsafe ZIP member name: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ReleaseBuildError(f"unsafe ZIP member path: {name}")
    if pure.parts and ":" in pure.parts[0]:
        raise ReleaseBuildError(f"unsafe ZIP member drive path: {name}")
    return pure


def safe_extract(archive_path: Path, destination: Path) -> tuple[Path, ...]:
    """Extract only unique regular files contained by a fresh destination."""

    if destination.is_symlink():
        raise ReleaseBuildError("extraction destination must not be a symlink")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = destination.resolve(strict=True)
    written: list[Path] = []
    seen: set[str] = set()
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            pure = _safe_member_path(info.filename)
            if info.filename in seen:
                raise ReleaseBuildError(f"duplicate ZIP member: {info.filename}")
            seen.add(info.filename)
            member_mode = info.external_attr >> 16
            if info.is_dir() or stat.S_ISLNK(member_mode) or not stat.S_ISREG(member_mode):
                raise ReleaseBuildError(f"ZIP member is not a regular file: {info.filename}")
            target = root.joinpath(*pure.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            resolved_parent = target.parent.resolve(strict=True)
            if not _is_relative_to(resolved_parent, root):
                raise ReleaseBuildError(f"ZIP member escapes extraction root: {info.filename}")
            if target.exists() or target.is_symlink():
                raise ReleaseBuildError(f"ZIP member would overwrite a path: {info.filename}")
            target.write_bytes(archive.read(info))
            target.chmod(stat.S_IMODE(member_mode))
            written.append(target)
    return tuple(written)


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


def validate_extracted_release(archive_path: Path) -> None:
    """Safely extract and run repository-local skill and schema validators."""

    with tempfile.TemporaryDirectory(prefix="reconstructing-raster-icons-release-") as temporary:
        root = Path(temporary)
        safe_extract(archive_path, root)
        _run_validator(
            [sys.executable, "scripts/validate_skill.py", "--path", "."],
            root,
        )
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


def build_release(source: Path, output: Path, version: str) -> tuple[Path, Path, str, int]:
    """Build, hash, safely extract, and validate one release archive."""

    if VERSION_RE.fullmatch(version) is None:
        raise ReleaseBuildError("version must be a plain semantic version such as 0.1.0")
    source_root = resolve_source_root(source)
    output_root = validate_output_root(source_root, output)
    output_root.mkdir(parents=True, exist_ok=True)
    if output_root.is_symlink():
        raise ReleaseBuildError("output root must not be a symlink")
    entries = collect_entries(source_root)
    reject_absolute_leaks(entries, source_root)

    archive_path = output_root / f"{ARCHIVE_PREFIX}{version}.zip"
    checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    _write_zip(archive_path, entries)
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="ascii", newline="\n")
    validate_extracted_release(archive_path)
    return archive_path, checksum_path, digest, len(entries)


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
