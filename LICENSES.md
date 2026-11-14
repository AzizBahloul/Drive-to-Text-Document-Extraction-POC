# Third-party licences

Every runtime dependency, development tool, system component and asset used by this
project, with its licence and whether it may be used in a closed-source commercial
product. Versions are the ones resolved on 13 November 2026; the ranges pinned in
`pyproject.toml` stay within the same licence terms.

The project itself is released under the MIT licence (see `LICENSE`). No copyleft
(GPL / AGPL) component is used at runtime; the alternatives that were rejected for that
reason are listed at the end.

## Runtime dependencies (Python)

| Dependency | Version | Licence | Commercial use | Note |
| --- | --- | --- | --- | --- |
| fastapi | 0.162.6 | MIT | Yes | |
| starlette | 1.10.0 | BSD-3-Clause | Yes | Pulled in by FastAPI. |
| uvicorn | 0.54.0 | BSD-3-Clause | Yes | `uvicorn[standard]` adds httptools (MIT), uvloop (MIT/Apache-2.0), websockets (BSD-3), watchfiles (MIT), python-dotenv (BSD-3), PyYAML (MIT). |
| pydantic | 2.14.3 | MIT | Yes | |
| pydantic-settings | 2.16.3 | MIT | Yes | Reads `.env`. |
| httpx | 0.28.1 | BSD-3-Clause | Yes | Drive listing and downloads. Depends on httpcore (BSD-3), h11 (MIT), anyio (MIT), certifi (MPL-2.0, file-level copyleft only, unmodified). |
| pypdfium2 | 5.15.2 | Apache-2.0 OR BSD-3-Clause | Yes | Bundles Google's PDFium (BSD-3-Clause) and its third-party libraries (FreeType, HarfBuzz, ICU, libjpeg-turbo, OpenJPEG, libpng, libtiff, zlib, Abseil: all permissive). Ship the notices found in the wheel's `BUILD_LICENSES/` directory with any binary distribution. |
| pytesseract | 0.3.13 | Apache-2.0 | Yes | Thin wrapper that calls the `tesseract` binary. Keep its NOTICE if redistributed. |
| Pillow | 13.0.0 | MIT-CMU (HPND) | Yes | Image loading, resizing, grayscale. |

## System components

| Component | Version | Licence | Commercial use | Note |
| --- | --- | --- | --- | --- |
| Tesseract OCR engine | 5.3 (Debian/Ubuntu packages) | Apache-2.0 | Yes | Keep the NOTICE file when redistributing. |
| Leptonica (Tesseract dependency) | 1.82 | BSD-2-Clause | Yes | |
| tessdata language packs `eng`, `fra`, `ara` | 4.1.0 (`tesseract-ocr-*` packages, built from `tessdata_fast`) | Apache-2.0 | Yes | `tessdata`, `tessdata_best` and `tessdata_fast` are all Apache-2.0. |
| Python | 3.12 | PSF-2.0 | Yes | |
| Docker base image `python:3.12-slim-bookworm` | bookworm | Debian packages, mixed permissive and copyleft | Yes | System packages are used, not linked into or distributed with the application code; the usual Debian image situation. |

## Development dependencies (not shipped)

| Dependency | Version | Licence | Commercial use | Note |
| --- | --- | --- | --- | --- |
| pytest | 9.2.0 | MIT | Yes | |
| pytest-asyncio | 1.5.0 | Apache-2.0 | Yes | |
| ruff | 0.17.5 | MIT | Yes | |

## Frontend assets

| Asset | Version | Licence | Commercial use | Note |
| --- | --- | --- | --- | --- |
| Schibsted Grotesk (Google Fonts) | latest | OFL-1.1 | Yes | Loaded from fonts.googleapis.com; falls back to the system sans-serif offline. |
| Newsreader (Google Fonts) | latest | OFL-1.1 | Yes | Used for extracted text; falls back to the system serif. |

No JavaScript or CSS library is used; the page is hand-written.

## Alternatives considered and rejected on licence grounds

| Alternative | Licence | Why it was not used |
| --- | --- | --- |
| PyMuPDF (fitz) | AGPL-3.0, or a paid Artifex licence | AGPL would force a closed-source product to either open its source or buy a licence. pypdfium2 gives the same text extraction and rendering under permissive terms. |
| pdf2image + poppler | MIT wrapper, but poppler-utils is GPL-2.0-or-later | Rendering pages would depend on GPL binaries; poppler's README states that programs calling it must be GPL. pypdfium2 renders pages without it. |
| OCRmyPDF | MPL-2.0, but requires Ghostscript (AGPL-3.0) | Same AGPL problem through Ghostscript. |
| Surya OCR | Apache-2.0 code, but model weights are OpenRAIL-M with a revenue cap for commercial use | Weights are not freely usable in a commercial product above the cap. |
| EasyOCR / PaddleOCR / docTR / RapidOCR | Apache-2.0 | Licence is fine; rejected only for size (PyTorch or Paddle runtime, hundreds of MB) in a POC. Any of them could replace Tesseract behind `app/extraction/ocr.py`. |
| Google Cloud Vision, AWS Textract, Azure Document Intelligence | Proprietary, paid | The brief prefers self-hosted tools; no cloud OCR is used. |
