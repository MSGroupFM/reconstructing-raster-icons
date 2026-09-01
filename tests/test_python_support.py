"""Regression coverage for the declared CPython support contract."""

from __future__ import annotations

from pathlib import Path
import sys
import sysconfig
import tomllib
import unittest

import yaml

from reconstructing_raster_icons import __version__


REPOSITORY = Path(__file__).resolve().parents[1]
PROJECT_VERSION = "0.2.0"
PYTHON_REQUIREMENT = ">=3.11,<3.15"
SUPPORTED_PYTHONS = ["3.11", "3.12", "3.13", "3.14"]


class PythonSupportContractTests(unittest.TestCase):
    def test_project_metadata_and_runtime_version_agree(self) -> None:
        with (REPOSITORY / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]

        self.assertEqual(project["version"], PROJECT_VERSION)
        self.assertEqual(project["requires-python"], PYTHON_REQUIREMENT)
        self.assertEqual(__version__, PROJECT_VERSION)

    def test_ci_runs_every_supported_python_minor(self) -> None:
        workflow = yaml.safe_load(
            (REPOSITORY / ".github" / "workflows" / "ci.yml").read_text(
                encoding="utf-8"
            )
        )
        job = workflow["jobs"]["python-matrix"]
        self.assertEqual(
            job["strategy"]["matrix"]["python-version"],
            SUPPORTED_PYTHONS,
        )
        portable_command = next(
            step["run"]
            for step in job["steps"]
            if step.get("name") == "Run portable test suite"
        )
        release_command = next(
            step["run"]
            for step in job["steps"]
            if step.get("name") == "Build and revalidate deterministic archive"
        )
        self.assertIn("tests.test_python_support", portable_command)
        self.assertIn(f"--version {PROJECT_VERSION}", release_command)

    def test_running_interpreter_is_within_declared_support(self) -> None:
        self.assertGreaterEqual(sys.version_info[:2], (3, 11))
        self.assertLess(sys.version_info[:2], (3, 15))
        self.assertNotEqual(sysconfig.get_config_var("Py_GIL_DISABLED"), 1)


if __name__ == "__main__":
    unittest.main()
