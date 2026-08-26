from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_skill.py"


class SkillValidatorTests(unittest.TestCase):
    def extract_skill(self, destination: Path) -> None:
        shutil.copy2(ROOT / "SKILL.md", destination / "SKILL.md")
        shutil.copytree(ROOT / "agents", destination / "agents")
        shutil.copytree(ROOT / "references", destination / "references")

    def run_validator(self, skill_root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(VALIDATOR), "--path", str(skill_root)],
            capture_output=True,
            check=False,
            text=True,
        )

    def mutate_skill(self, mutation) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory(prefix="validate-skill-test-") as temporary:
            root = Path(temporary)
            self.extract_skill(root)
            mutation(root)
            result = self.run_validator(root)
            return result, result.stdout + result.stderr

    def test_valid_extracted_skill(self) -> None:
        result, output = self.mutate_skill(lambda _root: None)
        self.assertEqual(result.returncode, 0, output)
        self.assertEqual(result.stdout, "Skill is valid.\n")
        self.assertEqual(result.stderr, "")

    def test_missing_reference_has_stable_diagnostic(self) -> None:
        def remove_reference(root: Path) -> None:
            (root / "references" / "acceptance-model.md").unlink()

        result, output = self.mutate_skill(remove_reference)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "ERROR link.missing: SKILL.md: references/acceptance-model.md\n",
            output,
        )

    def test_absolute_link_is_rejected(self) -> None:
        def add_absolute_link(root: Path) -> None:
            path = root / "SKILL.md"
            path.write_text(path.read_text(encoding="utf-8") + "\n[unsafe](/tmp/outside.md)\n", encoding="utf-8")

        result, output = self.mutate_skill(add_absolute_link)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR link.absolute: SKILL.md: /tmp/outside.md\n", output)

    def test_unknown_frontmatter_key_is_rejected(self) -> None:
        def add_unknown_key(root: Path) -> None:
            path = root / "SKILL.md"
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("name: reconstructing-raster-icons\n", "name: reconstructing-raster-icons\nversion: 1\n", 1), encoding="utf-8")

        result, output = self.mutate_skill(add_unknown_key)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR frontmatter.unknown_key: SKILL.md: version\n", output)

    def test_colon_mapping_frontmatter_is_rejected(self) -> None:
        def add_mapping(root: Path) -> None:
            path = root / "SKILL.md"
            lines = path.read_text(encoding="utf-8").splitlines()
            lines[2] = "description: invalid: mapping"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result, output = self.mutate_skill(add_mapping)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR frontmatter.scalar: SKILL.md:3: mapping syntax is not allowed\n", output)

    def test_non_string_frontmatter_scalars_are_rejected(self) -> None:
        cases = (
            "true",
            "null",
            "42",
            "[]",
            "{}",
            "1.",
            "01",
            "012",
            "1:20",
            ".inf",
            ".nan",
            "2026-08-26",
            "2026-08-26T12:34:56Z",
            "0x10",
            "0b10",
            "1.0e+3",
        )
        for key in ("name", "description"):
            for replacement in cases:
                with self.subTest(key=key, replacement=replacement):
                    def replace_value(root: Path) -> None:
                        path = root / "SKILL.md"
                        lines = path.read_text(encoding="utf-8").splitlines()
                        lines[1 if key == "name" else 2] = f"{key}: {replacement}"
                        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

                    result, output = self.mutate_skill(replace_value)
                    self.assertEqual(result.returncode, 1)
                    line_number = 2 if key == "name" else 3
                    self.assertIn(
                        f"ERROR frontmatter.scalar: SKILL.md:{line_number}: expected string scalar\n",
                        output,
                    )

    def test_quoted_yaml_scalar_lookalikes_remain_valid_strings(self) -> None:
        for replacement in ('"1:20"', '"description: # text"'):
            with self.subTest(replacement=replacement):
                def replace_description(root: Path) -> None:
                    path = root / "SKILL.md"
                    lines = path.read_text(encoding="utf-8").splitlines()
                    lines[1] = 'name: "reconstructing-raster-icons"'
                    lines[2] = f"description: {replacement}"
                    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

                result, output = self.mutate_skill(replace_description)
                self.assertEqual(result.returncode, 0, output)
                self.assertEqual(result.stdout, "Skill is valid.\n")

    def test_missing_pyyaml_has_stable_dependency_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="validate-skill-test-") as temporary:
            root = Path(temporary)
            self.extract_skill(root)
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            result = subprocess.run(
                [sys.executable, "-S", str(VALIDATOR), "--path", str(root)],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            "ERROR dependency.missing: PyYAML is required\n",
        )
        self.assertNotIn("Traceback", result.stderr)

    def test_duplicate_keys_and_unsafe_yaml_constructs_remain_rejected(self) -> None:
        attacks = ("&anchor value", "*anchor", "!unsafe value")
        for attack in attacks:
            with self.subTest(attack=attack):
                def add_attack(root: Path) -> None:
                    path = root / "SKILL.md"
                    lines = path.read_text(encoding="utf-8").splitlines()
                    lines[2] = f"description: {attack}"
                    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

                result, output = self.mutate_skill(add_attack)
                self.assertEqual(result.returncode, 1)
                self.assertIn("ERROR frontmatter.scalar:", output)

        def duplicate(root: Path) -> None:
            path = root / "SKILL.md"
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("description:", "name: reconstructing-raster-icons\ndescription:", 1), encoding="utf-8")

        result, output = self.mutate_skill(duplicate)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR frontmatter.duplicate_key: SKILL.md: name\n", output)

    def test_unresolved_placeholder_is_rejected(self) -> None:
        def add_placeholder(root: Path) -> None:
            path = root / "references" / "reconstruction-workflow.md"
            path.write_text(path.read_text(encoding="utf-8") + "\n[TODO: complete this]\n", encoding="utf-8")

        result, output = self.mutate_skill(add_placeholder)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "ERROR placeholder.unresolved: references/reconstruction-workflow.md: [TODO: complete this]\n",
            output,
        )

    def test_tbd_and_fixme_placeholders_are_rejected_case_insensitively(self) -> None:
        for placeholder in ("[TBD]", "fixme"):
            with self.subTest(placeholder=placeholder):
                def add_placeholder(root: Path) -> None:
                    path = root / "references" / "reconstruction-workflow.md"
                    path.write_text(
                        path.read_text(encoding="utf-8") + f"\n{placeholder}\n",
                        encoding="utf-8",
                    )

                result, output = self.mutate_skill(add_placeholder)
                self.assertEqual(result.returncode, 1)
                self.assertIn("ERROR placeholder.unresolved:", output)

    def test_unquoted_agent_string_is_rejected(self) -> None:
        def unquote(root: Path) -> None:
            path = root / "agents" / "openai.yaml"
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace('display_name: "Reconstruct Raster Icons"', "display_name: Reconstruct Raster Icons"), encoding="utf-8")

        result, output = self.mutate_skill(unquote)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR agents.string_unquoted: agents/openai.yaml: interface.display_name\n", output)

    def test_default_prompt_requires_standalone_skill_token(self) -> None:
        def add_prefix_collision(root: Path) -> None:
            path = root / "agents" / "openai.yaml"
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text.replace(
                    "$reconstructing-raster-icons",
                    "$reconstructing-raster-icons-extra",
                ),
                encoding="utf-8",
            )

        result, output = self.mutate_skill(add_prefix_collision)
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "ERROR agents.default_prompt: agents/openai.yaml: missing standalone $reconstructing-raster-icons\n",
            output,
        )

    def test_implicit_invocation_must_remain_enabled(self) -> None:
        def disable(root: Path) -> None:
            path = root / "agents" / "openai.yaml"
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("allow_implicit_invocation: true", "allow_implicit_invocation: false"), encoding="utf-8")

        result, output = self.mutate_skill(disable)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ERROR agents.implicit_invocation: agents/openai.yaml: expected true\n", output)


if __name__ == "__main__":
    unittest.main()
