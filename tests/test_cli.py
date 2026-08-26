"""Black-box CLI contract tests for the three pipeline stages."""

from __future__ import annotations

import json
from pathlib import Path
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

        self.assertEqual(result.returncode, 2)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertFalse(json.loads(lines[0])["ok"])
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
