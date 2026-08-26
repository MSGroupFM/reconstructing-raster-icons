from __future__ import annotations

import math
import unittest

import numpy as np

from reconstructing_raster_icons.metrics import (
    MetricSet,
    composite_score,
    contour_score,
    silhouette_score,
)


class MetricArithmeticTests(unittest.TestCase):
    def test_composite_formula_uses_the_unrounded_float64_scores(self) -> None:
        metrics = MetricSet(s=100.0, c=90.0, l=80.0, t=70.0)

        self.assertEqual(composite_score(metrics), 91.0)

        unrounded = MetricSet(s=9.26, c=15.0, l=13.9, t=59.15)
        self.assertEqual(composite_score(unrounded), 16.666999999999998)

    def test_silhouette_empty_mask_rules_are_exact(self) -> None:
        empty = np.zeros((16, 16), dtype=bool)
        filled = empty.copy()
        filled[4:12, 4:12] = True

        self.assertEqual(silhouette_score(empty, empty, empty), 100.0)
        self.assertEqual(silhouette_score(empty, filled, empty), 0.0)
        self.assertEqual(silhouette_score(filled, empty, empty), 0.0)

    def test_silhouette_uses_the_euclidean_delta_and_excludes_uncertainty(self) -> None:
        reference = np.zeros((32, 32), dtype=bool)
        candidate = np.zeros_like(reference)
        uncertainty = np.zeros_like(reference)
        reference[10, 10] = True
        candidate[10, 11] = True

        self.assertEqual(silhouette_score(reference, candidate, uncertainty), 100.0)

        uncertainty[10, 10:12] = True
        self.assertEqual(silhouette_score(reference, candidate, uncertainty), 100.0)

    def test_silhouette_delta_rounds_decimal_half_up(self) -> None:
        # A 1500 x 2000 canvas has D=2500, so 0.001D=2.5 and delta must be 3.
        reference = np.zeros((1500, 2000), dtype=bool)
        candidate = np.zeros_like(reference)
        uncertainty = np.zeros_like(reference)
        reference[750, 1000] = True
        candidate[750, 1003] = True

        self.assertEqual(silhouette_score(reference, candidate, uncertainty), 100.0)

    def test_contour_uses_euclidean_edt_and_spec_normalization(self) -> None:
        reference = np.zeros((100, 100), dtype=bool)
        candidate = np.zeros_like(reference)
        uncertainty = np.zeros_like(reference)
        reference[10, 10] = True
        candidate[10, 13] = True

        expected = 100.0 * (1.0 - (3.0 - 1.0) / (0.02 * math.sqrt(20_000.0)))
        self.assertAlmostEqual(
            contour_score(reference, candidate, uncertainty), expected, places=12
        )

    def test_contour_empty_boundary_rules_are_exact(self) -> None:
        empty = np.zeros((8, 8), dtype=bool)
        filled = empty.copy()
        filled[3, 3] = True

        self.assertEqual(contour_score(empty, empty, empty), 100.0)
        self.assertEqual(contour_score(empty, filled, empty), 0.0)

    def test_contour_extracts_boundary_before_removing_uncertainty(self) -> None:
        reference = np.zeros((10, 10), dtype=bool)
        candidate = np.zeros_like(reference)
        uncertainty = np.zeros_like(reference)
        reference[2:7, 2:7] = True
        candidate[3:7, 2:7] = True
        uncertainty[2, 2:7] = True

        self.assertAlmostEqual(
            contour_score(reference, candidate, uncertainty), 96.42857142857143, places=12
        )

    def test_masks_accept_bool_or_unit_coverage_and_require_matching_shapes(self) -> None:
        reference = np.zeros((4, 4), dtype=np.float64)
        candidate = np.zeros_like(reference)
        uncertainty = np.zeros_like(reference)
        reference[1, 1] = 0.5
        candidate[1, 1] = 1.0

        self.assertEqual(silhouette_score(reference, candidate, uncertainty), 100.0)
        with self.assertRaisesRegex(ValueError, "same shape"):
            silhouette_score(reference, candidate[:3], uncertainty)
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            silhouette_score(reference + 2.0, candidate, uncertainty)
        with self.assertRaisesRegex(TypeError, "bool or floating-point"):
            silhouette_score(reference.astype(np.uint8), candidate, uncertainty)


if __name__ == "__main__":
    unittest.main()
