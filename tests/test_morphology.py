from __future__ import annotations

import unittest

import numpy as np

from reconstructing_raster_icons.geometry import (
    binary_boundary,
    connected_components,
    count_holes,
    euclidean_disk,
    masked_boundary,
)


class MorphologyTests(unittest.TestCase):
    def test_integer_euclidean_disk(self) -> None:
        disk = euclidean_disk(2)
        self.assertEqual(disk.shape, (5, 5))
        self.assertTrue(disk[0, 2])
        self.assertFalse(disk[0, 0])
        self.assertTrue(disk[1, 1])

    def test_foreground_is_eight_connected_and_background_is_four_connected(self) -> None:
        diagonal = np.array([[True, False], [False, True]])
        self.assertEqual(len(connected_components(diagonal, connectivity=8)), 1)
        self.assertEqual(len(connected_components(diagonal, connectivity=4)), 2)

    def test_ring_has_one_four_connected_hole(self) -> None:
        ring = np.ones((7, 7), dtype=bool)
        ring[2:5, 2:5] = False
        self.assertEqual(count_holes(ring), 1)

    def test_boundary_is_extracted_before_uncertainty_is_removed(self) -> None:
        mask = np.zeros((5, 5), dtype=bool)
        mask[1:4, 1:4] = True
        uncertainty = np.zeros_like(mask)
        uncertainty[1, 1:4] = True

        full_boundary = binary_boundary(mask)
        comparison_boundary = masked_boundary(mask, uncertainty)

        self.assertTrue(full_boundary[2, 1])
        self.assertTrue(comparison_boundary[2, 1])
        self.assertFalse(comparison_boundary[1, 2])
        self.assertFalse(comparison_boundary[2, 2])


if __name__ == "__main__":
    unittest.main()
