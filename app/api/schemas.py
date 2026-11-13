"""Request and response models for the API."""

from typing import Annotated

from pydantic import BaseModel, Field

DriveId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


class FolderRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048, description="Public Drive folder link.")


class FileOut(BaseModel):
    id: str
    name: str
    mime_type: str
    kind: str
    size: int | None
    folder: str = Field(
        default="", description="Subfolder path the file is in; empty at the top level."
    )


class FolderResponse(BaseModel):
    folder_id: str
    folder_url: str
    folder_name: str | None
    source: str
    files: list[FileOut]
    skipped: int = Field(description="Entries ignored because they are not PDFs or images.")
    folders_scanned: int = Field(default=1, description="Folders listed, the shared one included.")
    folders_skipped: int = Field(
        default=0, description="Subfolders left out: too deep, over the limit, or unreadable."
    )


class BatchRequest(BaseModel):
    folder_id: DriveId
    file_ids: list[DriveId] = Field(min_length=1, max_length=200)
    ocr_language: str | None = Field(
        default=None, max_length=64, description="Tesseract language, e.g. fra+eng."
    )


class BatchCreated(BaseModel):
    batch_id: str


class CapabilitiesResponse(BaseModel):
    drive_mode: str
    ocr_available: bool
    ocr_languages: list[str]
    ocr_default_language: str
    max_file_size_mb: int
    max_parallel_files: int


class ErrorResponse(BaseModel):
    """Body of a Drive error: a message to show the user and a machine-readable kind."""

    error: str
    kind: str


class DetailResponse(BaseModel):
    """Body of every other refusal (FastAPI's ``HTTPException``)."""

    detail: str
