"""Safe one-pass raster intake and frozen-reference normalization."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
from pathlib import Path
import math
import warnings

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from .errors import InvalidInputError
from .geometry import dilate


MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_INPUT_PIXELS = 16_000_000
MAX_INPUT_SIDE = 8192
CANONICAL_MAX_SIDE = 1024
VIEWBOX_MAX_SIDE = 64
SUPPORTED_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})

# Pillow otherwise only warns, which is explicitly not an acceptable intake result.
Image.MAX_IMAGE_PIXELS = MAX_INPUT_PIXELS


@dataclass(frozen=True)
class NormalizationEstimate:
    """Frozen automatic normalization result derived exclusively from reference pixels."""

    background_luminance: float
    foreground_luminance: float
    polarity: str
    coverage: NDArray[np.float64]
    mask: NDArray[np.bool_]


def _round_half_up(value: Fraction) -> int:
    """Round a non-negative rational number with ties away from zero."""
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def canonical_size(ratio: Fraction) -> tuple[int, int]:
    """Return the canonical 1024px acceptance canvas for a permitted ratio."""
    if not isinstance(ratio, Fraction) or ratio <= 0:
        raise InvalidInputError("aspect ratio must be a positive Fraction")
    if ratio < Fraction(1, 16) or ratio > Fraction(16, 1):
        raise InvalidInputError("aspect ratio must be between 1:16 and 16:1")
    if ratio >= 1:
        width = CANONICAL_MAX_SIDE
        height = _round_half_up(Fraction(CANONICAL_MAX_SIDE, 1) / ratio)
    else:
        height = CANONICAL_MAX_SIDE
        width = _round_half_up(Fraction(CANONICAL_MAX_SIDE, 1) * ratio)
    if min(width, height) < VIEWBOX_MAX_SIDE:
        raise InvalidInputError("canonical raster minor side must be at least 64 pixels")
    return width, height


def _source_profile_to_srgb(image: Image.Image) -> Image.Image:
    """Convert any embedded ICC profile before converting the image to RGBA."""
    profile = image.info.get("icc_profile")
    try:
        if profile:
            source = ImageCms.ImageCmsProfile(BytesIO(profile))
            destination = ImageCms.createProfile("sRGB")
            output_mode = "RGBA" if "A" in image.getbands() else "RGB"
            converted = ImageCms.profileToProfile(image, source, destination, outputMode=output_mode)
            converted.info["reconstructing_raster_icons_icc_assumption"] = "embedded-profile-to-sRGB"
            return converted.convert("RGBA")
    except (ImageCms.PyCMSError, OSError, ValueError) as error:
        raise InvalidInputError("embedded ICC profile could not be converted to sRGB") from error
    converted = image.convert("RGBA")
    converted.info["reconstructing_raster_icons_icc_assumption"] = "source-assumed-sRGB"
    return converted


def load_raster(path: Path) -> Image.Image:
    """Load exactly one safe PNG, JPEG, or WebP frame as EXIF-corrected sRGB RGBA."""
    source = Path(path)
    try:
        if not source.is_file():
            raise InvalidInputError("raster input must be a regular file")
        if source.stat().st_size > MAX_INPUT_BYTES:
            raise InvalidInputError("raster input exceeds 50 MiB")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as opened:
                if opened.format not in SUPPORTED_FORMATS:
                    raise InvalidInputError("only PNG, JPEG, and single-frame WebP are supported")
                if getattr(opened, "n_frames", 1) != 1:
                    raise InvalidInputError("animated or multi-frame rasters are not supported")
                width, height = opened.size
                if width <= 0 or height <= 0 or width > MAX_INPUT_SIDE or height > MAX_INPUT_SIDE:
                    raise InvalidInputError("raster dimensions exceed the side limit")
                if width * height > MAX_INPUT_PIXELS:
                    raise InvalidInputError("raster dimensions exceed 16 MP")
                opened.load()
                oriented = ImageOps.exif_transpose(opened)
                return _source_profile_to_srgb(oriented)
    except InvalidInputError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, UnidentifiedImageError) as error:
        raise InvalidInputError("raster decoder rejected the input") from error


def _relative_luminance(rgba: Image.Image) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    pixels = np.asarray(rgba.convert("RGBA"), dtype=np.float64) / 255.0
    rgb = pixels[..., :3]
    linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    luminance = 0.2126 * linear[..., 0] + 0.7152 * linear[..., 1] + 0.0722 * linear[..., 2]
    return luminance.astype(np.float64, copy=False), pixels[..., 3].astype(np.float64, copy=False)


def estimate_normalization(image: Image.Image) -> NormalizationEstimate:
    """Estimate polarity and coverage using the frozen WCAG/type-7 contract."""
    if not isinstance(image, Image.Image):
        raise TypeError("image must be a PIL Image")
    rgba = _source_profile_to_srgb(ImageOps.exif_transpose(image))
    luminance, alpha = _relative_luminance(rgba)
    height, width = luminance.shape
    if not height or not width:
        raise InvalidInputError("raster cannot be empty")
    border_width = max(1, math.ceil(0.05 * min(height, width)))
    outer = np.zeros((height, width), dtype=bool)
    outer[:border_width, :] = True
    outer[-border_width:, :] = True
    outer[:, :border_width] = True
    outer[:, -border_width:] = True
    inner = ~outer
    opaque_inner = inner & (alpha >= 1.0)
    if not np.any(opaque_inner):
        raise InvalidInputError("inner raster area has no opaque pixels")
    border_luminance = luminance[outer]
    background = float(np.median(border_luminance))
    foreground_samples = luminance[opaque_inner]
    dark_foreground = float(np.percentile(foreground_samples, 5, method="linear"))
    light_foreground = float(np.percentile(foreground_samples, 95, method="linear"))
    dark_contrast = background - dark_foreground
    light_contrast = light_foreground - background
    contrast = max(dark_contrast, light_contrast)
    if (
        float(np.var(border_luminance)) > 0.02
        or abs(dark_contrast - light_contrast) < 0.05
        or contrast < 0.25
    ):
        raise InvalidInputError("automatic foreground/background estimate is ambiguous")
    if dark_contrast > light_contrast:
        polarity = "dark"
        foreground = dark_foreground
        denominator = background - foreground
        coverage = (background - luminance) / denominator
    else:
        polarity = "light"
        foreground = light_foreground
        denominator = foreground - background
        coverage = (luminance - background) / denominator
    if denominator < 0.05:
        raise InvalidInputError("foreground and background luminance are too similar")
    coverage = np.clip(coverage, 0.0, 1.0).astype(np.float64, copy=False) * alpha
    return NormalizationEstimate(
        background_luminance=background,
        foreground_luminance=foreground,
        polarity=polarity,
        coverage=coverage,
        mask=coverage >= 0.5,
    )


def build_uncertainty(coverage: NDArray[np.float64], delta: int) -> NDArray[np.bool_]:
    """Build bounded uncertainty only from reference midtone coverage."""
    values = np.asarray(coverage, dtype=np.float64)
    if values.ndim != 2:
        raise ValueError("coverage must be a two-dimensional array")
    if not np.all(np.isfinite(values)) or np.any(values < 0.0) or np.any(values > 1.0):
        raise InvalidInputError("coverage must be finite and within 0..1")
    if isinstance(delta, bool) or not isinstance(delta, (int, np.integer)) or delta < 0:
        raise ValueError("delta must be a non-negative integer")
    midtones = (values > 0.10) & (values < 0.90)
    uncertainty = dilate(midtones, int(delta))
    if uncertainty.mean(dtype=np.float64) > 0.05:
        raise InvalidInputError("uncertainty exceeds 5% of the canonical canvas")
    return uncertainty
