#!/usr/bin/env python3
"""Refresh today's Zerodha Kite access_token and save it to kite_credentials.

Usage:
  1. Put api_key and api_secret in ./kite_credentials (access_token optional).
  2. python3 scripts/kite_get_access_token.py
  3. Log in when the browser opens; paste request_token when prompted.

Prerequisites:
  - Kite Connect app at https://developers.kite.trade
  - Redirect URL on the app (e.g. http://127.0.0.1)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

LOGIN_BASE = "https://kite.zerodha.com/connect/login"
TOKEN_URL = "https://api.kite.trade/session/token"
KITE_VERSION = "3"

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CREDENTIALS = ROOT / "kite_credentials"


def credentials_path() -> Path:
    return Path(os.environ.get("KITE_CREDENTIALS_PATH", str(DEFAULT_CREDENTIALS))).expanduser()


def load_credentials(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(
            f"Credentials not found at {path}. "
            "Create kite_credentials with api_key and api_secret."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return raw


def save_credentials(path: Path, data: dict) -> None:
    payload = json.dumps(data, indent=2) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)


def checksum(api_key: str, request_token: str, api_secret: str) -> str:
    raw = f"{api_key}{request_token}{api_secret}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def login_url(api_key: str) -> str:
    return f"{LOGIN_BASE}?v=3&api_key={urllib.parse.quote(api_key)}"


def exchange_token(api_key: str, api_secret: str, request_token: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "api_key": api_key,
            "request_token": request_token.strip(),
            "checksum": checksum(api_key, request_token.strip(), api_secret),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Kite-Version": KITE_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc

    if body.get("status") == "error":
        raise RuntimeError(body.get("message") or body.get("error_type") or str(body))

    data = body.get("data")
    if not isinstance(data, dict) or not data.get("access_token"):
        raise RuntimeError(f"Unexpected response: {body}")

    return data


def extract_request_token(token_or_url: str) -> str:
    raw = token_or_url.strip()
    if "request_token=" in raw:
        parsed = urllib.parse.urlparse(raw)
        query = urllib.parse.parse_qs(parsed.query)
        if "request_token" in query and query["request_token"]:
            return query["request_token"][0].strip()
        for part in raw.replace("&", "?").split("?"):
            if part.startswith("request_token="):
                return part.split("=", 1)[1].strip()
    return raw


def main() -> int:
    path = credentials_path()
    creds = load_credentials(path)
    api_key = str(creds.get("api_key") or "").strip()
    api_secret = str(creds.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        print(
            f"kite_credentials at {path} must include api_key and api_secret.",
            file=sys.stderr,
        )
        return 1

    url = login_url(api_key)
    print("\n1. Open this URL and log in to Zerodha:\n")
    print(url)
    print("\n2. After login, copy request_token from the browser redirect URL.")
    print("   Example: http://127.0.0.1/?request_token=XXXX&status=success\n")
    print("   After save, credentials are synced to OCI Atlas Lite automatically.\n")

    cli_token = next((arg for arg in sys.argv[1:] if not arg.startswith("-")), None)

    if not cli_token and "--no-browser" not in sys.argv:
        try:
            webbrowser.open(url)
        except OSError:
            pass

    if cli_token:
        request_token = extract_request_token(cli_token)
    else:
        raw_input = input("Paste request_token or redirect URL here: ").strip()
        request_token = extract_request_token(raw_input)

    if not request_token:
        print("request_token is required.", file=sys.stderr)
        return 1

    try:
        session = exchange_token(api_key, api_secret, request_token)
    except Exception as exc:
        print(f"\nAuthentication failed: {exc}", file=sys.stderr)
        return 1

    access_token = str(session["access_token"])
    creds["access_token"] = access_token
    if session.get("refresh_token"):
        creds["refresh_token"] = session["refresh_token"]
    save_credentials(path, creds)

    print("\n--- Success ---")
    print(f"Saved access_token to {path}")
    print(f"user_id: {session.get('user_id', '')}")
    print("Token expires around 06:00 IST tomorrow.")
    _sync_credentials_to_oci(path)
    return 0


def _sync_credentials_to_oci(path: Path) -> None:
    """Copy kite_credentials to the OCI Atlas Lite host (picked up on next 403 / 60s)."""
    if "--no-oci" in sys.argv:
        return
    key = os.environ.get(
        "ATLAS_OCI_KEY",
        str(Path.home() / "MyWork/atlas/keys/atlas-oci.key.key"),
    )
    host = os.environ.get("ATLAS_OCI_HOST", "opc@137.23.61.107")
    remote = os.environ.get("ATLAS_LITE_OCI_DIR", "~/atlas_lite/kite_credentials")
    if not Path(key).is_file():
        return
    import subprocess

    try:
        subprocess.run(
            [
                "scp",
                "-i",
                key,
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                str(path),
                f"{host}:{remote}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        print(f"Synced kite_credentials to {host}:{remote}")
        print(
            "If OCI already shows an expired-token error, it reloads from disk "
            "within about 30–60s (no restart)."
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"OCI sync skipped: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
