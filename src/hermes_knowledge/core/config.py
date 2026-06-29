from __future__ import annotations

import os
from dataclasses import dataclass


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./hermes_knowledge_vault.db")
    object_store_type: str = os.getenv("OBJECT_STORE_TYPE", "local")
    object_store_path: str = os.getenv("OBJECT_STORE_PATH", "./data/object-store")
    embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "local")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "deterministic-hash-v1")
    embedding_dimensions: int = int(os.getenv("EMBEDDING_DIMENSIONS", "8"))
    transcription_provider: str = os.getenv("TRANSCRIPTION_PROVIDER", "local")
    ocr_provider: str = os.getenv("OCR_PROVIDER", "local")
    default_tenant_id: str = os.getenv("DEFAULT_TENANT_ID", "local")
    default_user_id: str = os.getenv("DEFAULT_USER_ID", "owner")
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    local_embedding_model_path: str | None = os.getenv("LOCAL_EMBEDDING_MODEL_PATH")
    whisper_model: str | None = os.getenv("WHISPER_MODEL")
    max_upload_size_mb: int = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))
    allow_temp_audio_download: bool = _get_bool("ALLOW_TEMP_AUDIO_DOWNLOAD", True)
    store_external_media_by_default: bool = _get_bool("STORE_EXTERNAL_MEDIA_BY_DEFAULT", False)


settings = Settings()
