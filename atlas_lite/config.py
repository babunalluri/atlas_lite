from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_PORT = 8090
DEFAULT_HOST = "0.0.0.0"
DEFAULT_CREDENTIALS = Path(__file__).resolve().parents[1] / "kite_credentials"
DEFAULT_LLM_CREDENTIALS = Path(__file__).resolve().parents[1] / "llm_credentials"
DEFAULT_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "openai/gpt-4o-mini"
DEFAULT_OPENAI_FAILOVER_MODEL = "gpt-4o"
STREAM_INTERVAL_MS = 200


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    api_key: str
    access_token: str
    credentials_path: Path


@dataclass(frozen=True)
class LlmCredentials:
    """Primary LLM (usually OpenRouter) plus optional OpenAI failover."""

    api_key: str
    base_url: str = DEFAULT_OPENROUTER_BASE
    model: str = DEFAULT_LLM_MODEL
    openai_api_key: str | None = None
    openai_base_url: str = DEFAULT_OPENAI_BASE
    openai_model: str = DEFAULT_OPENAI_FAILOVER_MODEL
    credentials_path: Path | None = None

    @property
    def has_openai_failover(self) -> bool:
        return bool(self.openai_api_key and str(self.openai_api_key).strip())


def load_settings() -> Settings:
    cred_path = Path(
        os.environ.get("KITE_CREDENTIALS_PATH", str(DEFAULT_CREDENTIALS))
    ).expanduser()
    api_key, access_token = read_kite_credentials(cred_path)
    return Settings(
        host=os.environ.get("ATLAS_LITE_HOST", DEFAULT_HOST),
        port=int(os.environ.get("ATLAS_LITE_PORT", str(DEFAULT_PORT))),
        api_key=api_key,
        access_token=access_token,
        credentials_path=cred_path,
    )


def read_kite_credentials(cred_path: Path) -> tuple[str, str]:
    """Read api_key + access_token from kite_credentials (safe to call for reload)."""
    if not cred_path.is_file():
        raise FileNotFoundError(
            f"Kite credentials not found at {cred_path}. "
            "Set KITE_CREDENTIALS_PATH or place kite_credentials in the project root."
        )
    raw = json.loads(cred_path.read_text(encoding="utf-8"))
    api_key = str(raw.get("api_key") or "").strip()
    access_token = str(raw.get("access_token") or raw.get("token") or "").strip()
    if not api_key or not access_token:
        raise ValueError("kite_credentials must include api_key and access_token")
    return api_key, access_token


def llm_credentials_path() -> Path:
    return Path(
        os.environ.get("ATLAS_LITE_LLM_CREDENTIALS_PATH", str(DEFAULT_LLM_CREDENTIALS))
    ).expanduser()


def read_llm_credentials(cred_path: Path | None = None) -> LlmCredentials | None:
    """Load primary OpenRouter creds + optional OpenAI failover from env/file."""
    path = Path(cred_path).expanduser() if cred_path else llm_credentials_path()
    # Prefer explicit OpenRouter for primary; OPENAI_API_KEY is failover (or primary if alone).
    openrouter_key = str(os.environ.get("OPENROUTER_API_KEY") or "").strip()
    openai_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    base_url = str(os.environ.get("ATLAS_LITE_LLM_BASE_URL") or "").strip()
    model = str(os.environ.get("ATLAS_LITE_LLM_MODEL") or "").strip()
    openai_base = str(os.environ.get("ATLAS_LITE_OPENAI_BASE_URL") or "").strip()
    openai_model = str(os.environ.get("ATLAS_LITE_OPENAI_MODEL") or "").strip()
    file_path: Path | None = None
    if path.is_file():
        file_path = path
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            openrouter_key = openrouter_key or str(
                raw.get("api_key") or raw.get("OPENROUTER_API_KEY") or ""
            ).strip()
            openai_key = openai_key or str(
                raw.get("openai_api_key") or raw.get("OPENAI_API_KEY") or ""
            ).strip()
            base_url = base_url or str(raw.get("base_url") or "").strip()
            model = model or str(raw.get("model") or "").strip()
            openai_base = openai_base or str(raw.get("openai_base_url") or "").strip()
            openai_model = openai_model or str(raw.get("openai_model") or "").strip()

    if openrouter_key:
        return LlmCredentials(
            api_key=openrouter_key,
            base_url=(base_url or DEFAULT_OPENROUTER_BASE).rstrip("/"),
            model=model or DEFAULT_LLM_MODEL,
            openai_api_key=openai_key or None,
            openai_base_url=(openai_base or DEFAULT_OPENAI_BASE).rstrip("/"),
            openai_model=openai_model or DEFAULT_OPENAI_FAILOVER_MODEL,
            credentials_path=file_path,
        )
    if openai_key:
        # OpenAI-only: use as primary, no separate failover.
        # Never inherit an OpenRouter base_url — that yields 401 with an OpenAI key.
        return LlmCredentials(
            api_key=openai_key,
            base_url=(openai_base or DEFAULT_OPENAI_BASE).rstrip("/"),
            model=openai_model or model or DEFAULT_OPENAI_FAILOVER_MODEL,
            openai_api_key=None,
            openai_base_url=(openai_base or DEFAULT_OPENAI_BASE).rstrip("/"),
            openai_model=openai_model or DEFAULT_OPENAI_FAILOVER_MODEL,
            credentials_path=file_path,
        )
    return None
