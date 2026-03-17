from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    app_name: str = "ANav1"
    database_path: Path = ROOT_DIR / "data" / "app.db"
    uploads_dir: Path = ROOT_DIR / "data" / "uploads"
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "25"))
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
    transcription_model: str = os.getenv("OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-transcribe").strip()
    translation_model: str = os.getenv("OPENAI_TRANSLATION_MODEL", "gpt-4o-mini").strip()
    transcription_language: str = os.getenv("OPENAI_TRANSCRIPTION_LANGUAGE", "").strip()

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)


settings = Settings()
