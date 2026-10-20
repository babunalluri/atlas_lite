"""UI login session for Live agent toggle."""

from __future__ import annotations

from atlas_lite.ui_auth import (
    COOKIE_NAME,
    issue_session,
    read_session,
    ui_auth_configured,
    ui_email,
    verify_login,
)


def _configure_auth(monkeypatch, *, email="ops@example.com", password="secret-pass", secret="test-secret") -> None:
    monkeypatch.setenv("ATLAS_LITE_UI_EMAIL", email)
    monkeypatch.setenv("ATLAS_LITE_UI_PASSWORD", password)
    monkeypatch.setenv("ATLAS_LITE_SESSION_SECRET", secret)
    monkeypatch.delenv("ATLAS_LITE_API_TOKEN", raising=False)


def test_verify_login(monkeypatch) -> None:
    _configure_auth(monkeypatch)
    assert ui_auth_configured() is True
    assert verify_login("ops@example.com", "secret-pass") is True
    assert verify_login("Ops@Example.com", "secret-pass") is True
    assert verify_login("ops@example.com", "wrong") is False
    assert verify_login("other@x.com", "secret-pass") is False


def test_session_roundtrip(monkeypatch) -> None:
    _configure_auth(monkeypatch)
    token = issue_session("ops@example.com")
    sess = read_session(token)
    assert sess is not None
    assert sess["email"] == "ops@example.com"
    assert read_session(token + "x") is None
    assert COOKIE_NAME == "atlas_ui_session"


def test_auth_not_configured_without_password(monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_UI_EMAIL", "ops@example.com")
    monkeypatch.setenv("ATLAS_LITE_SESSION_SECRET", "test-secret")
    monkeypatch.delenv("ATLAS_LITE_UI_PASSWORD", raising=False)
    monkeypatch.delenv("ATLAS_LITE_API_TOKEN", raising=False)
    assert ui_auth_configured() is False
    assert verify_login("ops@example.com", "anything") is False


def test_auth_not_configured_without_email(monkeypatch) -> None:
    monkeypatch.delenv("ATLAS_LITE_UI_EMAIL", raising=False)
    monkeypatch.setenv("ATLAS_LITE_UI_PASSWORD", "secret-pass")
    monkeypatch.setenv("ATLAS_LITE_SESSION_SECRET", "test-secret")
    assert ui_email() == ""
    assert ui_auth_configured() is False


def test_auth_not_configured_without_session_secret(monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_UI_EMAIL", "ops@example.com")
    monkeypatch.setenv("ATLAS_LITE_UI_PASSWORD", "secret-pass")
    monkeypatch.delenv("ATLAS_LITE_SESSION_SECRET", raising=False)
    monkeypatch.delenv("ATLAS_LITE_API_TOKEN", raising=False)
    assert ui_auth_configured() is False
    assert verify_login("ops@example.com", "secret-pass") is False
    assert read_session("anything.bad") is None


def test_api_token_counts_as_session_secret(monkeypatch) -> None:
    monkeypatch.setenv("ATLAS_LITE_UI_EMAIL", "ops@example.com")
    monkeypatch.setenv("ATLAS_LITE_UI_PASSWORD", "secret-pass")
    monkeypatch.delenv("ATLAS_LITE_SESSION_SECRET", raising=False)
    monkeypatch.setenv("ATLAS_LITE_API_TOKEN", "api-token-as-secret")
    assert ui_auth_configured() is True
    token = issue_session("ops@example.com")
    assert read_session(token) is not None


def test_non_ascii_cookie_rejected(monkeypatch) -> None:
    _configure_auth(monkeypatch)
    assert read_session("abcé.def") is None
    assert read_session("abc.\u2603") is None
    assert read_session("好.sig") is None
