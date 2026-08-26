from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np
from PIL import Image

from reconstructing_raster_icons.errors import InvalidInputError
from reconstructing_raster_icons.raster import (
    build_uncertainty,
    canonical_size,
    estimate_normalization,
    load_raster,
)


class RasterNormalizationTests(unittest.TestCase):
    def test_canonical_ratio_limits(self) -> None:
        self.assertEqual(canonical_size(Fraction(16, 1)), (1024, 64))
        self.assertEqual(canonical_size(Fraction(3, 2)), (1024, 683))
        with self.assertRaises(InvalidInputError):
            canonical_size(Fraction(17, 1))

    def test_loader_normalizes_orientation_and_mode(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "oriented.jpg"
            image = Image.new("RGB", (3, 2), "white")
            image.putpixel((0, 0), (0, 0, 0))
            exif = Image.Exif()
            exif[274] = 6
            image.save(path, exif=exif)

            normalized = load_raster(path)

        self.assertEqual(normalized.mode, "RGBA")
        self.assertEqual(normalized.size, (2, 3))

    def test_loader_rejects_unsupported_format(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "source.gif"
            Image.new("RGBA", (8, 8), "black").save(path, format="GIF")
            with self.assertRaises(InvalidInputError):
                load_raster(path)

    def test_loader_preserves_alpha_in_deterministic_fixture(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "raster" / "transparent-square.png"
        normalized = load_raster(fixture)
        self.assertEqual(normalized.getpixel((0, 0))[3], 0)
        self.assertEqual(normalized.getpixel((3, 3))[3], 255)

    def test_estimator_uses_type_7_percentiles_and_wcag_luminance(self) -> None:
        image = Image.new("RGBA", (12, 12), "white")
        for index, value in enumerate(range(100)):
            image.putpixel((1 + index % 10, 1 + index // 10), (value, value, value, 255))

        estimate = estimate_normalization(image)

        self.assertEqual(estimate.polarity, "dark")
        self.assertAlmostEqual(estimate.background_luminance, 1.0)
        def luminance(value: int) -> float:
            channel = value / 255.0
            return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

        # With 100 sorted values, type-7's 5th percentile lies 95% from rank 4 to 5.
        self.assertAlmostEqual(estimate.foreground_luminance, 0.05 * luminance(4) + 0.95 * luminance(5))
        self.assertTrue(estimate.mask[1, 1])
        self.assertFalse(estimate.mask[0, 0])

    def test_uncertainty_is_reference_only_and_bounded(self) -> None:
        coverage = np.zeros((64, 64), dtype=np.float64)
        coverage[16:48, 16:48] = 1.0
        coverage[15, 16:48] = 0.5

        uncertainty = build_uncertainty(coverage, delta=1)

        self.assertEqual(uncertainty.dtype, np.bool_)
        self.assertLessEqual(uncertainty.mean(), 0.05)
        self.assertTrue(uncertainty[15, 16])
        self.assertTrue(uncertainty[14, 16])

    def test_uncertainty_over_five_percent_is_invalid(self) -> None:
        coverage = np.full((64, 64), 0.5, dtype=np.float64)
        with self.assertRaises(InvalidInputError):
            build_uncertainty(coverage, delta=1)


if __name__ == "__main__":
    unittest.main()
