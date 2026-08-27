"""Release archive contract and deterministic builder regressions."""

from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


REPOSITORY = Path(__file__).resolve().parents[1]
BUILDER = REPOSITORY / "scripts" / "build_release.py"
VERSION = "0.1.0"
ARCHIVE_NAME = f"reconstructing-raster-icons-v{VERSION}.zip"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_release", BUILDER)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load release builder")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_builder(
    output: Path,
    *,
    source: Path = REPOSITORY,
    cwd: Path = REPOSITORY,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--source",
            str(source),
            "--output",
            str(output),
            "--version",
            VERSION,
        ],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _copy_source(destination: Path) -> Path:
    return Path(
        shutil.copytree(
            REPOSITORY,
            destination,
            ignore=shutil.ignore_patterns(
                ".git",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                "__pycache__",
                "node_modules",
            ),
        )
    )


class ReleasePackageTests(unittest.TestCase):
    def test_builds_byte_identical_validated_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = _run_builder(root / "first")
            second = _run_builder(root / "second")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            first_zip = root / "first" / ARCHIVE_NAME
            second_zip = root / "second" / ARCHIVE_NAME
            self.assertEqual(first_zip.read_bytes(), second_zip.read_bytes())

            digest = hashlib.sha256(first_zip.read_bytes()).hexdigest()
            checksum = first_zip.with_suffix(first_zip.suffix + ".sha256")
            self.assertEqual(checksum.read_text(encoding="ascii"), f"{digest}  {ARCHIVE_NAME}\n")

            with zipfile.ZipFile(first_zip) as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                self.assertEqual(len(names), 146)
                self.assertEqual(
                    names,
                    (REPOSITORY / "release-manifest.txt").read_text(encoding="utf-8").splitlines(),
                )
                self.assertEqual(names, sorted(names))
                self.assertEqual(len(names), len(set(names)))
                self.assertFalse(any(info.is_dir() for info in infos))

                required = {
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
                    "docs/examples/strategy-board-before.png",
                    "docs/examples/strategy-board-vtracer.png",
                    "docs/examples/vintage-phone-before.png",
                    "docs/examples/vintage-phone-vtracer.png",
                    "docs/provenance.md",
                    "docs/releases/v0.1.0.md",
                    "package-lock.json",
                    "package.json",
                    "pyproject.toml",
                    "release-manifest.txt",
                    "references/acceptance-model.md",
                    "requirements-lock.txt",
                    "requirements.txt",
                    "schemas/acceptance-report.schema.json",
                    "scripts/build_release.py",
                    "scripts/evaluate_icon.py",
                    "scripts/validate_schemas.py",
                    "scripts/validate_skill.py",
                    "src/reconstructing_raster_icons/pipeline.py",
                    "tests/goldens/acceptance-model-1.0.2.json",
                    "tests/test_release_package.py",
                    "tests/test_vtracer_workflow_contract.py",
                    "references/vtracer-workflow.md",
                    "tests/behavioral/scenarios/vtracer-only-pipeline.md",
                }
                self.assertTrue(required.issubset(names), sorted(required - set(names)))
                self.assertTrue(any(name.startswith("tests/fixtures/conformance/") for name in names))
                self.assertTrue(any(name.startswith("tests/behavioral/scenarios/") for name in names))

                forbidden_parts = {
                    ".git",
                    ".idea",
                    ".pytest_cache",
                    ".venv",
                    "__pycache__",
                    "build",
                    "dist",
                    "node_modules",
                }
                for name in names:
                    path = Path(name)
                    self.assertFalse(forbidden_parts.intersection(path.parts), name)
                    self.assertFalse(name.endswith((".pyc", ".pyo", ".DS_Store")), name)
                    self.assertFalse(name.startswith("tests/behavioral/evidence/"), name)
                    self.assertFalse(any(token in path.name for token in ("preview-i", "overlay-i", "diff-i")), name)

                for info in infos:
                    self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0), info.filename)
                    mode = stat.S_IMODE(info.external_attr >> 16)
                    expected_mode = 0o755 if info.filename.startswith("scripts/") else 0o644
                    self.assertEqual(mode, expected_mode, info.filename)
                    self.assertFalse(stat.S_ISLNK(info.external_attr >> 16), info.filename)

                payload = b"\n".join(archive.read(name) for name in names)
                self.assertNotIn(b"/" + b"Users/", payload)
                self.assertNotIn(str(REPOSITORY).encode(), payload)

            self.assertEqual(stat.S_IMODE(first_zip.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(checksum.stat().st_mode), 0o644)

            with tempfile.TemporaryDirectory() as extraction:
                with zipfile.ZipFile(first_zip) as archive:
                    archive.extractall(extraction)
                extracted = Path(extraction)
                skill = subprocess.run(
                    [sys.executable, "scripts/validate_skill.py", "--path", "."],
                    cwd=extracted,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(skill.returncode, 0, skill.stdout + skill.stderr)
                valid_documents = sorted((extracted / "tests" / "fixtures" / "contracts").glob("valid-*.json"))
                schemas = subprocess.run(
                    [
                        sys.executable,
                        "scripts/validate_schemas.py",
                        "--schemas",
                        "schemas",
                        "--documents",
                        *(str(path.relative_to(extracted)) for path in valid_documents),
                    ],
                    cwd=extracted,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(schemas.returncode, 0, schemas.stdout + schemas.stderr)

    def test_manifest_is_the_only_source_of_archive_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _copy_source(root / "source")
            (source / ".env.production").write_text("TOKEN=do-not-package\n", encoding="utf-8")
            (source / "local-credentials.json").write_text('{"secret": true}\n', encoding="utf-8")

            completed = _run_builder(root / "dist", source=source)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            with zipfile.ZipFile(root / "dist" / ARCHIVE_NAME) as archive:
                names = set(archive.namelist())
            self.assertNotIn(".env.production", names)
            self.assertNotIn("local-credentials.json", names)

    def test_manifest_rejects_unsafe_duplicate_and_collision_paths(self) -> None:
        builder = _load_builder()
        invalid_manifests = (
            b"/absolute\n",
            b"../escape\n",
            b"dir\\file\n",
            b"safe\x00name\n",
            b"duplicate\nduplicate\n",
            b"Case.txt\ncase.txt\n",
            "caf\u00e9.txt\ncafe\u0301.txt\n".encode(),
            b"z-last\na-first\n",
            b"release-manifest.txt\n",
        )
        for payload in invalid_manifests:
            with self.subTest(payload=payload):
                with self.assertRaises(builder.ReleaseBuildError):
                    builder.parse_release_manifest(payload)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires POSIX")
    def test_manifest_special_file_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _copy_source(root / "source")
            fifo = source / "docs" / "declared-fifo"
            os.mkfifo(fifo)
            manifest = source / "release-manifest.txt"
            names = manifest.read_text(encoding="utf-8").splitlines()
            names.append("docs/declared-fifo")
            manifest.write_text("\n".join(sorted(names)) + "\n", encoding="utf-8")
            completed = _run_builder(root / "dist", source=source, timeout=3)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("regular file", completed.stderr)

    def test_source_reads_remain_anchored_to_the_open_root_descriptor(self) -> None:
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "payload.txt").write_bytes(b"trusted")
            descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
            try:
                moved = root / "moved"
                source.rename(moved)
                source.mkdir()
                (source / "payload.txt").write_bytes(b"replacement")
                self.assertEqual(
                    builder._read_regular_file_at(descriptor, "payload.txt"),
                    b"trusted",
                )
            finally:
                os.close(descriptor)

    def test_source_reads_remain_anchored_to_open_parent_and_file_descriptors(self) -> None:
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            parent = source / "parent"
            parent.mkdir(parents=True)
            (parent / "payload.txt").write_bytes(b"trusted-parent")
            descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
            original_open_directory = builder._open_directory_at

            def replace_parent_after_open(parent_fd: int, name: str, context: str) -> int:
                opened = original_open_directory(parent_fd, name, context)
                if name == "parent":
                    parent.rename(source / "moved-parent")
                    parent.mkdir()
                    (parent / "payload.txt").write_bytes(b"replacement-parent")
                return opened

            try:
                with mock.patch.object(
                    builder,
                    "_open_directory_at",
                    side_effect=replace_parent_after_open,
                ):
                    self.assertEqual(
                        builder._read_regular_file_at(descriptor, "parent/payload.txt"),
                        b"trusted-parent",
                    )

                trusted = source / "final.txt"
                replacement = source / "replacement.txt"
                trusted.write_bytes(b"trusted-final")
                replacement.write_bytes(b"replacement-final")
                original_os_open = os.open
                replaced = False

                def replace_final_after_open(path, flags, *args, **kwargs):
                    nonlocal replaced
                    opened = original_os_open(path, flags, *args, **kwargs)
                    if path == "final.txt" and not replaced:
                        replaced = True
                        os.replace(replacement, trusted)
                    return opened

                with mock.patch.object(builder.os, "open", side_effect=replace_final_after_open):
                    with self.assertRaisesRegex(builder.ReleaseBuildError, "exactly one link"):
                        builder._read_regular_file_at(descriptor, "final.txt")
                self.assertEqual(trusted.read_bytes(), b"replacement-final")
            finally:
                os.close(descriptor)

    @unittest.skipUnless(hasattr(socket, "socketpair"), "socket-pair test requires POSIX")
    def test_declared_socket_and_device_are_rejected_without_blocking(self) -> None:
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            endpoints = socket.socketpair()
            root_descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
            original_os_open = os.open
            observed_flags: list[int] = []

            def return_socket_descriptor(path, flags, *args, **kwargs):
                if path == "declared.socket":
                    observed_flags.append(flags)
                    return os.dup(endpoints[0].fileno())
                return original_os_open(path, flags, *args, **kwargs)

            try:
                with mock.patch.object(builder.os, "open", side_effect=return_socket_descriptor):
                    with self.assertRaisesRegex(builder.ReleaseBuildError, "regular file"):
                        builder._read_regular_file_at(root_descriptor, "declared.socket")
            finally:
                os.close(root_descriptor)
                endpoints[0].close()
                endpoints[1].close()
            self.assertTrue(observed_flags[0] & os.O_NONBLOCK)
            self.assertTrue(observed_flags[0] & os.O_NOFOLLOW)

        device_root = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaisesRegex(builder.ReleaseBuildError, "regular file"):
                builder._read_regular_file_at(device_root, "dev/null")
        finally:
            os.close(device_root)

    def test_source_root_and_entries_must_be_safe(self) -> None:
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _copy_source(root / "source")
            source_link = root / "source-link"
            source_link.symlink_to(source, target_is_directory=True)
            with self.assertRaises(builder.ReleaseBuildError):
                builder.resolve_source_root(source_link)

            outside = root / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            (source / "docs" / "escape").symlink_to(outside)
            manifest = source / "release-manifest.txt"
            names = manifest.read_text(encoding="utf-8").splitlines()
            names.append("docs/escape")
            manifest.write_text("\n".join(sorted(names)) + "\n", encoding="utf-8")
            with self.assertRaises(builder.ReleaseBuildError):
                builder.collect_entries(source)

            with self.assertRaises(builder.ReleaseBuildError):
                builder.validate_output_root(source, source / "dist")

    def test_safe_extraction_rejects_traversal_absolute_and_symlink_members(self) -> None:
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for filename in ("../escape", "/absolute", "C:/absolute"):
                archive_path = root / (filename.replace("/", "-") + ".zip")
                with zipfile.ZipFile(archive_path, "w") as archive:
                    archive.writestr(filename, b"unsafe")
                with self.assertRaises(builder.ReleaseBuildError, msg=filename):
                    builder.safe_extract(archive_path, root / "extract")

            symlink_zip = root / "symlink.zip"
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(symlink_zip, "w") as archive:
                archive.writestr(info, b"target")
            with self.assertRaises(builder.ReleaseBuildError):
                builder.safe_extract(symlink_zip, root / "extract-link")

            regular_zip = root / "regular.zip"
            regular = zipfile.ZipInfo("file.txt")
            regular.create_system = 3
            regular.external_attr = (stat.S_IFREG | 0o644) << 16
            with zipfile.ZipFile(regular_zip, "w") as archive:
                archive.writestr(regular, b"safe")
            actual_destination = root / "actual-destination"
            actual_destination.mkdir()
            destination_link = root / "destination-link"
            destination_link.symlink_to(actual_destination, target_is_directory=True)
            with self.assertRaises(builder.ReleaseBuildError):
                builder.safe_extract(regular_zip, destination_link)

            final_destination = root / "final-destination"
            final_destination.mkdir()
            outside = root / "outside.txt"
            outside.write_bytes(b"foreign")
            (final_destination / "file.txt").symlink_to(outside)
            with self.assertRaises(builder.ReleaseBuildError):
                builder.safe_extract(regular_zip, final_destination)
            self.assertEqual(outside.read_bytes(), b"foreign")

    def test_extraction_remains_anchored_to_the_open_root_descriptor(self) -> None:
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "regular.zip"
            info = zipfile.ZipInfo("parent/file.txt")
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(info, b"trusted")

            destination = root / "destination"
            destination.mkdir()
            descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
            try:
                moved = root / "moved-destination"
                destination.rename(moved)
                destination.mkdir()
                with zipfile.ZipFile(archive_path) as archive:
                    builder._extract_archive_to_fd(archive, descriptor)
                self.assertEqual((moved / "parent" / "file.txt").read_bytes(), b"trusted")
                self.assertFalse((destination / "parent").exists())
            finally:
                os.close(descriptor)

    def test_extraction_remains_anchored_to_an_open_parent_descriptor(self) -> None:
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "regular.zip"
            info = zipfile.ZipInfo("parent/file.txt")
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(info, b"trusted")

            destination = root / "destination"
            (destination / "parent").mkdir(parents=True)
            descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
            original_open_parent = builder._open_or_create_directory_at

            def replace_parent_after_open(parent_fd: int, name: str, context: str) -> int:
                opened = original_open_parent(parent_fd, name, context)
                if name == "parent":
                    (destination / "parent").rename(destination / "moved-parent")
                    (destination / "parent").mkdir()
                return opened

            try:
                with zipfile.ZipFile(archive_path) as archive, mock.patch.object(
                    builder,
                    "_open_or_create_directory_at",
                    side_effect=replace_parent_after_open,
                ):
                    builder._extract_archive_to_fd(archive, descriptor)
                self.assertEqual((destination / "moved-parent" / "file.txt").read_bytes(), b"trusted")
                self.assertFalse((destination / "parent" / "file.txt").exists())
            finally:
                os.close(descriptor)

    def test_foreign_output_paths_are_never_overwritten_or_removed(self) -> None:
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_name = ARCHIVE_NAME
            checksum_name = f"{ARCHIVE_NAME}.sha256"

            scenarios = ("regular-zip", "regular-sha", "symlink-zip", "symlink-sha", "hardlink-zip", "hardlink-sha")
            for scenario in scenarios:
                with self.subTest(scenario=scenario):
                    output = root / scenario
                    output.mkdir()
                    target_name = checksum_name if scenario.endswith("sha") else archive_name
                    target = output / target_name
                    outside = root / f"{scenario}-outside"
                    original = f"foreign:{scenario}\n".encode()
                    outside.write_bytes(original)
                    if scenario.startswith("regular"):
                        target.write_bytes(original)
                        expected_inode = target.stat().st_ino
                    elif scenario.startswith("symlink"):
                        target.symlink_to(outside)
                        expected_inode = target.lstat().st_ino
                    else:
                        os.link(outside, target)
                        expected_inode = target.stat().st_ino

                    with self.assertRaises(builder.ReleaseBuildError):
                        builder.build_release(REPOSITORY, output, VERSION)
                    self.assertEqual(outside.read_bytes(), original)
                    self.assertTrue(target.exists() or target.is_symlink())
                    self.assertEqual(target.lstat().st_ino, expected_inode)
                    if not target.is_symlink():
                        self.assertEqual(target.read_bytes(), original)
                    self.assertEqual({path.name for path in output.iterdir()}, {target_name})

    def test_validation_failure_rolls_back_only_transaction_outputs(self) -> None:
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dist"
            with mock.patch.object(
                builder,
                "validate_extracted_release",
                side_effect=builder.ReleaseBuildError("injected validation failure"),
            ):
                with self.assertRaises(builder.ReleaseBuildError):
                    builder.build_release(REPOSITORY, output, VERSION)
            self.assertEqual(list(output.iterdir()), [])

    def test_archive_is_deterministic_across_cwd_umask_and_source_mtimes(self) -> None:
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _copy_source(root / "source")
            original_cwd = Path.cwd()
            original_umask = os.umask(0o077)
            try:
                os.chdir(root)
                first, _, _, _ = builder.build_release(source, root / "first", VERSION)
            finally:
                os.chdir(original_cwd)
                os.umask(original_umask)

            for name in (source / "release-manifest.txt").read_text(encoding="utf-8").splitlines():
                os.utime(source / name, (1_900_000_000, 1_900_000_000))

            original_umask = os.umask(0o002)
            try:
                os.chdir("/")
                second, _, _, _ = builder.build_release(source, root / "second", VERSION)
            finally:
                os.chdir(original_cwd)
                os.umask(original_umask)
            self.assertEqual(first.read_bytes(), second.read_bytes())


if __name__ == "__main__":
    unittest.main()
