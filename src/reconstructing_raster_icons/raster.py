"""Safe one-pass raster intake and frozen-reference normalization."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from io import BytesIO
from pathlib import Path
import math
from typing import Literal
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


@dataclass(frozen=True)
class NormalizationDecision:
    """A user-confirmed foreground/background decision for ambiguous references."""

    background_luminance: float
    foreground_luminance: float
    polarity: Literal["dark", "light"]


@dataclass(frozen=True)
class FrozenPlacement:
    """A frozen, reusable source-to-canonical-canvas raster transform."""

    image: Image.Image
    source_size: tuple[int, int]
    canvas_size: tuple[int, int]
    resampled_size: tuple[int, int]
    scale_x: float
    scale_y: float
    offset_x: int
    offset_y: int
    fit_mode: Literal["contain", "cover", "stretch"]


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


def _resize_dimension(source: int, scale: Fraction) -> int:
    return _round_half_up(Fraction(source, 1) * scale)


def _resample_rgba(source: Image.Image, size: tuple[int, int]) -> Image.Image:
    return source.convert("RGBA").resize(size, Image.Resampling.LANCZOS)


def _composite_with_crop(
    source: Image.Image, canvas_size: tuple[int, int], offset: tuple[int, int]
) -> Image.Image:
    """Composite a possibly off-canvas source without depending on Pillow crop behaviour."""
    canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    offset_x, offset_y = offset
    source_left = max(0, -offset_x)
    source_top = max(0, -offset_y)
    source_right = min(source.width, canvas.width - offset_x)
    source_bottom = min(source.height, canvas.height - offset_y)
    if source_left < source_right and source_top < source_bottom:
        visible = source.crop((source_left, source_top, source_right, source_bottom))
        canvas.alpha_composite(visible, (max(offset_x, 0), max(offset_y, 0)))
    return canvas


def place_raster(
    image: Image.Image,
    ratio: Fraction,
    *,
    fit_mode: Literal["contain", "cover", "stretch"] = "contain",
    confirmed: bool = False,
) -> FrozenPlacement:
    """Apply and record the one frozen reference transform on the canonical canvas.

    ``contain`` is the default.  ``cover`` and ``stretch`` may only be used
    after an explicit confirmed decision has been recorded by the caller.
    Odd residual pixels are deterministically kept on the right/bottom.
    """
    if not isinstance(image, Image.Image):
        raise TypeError("image must be a PIL Image")
    if fit_mode not in {"contain", "cover", "stretch"}:
        raise InvalidInputError("fit mode must be contain, cover, or stretch")
    if fit_mode != "contain" and confirmed is not True:
        raise InvalidInputError("cover and stretch require an explicit confirmed decision")
    source_size = image.size
    source_width, source_height = source_size
    if source_width <= 0 or source_height <= 0:
        raise InvalidInputError("raster cannot be empty")
    canvas_size = canonical_size(ratio)
    canvas_width, canvas_height = canvas_size

    if fit_mode == "stretch":
        scale_x = Fraction(canvas_width, source_width)
        scale_y = Fraction(canvas_height, source_height)
        resampled_size = canvas_size
        offset_x = offset_y = 0
    else:
        width_scale = Fraction(canvas_width, source_width)
        height_scale = Fraction(canvas_height, source_height)
        scale = min(width_scale, height_scale) if fit_mode == "contain" else max(width_scale, height_scale)
        scale_x = scale_y = scale
        resampled_size = (_resize_dimension(source_width, scale), _resize_dimension(source_height, scale))
        scaled_width, scaled_height = resampled_size
        if fit_mode == "contain":
            # A rational min scale cannot exceed either canvas dimension; keep that true after rounding.
            resampled_size = (min(scaled_width, canvas_width), min(scaled_height, canvas_height))
            scaled_width, scaled_height = resampled_size
            offset_x = (canvas_width - scaled_width) // 2
            offset_y = (canvas_height - scaled_height) // 2
        else:
            resampled_size = (max(scaled_width, canvas_width), max(scaled_height, canvas_height))
            scaled_width, scaled_height = resampled_size
            offset_x = -((scaled_width - canvas_width) // 2)
            offset_y = -((scaled_height - canvas_height) // 2)

    placed = _composite_with_crop(_resample_rgba(image, resampled_size), canvas_size, (offset_x, offset_y))
    return FrozenPlacement(
        image=placed,
        source_size=source_size,
        canvas_size=canvas_size,
        resampled_size=resampled_size,
        scale_x=float(scale_x),
        scale_y=float(scale_y),
        offset_x=offset_x,
        offset_y=offset_y,
        fit_mode=fit_mode,
    )


def apply_frozen_placement(image: Image.Image, placement: FrozenPlacement) -> Image.Image:
    """Apply a previously recorded transform without any independent registration."""
    if not isinstance(image, Image.Image):
        raise TypeError("image must be a PIL Image")
    if image.size != placement.source_size:
        raise InvalidInputError("candidate raster size differs from the frozen reference source size")
    return _composite_with_crop(
        _resample_rgba(image, placement.resampled_size),
        placement.canvas_size,
        (placement.offset_x, placement.offset_y),
    )


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
    decision = NormalizationDecision(
        background_luminance=background,
        foreground_luminance=dark_foreground if dark_contrast > light_contrast else light_foreground,
        polarity="dark" if dark_contrast > light_contrast else "light",
    )
    return normalize_with_decision(rgba, decision)


def normalize_with_decision(image: Image.Image, decision: NormalizationDecision) -> NormalizationEstimate:
    """Compute frozen coverage from an explicit confirmed normalization decision."""
    if not isinstance(image, Image.Image):
        raise TypeError("image must be a PIL Image")
    if not isinstance(decision, NormalizationDecision):
        raise TypeError("decision must be a NormalizationDecision")
    background = float(decision.background_luminance)
    foreground = float(decision.foreground_luminance)
    if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in (background, foreground)):
        raise InvalidInputError("normalization luminance values must be finite and within 0..1")
    rgba = _source_profile_to_srgb(ImageOps.exif_transpose(image))
    luminance, alpha = _relative_luminance(rgba)
    if decision.polarity == "dark":
        denominator = background - foreground
        coverage = (background - luminance) / denominator if denominator >= 0.05 else None
    elif decision.polarity == "light":
        denominator = foreground - background
        coverage = (luminance - background) / denominator if denominator >= 0.05 else None
    else:
        raise InvalidInputError("normalization polarity must be dark or light")
    if coverage is None:
        raise InvalidInputError("foreground and background luminance are too similar")
    coverage = np.clip(coverage, 0.0, 1.0).astype(np.float64, copy=False) * alpha
    return NormalizationEstimate(
        background_luminance=background,
        foreground_luminance=foreground,
        polarity=decision.polarity,
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
