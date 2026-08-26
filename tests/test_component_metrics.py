from __future__ import annotations

import unittest

import numpy as np

from reconstructing_raster_icons.geometry import component_enclosure
from reconstructing_raster_icons.metrics import (
    ComponentLayoutEvaluation,
    TopologyEvaluation,
    component_layout_score,
    topology_score,
    visible_component_mask,
)


class ComponentMetricTests(unittest.TestCase):
    def test_visible_component_mask_requires_both_alpha_and_relative_luminance(self) -> None:
        alpha = np.array([[1.0, 0.49], [0.5, 1.0]], dtype=np.float64)
        luminance = np.array([[1.0, 1.0], [0.5, 0.49]], dtype=np.float64)

        np.testing.assert_array_equal(
            visible_component_mask(alpha, luminance),
            np.array([[True, False], [True, False]], dtype=bool),
        )

    def test_layout_callback_measures_visible_contribution_after_occlusion(self) -> None:
        reference = np.zeros((10, 10), dtype=bool)
        reference[4, 4:6] = True
        calls: list[tuple[str, dict[str, str]]] = []

        def render(selected: str, palette: dict[str, str]) -> tuple[np.ndarray, np.ndarray]:
            calls.append((selected, palette))
            alpha = np.zeros((10, 10), dtype=np.float64)
            luminance = np.zeros_like(alpha)
            alpha[4, 4:6] = 1.0
            luminance[4, 4] = 1.0
            return alpha, luminance

        result = component_layout_score(
            {"lower": reference},
            render,
            component_ids=("lower", "upper"),
            mandatory={"lower"},
        )

        self.assertIsInstance(result, ComponentLayoutEvaluation)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], ("lower", {"lower": "#ffffff", "upper": "#000000"}))
        self.assertAlmostEqual(result.components[0].score, 31.7157287525381, places=12)
        self.assertTrue(result.gate_pass)

    def test_missing_mandatory_component_scores_zero_and_fails_gate(self) -> None:
        reference = np.zeros((8, 8), dtype=bool)
        reference[2:6, 2:6] = True

        result = component_layout_score(
            {"mark": reference},
            {},
            mandatory={"mark"},
        )

        self.assertEqual(result.score, 0.0)
        self.assertEqual(result.components[0].score, 0.0)
        self.assertFalse(result.components[0].present)
        self.assertFalse(result.gate_pass)
        self.assertEqual(result.failed_mandatory, frozenset({"mark"}))

    def test_layout_uses_declared_weights(self) -> None:
        first = np.zeros((16, 16), dtype=bool)
        second = np.zeros_like(first)
        first[2:4, 2:4] = True
        second[10:12, 10:12] = True

        result = component_layout_score(
            {"first": first, "second": second},
            {"first": first},
            weights={"first": 3.0, "second": 1.0},
            mandatory={"first", "second"},
        )

        self.assertEqual(result.score, 75.0)
        self.assertFalse(result.gate_pass)


