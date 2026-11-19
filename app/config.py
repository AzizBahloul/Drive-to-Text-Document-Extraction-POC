"""Application settings, read from environment variables and an optional .env file."""

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    google_api_key: str | None = Field(default=None, description="Drive API key; optional.")
    google_oauth_client_id: str | None = None
    google_picker_key: str | None = None
    google_project_number: str | None = None
    max_parallel_downloads: int = Field(default=4, ge=1, le=16)
    max_file_size_mb: int = Field(default=25, ge=1, le=500)
    max_parallel_files: int = Field(default=2, ge=1, le=16)
    ocr_default_language: str = Field(default="eng")
    ocr_render_dpi: int = Field(default=300, ge=72, le=600)
    pdf_text_min_chars_per_page: int = Field(default=25, ge=1)
    tesseract_cmd: str = Field(default="tesseract")
    tessdata_dir: str | None = Field(
        default=None, description="Folder of Tesseract language packs; unset = Tesseract's own."
    )
    ocr_timeout_seconds: int = Field(default=60, ge=5, le=3600)
    ocr_page_workers: int = Field(
        default=0, ge=0, le=32, description="Scanned pages OCR'd in parallel per file; 0 = auto."
    )
    max_pdf_pages: int = Field(default=2000, ge=1)
    file_time_limit_seconds: int = Field(default=900, ge=10)
    subfolder_depth: int = Field(
        default=3, ge=0, le=10, description="Subfolder levels listed below the shared folder."
    )
    max_folders: int = Field(
        default=50, ge=1, le=500, description="Folders read per listing, the shared one included."
    )
    max_running_batches: int = Field(default=4, ge=1, le=100)
    batch_retention: int = Field(default=50, ge=1, le=10_000)
    allowed_hosts: str = Field(
        default="localhost,127.0.0.1",
        description="Comma-separated Host header allowlist; '*' disables the check.",
    )
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = Field(default="info")

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def effective_ocr_page_workers(self) -> int:
        """Spread the cores over the files that run at once, between 1 and 4 per file."""
        if self.ocr_page_workers:
            return self.ocr_page_workers
        cores = os.cpu_count() or 2
        return max(1, min(4, cores // self.max_parallel_files))

    @property
    def allowed_host_list(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
