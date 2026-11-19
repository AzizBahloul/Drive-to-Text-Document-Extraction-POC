"""OCR through Tesseract via pytesseract."""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
from itertools import pairwise
from pathlib import Path

import pytesseract
from PIL import Image, ImageOps

log = logging.getLogger(__name__)

_LANGUAGE_RE = re.compile(r"^[a-z]{3}(?:_[a-z]+)?(?:\+[a-z]{3}(?:_[a-z]+)?)*$")
MIN_SIDE_PX = 1200
# Upper bound on the pixels handed to Tesseract, for input images and for rendered PDF
# pages alike. 60 MP covers an A4 page scanned at 600 dpi; anything larger is either a
# mistake or a decompression bomb that would exhaust memory.
MAX_PIXELS = 60_000_000


class OcrUnavailableError(RuntimeError):
    """Tesseract is missing or a requested language pack is not installed."""


class OcrTimeoutError(RuntimeError):
    """Tesseract ran longer than the configured limit and was stopped."""


_languages_cache: tuple[str, ...] | None = None


def configure(tesseract_cmd: str, tessdata_dir: str | None = None) -> None:
    """Point pytesseract at the configured binary and forget any cached language list.

    ``tessdata_dir`` lets language packs live outside the system folder (no root needed);
    Tesseract reads it from ``TESSDATA_PREFIX``, which its child processes inherit.
    """
    global _languages_cache
    if tesseract_cmd and tesseract_cmd != "tesseract":
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    if tessdata_dir:
        if Path(tessdata_dir).is_dir():
            os.environ["TESSDATA_PREFIX"] = tessdata_dir
        else:
            log.warning("Tessdata folder '%s' does not exist; using system default", tessdata_dir)
            os.environ.pop("TESSDATA_PREFIX", None)
    # Parallelism comes from running several Tesseract processes (pages and files). Letting
    # each one also spawn OpenMP threads oversubscribes the CPU: measured 16-20% slower.
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    _languages_cache = None


def installed_languages() -> tuple[str, ...]:
    """Language packs Tesseract can use, or an empty tuple when it is not installed."""
    global _languages_cache
    if _languages_cache is None:
        _languages_cache = _probe_languages()
    return _languages_cache


def _probe_languages() -> tuple[str, ...]:
    binary = pytesseract.pytesseract.tesseract_cmd
    if shutil.which(binary) is None:
        return ()
    try:
        languages = pytesseract.get_languages(config="")
    except (pytesseract.TesseractError, pytesseract.TesseractNotFoundError, OSError):
        return ()
    return tuple(sorted(lang for lang in languages if lang != "osd"))


def validate_language(language: str) -> str:
    """Return a normalised Tesseract language string, or raise with a clear reason."""
    value = (language or "").strip().lower()
    if not _LANGUAGE_RE.match(value):
        raise ValueError(f"'{language}' is not a valid Tesseract language code (example: fra+eng).")
    available = installed_languages()
    if not available:
        raise OcrUnavailableError("Tesseract is not installed or not on PATH.")
    missing = [part for part in value.split("+") if part not in available]
    if missing:
        raise OcrUnavailableError(
            f"Tesseract language pack(s) not installed: {', '.join(missing)}. "
            f"Available: {', '.join(available)}."
        )
    return value


def deskew(image: Image.Image) -> Image.Image:
    """Straighten modestly tilted text by measuring horizontal ink bands on a small copy."""
    scale = min(1, 800 / max(image.size))
    sample = image.resize(
        tuple(max(1, round(n * scale)) for n in image.size), Image.Resampling.BILINEAR
    )
    low, high = sample.getextrema()
    if min(sample.size) < 32 or high - low < 32:
        return image
    # Dark text on light paper. A white margin avoids scoring artificial rotation edges.
    sample = ImageOps.expand(sample.point(lambda p: 255 if p < 160 else 0), border=20, fill=0)

    def score(degrees: float) -> int:
        rotated = sample.rotate(degrees, resample=Image.Resampling.BILINEAR) if degrees else sample
        rows = rotated.resize((1, rotated.height), Image.Resampling.BOX).tobytes()
        return sum((a - b) ** 2 for a, b in pairwise(rows))

    base = score(0)
    coarse = max(range(-6, 7, 2), key=score)
    angle = max((coarse + d for d in (-1, -0.5, 0, 0.5, 1)), key=score)
    peak = score(angle)
    if abs(angle) < 0.5 or peak < max(50, base * 1.25):
        return image
    return image.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=255)


def prepare_image(image: Image.Image) -> Image.Image:
    """Normalise an image for Tesseract: orientation, grayscale, and a sensible size."""
    image = ImageOps.exif_transpose(image)
    if image.mode not in {"L", "RGB"}:
        image = image.convert("RGB")
    image = ImageOps.grayscale(image)
    width, height = image.size
    shortest = min(width, height)
    if 0 < shortest < MIN_SIDE_PX:
        # Never upscale past the pixel budget: a 300 x 290000 strip would otherwise grow 4x.
        budget = math.sqrt(MAX_PIXELS / (width * height))
        factor = min(MIN_SIDE_PX / shortest, 4.0, budget)
        if factor > 1:
            image = image.resize(
                (round(width * factor), round(height * factor)), Image.Resampling.LANCZOS
            )
    return deskew(image)


def _call_tesseract(image: Image.Image, language: str, psm: int, timeout: float) -> str:
    try:
        return pytesseract.image_to_string(
            image,
            lang=language,
            config=f"--psm {psm}",
            timeout=timeout,  # pyright: ignore[reportArgumentType]
        )
    except RuntimeError as exc:
        if "timeout" in str(exc).lower():
            raise OcrTimeoutError(f"OCR took longer than {timeout:g} s and was stopped.") from exc
        raise


def ocr_image(image: Image.Image, language: str, *, psm: int = 3, timeout: float = 0) -> str:
    """Run Tesseract on a PIL image and return cleaned text.

    ``timeout`` (seconds, 0 for none) kills Tesseract if it runs longer and raises
    ``OcrTimeoutError``.
    """
    prepared = prepare_image(image)
    # pytesseract hands the image over through a temp file in ``image.format``. BMP skips
    # PNG compression on both sides: ~8% faster per page, identical text.
    prepared.format = "BMP"
    try:
        text = _call_tesseract(prepared, language, psm, timeout)
    except OcrTimeoutError:
        raise
    except Exception as exc:
        available = installed_languages()
        if "+" in language and available:
            fallback = "eng" if "eng" in available else available[0]
            log.warning("OCR failed for '%s' (%s); falling back to '%s'", language, exc, fallback)
            text = _call_tesseract(prepared, fallback, psm, timeout)
        else:
            raise

    cleaned = clean_text(text)
    # Preprocessing fallback: if initial pass yielded no text, try an auto-contrast pass
    if not cleaned:
        try:
            enhanced = ImageOps.autocontrast(prepared)
            enhanced.format = "BMP"
            alt_text = _call_tesseract(enhanced, language, psm, timeout)
            alt_cleaned = clean_text(alt_text)
            if alt_cleaned:
                return alt_cleaned
        except Exception:
            pass

    return cleaned


def clean_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\x0c", "").splitlines()]
    collapsed: list[str] = []
    blank = 0
    for line in lines:
        if not line.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        collapsed.append(line)
    return "\n".join(collapsed).strip()
