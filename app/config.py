from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the service.

    Paths default to directories inside the repository so the app can run
    without machine-specific configuration.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "PDF Table of Contents Generator"
    base_dir: Path = Field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    data_dir_name: str = "data"
    upload_dir_name: str = "uploads"
    output_dir_name: str = "output"

    job_timeout_seconds: int = 600

    eicas_ocr_enabled: bool = True
    ocr_language: str = "eng"
    ocr_dpi: int = 220
    tessdata_dir: Path | None = None

    max_upload_mb: int = 100
    heading_confidence_threshold: float = 0.55
    numbered_heading_threshold: float = 0.45

    toc_title: str = "Table of Contents"
    toc_font: str = "helv"
    toc_margin_x: float = 54.0
    toc_margin_top: float = 54.0
    toc_margin_bottom: float = 54.0
    toc_title_font_size: float = 22.0
    toc_entry_font_size: float = 10.5
    toc_line_height: float = 16.0
    toc_indent_per_level: float = 18.0

    @property
    def data_dir(self) -> Path:
        return self.base_dir / self.data_dir_name

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / self.upload_dir_name

    @property
    def output_dir(self) -> Path:
        return self.data_dir / self.output_dir_name

    @property
    def documents_index_path(self) -> Path:
        return self.data_dir / "documents.json"

    @property
    def jobs_index_path(self) -> Path:
        return self.data_dir / "jobs.json"

    @property
    def activity_log_path(self) -> Path:
        return self.data_dir / "activity.log"


@lru_cache
def get_settings() -> Settings:
    return Settings()
