"""Shared fixtures: synthetic PDFs and images so no binary files live in the repo."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.drive.client import DriveFile, FileKind


def build_text_pdf(lines: list[str], media_box: tuple[int, int] = (612, 792)) -> bytes:
    """Hand-assemble a one-page PDF with a real text layer (Helvetica).

    With no ``lines`` the page has no text layer at all, like a blank scan.
    """
    content_lines = ["BT", "/F1 24 Tf", "72 720 Td", "28 TL"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content_lines.append(f"({escaped}) Tj T*")
    content_lines.append("ET")
    content = "\n".join(content_lines).encode("latin-1")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents 4 0 R " % media_box
        + b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    )
    out += trailer.encode()
    return bytes(out)


def build_text_image(text: str, size: tuple[int, int] = (1400, 500)) -> Image.Image:
    """A white image with large black text, easy for Tesseract to read."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = _font(72)
    draw.multiline_text((60, 60), text, fill="black", font=font, spacing=24)
    return image


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


@pytest.fixture
def text_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "native.pdf"
    path.write_bytes(build_text_pdf(["Quarterly report 2026", "Revenue grew by twelve percent."]))
    return path


@pytest.fixture
def scanned_pdf(tmp_path: Path) -> Path:
    """An image-only PDF: Pillow writes the page as a raster with no text layer."""
    path = tmp_path / "scanned.pdf"
    build_text_image("SCANNED PAGE\nINVOICE 4471").save(path, "PDF", resolution=150)
    return path


@pytest.fixture
def text_png(tmp_path: Path) -> Path:
    path = tmp_path / "note.png"
    build_text_image("HELLO OCR WORLD").save(path, "PNG")
    return path


@pytest.fixture
def broken_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4\nthis is not really a pdf")
    return path


def drive_file(
    name: str, kind: FileKind, size: int | None = 1234, file_id: str | None = None
) -> DriveFile:
    mime = "application/pdf" if kind is FileKind.PDF else "image/png"
    return DriveFile(
        id=file_id or "id-" + name.replace(".", "_"),
        name=name,
        mime_type=mime,
        kind=kind,
        size=size,
    )


requires_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract binary is not installed"
)