class TopologyMetricTests(unittest.TestCase):
    def test_component_enclosure_rejects_noncoverage_masks(self) -> None:
        with self.assertRaisesRegex(TypeError, "bool or floating-point"):
            component_enclosure(np.ones((4, 4), dtype=np.uint8), 1)

    def test_ring_enclosure_contains_an_inner_symbol(self) -> None:
        ring = np.zeros((15, 15), dtype=bool)
        ring[2:13, 2:13] = True
        ring[4:11, 4:11] = False
        inner = np.zeros_like(ring)
        inner[6:9, 6:9] = True

        enclosure = component_enclosure(ring, 1)
        self.assertTrue(np.all(enclosure[inner]))

        result = topology_score(
            {("ring", 1), ("inner", 0)},
            {("contains", "ring", "inner")},
            visible_masks={"ring": ring, "inner": inner},
            isolated_masks={"ring": ring, "inner": inner},
            paint_order=("ring", "inner"),
        )
        self.assertEqual(result.score, 100.0)
        self.assertTrue(result.gate_pass)

    def test_touches_uses_visible_masks_without_requiring_overlap(self) -> None:
        left = np.zeros((10, 10), dtype=bool)
        right = np.zeros_like(left)
        left[5, 4] = True
        right[5, 5] = True

        result = topology_score(
            {("left", 0), ("right", 0)},
            {("touches", "left", "right")},
            visible_masks={"left": left, "right": right},
            isolated_masks={"left": left, "right": right},
            paint_order=("left", "right"),
        )

        self.assertIn(("touches", "left", "right"), result.observed_edge_facts)
        self.assertNotIn(("overlaps", "left", "right"), result.observed_edge_facts)
        self.assertEqual(result.score, 100.0)

    def test_paint_order_is_emitted_only_for_overlap_or_declared_pair(self) -> None:
        a = np.zeros((12, 12), dtype=bool)
        b = np.zeros_like(a)
        c = np.zeros_like(a)
        a[2:5, 2:5] = True
        b[4:7, 4:7] = True
        c[9, 9] = True

        result = topology_score(
            {("a", 0), ("b", 0), ("c", 0)},
            {
                ("overlaps", "a", "b"),
                ("paint_order", "a", "b"),
                ("paint_order", "a", "c"),
            },
            visible_masks={"a": a, "b": b, "c": c},
            isolated_masks={"a": a, "b": b, "c": c},
            paint_order=("a", "b", "c"),
        )

        paint_facts = {fact for fact in result.observed_edge_facts if fact[0] == "paint_order"}
        self.assertEqual(paint_facts, {("paint_order", "a", "b"), ("paint_order", "a", "c")})
        self.assertEqual(result.score, 100.0)

    def test_empty_edge_fact_sets_have_perfect_f1(self) -> None:
        mark = np.zeros((4, 4), dtype=bool)
        mark[1:3, 1:3] = True
        result = topology_score(
            {("mark", 0)},
            set(),
            visible_masks={"mark": mark},
            isolated_masks={"mark": mark},
            paint_order=("mark",),
        )

        self.assertIsInstance(result, TopologyEvaluation)
        self.assertEqual(result.node_f1, 1.0)
        self.assertEqual(result.edge_f1, 1.0)
        self.assertEqual(result.score, 100.0)
        self.assertTrue(result.gate_pass)

    def test_missing_visible_component_is_rejected_instead_of_erasing_touch(self) -> None:
        left = np.zeros((10, 10), dtype=bool)
        right = np.zeros_like(left)
        left[5, 4] = True
        right[5, 5] = True

        complete = topology_score(
            {("left", 0), ("right", 0)},
            set(),
            visible_masks={"left": left, "right": right},
            isolated_masks={"left": left, "right": right},
            paint_order=("left", "right"),
        )
        self.assertFalse(complete.gate_pass)
        self.assertIn(("touches", "left", "right"), complete.unexpected_edge_facts)

        with self.assertRaisesRegex(ValueError, "same nonempty component IDs"):
            topology_score(
                {("left", 0), ("right", 0)},
                set(),
                visible_masks={"left": left},
                isolated_masks={"left": left, "right": right},
                paint_order=("left", "right"),
            )

    def test_extra_visible_component_is_rejected(self) -> None:
        mask = np.zeros((6, 6), dtype=bool)
        mask[2, 2] = True

        with self.assertRaisesRegex(ValueError, "same nonempty component IDs"):
            topology_score(
                {("mark", 0)},
                set(),
                visible_masks={"mark": mask, "extra": mask},
                isolated_masks={"mark": mask},
                paint_order=("mark",),
            )

    def test_topology_component_masks_require_matching_shapes(self) -> None:
        visible = np.zeros((6, 6), dtype=bool)
        isolated = np.zeros((7, 6), dtype=bool)
        visible[2, 2] = True
        isolated[2, 2] = True

        with self.assertRaisesRegex(ValueError, "same shape"):
            topology_score(
                {("mark", 0)},
                set(),
                visible_masks={"mark": visible},
                isolated_masks={"mark": isolated},
                paint_order=("mark",),
            )

    def test_exact_topology_mismatch_fails_gate_without_hiding_score(self) -> None:
        filled = np.zeros((8, 8), dtype=bool)
        filled[2:6, 2:6] = True

        result = topology_score(
            {("mark", 1)},
            set(),
            visible_masks={"mark": filled},
            isolated_masks={"mark": filled},
            paint_order=("mark",),
        )

        self.assertEqual(result.score, 50.0)
        self.assertFalse(result.gate_pass)
        self.assertEqual(result.missing_node_facts, frozenset({("mark", 1)}))
        self.assertEqual(result.unexpected_node_facts, frozenset({("mark", 0)}))

    def test_connects_is_excluded_from_topology_score(self) -> None:
        mark = np.zeros((4, 4), dtype=bool)
        mark[1:3, 1:3] = True
        result = topology_score(
            {("mark", 0)},
            {("connects", "a", "b")},
            visible_masks={"mark": mark},
            isolated_masks={"mark": mark},
            paint_order=("mark",),
        )

        self.assertEqual(result.score, 100.0)
        self.assertTrue(result.gate_pass)

    def test_empty_component_inventory_is_rejected_before_scoring(self) -> None:
        with self.assertRaisesRegex(ValueError, "same nonempty component IDs"):
            topology_score(
                set(),
                set(),
                visible_masks={},
                isolated_masks={},
                paint_order=(),
            )


if __name__ == "__main__":
    unittest.main()
