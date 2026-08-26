"""Release archive contract and deterministic builder regressions."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
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


def _run_builder(output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--source",
            str(REPOSITORY),
            "--output",
            str(output),
            "--version",
            VERSION,
        ],
        cwd=REPOSITORY,
        text=True,
        capture_output=True,
        check=False,
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
                    "docs/provenance.md",
                    "docs/releases/v0.1.0.md",
                    "package-lock.json",
                    "package.json",
                    "pyproject.toml",
                    "references/acceptance-model.md",
                    "requirements-lock.txt",
                    "requirements.txt",
                    "schemas/acceptance-report.schema.json",
                    "scripts/build_release.py",
                    "scripts/evaluate_icon.py",
                    "scripts/validate_schemas.py",
                    "scripts/validate_skill.py",
                    "src/reconstructing_raster_icons/pipeline.py",
                    "tests/goldens/acceptance-model-1.0.0.json",
                    "tests/test_release_package.py",
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

    def test_source_root_and_entries_must_be_safe(self) -> None:
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "file.txt").write_text("safe\n", encoding="utf-8")
            source_link = root / "source-link"
            source_link.symlink_to(source, target_is_directory=True)
            with self.assertRaises(builder.ReleaseBuildError):
                builder.resolve_source_root(source_link)

            outside = root / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            (source / "escape").symlink_to(outside)
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


if __name__ == "__main__":
    unittest.main()
