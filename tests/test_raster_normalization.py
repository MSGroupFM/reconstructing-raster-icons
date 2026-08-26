from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np
from PIL import Image

from reconstructing_raster_icons.errors import InvalidInputError
from reconstructing_raster_icons.raster import (
    NormalizationDecision,
    apply_frozen_placement,
    build_uncertainty,
    canonical_size,
    estimate_normalization,
    load_raster,
    normalize_with_decision,
    place_raster,
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

    def test_contain_placement_is_centered_and_reusable_without_deformation(self) -> None:
        source = Image.new("RGBA", (1, 1), (20, 30, 40, 255))
        placement = place_raster(source, Fraction(3, 2))

        self.assertEqual(placement.canvas_size, (1024, 683))
        self.assertEqual(placement.source_size, (1, 1))
        self.assertEqual(placement.resampled_size, (683, 683))
        self.assertEqual((placement.offset_x, placement.offset_y), (170, 0))
        self.assertEqual(placement.scale_x, placement.scale_y)
        self.assertEqual(placement.image.getpixel((0, 0))[3], 0)
        self.assertEqual(placement.image.getpixel((170, 0))[3], 255)
        self.assertEqual(placement.image.getpixel((852, 682))[3], 255)
        self.assertEqual(placement.image.getpixel((853, 682))[3], 0)
        self.assertEqual(apply_frozen_placement(source, placement).tobytes(), placement.image.tobytes())

    def test_contain_placement_supports_canonical_landscape_and_portrait_ratios(self) -> None:
        landscape = place_raster(Image.new("RGBA", (16, 9), "black"), Fraction(16, 9))
        portrait = place_raster(Image.new("RGBA", (9, 16), "black"), Fraction(9, 16))
        square = place_raster(Image.new("RGBA", (1, 1), "black"), Fraction(1, 1))

        self.assertEqual(landscape.canvas_size, (1024, 576))
        self.assertEqual(landscape.offset_x, 0)
        self.assertEqual(landscape.offset_y, 0)
        self.assertEqual(portrait.canvas_size, (576, 1024))
        self.assertEqual(portrait.offset_x, 0)
        self.assertEqual(portrait.offset_y, 0)
        self.assertEqual(square.canvas_size, (1024, 1024))
        self.assertEqual(square.scale_x, square.scale_y)

    def test_cover_and_stretch_require_confirmation_and_have_fixed_semantics(self) -> None:
        source = Image.new("RGBA", (1, 1), "black")
        with self.assertRaises(InvalidInputError):
            place_raster(source, Fraction(3, 2), fit_mode="cover")
        with self.assertRaises(InvalidInputError):
            place_raster(source, Fraction(3, 2), fit_mode="stretch")

        cover = place_raster(source, Fraction(3, 2), fit_mode="cover", confirmed=True)
        stretch = place_raster(source, Fraction(3, 2), fit_mode="stretch", confirmed=True)
        self.assertEqual(cover.resampled_size, (1024, 1024))
        self.assertEqual((cover.offset_x, cover.offset_y), (0, -170))
        self.assertEqual(stretch.resampled_size, (1024, 683))
        self.assertNotEqual(stretch.scale_x, stretch.scale_y)

    def test_explicit_normalization_decision_handles_ambiguous_transparent_black(self) -> None:
        image = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
        image.putpixel((4, 4), (0, 0, 0, 255))
        with self.assertRaises(InvalidInputError):
            estimate_normalization(image)

        normalized = normalize_with_decision(
            image,
            NormalizationDecision(background_luminance=1.0, foreground_luminance=0.0, polarity="dark"),
        )
        self.assertTrue(normalized.mask[4, 4])
        self.assertFalse(normalized.mask[0, 0])
        self.assertEqual(normalized.coverage[0, 0], 0.0)


if __name__ == "__main__":
    unittest.main()
