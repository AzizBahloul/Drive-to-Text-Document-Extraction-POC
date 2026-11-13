import os
from pathlib import Path

import pytest
from PIL import Image

from app.extraction import ocr
from tests.conftest import requires_tesseract


def test_clean_text_collapses_blank_runs_and_form_feeds() -> None:
    raw = "line one  \n\n\n\nline two\x0c\n   \n"
    assert ocr.clean_text(raw) == "line one\n\nline two"


def test_prepare_image_upscales_small_images_to_grayscale() -> None:
    small = Image.new("RGBA", (300, 200), "white")
    prepared = ocr.prepare_image(small)
    assert prepared.mode == "L"
    assert min(prepared.size) >= ocr.MIN_SIDE_PX or prepared.size == (1200, 800)


@pytest.mark.parametrize("bad", ["", "english", "fra+", "FR", "eng;rm -rf /"])
def test_validate_language_rejects_malformed_codes(bad: str) -> None:
    with pytest.raises(ValueError):
        ocr.validate_language(bad)


@requires_tesseract
def test_validate_language_reports_missing_packs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "installed_languages", lambda: ("eng",))
    assert ocr.validate_language("ENG") == "eng"
    with pytest.raises(ocr.OcrUnavailableError) as info:
        ocr.validate_language("eng+xyz")
    assert "xyz" in str(info.value)


@requires_tesseract
def test_tesseract_reads_a_rendered_sentence(text_png: Path) -> None:
    with Image.open(text_png) as image:
        text = ocr.ocr_image(image, "eng")
    assert "HELLO" in text.upper()
    assert "WORLD" in text.upper()


def test_tessdata_dir_points_tesseract_at_extra_language_packs(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Language packs installed without root (e.g. fra/ara in ~/.local) must be found.
    monkeypatch.setenv("TESSDATA_PREFIX", "")  # recorded, so teardown restores the original
    monkeypatch.delenv("TESSDATA_PREFIX")
    ocr.configure("tesseract", str(tmp_path))
    assert os.environ["TESSDATA_PREFIX"] == str(tmp_path)
    ocr.configure("tesseract")  # unset: Tesseract's own folder, nothing forced
    assert os.environ["TESSDATA_PREFIX"] == str(tmp_path)  # left as the environment had it
