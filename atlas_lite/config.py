from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_PORT = 8090
DEFAULT_HOST = "0.0.0.0"
DEFAULT_CREDENTIALS = Path(__file__).resolve().parents[1] / "kite_credentials"
STREAM_INTERVAL_MS = 200


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    api_key: str
    access_token: str
    credentials_path: Path


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
