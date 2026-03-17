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
    transcription_chunk_seconds: int = int(os.getenv("OPENAI_TRANSCRIPTION_CHUNK_SECONDS", "75"))
    sample_audio_path: str = os.getenv("SAMPLE_AUDIO_PATH", "").strip()

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def sample_audio_file(self) -> Path | None:
        if not self.sample_audio_path:
            return None
        return Path(self.sample_audio_path).expanduser()

    @property
    def sample_audio_available(self) -> bool:
        sample_path = self.sample_audio_file
        return bool(sample_path and sample_path.exists() and sample_path.is_file())


settings = Settings()
