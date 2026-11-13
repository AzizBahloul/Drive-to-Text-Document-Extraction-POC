"""Result types shared by the extraction pipeline."""

from dataclasses import dataclass, field
from enum import StrEnum


class Method(StrEnum):
    PDF_PARSER = "pdf-parser"
    OCR = "ocr"
    MIXED = "mixed"


@dataclass(frozen=True)
class PageResult:
    number: int
    method: Method
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)

    def to_dict(self) -> dict:
        return {"page": self.number, "method": self.method.value, "chars": self.char_count}


@dataclass
class ExtractionResult:
    method: Method
    text: str
    pages: list[PageResult] = field(default_factory=list)
    ocr_language: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @classmethod
    def from_pages(cls, pages: list[PageResult], ocr_language: str | None) -> "ExtractionResult":
        methods = {page.method for page in pages}
        if methods == {Method.PDF_PARSER}:
            method = Method.PDF_PARSER
        elif methods == {Method.OCR}:
            method = Method.OCR
        elif not methods:
            method = Method.PDF_PARSER
        else:
            method = Method.MIXED
        text = "\n\n".join(page.text for page in pages if page.text.strip())
        used_ocr = method is not Method.PDF_PARSER
        return cls(
            method=method, text=text, pages=pages, ocr_language=ocr_language if used_ocr else None
        )
