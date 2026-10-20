"""UI login for operator actions (Live agent toggle).

Credentials come from env — never hard-code email or password in source:

  ATLAS_LITE_UI_EMAIL=...
  ATLAS_LITE_UI_PASSWORD=...
  ATLAS_LITE_SESSION_SECRET=...   # or ATLAS_LITE_API_TOKEN
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

COOKIE_NAME = "atlas_ui_session"
SESSION_TTL_S = 7 * 24 * 3600


def ui_email() -> str:
    return (os.environ.get("ATLAS_LITE_UI_EMAIL") or "").strip().lower()


def ui_password() -> str:
    return os.environ.get("ATLAS_LITE_UI_PASSWORD", "").strip()


def _session_secret_raw() -> str:
    return (
        os.environ.get("ATLAS_LITE_SESSION_SECRET")
        or os.environ.get("ATLAS_LITE_API_TOKEN")
        or ""
    ).strip()


def ui_auth_configured() -> bool:
    """Login is off unless email, password, and a real signing secret are set."""
    return bool(ui_email() and ui_password() and _session_secret_raw())


def _session_secret() -> bytes:
    raw = _session_secret_raw()
    if not raw:
        raise RuntimeError("session_secret_not_configured")
    return raw.encode("utf-8")


def verify_login(email: str, password: str) -> bool:
    if not ui_auth_configured():
        return False
    expect_email = ui_email()
    expect_password = ui_password()
    got_email = (email or "").strip().lower()
    got_password = password or ""
    if not got_email or not got_password:
        return False
    if len(got_email) != len(expect_email) or not secrets.compare_digest(got_email, expect_email):
        return False
    if len(got_password) != len(expect_password) or not secrets.compare_digest(
        got_password, expect_password
    ):
        return False
    return True


def issue_session(email: str) -> str:
    payload = {
        "e": (email or "").strip().lower(),
        "exp": int(time.time()) + int(SESSION_TTL_S),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_session_secret(), body, hashlib.sha256).hexdigest()
    return f"{body.decode('ascii')}.{sig}"


def read_session(token: str | None) -> dict[str, Any] | None:
    raw = (token or "").strip()
    if not raw or "." not in raw:
        return None
    if not _session_secret_raw():
        return None
    body_b64, sig = raw.rsplit(".", 1)
    try:
        body_bytes = body_b64.encode("ascii")
    except UnicodeEncodeError:
        # Garbage / non-ASCII cookie → reject, never 500.
        return None
    expect = hmac.new(_session_secret(), body_bytes, hashlib.sha256).hexdigest()
    if len(sig) != len(expect) or not secrets.compare_digest(sig, expect):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body_bytes))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        exp = int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return None
    if exp < int(time.time()):
        return None
    email = str(payload.get("e") or "").strip().lower()
    if not email or email != ui_email():
        return None
    return {"email": email, "exp": exp}


def session_from_request(cookie_header: str | None, cookie_value: str | None = None) -> dict[str, Any] | None:
    if cookie_value:
        return read_session(cookie_value)
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(f"{COOKIE_NAME}="):
            return read_session(part.split("=", 1)[1])
    return None
