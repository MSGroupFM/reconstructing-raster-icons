from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class VTracerWorkflowContractTests(unittest.TestCase):
    def test_skill_requires_the_dedicated_vtracer_workflow(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("references/vtracer-workflow.md", skill)
        self.assertIn("VTracer", skill)
        self.assertIn("no manual fallback", skill)

    def test_vtracer_workflow_preserves_provenance_and_limits_shape_edits(self) -> None:
        workflow = (ROOT / "references" / "vtracer-workflow.md").read_text(
            encoding="utf-8"
        )

        required_contract = (
            "Generate at least three black-and-white spline variants",
            "same frozen source",
            "source hash",
            "candidate hash",
            "VTracer version, exact command, and complete parameters",
            "exact command",
            "composite metric",
            "preview, overlay, and diff",
            "per-component metrics",
            "circle",
            "ellipse",
            "evidence-backed straight segment",
            "Do not redraw, sculpt, or reinterpret organic geometry by hand",
            "currentColor",
            "safe-subset checks",
            "flatten transforms while preserving the rendered geometry",
            "preview score",
            "non_canonical",
            "128, 64, 32, and 24",
        )
        for clause in required_contract:
            with self.subTest(clause=clause):
                self.assertIn(clause, workflow)

    def test_component_substitution_keeps_all_same_source_evidence_conditions(self) -> None:
        workflow = (ROOT / "references" / "vtracer-workflow.md").read_text(
            encoding="utf-8"
        )
        selection = workflow.split("## Selection", 1)[1].split(
            "## Allowed postprocessing", 1
        )[0]

        required_conditions = (
            "both variants trace the same frozen source and normalization revision",
            "per-component metrics and overlay/diff show a material improvement",
            "the substitution copies VTracer geometry without manual node editing",
            "the final provenance log maps the component to its candidate hash",
        )
        for condition in required_conditions:
            with self.subTest(condition=condition):
                self.assertIn(condition, selection)

    def test_trace_input_preserves_antialiased_edge_coverage(self) -> None:
        workflow = (ROOT / "references" / "vtracer-workflow.md").read_text(
            encoding="utf-8"
        )
        frozen_input = workflow.split("## Frozen trace input", 1)[1].split(
            "## Source-evidenced sweep", 1
        )[0]
        frozen_input = " ".join(frozen_input.split())

        required_contract = (
            "cleaned grayscale trace master",
            "preserving the foreground shape and antialiased edge coverage",
            "Do not hard-threshold",
            "diagnostic binary masks",
            "never VTracer input",
            "`bw` is a VTracer clustering preset",
        )
        for clause in required_contract:
            with self.subTest(clause=clause):
                self.assertIn(clause, frozen_input)

    def test_target_size_review_renders_each_size_from_final_svg(self) -> None:
        workflow = (ROOT / "references" / "reconstruction-workflow.md").read_text(
            encoding="utf-8"
        )
        workflow = " ".join(workflow.split())

        required_contract = (
            "render the final SVG independently",
            "128, 64, 32, and 24 px",
            "Do not resize one raster preview",
        )
        for clause in required_contract:
            with self.subTest(clause=clause):
                self.assertIn(clause, workflow)

    def test_release_manifest_includes_the_vtracer_contract_and_scenario(self) -> None:
        entries = set(
            (ROOT / "release-manifest.txt").read_text(encoding="utf-8").splitlines()
        )

        self.assertIn("references/vtracer-workflow.md", entries)
        self.assertIn("tests/behavioral/scenarios/vtracer-only-pipeline.md", entries)
        self.assertIn("tests/test_vtracer_workflow_contract.py", entries)


if __name__ == "__main__":
    unittest.main()
