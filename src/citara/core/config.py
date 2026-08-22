from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    """Populate os.environ from a `.env` file, if one is present.

    Settings below are read at import time, so this has to run first --
    which is also why it is done here rather than by a caller.

    Real environment variables always win: a `.env` only fills in what is
    not already set, so `docker compose` and shell exports keep precedence.
    Before this existed, `.env` was silently inert outside docker compose --
    the file looked like configuration but did nothing.
    """

    # Tests set this so a developer's real `.env` can never leak into the
    # suite. Without it, whether a test passes depends on whether the machine
    # running it happens to have credentials on disk.
    if os.getenv("CITARA_SKIP_DOTENV", "").strip().lower() in {"1", "true", "yes", "on"}:
        return

    candidates = []
    override = os.getenv("CITARA_ENV_FILE")
    if override:
        candidates.append(Path(override))
    else:
        candidates.append(Path.cwd() / ".env")
        # Also check the repo root, so scripts run from a subdirectory work.
        candidates.append(Path(__file__).resolve().parents[3] / ".env")

    for path in candidates:
        if not path.is_file():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export ") :].strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
        return


_load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _data_root() -> str:
    """Corpus location, defaulting to a sibling directory OUTSIDE the checkout.

    `../citara` would resolve back into a checkout named `citara`, which is how corpus
    data previously ended up inside the repository. Keep this pointing at a sibling that
    cannot collide with the checkout's own name.
    """
    return os.getenv("CITARA_DATA_ROOT", "../citara-data").rstrip("/")


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{_data_root()}/citara.db")
    object_store_type: str = os.getenv("OBJECT_STORE_TYPE", "local")
    object_store_path: str = os.getenv("OBJECT_STORE_PATH", f"{_data_root()}/object-store")
    source_artifact_root: str = os.getenv("SOURCE_ARTIFACT_ROOT", f"{_data_root()}/source-artifacts")
    source_state_root: str = os.getenv("SOURCE_STATE_ROOT", f"{_data_root()}/import-state")
    embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "local")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "deterministic-hash-v1")
    embedding_dimensions: int = int(os.getenv("EMBEDDING_DIMENSIONS", "8"))
    # Server-side query-translation fallback (used only when a client calls
    # search_knowledge/retrieve_context_pack without its own query_translated).
    # Defaults to a no-op so local/offline/test runs never touch the network.
    translation_provider: str = os.getenv("TRANSLATION_PROVIDER", "noop")
    translation_model: str = os.getenv("TRANSLATION_MODEL", "gpt-4o-mini")
    transcription_provider: str = os.getenv("TRANSCRIPTION_PROVIDER", "local")
    ocr_provider: str = os.getenv("OCR_PROVIDER", "local")
    default_tenant_id: str = os.getenv("DEFAULT_TENANT_ID", "local")
    default_user_id: str = os.getenv("DEFAULT_USER_ID", "owner")
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    azure_openai_api_key: str | None = os.getenv("AZURE_OPENAI_API_KEY")
    azure_openai_endpoint: str | None = os.getenv("AZURE_OPENAI_ENDPOINT")
    azure_openai_deployment: str | None = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    azure_openai_api_version: str = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
    local_embedding_model_path: str | None = os.getenv("LOCAL_EMBEDDING_MODEL_PATH")
    whisper_model: str | None = os.getenv("WHISPER_MODEL")
    max_upload_size_mb: int = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))
    allow_temp_audio_download: bool = _get_bool("ALLOW_TEMP_AUDIO_DOWNLOAD", True)
    store_external_media_by_default: bool = _get_bool("STORE_EXTERNAL_MEDIA_BY_DEFAULT", False)


settings = Settings()
