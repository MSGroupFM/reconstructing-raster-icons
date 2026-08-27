"""Black-box CLI contract tests for the three pipeline stages."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]


class PipelineCliTests(unittest.TestCase):
    def _run(self, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPOSITORY / "scripts" / script), *arguments],
            cwd=REPOSITORY,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_wrappers_publish_only_the_normative_flags(self) -> None:
        cases = {
            "prepare_reference.py": ("--source", "--draft", "--output", "--freeze"),
            "evaluate_icon.py": ("--map", "--candidate", "--iteration", "--run-dir"),
            "finalize_review.py": ("--evaluation", "--semantic-review", "--output"),
        }
        for script, flags in cases.items():
            with self.subTest(script=script):
                result = self._run(script, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                for flag in flags:
                    self.assertIn(flag, result.stdout)
                self.assertNotIn("--force", result.stdout)

    def test_expected_validation_failure_is_one_json_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            source.write_bytes(b"not-an-image")
            draft = root / "draft.json"
            draft.write_text("{}", encoding="utf-8")

            result = self._run(
                "prepare_reference.py",
                "--source",
                str(source),
                "--draft",
                str(draft),
                "--output",
                str(root / "output"),
                "--freeze",
            )
            self.assertTrue((root / "output" / "failure-report.json").is_file())

        self.assertEqual(result.returncode, 2)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertFalse(json.loads(lines[0])["ok"])
        self.assertNotIn("Traceback", result.stderr)

    def test_multicolor_source_without_merge_confirmation_stops_before_freeze(self) -> None:
        case = REPOSITORY / "tests" / "fixtures" / "conformance" / "multicolor-rejection"
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(
                "prepare_reference.py",
                "--source", str(case / "source.png"),
                "--draft", str(case / "draft.json"),
                "--output", str(Path(directory) / "output"),
                "--freeze",
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["status"], "invalid_input")
        self.assertIn("merge-to-monochrome", result.stderr)

    def test_clear_source_rejects_explicit_normalization_override(self) -> None:
        case = REPOSITORY / "tests" / "fixtures" / "conformance" / "analytic-fill"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            draft = json.loads((case / "draft.json").read_text(encoding="utf-8"))
            shutil.copyfile(case / "masks" / "mark.png", root / "mark.png")
            draft["components"][0]["source_mask_path"] = "mark.png"
            draft["source_color_scope"] = {
                "classification": "monochrome", "merge_to_monochrome": None,
            }
            draft["normalization"]["estimator_basis"] = "explicit_override"
            draft["normalization"]["explicit_overrides"] = {
                "background_luminance": 1,
                "foreground_luminance": 0,
                "reason": "caller override",
                "confirmed": True,
                "confirmed_at": "2026-08-26T00:00:00Z",
            }
            draft_path = root / "draft.json"
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            result = self._run(
                "prepare_reference.py", "--source", str(case / "source.png"),
                "--draft", str(draft_path), "--output", str(root / "output"), "--freeze",
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("only allowed when the automatic estimate is ambiguous", result.stderr)

    def test_source_color_scope_is_required_before_freeze(self) -> None:
        case = REPOSITORY / "tests" / "fixtures" / "conformance" / "analytic-fill"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            draft = json.loads((case / "draft.json").read_text(encoding="utf-8"))
            del draft["source_color_scope"]
            shutil.copyfile(case / "masks" / "mark.png", root / "mark.png")
            draft["components"][0]["source_mask_path"] = "mark.png"
            draft_path = root / "draft.json"
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            result = self._run(
                "prepare_reference.py", "--source", str(case / "source.png"),
                "--draft", str(draft_path), "--output", str(root / "output"),
                "--freeze",
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("source_color_scope", result.stderr)

    def test_argparse_failures_emit_one_json_summary(self) -> None:
        cases = (
            ("prepare_reference.py", ()),
            ("evaluate_icon.py", ()),
            ("evaluate_icon.py", ("--iteration", "not-an-int")),
            (
                "evaluate_icon.py",
                ("--map", "x", "--candidate", "x", "--iteration", "0", "--run-dir", "x", "--force"),
            ),
            ("finalize_review.py", ()),
        )
        for script, arguments in cases:
            with self.subTest(script=script, arguments=arguments):
                result = self._run(script, *arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(len(result.stdout.splitlines()), 1)
                self.assertEqual(json.loads(result.stdout)["status"], "invalid_input")
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
