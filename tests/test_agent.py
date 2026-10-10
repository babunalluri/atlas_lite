"""Tests for LLM creds, macro snapshot helpers, gates, paper agent, advisor tools."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from atlas_lite.agent_advisor import AgentAdvisor, SYSTEM_PROMPT
from atlas_lite.agent_gates import AgentGateStore, next_session_end
from atlas_lite.config import read_llm_credentials
from atlas_lite.macro_sentiment import _day_pct, _quote_field, clear_macro_cache, fetch_macro_snapshot
from atlas_lite.paper_agent import PaperAgent

IST = ZoneInfo("Asia/Kolkata")


def test_read_llm_credentials_from_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ATLAS_LITE_LLM_CREDENTIALS_PATH", str(tmp_path / "missing.json"))
    assert read_llm_credentials() is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("ATLAS_LITE_LLM_MODEL", "openai/gpt-4o-mini")
    creds = read_llm_credentials()
    assert creds is not None
    assert creds.api_key == "sk-test"
    assert "openrouter" in creds.base_url


def test_read_llm_credentials_from_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "llm_credentials"
    path.write_text(
        json.dumps({"api_key": "file-key", "model": "anthropic/claude-haiku", "base_url": "https://openrouter.ai/api/v1"}),
        encoding="utf-8",
    )
    creds = read_llm_credentials(path)
    assert creds is not None
    assert creds.api_key == "file-key"
    assert creds.model == "anthropic/claude-haiku"
    assert creds.has_openai_failover is False


def test_read_llm_credentials_openai_only_ignores_openrouter_base(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "llm_credentials"
    path.write_text(
        json.dumps(
            {
                "openai_api_key": "sk-openai-only",
                "openai_model": "gpt-4o",
                "base_url": "https://openrouter.ai/api/v1",
            }
        ),
        encoding="utf-8",
    )
    creds = read_llm_credentials(path)
    assert creds is not None
    assert creds.api_key == "sk-openai-only"
    assert creds.base_url == "https://api.openai.com/v1"
    assert creds.has_openai_failover is False


def test_read_llm_credentials_both_keys_openrouter_priority(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    path = tmp_path / "llm_credentials"
    path.write_text(
        json.dumps(
            {
                "api_key": "sk-or-primary",
                "model": "anthropic/claude-sonnet-4",
                "base_url": "https://openrouter.ai/api/v1",
                "openai_api_key": "sk-openai-failover",
                "openai_model": "gpt-4o",
                "openai_base_url": "https://api.openai.com/v1",
            }
        ),
        encoding="utf-8",
    )
    creds = read_llm_credentials(path)
    assert creds is not None
    assert creds.api_key == "sk-or-primary"
    assert "openrouter" in creds.base_url
    assert creds.model == "anthropic/claude-sonnet-4"
    assert creds.has_openai_failover is True
    assert creds.openai_api_key == "sk-openai-failover"
    assert creds.openai_model == "gpt-4o"


def test_chat_failsover_to_openai_on_401(tmp_path: Path) -> None:
    import httpx
    from atlas_lite.config import LlmCredentials

    gates = AgentGateStore(tmp_path / "gates.json")
    advisor = AgentAdvisor(
        data_dir=tmp_path,
        gates=gates,
        get_context=lambda: {},
        get_paper=lambda: {},
        get_trades=lambda limit: {"ok": True, "trades": [], "count": 0},
        propose_entry=lambda **kw: {"ok": True},
        propose_exit=lambda **kw: {"ok": True},
        tape_ready=lambda: True,
        credentials=LlmCredentials(
            api_key="sk-or-primary",
            model="anthropic/claude-sonnet-4",
            base_url="https://openrouter.ai/api/v1",
            openai_api_key="sk-openai-failover",
            openai_model="gpt-4o",
            openai_base_url="https://api.openai.com/v1",
        ),
    )

    class FakeResp:
        def __init__(self, status_code: int, payload: dict | None = None):
            self.status_code = status_code
            self._payload = payload or {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                req = httpx.Request("POST", "https://example/chat/completions")
                resp = httpx.Response(self.status_code, request=req)
                raise httpx.HTTPStatusError("err", request=req, response=resp)

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            if "openrouter" in url:
                return FakeResp(401)
            return FakeResp(200, {"choices": [{"message": {"content": "ok"}}]})

    with patch("atlas_lite.agent_advisor.httpx.Client", FakeClient):
        out = advisor._chat([{"role": "user", "content": "hi"}])
    assert out["choices"]
    assert advisor.last_provider == "openai_failover"


def test_chat_failsover_to_openai_on_402(tmp_path: Path) -> None:
    import httpx
    from atlas_lite.config import LlmCredentials

    gates = AgentGateStore(tmp_path / "gates.json")
    creds = LlmCredentials(
        api_key="sk-or-primary",
        model="anthropic/claude-sonnet-4",
        base_url="https://openrouter.ai/api/v1",
        openai_api_key="sk-openai-failover",
        openai_model="gpt-4o",
        openai_base_url="https://api.openai.com/v1",
    )
    advisor = AgentAdvisor(
        data_dir=tmp_path,
        gates=gates,
        get_context=lambda: {},
        get_paper=lambda: {},
        get_trades=lambda limit: {"ok": True, "trades": [], "count": 0},
        propose_entry=lambda **kw: {"ok": True},
        propose_exit=lambda **kw: {"ok": True},
        tape_ready=lambda: True,
        credentials=creds,
    )

    class FakeResp:
        def __init__(self, status_code: int, payload: dict | None = None):
            self.status_code = status_code
            self._payload = payload or {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                req = httpx.Request("POST", "https://example/chat/completions")
                resp = httpx.Response(self.status_code, request=req)
                raise httpx.HTTPStatusError("err", request=req, response=resp)

        def json(self) -> dict:
            return self._payload

    calls: list[str] = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            calls.append(url)
            if "openrouter" in url:
                return FakeResp(402)
            return FakeResp(200, {"choices": [{"message": {"content": "ok", "tool_calls": []}}]})

    with patch("atlas_lite.agent_advisor.httpx.Client", FakeClient):
        out = advisor._chat([{"role": "user", "content": "hi"}])
    assert out["choices"]
    assert advisor.last_provider == "openai_failover"
    assert any("openrouter" in u for u in calls)
    assert any("api.openai.com" in u for u in calls)


def test_macro_day_pct_and_null_field() -> None:
    assert _day_pct(110.0, 100.0) == 10.0
    assert _day_pct(None, 100.0) is None
    field = _quote_field(last=None, prev_close=None, as_of=None, error="boom")
    assert field["value"] is None
    assert field["error"] == "boom"


def test_macro_snapshot_uses_cache(monkeypatch) -> None:
    clear_macro_cache()
    fake = {
        "ok": True,
        "fetched_at": "x",
        "crude": _quote_field(last=80.0, prev_close=79.0, as_of="2026-09-29T10:00:00+05:30"),
        "gold": _quote_field(last=None, prev_close=None, as_of=None, error="skip"),
        "usdinr": _quote_field(last=83.0, prev_close=83.0, as_of="2026-09-29T10:00:00+05:30"),
        "es_futures": _quote_field(last=None, prev_close=None, as_of=None, error="skip"),
        "nq_futures": _quote_field(last=None, prev_close=None, as_of=None, error="skip"),
        "india_vix": _quote_field(last=None, prev_close=None, as_of=None, error="skip"),
        "crude_wti": None,
        "crude_brent": None,
        "headlines": [],
        "news_enabled": False,
        "ttl_s": 120,
    }

    calls = {"n": 0}

    def fake_fetch(**_kwargs):
        calls["n"] += 1
        return fake

    # Patch internal path: force first call via monkeypatching httpx is heavy —
    # instead seed cache through module state after one controlled fetch.
    with patch("atlas_lite.macro_sentiment.httpx.Client") as client_cls:
        client = MagicMock()
        client_cls.return_value.__enter__.return_value = client
        client.get.side_effect = Exception("offline")
        out1 = fetch_macro_snapshot(force=True)
        assert out1["crude"]["value"] is None or out1["crude"].get("error")
        out2 = fetch_macro_snapshot(force=False)
        assert out2 is out1 or out2.get("fetched_at") == out1.get("fetched_at")


def test_agent_gate_store_pause_blocks_entries(tmp_path: Path) -> None:
    store = AgentGateStore(tmp_path / "agent_gates.json")
    assert store.entries_allowed("combo") is True
    now = datetime(2026, 9, 29, 10, 30, tzinfo=IST)
    row = store.set_gate("combo", "pause", reason="test", now=now)
    assert row["until"] == "2026-09-29T15:30:00+05:30"
    assert store.entries_allowed("combo", now=now) is False
    store.set_gate("combo", "allow", source="manual", now=now)
    assert store.entries_allowed("combo", now=now) is True
    snap = store.snapshot()
    assert "combo" in snap["gates"]


def test_next_session_end_skips_weekend_and_nse_holiday() -> None:
    fri = datetime(2026, 9, 25, 16, 0, tzinfo=IST)  # after Friday close → Monday
    assert next_session_end(fri).isoformat() == "2026-09-28T15:30:00+05:30"
    # Republic Day 2026 is Monday — roll to Tuesday
    holiday = datetime(2026, 1, 26, 10, 0, tzinfo=IST)
    assert next_session_end(holiday).isoformat() == "2026-01-27T15:30:00+05:30"


def test_agent_gate_until_hhmm_expires_next_day(tmp_path: Path) -> None:
    store = AgentGateStore(tmp_path / "agent_gates.json")
    morning = datetime(2026, 9, 29, 10, 0, tzinfo=IST)
    store.set_gate("combo", "pause", until="15:14", reason="lunch", now=morning)
    assert store.entries_allowed("combo", now=morning) is False
    assert store.entries_allowed("combo", now=datetime(2026, 9, 29, 15, 14, tzinfo=IST)) is True
    assert store.entries_allowed("combo", now=datetime(2026, 9, 30, 10, 0, tzinfo=IST)) is True


def test_agent_gate_after_hours_pause_rolls_to_next_session(tmp_path: Path) -> None:
    store = AgentGateStore(tmp_path / "agent_gates.json")
    evening = datetime(2026, 9, 29, 20, 0, tzinfo=IST)  # Tue after cash close
    row = store.set_gate("iron_fly", "pause", reason="manual evening", now=evening)
    assert row["until"] == "2026-09-30T15:30:00+05:30"
    assert store.entries_allowed("iron_fly", now=evening) is False
    assert store.entries_allowed("iron_fly", now=datetime(2026, 9, 30, 10, 0, tzinfo=IST)) is False
    assert store.entries_allowed("iron_fly", now=datetime(2026, 9, 30, 15, 30, tzinfo=IST)) is True


def test_agent_gate_rejects_junk_until(tmp_path: Path) -> None:
    store = AgentGateStore(tmp_path / "agent_gates.json")
    now = datetime(2026, 9, 29, 10, 0, tzinfo=IST)
    for junk in ("none", "end of day", "tomorrow", "15:99"):
        try:
            store.set_gate("combo", "pause", until=junk, now=now)
            raise AssertionError(f"expected ValueError for {junk!r}")
        except ValueError:
            pass


def test_agent_gate_legacy_junk_until_fails_open(tmp_path: Path) -> None:
    path = tmp_path / "agent_gates.json"
    path.write_text(
        json.dumps({"gates": {"combo": {"mode": "pause", "until": "none", "reason": "old"}}}),
        encoding="utf-8",
    )
    store = AgentGateStore(path)
    assert store.entries_allowed("combo") is True


def test_decision_log_trims_to_max_lines(tmp_path: Path, monkeypatch) -> None:
    gates = AgentGateStore(tmp_path / "gates.json")
    advisor = AgentAdvisor(
        data_dir=tmp_path,
        gates=gates,
        get_context=lambda: {},
        get_paper=lambda: {},
        get_trades=lambda limit: {"ok": True, "trades": [], "count": 0},
        propose_entry=lambda **kw: {"ok": True},
        propose_exit=lambda **kw: {"ok": True},
        tape_ready=lambda: True,
        credentials=None,
    )
    monkeypatch.setattr("atlas_lite.agent_advisor.DECISIONS_MAX_LINES", 5)
    for i in range(12):
        advisor._append_decision({"ok": True, "n": i})
    advisor._trim_decision_log()
    lines = [
        ln for ln in advisor.decisions_path.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert len(lines) == 5
    assert json.loads(lines[0])["n"] == 7
    assert json.loads(lines[-1])["n"] == 11
    # Overflow archived (not dropped); live rewrite is atomic (no .tmp left behind).
    arch = advisor._decisions_archive_path()
    assert arch.is_file()
    archived = [ln for ln in arch.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(archived) == 7
    assert json.loads(archived[0])["n"] == 0
    assert json.loads(archived[-1])["n"] == 6
    assert not advisor.decisions_path.with_suffix(".jsonl.tmp").exists()


def test_scorecard_autopilot_proposes_entry(tmp_path: Path, monkeypatch) -> None:
    from atlas_lite.config import LlmCredentials

    gates = AgentGateStore(tmp_path / "gates.json")
    proposed: dict[str, Any] = {}

    def _propose(**kw):
        proposed.update(kw)
        return {"ok": True, "pending": True, **kw}

    advisor = AgentAdvisor(
        data_dir=tmp_path,
        gates=gates,
        get_context=lambda: {
            "spot": 22600,
            "atm": 22600,
            "spot_chg_open_pct": 0.4,
            "adx": 28,
            "adx_regime": "trend",
            "pdi": 30,
            "mdi": 12,
            "combo": {"side": "B", "signal": "B"},
        },
        get_paper=lambda: {"agent": {}},
        get_trades=lambda limit: {"ok": True, "trades": [], "count": 0},
        propose_entry=_propose,
        propose_exit=lambda **kw: {"ok": True},
        tape_ready=lambda: True,
        credentials=None,
    )
    monkeypatch.setattr("atlas_lite.agent_advisor.SCORECARD_AUTOPILOT", True)
    monkeypatch.setattr("atlas_lite.agent_advisor.is_record_session", lambda now=None: True)
    out = advisor.advise()
    assert out["ok"] is True
    assert out.get("executor") == "scorecard"
    assert out.get("provider") == "scorecard"
    assert proposed.get("side") == "ce"
    assert proposed.get("style") == "long"
    assert any(t.get("tool") == "propose_entry" for t in out.get("tools") or [])


def test_scorecard_autopilot_waits_without_llm(tmp_path: Path, monkeypatch) -> None:
    from atlas_lite.config import LlmCredentials

    gates = AgentGateStore(tmp_path / "gates.json")
    called = {"n": 0}

    advisor = AgentAdvisor(
        data_dir=tmp_path,
        gates=gates,
        get_context=lambda: {
            "spot": 22500,
            "atm": 22500,
            "spot_chg_open_pct": 0.0,
            "adx": 16,
            "adx_regime": "range",
        },
        get_paper=lambda: {"agent": {}},
        get_trades=lambda limit: {"ok": True, "trades": [], "count": 0},
        propose_entry=lambda **kw: called.__setitem__("n", called["n"] + 1) or {"ok": True},
        propose_exit=lambda **kw: {"ok": True},
        tape_ready=lambda: True,
        credentials=LlmCredentials(api_key="sk", model="x", base_url="https://example"),
    )
    monkeypatch.setattr("atlas_lite.agent_advisor.SCORECARD_AUTOPILOT", True)
    monkeypatch.setattr("atlas_lite.agent_advisor.is_record_session", lambda now=None: True)

    def boom(_m):
        raise RuntimeError("LLM must not run in autopilot")

    monkeypatch.setattr(advisor, "_chat", boom)
    out = advisor.advise()
    assert out["ok"] is True
    assert out.get("executor") == "scorecard"
    assert called["n"] == 0
    assert (out.get("expert_choice") or {}).get("chosen", {}).get("action") == "wait"


def test_scorecard_autopilot_due_without_credentials(tmp_path: Path, monkeypatch) -> None:
    gates = AgentGateStore(tmp_path / "gates.json")
    advisor = AgentAdvisor(
        data_dir=tmp_path,
        gates=gates,
        get_context=lambda: {},
        get_paper=lambda: {},
        get_trades=lambda limit: {"ok": True, "trades": [], "count": 0},
        propose_entry=lambda **kw: {"ok": True},
        propose_exit=lambda **kw: {"ok": True},
        tape_ready=lambda: True,
        credentials=None,
    )
    monkeypatch.setattr("atlas_lite.agent_advisor.SCORECARD_AUTOPILOT", True)
    monkeypatch.setattr("atlas_lite.agent_advisor.is_record_session", lambda now=None: True)
    assert advisor.due() is True


def test_llm_wake_reasons_score_regime_exit() -> None:
    from atlas_lite.agent_advisor import llm_wake_reasons

    quiet = {
        "regime": "range",
        "min_entry_score": 3.0,
        "recommended": {"action": "wait", "score": 2.0},
        "candidates": [
            {"action": "wait", "score": 2.0, "agreement": True, "blocked": False},
            {
                "action": "propose_entry",
                "side": "ce",
                "style": "long",
                "score": 2.5,
                "agreement": True,
                "blocked": False,
            },
        ],
    }
    assert llm_wake_reasons(
        quiet, {}, last_regime="range", last_llm_mono=100.0, now_mono=200.0, heartbeat_s=0
    ) == []

    entry = {
        **quiet,
        "recommended": {
            "action": "propose_entry",
            "side": "ce",
            "style": "long",
            "score": 4.0,
        },
    }
    assert "scorecard_entry" in llm_wake_reasons(
        entry, {}, last_regime="range", last_llm_mono=100.0, now_mono=200.0, heartbeat_s=0
    )

    assert any(
        r.startswith("regime_flip")
        for r in llm_wake_reasons(
            quiet, {}, last_regime="trend", last_llm_mono=100.0, now_mono=200.0, heartbeat_s=0
        )
    )
    assert "exit_allowed" in llm_wake_reasons(
        quiet,
        {"position": {"side": "ce", "exit_allowed": True}},
        last_regime="range",
        last_llm_mono=100.0,
        now_mono=200.0,
        heartbeat_s=0,
    )
    # Open book must not wake on entry-candidate scores.
    hot = {
        "regime": "trend",
        "min_entry_score": 3.0,
        "recommended": {"action": "manage"},
        "candidates": [
            {
                "action": "propose_entry",
                "side": "ce",
                "style": "long",
                "score": 5.0,
                "agreement": True,
                "blocked": False,
            }
        ],
    }
    open_quiet = llm_wake_reasons(
        hot,
        {"position": {"side": "ce", "exit_allowed": False}},
        last_regime="trend",
        last_llm_mono=100.0,
        now_mono=200.0,
        heartbeat_s=0,
    )
    assert "candidate_above_threshold" not in open_quiet
    assert "scorecard_entry" not in open_quiet
    assert "heartbeat" in llm_wake_reasons(
        quiet, {}, last_regime="range", last_llm_mono=100.0, now_mono=1000.0, heartbeat_s=900
    )
    assert "cold_start" in llm_wake_reasons(
        quiet, {}, last_regime=None, last_llm_mono=0.0, now_mono=1.0, heartbeat_s=0
    )


def test_advise_skips_llm_when_scorecard_quiet(tmp_path: Path, monkeypatch) -> None:
    from atlas_lite.config import LlmCredentials

    gates = AgentGateStore(tmp_path / "gates.json")
    creds = LlmCredentials(
        api_key="sk-test",
        model="openai/gpt-4o-mini",
        base_url="https://openrouter.ai/api/v1",
    )
    advisor = AgentAdvisor(
        data_dir=tmp_path,
        gates=gates,
        get_context=lambda: {
            "spot": 22500,
            "atm": 22500,
            "spot_chg_open_pct": 0.0,
            "adx": 16,
            "adx_regime": "range",
        },
        get_paper=lambda: {"agent": {}},
        get_trades=lambda limit: {"ok": True, "trades": [], "count": 0},
        propose_entry=lambda **kw: {"ok": True},
        propose_exit=lambda **kw: {"ok": True},
        tape_ready=lambda: True,
        credentials=creds,
    )
    monkeypatch.setattr("atlas_lite.agent_advisor.SCORECARD_AUTOPILOT", False)
    monkeypatch.setattr("atlas_lite.agent_advisor.LLM_SPARSE", True)
    monkeypatch.setattr("atlas_lite.agent_advisor.LLM_HEARTBEAT_S", 0.0)
    monkeypatch.setattr("atlas_lite.agent_advisor.is_record_session", lambda now=None: True)
    monkeypatch.setattr(
        "atlas_lite.agent_advisor.fetch_macro_snapshot",
        lambda: (_ for _ in ()).throw(RuntimeError("macro should not run on skip")),
    )
    called = {"n": 0}

    def boom(_messages):
        called["n"] += 1
        raise RuntimeError("LLM should not be called")

    monkeypatch.setattr(advisor, "_chat", boom)
    # Seed regime so cold_start does not force a call.
    advisor._last_regime = "range"
    advisor._last_llm_mono = time.monotonic()
    out = advisor.advise()
    assert out["ok"] is True
    assert out.get("skipped_llm") is True
    assert called["n"] == 0
    assert advisor.last_skip_reason == "scorecard_quiet"


def test_advise_calls_llm_on_force_even_if_quiet(tmp_path: Path, monkeypatch) -> None:
    from atlas_lite.config import LlmCredentials

    gates = AgentGateStore(tmp_path / "gates.json")
    creds = LlmCredentials(
        api_key="sk-test",
        model="openai/gpt-4o-mini",
        base_url="https://openrouter.ai/api/v1",
    )
    advisor = AgentAdvisor(
        data_dir=tmp_path,
        gates=gates,
        get_context=lambda: {"spot": 22500, "atm": 22500, "adx_regime": "range"},
        get_paper=lambda: {"agent": {}},
        get_trades=lambda limit: {"ok": True, "trades": [], "count": 0},
        propose_entry=lambda **kw: {"ok": True},
        propose_exit=lambda **kw: {"ok": True},
        tape_ready=lambda: True,
        credentials=creds,
    )
    monkeypatch.setattr("atlas_lite.agent_advisor.SCORECARD_AUTOPILOT", False)
    monkeypatch.setattr("atlas_lite.agent_advisor.LLM_SPARSE", True)
    monkeypatch.setattr("atlas_lite.agent_advisor.LLM_HEARTBEAT_S", 0.0)
    monkeypatch.setattr("atlas_lite.agent_advisor.is_record_session", lambda now=None: True)
    monkeypatch.setattr("atlas_lite.agent_advisor.fetch_macro_snapshot", lambda: {"ok": True})
    advisor._last_regime = "range"
    advisor._last_llm_mono = time.monotonic()
    monkeypatch.setattr(
        advisor,
        "_chat",
        lambda messages: {"choices": [{"message": {"content": "forced", "tool_calls": []}}]},
    )
    out = advisor.advise(force=True)
    assert out["ok"] is True
    assert out.get("skipped_llm") is False
    assert "force" in (out.get("wake_reasons") or [])


def test_agent_advise_failure_backs_off_interval(tmp_path: Path, monkeypatch) -> None:
    from atlas_lite.config import LlmCredentials

    gates = AgentGateStore(tmp_path / "gates.json")
    creds = LlmCredentials(
        api_key="sk-test",
        model="openai/gpt-4o-mini",
        base_url="https://openrouter.ai/api/v1",
    )
    advisor = AgentAdvisor(
        data_dir=tmp_path,
        gates=gates,
        get_context=lambda: {"spot": 1, "atm": 1, "ce": 1, "pe": 1},
        get_paper=lambda: {},
        get_trades=lambda limit: {"ok": True, "trades": [], "count": 0},
        propose_entry=lambda **kw: {"ok": True},
        propose_exit=lambda **kw: {"ok": True},
        tape_ready=lambda: True,
        credentials=creds,
    )
    monkeypatch.setattr("atlas_lite.agent_advisor.SCORECARD_AUTOPILOT", False)
    monkeypatch.setattr(advisor, "_chat", lambda messages: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr("atlas_lite.agent_advisor.fetch_macro_snapshot", lambda: {"ok": True})
    monkeypatch.setattr("atlas_lite.agent_advisor.is_record_session", lambda now=None: True)
    out = advisor.advise()
    assert out["ok"] is False
    assert advisor._last_run_mono > 0
    assert advisor.due() is False


def test_agent_due_skips_weekend(tmp_path: Path) -> None:
    from atlas_lite.config import LlmCredentials

    gates = AgentGateStore(tmp_path / "gates.json")
    creds = LlmCredentials(
        api_key="sk-test",
        model="openai/gpt-4o-mini",
        base_url="https://openrouter.ai/api/v1",
    )
    advisor = AgentAdvisor(
        data_dir=tmp_path,
        gates=gates,
        get_context=lambda: {},
        get_paper=lambda: {},
        get_trades=lambda limit: {"ok": True, "trades": [], "count": 0},
        propose_entry=lambda **kw: {"ok": True},
        propose_exit=lambda **kw: {"ok": True},
        tape_ready=lambda: True,
        credentials=creds,
    )
    saturday = datetime(2026, 9, 26, 11, 0, tzinfo=IST)
    assert advisor.due(now=saturday) is False


def test_paper_agent_propose_and_stale_intent(tmp_path: Path) -> None:
    book = PaperAgent(path=tmp_path / "paper_agent.jsonl", lot_size=65)
    now = datetime(2026, 9, 29, 10, 30, tzinfo=IST)
    ok = book.propose_entry(side="ce", reason="tape up", spot=22600.0, now=now)
    assert ok["ok"] is True
    assert book.pending is not None
    # Stale: age past intent_max_age_s
    later = now + timedelta(seconds=book.intent_max_age_s + 5)
    feed = {"ce": 100.0, "pe": 90.0, "ce_symbol": "NFO:CE", "pe_symbol": "NFO:PE"}
    event = book.on_frame(
        now=later,
        feed=feed,
        book=None,
        ce_symbol="NFO:CE",
        pe_symbol="NFO:PE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert event is None
    assert book.last_reject == "stale_intent"
    assert book.pending is None


def test_paper_agent_opens_on_fresh_intent(tmp_path: Path) -> None:
    class _Book:
        def get(self, symbol: str):
            return {"last_price": 55.0 if "CE" in symbol else 40.0}

    bot = PaperAgent(path=tmp_path / "paper_agent.jsonl", lot_size=65)
    now = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    bot.propose_entry(side="ce", reason="skew", spot=22600.0, now=now)
    event = bot.on_frame(
        now=now + timedelta(seconds=5),
        feed={},
        book=_Book(),
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert event is not None
    assert event["event"] == "open"
    assert event["side"] == "ce"
    assert event["gates"] == "agent"
    assert bot.position is not None


def test_agent_daily_loss_stop_default_is_5000(monkeypatch) -> None:
    monkeypatch.delenv("ATLAS_LITE_AGENT_DAILY_LOSS_STOP", raising=False)
    monkeypatch.delenv("ATLAS_LITE_AGENT_MAX_TRADES_DAY", raising=False)
    from atlas_lite.paper_agent import agent_daily_loss_stop, agent_max_entries_per_day

    assert agent_daily_loss_stop() == -5000.0
    assert agent_max_entries_per_day() == 0  # unlimited by default
    monkeypatch.setenv("ATLAS_LITE_AGENT_DAILY_LOSS_STOP", "5000")
    monkeypatch.setenv("ATLAS_LITE_AGENT_MAX_TRADES_DAY", "8")
    assert agent_daily_loss_stop() == -5000.0
    assert agent_max_entries_per_day() == 8


def test_agent_scalp_timing_defaults(monkeypatch) -> None:
    monkeypatch.delenv("ATLAS_LITE_AGENT_COOLDOWN_MIN", raising=False)
    monkeypatch.delenv("ATLAS_LITE_AGENT_ENTRY_UNTIL", raising=False)
    monkeypatch.delenv("ATLAS_LITE_AGENT_ENTRY_AFTER", raising=False)
    monkeypatch.delenv("ATLAS_LITE_AGENT_MIN_EXIT_NET", raising=False)
    monkeypatch.delenv("ATLAS_LITE_AGENT_MAX_CUT_NET", raising=False)
    from atlas_lite.paper_agent import (
        agent_cooldown_min,
        agent_entry_after,
        agent_entry_until,
        agent_max_cut_net,
        agent_min_exit_net,
        in_agent_entry_window,
    )

    assert agent_cooldown_min() == 3
    assert agent_entry_after() == (9, 45)
    assert agent_entry_until() == (15, 0)
    assert agent_min_exit_net() == 140.0
    assert agent_max_cut_net() == 200.0
    assert in_agent_entry_window(datetime(2026, 9, 29, 9, 30, tzinfo=IST)) is False
    assert in_agent_entry_window(datetime(2026, 9, 29, 9, 45, tzinfo=IST)) is True
    assert in_agent_entry_window(datetime(2026, 9, 29, 14, 50, tzinfo=IST)) is True
    assert in_agent_entry_window(datetime(2026, 9, 29, 15, 0, tzinfo=IST)) is True
    assert in_agent_entry_window(datetime(2026, 9, 29, 15, 1, tzinfo=IST)) is False
    monkeypatch.setenv("ATLAS_LITE_AGENT_COOLDOWN_MIN", "2")
    monkeypatch.setenv("ATLAS_LITE_AGENT_ENTRY_UNTIL", "14:50")
    monkeypatch.setenv("ATLAS_LITE_AGENT_ENTRY_AFTER", "10:00")
    assert agent_cooldown_min() == 2
    assert agent_entry_until() == (14, 50)
    assert agent_entry_after() == (10, 0)


def test_paper_agent_propose_rolls_ist_day(tmp_path: Path) -> None:
    bot = PaperAgent(path=tmp_path / "paper_agent.jsonl", lot_size=65)
    bot.traded_day = "2026-09-28"
    bot.entries_today = 4
    bot.day_pnl = -6000.0
    now = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    out = bot.propose_entry(side="ce", reason="new day", spot=22600.0, now=now)
    assert out["ok"] is True
    assert bot.traded_day == "2026-09-29"
    assert bot.entries_today == 0
    assert bot.day_pnl == 0.0
    # Prior loss day must leave a day_pnl seal before counters reset.
    lines = (tmp_path / "paper_agent.jsonl").read_text(encoding="utf-8").strip().splitlines()
    sealed = json.loads(lines[0])
    assert sealed["event"] == "day_pnl"
    assert sealed["day"] == "2026-09-28"
    assert sealed["day_pnl"] == -6000.0


def test_paper_agent_seals_prior_day_on_frame(tmp_path: Path) -> None:
    bot = PaperAgent(path=tmp_path / "paper_agent.jsonl", lot_size=65)
    bot.traded_day = "2026-09-24"
    bot.entries_today = 2
    bot.day_pnl = 586.0
    now = datetime(2026, 9, 25, 9, 35, tzinfo=IST)
    event = bot.on_frame(
        now=now,
        feed={},
        book=None,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert event is not None
    assert event["event"] == "day_pnl"
    assert event["day"] == "2026-09-24"
    assert event["day_pnl"] == 586.0
    assert bot.traded_day == "2026-09-25"
    assert bot.entries_today == 0
    assert bot.day_pnl == 0.0


def test_paper_agent_session_gap_flattens_leftover(tmp_path: Path) -> None:
    class _Book:
        def get(self, symbol: str):
            return {"last_price": 40.0}

    bot = PaperAgent(path=tmp_path / "paper_agent.jsonl", lot_size=65)
    day1 = datetime(2026, 9, 24, 14, 0, tzinfo=IST)
    bot.propose_entry(side="pe", reason="carry", spot=22600.0, now=day1)
    opened = bot.on_frame(
        now=day1 + timedelta(seconds=5),
        feed={},
        book=_Book(),
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert opened is not None and opened["event"] == "open"
    assert bot.position is not None
    day2 = datetime(2026, 9, 25, 9, 35, tzinfo=IST)
    closed = bot.on_frame(
        now=day2,
        feed={},
        book=_Book(),
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert closed is not None
    assert closed["reason"] == "session_gap"
    assert closed["day"] == "2026-09-25"
    assert bot.position is None
    assert bot.traded_day == "2026-09-25"
    assert bot.entries_today == 1


def test_system_prompt_forbids_hallucination() -> None:
    assert "NO HALLUCINATION" in SYSTEM_PROMPT
    assert "scorecard" in SYSTEM_PROMPT
    assert "record_decision" in SYSTEM_PROMPT
    assert "long CE" in SYSTEM_PROMPT or "long premium" in SYSTEM_PROMPT
    assert "short premium" in SYSTEM_PROMPT


def test_strategy_scorecard_prefers_combo_bull() -> None:
    from atlas_lite.agent_scorecard import build_strategy_scorecard, entry_permitted

    card = build_strategy_scorecard(
        {
            "spot_chg_open_pct": 0.25,
            "adx": 28,
            "adx_regime": "trend",
            "pdi": 30,
            "mdi": 18,
            "ce_pe_skew_rich": 2,
            "combo": {"side": "B", "signal": None, "position": None},
            "impulse": {"delta": -15, "fade_side": "ce", "position": None},
        }
    )
    assert card["regime"] == "trend"
    rec = card["recommended"]
    assert rec["action"] == "propose_entry"
    assert rec["side"] == "ce"
    assert rec["style"] == "long"
    assert float(rec["score"]) >= card["min_entry_score"]
    ok, why = entry_permitted(card, side="ce", style="long")
    assert ok is True and why == "recommended"
    bad, bad_why = entry_permitted(card, side="pe", style="long")
    assert bad is False
    assert bad_why in ("scorecard_wait", "weak_score", "no_agreement", "not_in_scorecard", "thesis_or_lesson_blocked")


def test_strategy_scorecard_allows_regime_without_flip_signal() -> None:
    """Regression: lasting combo side=B with signal=None must still allow long CE."""
    from atlas_lite.agent_scorecard import build_strategy_scorecard, entry_permitted

    card = build_strategy_scorecard(
        {
            "spot_chg_open_pct": 0.45,
            "adx": 28,
            "adx_regime": "trend",
            "pdi": 28,
            "mdi": 16,
            "combo": {"side": "B", "signal": None},
            "impulse": {},
        }
    )
    rec = card["recommended"]
    assert rec["action"] == "propose_entry"
    assert rec["side"] == "ce" and rec["style"] == "long"
    assert entry_permitted(card, side="ce", style="long")[0] is True


def test_strategy_scorecard_works_with_combo_impulse_off() -> None:
    """Agent must trade from intraday spot/+DI alone when other books are absent."""
    from atlas_lite.agent_scorecard import build_strategy_scorecard, entry_permitted

    card = build_strategy_scorecard(
        {
            "spot_chg_open_pct": 0.45,
            "adx": 28,
            "adx_regime": "trend",
            "pdi": 28,
            "mdi": 18,
            # No combo / impulse context — books may be paused.
        }
    )
    rec = card["recommended"]
    assert rec["action"] == "propose_entry"
    assert rec["side"] == "ce" and rec["style"] == "long"
    assert rec.get("agreement") is True
    assert entry_permitted(card, side="ce", style="long")[0] is True


def test_strategy_scorecard_rejects_ce_into_intraday_selloff() -> None:
    """Day still green vs yesterday must not buy CE while selling vs open."""
    from atlas_lite.agent_scorecard import build_strategy_scorecard, entry_permitted

    # Gap-up day (+0.30% vs yesterday) but selling vs open (−0.25%); ADX high from selloff.
    card = build_strategy_scorecard(
        {
            "spot_chg_pct": 0.30,
            "spot_chg_open_pct": -0.25,
            "adx": 28,
            "adx_regime": "trend",
            "pdi": 16,
            "mdi": 30,
            "combo": {"side": None, "signal": None},  # combo book paused → no regime
        }
    )
    assert card["recommended"]["action"] != "propose_entry" or card["recommended"].get(
        "side"
    ) != "ce"
    ok, _ = entry_permitted(card, side="ce", style="long")
    assert ok is False

    # Same selloff but combo regime S still available as market tape → prefer PE / wait.
    card2 = build_strategy_scorecard(
        {
            "spot_chg_pct": 0.30,
            "spot_chg_open_pct": -0.25,
            "adx": 28,
            "adx_regime": "trend",
            "pdi": 16,
            "mdi": 30,
            "combo": {"side": "S", "signal": None},
        }
    )
    rec2 = card2["recommended"]
    assert not (rec2.get("action") == "propose_entry" and rec2.get("side") == "ce")


def test_strategy_scorecard_missing_open_is_not_day_chg_direction() -> None:
    """Without session open, do not treat day chg vs yesterday as intraday direction."""
    from atlas_lite.agent_scorecard import build_strategy_scorecard, entry_permitted

    card = build_strategy_scorecard(
        {
            "spot_chg_pct": 0.30,  # green vs yesterday only
            "adx": 28,
            "adx_regime": "trend",
            # no spot_chg_open_pct, no DI yet
            "combo": {"side": None, "signal": None},
        }
    )
    ok, _ = entry_permitted(card, side="ce", style="long")
    assert ok is False
    assert card["inputs"]["spot_chg_open_pct"] is None


def test_strategy_scorecard_ignores_moneyness_as_rich_skew() -> None:
    """Raw CE−PE≈spot−ATM must not count as 'calls rich' for short CE."""
    from atlas_lite.agent_scorecard import build_strategy_scorecard

    card = build_strategy_scorecard(
        {
            "spot": 22720,
            "atm": 22700,
            "spot_chg_pct": 0.4,
            "adx": 28,
            "adx_regime": "trend",
            "ce_pe_skew": 22.0,  # ~ moneyness, not richness
            "combo": {"side": "B", "signal": None},
            "impulse": {},
        }
    )
    # Adjusted rich ≈ 22 - 20 = 2 → below ±8 threshold
    assert card["inputs"]["ce_pe_skew_rich"] == 2.0
    shorts = [c for c in card["candidates"] if c.get("style") == "short" and c.get("side") == "ce"]
    assert shorts
    assert all("calls rich" not in " ".join(c.get("reasons") or []) for c in shorts)


def test_strategy_scorecard_ignores_carry_as_rich_skew() -> None:
    """Parity carry (F−S) must not unlock short CE when spot is at ATM."""
    from atlas_lite.agent_scorecard import build_strategy_scorecard, entry_permitted

    # Spot at strike, CE−PE = fut−spot carry ≈ 20 → richness 0 after forward adjust.
    card = build_strategy_scorecard(
        {
            "spot": 22700,
            "atm": 22700,
            "fut": 22720,
            "ce_pe_skew": 20.0,
            "spot_chg_pct": 0.0,
            "adx": 15,
            "adx_regime": "range",
            "combo": {"side": None, "signal": None},
            "impulse": {},
        }
    )
    assert card["inputs"]["ce_pe_skew_rich"] == 0.0
    assert card["recommended"]["action"] == "wait"
    ok, _ = entry_permitted(card, side="ce", style="short")
    assert ok is False

    # Same via estimated DTE carry when fut is absent (~20 pts at 5 DTE).
    card2 = build_strategy_scorecard(
        {
            "spot": 22700,
            "atm": 22700,
            "days_to_expiry": 5,
            "ce_pe_skew": 20.0,
            "spot_chg_pct": 0.0,
            "adx": 15,
            "adx_regime": "range",
            "combo": {"side": None, "signal": None},
            "impulse": {},
        }
    )
    rich = card2["inputs"]["ce_pe_skew_rich"]
    assert rich is not None and abs(rich) < 8
    shorts = [
        c
        for c in card2["candidates"]
        if c.get("style") == "short" and c.get("side") == "ce"
    ]
    assert shorts
    assert all("calls rich" not in " ".join(c.get("reasons") or []) for c in shorts)


def test_strategy_scorecard_scales_monthly_fut_basis_to_weekly_dte() -> None:
    """Monthly fut carry must not read as 'puts rich' on weekly options."""
    from atlas_lite.agent_scorecard import build_strategy_scorecard
    from atlas_lite.metrics import option_carry_from_fut

    spot = 22700.0
    fut = 22820.0  # +120 monthly basis
    weekly_dte, fut_dte = 6.0, 27.0
    fair_weekly = 27.0  # ~ weekly parity CE−PE
    fwd, carry = option_carry_from_fut(
        spot, fut, option_dte=weekly_dte, fut_dte=fut_dte
    )
    assert fwd is not None and carry is not None
    assert abs(carry - (120.0 * 6.0 / 27.0)) < 0.05
    # Unscaled bug: 27 - 120 = -93 → puts rich. Scaled ≈ 0.
    assert fair_weekly - 120.0 < -8
    card = build_strategy_scorecard(
        {
            "spot": spot,
            "atm": spot,
            "fut": fut,
            "days_to_expiry": weekly_dte,
            "fut_days_to_expiry": fut_dte,
            "ce_pe_skew": fair_weekly,
            "spot_chg_pct": 0.0,
            "adx": 15,
            "adx_regime": "range",
            "combo": {"side": None, "signal": None},
            "impulse": {},
        }
    )
    rich = card["inputs"]["ce_pe_skew_rich"]
    assert rich is not None and abs(rich) < 8
    shorts_pe = [
        c
        for c in card["candidates"]
        if c.get("style") == "short" and c.get("side") == "pe"
    ]
    assert shorts_pe
    assert all("puts rich" not in " ".join(c.get("reasons") or []) for c in shorts_pe)


def test_strategy_scorecard_waits_when_conflicted_flat() -> None:
    from atlas_lite.agent_scorecard import build_strategy_scorecard, entry_permitted

    card = build_strategy_scorecard(
        {
            "spot_chg_pct": 0.0,
            "adx": 14,
            "adx_regime": "range",
            "combo": {"side": None, "signal": None, "position": None},
            "impulse": {"delta": None, "fade_side": None, "position": None},
        }
    )
    assert card["recommended"]["action"] == "wait"
    ok, why = entry_permitted(card, side="ce", style="long")
    assert ok is False
    assert why == "scorecard_wait"


def test_daily_review_avoids_repeated_stops() -> None:
    from atlas_lite.agent_review import build_daily_review
    from atlas_lite.agent_scorecard import build_strategy_scorecard, entry_permitted

    closes = [
        {"day": "2026-09-30", "side": "pe", "style": "long", "pnl": -640, "reason": "stop"},
        {"day": "2026-09-30", "side": "pe", "style": "long", "pnl": -644, "reason": "stop"},
        {"day": "2026-09-30", "side": "ce", "style": "long", "pnl": 67, "reason": "agent_exit"},
    ]
    review = build_daily_review(closes, day="2026-09-30")
    assert review["stops"] == 2
    assert review["stance"] in ("cautious", "defensive")
    assert any(a["side"] == "pe" and a["style"] == "long" for a in review["avoid_today"])
    card = build_strategy_scorecard(
        {
            "spot_chg_pct": -0.3,
            "adx": 28,
            "adx_regime": "trend",
            "combo": {"side": "S", "signal": None},
            "impulse": {"delta": 15, "fade_side": "pe"},
        },
        daily_review=review,
    )
    # long PE must not be recommended after 2 stops today
    rec = card["recommended"]
    assert not (rec.get("action") == "propose_entry" and rec.get("side") == "pe" and rec.get("style") == "long")
    ok, why = entry_permitted(card, side="pe", style="long")
    assert ok is False


def test_setup_memory_avoids_weak_multi_day_setup() -> None:
    from atlas_lite.agent_memory import build_setup_memory
    from atlas_lite.agent_scorecard import build_strategy_scorecard, entry_permitted

    closes = []
    # 12 losing long PE over recent days — should hard-avoid.
    for i in range(12):
        day = f"2026-09-{18 + (i % 10):02d}"
        closes.append(
            {
                "day": day,
                "side": "pe",
                "style": "long",
                "pnl": -200.0,
                "reason": "stop",
            }
        )
    # Winning long CE — soft prefer (and hard prefer at n≥12, E≥80).
    for i in range(12):
        day = f"2026-09-{18 + (i % 10):02d}"
        closes.append(
            {
                "day": day,
                "side": "ce",
                "style": "long",
                "pnl": 150.0,
                "reason": "trail",
            }
        )
    mem = build_setup_memory(closes, as_of_day="2026-09-30")
    assert mem["closes_used"] >= 20
    assert any(a["side"] == "pe" and a["style"] == "long" for a in mem["avoid_setups"])
    # Hard-avoided keys must not also carry soft adjust (no double penalty).
    assert "long_pe" not in mem["score_adjust"]
    assert mem["score_adjust"].get("long_ce", 0) > 0
    assert any(p["side"] == "ce" and p["style"] == "long" for p in mem["prefer_setups"])

    # Soft adjust still fires below hard-n threshold.
    soft_only = [
        {
            "day": f"2026-09-{20 + i:02d}",
            "side": "pe",
            "style": "short",
            "pnl": -200.0,
            "reason": "stop",
        }
        for i in range(9)
    ]
    soft_mem = build_setup_memory(soft_only, as_of_day="2026-09-30")
    assert soft_mem["avoid_setups"] == []
    assert soft_mem["score_adjust"].get("short_pe", 0) < 0

    review = {
        "stance": "normal",
        "min_entry_score_hint": 3.0,
        "max_entries_today": None,
        "avoid_today": list(mem["avoid_setups"]),
        "score_adjust": mem["score_adjust"],
        # Prefer rules must not be fed into scorecard wait-nudge path.
        "rules": [r for r in mem["rules"] if str(r).startswith("MEMORY AVOID")],
    }
    card = build_strategy_scorecard(
        {
            "spot_chg_open_pct": -0.4,
            "adx": 28,
            "adx_regime": "trend",
            "pdi": 15,
            "mdi": 30,
            "combo": {"side": "S", "signal": None},
        },
        daily_review=review,
    )
    pe_row = next(
        (
            c
            for c in card["candidates"]
            if c.get("side") == "pe" and c.get("style") == "long"
        ),
        None,
    )
    assert pe_row is not None
    assert pe_row["blocked"] is True
    assert "setup_memory long_pe adjust" not in " ".join(pe_row.get("reasons") or [])
    ok, why = entry_permitted(card, side="pe", style="long")
    assert ok is False
    assert why in ("thesis_or_lesson_blocked", "scorecard_wait", "not_in_scorecard", "weak_score")


def test_daily_review_one_stop_mild_red_stays_normal() -> None:
    """Single stop + day still only mildly red must not raise min score to 4.0."""
    from atlas_lite.agent_review import build_daily_review

    # Mirrors 2026-10-01 morning: +314, stop -581, trail +32 → net ≈ -235, stops=1.
    closes = [
        {"day": "2026-10-01", "side": "ce", "style": "short", "pnl": 314.49, "reason": "agent_exit"},
        {"day": "2026-10-01", "side": "ce", "style": "short", "pnl": -581.11, "reason": "stop"},
        {"day": "2026-10-01", "side": "ce", "style": "long", "pnl": 31.83, "reason": "trail"},
    ]
    review = build_daily_review(closes, day="2026-10-01")
    assert review["stops"] == 1
    assert review["net_pnl"] == -234.79
    assert review["stance"] == "normal"
    assert review["min_entry_score_hint"] == 3.0
    assert review["max_entries_today"] is None
    # Prior short-CE winner offsets the stop on that key (net≈-267 > -400) → no day avoid.
    # 15m thesis block still covers immediate re-entry; whole-day bar stays normal.
    assert not any(a["side"] == "ce" and a["style"] == "short" for a in review["avoid_today"])


def test_daily_review_cautious_needs_real_bleed() -> None:
    from atlas_lite.agent_review import build_daily_review

    # Deep red without 2 stops → cautious via net threshold.
    closes = [
        {"day": "2026-10-01", "side": "ce", "style": "long", "pnl": -520.0, "reason": "agent_exit"},
    ]
    review = build_daily_review(closes, day="2026-10-01")
    assert review["stance"] == "cautious"
    assert review["min_entry_score_hint"] == 4.0


def test_daily_review_defensive_entry_cap() -> None:
    from atlas_lite.agent_review import build_daily_review
    from atlas_lite.agent_scorecard import build_strategy_scorecard, entry_permitted

    closes = [
        {"day": "2026-09-30", "side": "pe", "style": "long", "pnl": -600, "reason": "stop"},
        {"day": "2026-09-30", "side": "pe", "style": "long", "pnl": -600, "reason": "stop"},
        {"day": "2026-09-30", "side": "ce", "style": "long", "pnl": -500, "reason": "stop"},
    ]
    review = build_daily_review(closes, day="2026-09-30")
    assert review["stance"] == "defensive"
    assert review["max_entries_today"] == 8
    assert review["min_entry_score_hint"] == 5.0
    card = build_strategy_scorecard(
        {
            "spot_chg_open_pct": 0.4,
            "adx": 28,
            "adx_regime": "trend",
            "pdi": 30,
            "mdi": 15,
            "combo": {"side": "B", "signal": None},
        },
        agent_book={"entries_today": 8, "position": None},
        daily_review=review,
    )
    assert card["recommended"]["action"] == "wait"
    ok, why = entry_permitted(card, side="ce", style="long")
    assert ok is False
    assert why == "day_entry_cap"


def test_paper_agent_loss_sets_thesis_cooldown(tmp_path: Path) -> None:
    class _Book:
        def get(self, symbol: str):
            return {"last_price": 90.0}

    bot = PaperAgent(path=tmp_path / "paper_agent.jsonl", lot_size=65)
    now = datetime(2026, 9, 30, 11, 0, tzinfo=IST)
    bot.propose_entry(side="ce", style="long", reason="test", spot=22700.0, now=now)
    opened = bot.on_frame(
        now=now + timedelta(seconds=2),
        feed={},
        book=_Book(),
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22700,
        allow_new_entries=True,
        spot=22700.0,
    )
    assert opened and opened["event"] == "open"
    # Mark down so agent_exit is a loss (entry ~100 book was wrong - use low mark)
    bot.position.entry = 100.0  # type: ignore[union-attr]
    closed = bot.on_frame(
        now=now + timedelta(minutes=1),
        feed={},
        book=_Book(),
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22700,
        allow_new_entries=True,
        spot=22700.0,
    )
    # Force close via propose_exit path if needed
    if bot.position is not None:
        bot.propose_exit(reason="agent_exit", mark=90.0, now=now + timedelta(minutes=1))
        closed = bot.on_frame(
            now=now + timedelta(minutes=1, seconds=2),
            feed={},
            book=_Book(),
            ce_symbol="NFO:XCE",
            pe_symbol="NFO:XPE",
            atm=22700,
            allow_new_entries=True,
            spot=22700.0,
        )
    assert bot.position is None
    t = now + timedelta(minutes=5)
    blocks = bot.thesis_blocks_snapshot(now=t, spot=22700.0)
    assert any(b["side"] == "ce" and b["style"] == "long" for b in blocks)
    # Past generic exit cooldown (3m), still inside loss thesis cooldown (10m).
    out = bot.propose_entry(
        side="ce", style="long", reason="again", spot=22700.0, now=t
    )
    assert out.get("ok") is False
    assert out.get("rejected") == "same_thesis_cooldown"


def test_paper_agent_trailing_stop_long(tmp_path: Path) -> None:
    class _Book:
        def __init__(self) -> None:
            self.px = 100.0

        def get(self, symbol: str):
            return {"last_price": self.px}

    bot = PaperAgent(path=tmp_path / "paper_agent.jsonl", lot_size=65)
    now = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    book = _Book()
    bot.propose_entry(side="ce", style="long", reason="up", spot=22600.0, now=now)
    opened = bot.on_frame(
        now=now + timedelta(seconds=5),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert opened and opened["event"] == "open"
    assert bot.position is not None
    hard_stop = bot.position.stop
    book.px = 105.0  # +5% arms trail (arm at +4%)
    assert (
        bot.on_frame(
            now=now + timedelta(minutes=1),
            feed={},
            book=book,
            ce_symbol="NFO:XCE",
            pe_symbol="NFO:XPE",
            atm=22600,
            allow_new_entries=True,
            spot=22600.0,
        )
        is None
    )
    assert bot.position is not None
    assert bot.position.trail_armed is True
    assert bot.position.stop > hard_stop
    book.px = bot.position.stop - 0.5
    closed = bot.on_frame(
        now=now + timedelta(minutes=2),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert closed is not None
    assert closed["reason"] == "trail"
    assert closed["style"] == "long"


def test_paper_agent_trail_survives_restore(tmp_path: Path) -> None:
    class _Book:
        def __init__(self) -> None:
            self.px = 100.0

        def get(self, symbol: str):
            return {"last_price": self.px}

    path = tmp_path / "paper_agent.jsonl"
    bot = PaperAgent(path=path, lot_size=65)
    now = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    book = _Book()
    bot.propose_entry(side="ce", style="long", reason="up", spot=22600.0, now=now)
    bot.on_frame(
        now=now + timedelta(seconds=5),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    book.px = 108.0
    bot.on_frame(
        now=now + timedelta(minutes=1),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert bot.position is not None
    armed_stop = bot.position.stop
    assert bot.position.trail_armed is True
    restored = PaperAgent(path=path, lot_size=65)
    assert restored.position is not None
    assert restored.position.trail_armed is True
    assert restored.position.stop == armed_stop
    assert restored.position.best_px == 108.0


def test_paper_agent_trail_ledger_only_on_arm_or_stop(tmp_path: Path) -> None:
    class _Book:
        def __init__(self) -> None:
            self.px = 100.0

        def get(self, symbol: str):
            return {"last_price": self.px}

    path = tmp_path / "paper_agent.jsonl"
    bot = PaperAgent(path=path, lot_size=65)
    now = datetime(2026, 9, 29, 9, 50, tzinfo=IST)
    book = _Book()
    bot.propose_entry(side="ce", style="long", reason="up", spot=22600.0, now=now)
    bot.on_frame(
        now=now + timedelta(seconds=5),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    for i, px in enumerate((100.5, 101.0, 101.5, 102.0, 103.0, 103.5)):
        book.px = px
        bot.on_frame(
            now=now + timedelta(minutes=1, seconds=i),
            feed={},
            book=book,
            ce_symbol="NFO:XCE",
            pe_symbol="NFO:XPE",
            atm=22600,
            allow_new_entries=True,
            spot=22600.0,
        )
    trails = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event") == "trail"
    ]
    assert trails == []
    book.px = 104.0  # arms → one ledger row
    bot.on_frame(
        now=now + timedelta(minutes=2),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    # Dense ticks: in-memory stop ratchets every tick, ledger only on ≥0.5×gap step.
    for i in range(20):
        book.px = round(104.0 + 0.05 * (i + 1), 2)  # up to 105.0
        bot.on_frame(
            now=now + timedelta(minutes=2, seconds=i + 1),
            feed={},
            book=book,
            ce_symbol="NFO:XCE",
            pe_symbol="NFO:XPE",
            atm=22600,
            allow_new_entries=True,
            spot=22600.0,
        )
    book.px = 108.0  # large step → another ledger row
    bot.on_frame(
        now=now + timedelta(minutes=3),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    trails = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event") == "trail"
    ]
    assert 2 <= len(trails) <= 4, len(trails)
    assert trails[0]["trail_armed"] is True
    assert trails[-1]["stop"] > trails[0]["stop"]
    assert bot.position is not None
    assert bot.position.stop == round(108.0 - 3.0, 2)  # live stop fully ratcheted


def test_paper_agent_hold_waits_for_quote(tmp_path: Path) -> None:
    class _Book:
        def __init__(self) -> None:
            self.alive = True
            self.px = 100.0

        def get(self, symbol: str):
            if not self.alive:
                return None
            return {"last_price": self.px}

    bot = PaperAgent(path=tmp_path / "paper_agent.jsonl", lot_size=65, hold_minutes=20)
    now = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    book = _Book()
    bot.propose_entry(side="ce", style="long", reason="up", spot=22600.0, now=now)
    assert (
        bot.on_frame(
            now=now + timedelta(seconds=5),
            feed={},
            book=book,
            ce_symbol="NFO:XCE",
            pe_symbol="NFO:XPE",
            atm=22600,
            allow_new_entries=True,
            spot=22600.0,
        )
        is not None
    )
    book.alive = False
    waited = bot.on_frame(
        now=now + timedelta(minutes=21),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert waited is None
    assert bot.position is not None
    book.alive = True
    book.px = 99.0
    closed = bot.on_frame(
        now=now + timedelta(minutes=21, seconds=5),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert closed is not None
    assert closed["reason"] == "time"
    assert closed["pnl_known"] is True


def _stop_long_pe(bot: PaperAgent, *, now: datetime, spot: float = 22600.0) -> None:
    class _Book:
        def __init__(self) -> None:
            self.px = 100.0

        def get(self, symbol: str):
            return {"last_price": self.px}

    book = _Book()
    bot.propose_entry(side="pe", style="long", reason="bear", spot=spot, now=now)
    bot.on_frame(
        now=now + timedelta(seconds=5),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=int(spot),
        allow_new_entries=True,
        spot=spot,
    )
    book.px = 89.0  # through the 6% morning stop and the 10% late stop
    closed = bot.on_frame(
        now=now + timedelta(minutes=1),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=int(spot),
        allow_new_entries=True,
        spot=spot,
    )
    assert closed is not None and closed["reason"] == "stop"


def test_thesis_blocks_do_not_overwrite_across_sides(tmp_path: Path) -> None:
    """Loss on CE must not clear an active stop block on PE."""
    bot = PaperAgent(
        path=tmp_path / "paper_agent.jsonl",
        lot_size=65,
        thesis_cooldown_min=15,
        loss_cooldown_min=10,
        cooldown_min=1,
    )
    now = datetime(2026, 9, 30, 13, 31, tzinfo=IST)
    _stop_long_pe(bot, now=now, spot=22600.0)
    assert bot._thesis_key("pe", "long") in bot.thesis_blocks

    class _Book:
        def get(self, symbol: str):
            return {"last_price": 90.0}

    t_ce = now + timedelta(minutes=7)
    bot.propose_entry(side="ce", style="long", reason="flip", spot=22600.0, now=t_ce)
    bot.on_frame(
        now=t_ce + timedelta(seconds=2),
        feed={},
        book=_Book(),
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert bot.position is not None
    bot.position.entry = 100.0
    bot.propose_exit(reason="agent_exit", mark=90.0, now=t_ce + timedelta(minutes=1))
    bot.on_frame(
        now=t_ce + timedelta(minutes=1, seconds=2),
        feed={},
        book=_Book(),
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert bot.position is None
    # Past generic exit cooldown (3m after CE loss), PE stop block still active (15m).
    t_check = now + timedelta(minutes=12)
    assert bot._thesis_key("pe", "long") in bot.thesis_blocks
    assert bot._thesis_key("ce", "long") in bot.thesis_blocks
    pe_blocked = bot.propose_entry(
        side="pe", style="long", reason="again", spot=22600.0, now=t_check
    )
    assert pe_blocked["ok"] is False
    assert pe_blocked["rejected"] == "same_thesis_cooldown"


def test_thesis_blocks_restore_from_lessons(tmp_path: Path) -> None:
    path = tmp_path / "paper_agent.jsonl"
    path.write_text("", encoding="utf-8")
    bot = PaperAgent(
        path=path,
        lot_size=65,
        thesis_cooldown_min=15,
        loss_cooldown_min=10,
    )
    now = datetime.now(IST)
    day = now.strftime("%Y-%m-%d")
    bot._append_lesson(
        {
            "ts": now.isoformat(),
            "day": day,
            "kind": "stop",
            "side": "pe",
            "style": "long",
            "pnl": -485.0,
            "spot": 22600.0,
            "until": (now + timedelta(minutes=15)).isoformat(),
            "lesson": "stop on long PE",
        }
    )
    bot._append_lesson(
        {
            "ts": (now + timedelta(minutes=1)).isoformat(),
            "day": day,
            "kind": "loss",
            "side": "ce",
            "style": "long",
            "pnl": -323.0,
            "spot": 22600.0,
            "until": (now + timedelta(minutes=10)).isoformat(),
            "lesson": "loss on long CE",
        }
    )
    restored = PaperAgent(
        path=path,
        lot_size=65,
        thesis_cooldown_min=15,
        loss_cooldown_min=10,
    )
    assert restored._thesis_key("pe", "long") in restored.thesis_blocks
    assert restored._thesis_key("ce", "long") in restored.thesis_blocks


def test_advisor_llm_payload_uses_today_closes_once(tmp_path: Path) -> None:
    from atlas_lite.agent_advisor import AgentAdvisor
    from atlas_lite.agent_gates import AgentGateStore

    day = "2026-09-30"
    closes = [
        {"day": "2026-09-29", "side": "pe", "style": "long", "pnl": -100, "reason": "stop"},
        {"day": day, "side": "ce", "style": "long", "pnl": 50, "reason": "trail"},
        {"day": day, "side": "pe", "style": "long", "pnl": -80, "reason": "stop"},
    ]
    gates = AgentGateStore(tmp_path / "gates.json")
    advisor = AgentAdvisor(
        data_dir=tmp_path,
        gates=gates,
        get_context=lambda: {
            "as_of": f"{day}T12:00:00+05:30",
            "clock": {"ist": f"{day}T12:00:00+05:30"},
            "spot": 22600,
            "atm": 22600,
        },
        get_paper=lambda: {
            "agent": {
                "recent_closes": closes * 40,  # bloated history
                "lessons_today": [],
                "position": None,
                "entries_today": 2,
                "thesis_block": None,
                "thesis_blocks": [],
            }
        },
        get_trades=lambda limit: {"ok": True, "trades": [], "count": 0},
        propose_entry=lambda **kw: {"ok": True},
        propose_exit=lambda **kw: {"ok": True},
        tape_ready=lambda: True,
        credentials=None,
    )
    # Build the same slim lists advise() would send (without calling LLM).
    context = advisor.build_context_pack()
    daily_review = context.get("daily_review") or {}
    assert daily_review.get("day") == day
    assert (daily_review.get("setup_memory") or {}).get("closes_used", 0) >= 3
    agent_book = advisor._get_paper()["agent"]
    recent_all = list(agent_book.get("recent_closes") or [])
    recent_today = [c for c in recent_all if str(c.get("day") or "") == day]
    agent_slim = {
        k: v for k, v in agent_book.items() if k not in ("recent_closes", "lessons_today")
    }
    assert "recent_closes" not in agent_slim
    assert all(str(c.get("day")) == day for c in recent_today)
    assert len(recent_today) < len(recent_all)


def test_paper_agent_same_thesis_cooldown_after_stop(tmp_path: Path) -> None:
    bot = PaperAgent(
        path=tmp_path / "paper_agent.jsonl",
        lot_size=65,
        thesis_cooldown_min=15,
        cooldown_min=1,
    )
    now = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    _stop_long_pe(bot, now=now, spot=22600.0)
    assert bot.thesis_block_snapshot(now=now + timedelta(minutes=2)) is not None
    blocked = bot.propose_entry(
        side="pe",
        style="long",
        reason="again",
        spot=22600.0,
        now=now + timedelta(minutes=2),
    )
    assert blocked["ok"] is False
    assert blocked["rejected"] == "same_thesis_cooldown"
    # Opposite side still allowed after normal cooldown
    ok_other = bot.propose_entry(
        side="ce",
        style="long",
        reason="flip",
        spot=22600.0,
        now=now + timedelta(minutes=2),
    )
    assert ok_other["ok"] is True
    lessons = bot.lessons_today(now=now + timedelta(minutes=2))
    assert lessons and lessons[-1]["kind"] == "stop"


def test_paper_agent_thesis_block_ignores_adverse_spot_move(tmp_path: Path) -> None:
    bot = PaperAgent(path=tmp_path / "paper_agent.jsonl", lot_size=65, thesis_cooldown_min=15)
    now = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    _stop_long_pe(bot, now=now, spot=22600.0)
    t = now + timedelta(minutes=4)
    # Spot rose further — worse for long PE; must stay blocked.
    adverse = bot.propose_entry(
        side="pe",
        style="long",
        reason="chase",
        spot=22600.0 * 1.0025,  # +0.25%
        now=t,
    )
    assert adverse["ok"] is False
    assert adverse["rejected"] == "same_thesis_cooldown"
    assert bot.thesis_block_snapshot(now=t, spot=22600.0 * 1.0025) is not None
    # Spot fell — favorable flip releases the block.
    ok_flip = bot.propose_entry(
        side="pe",
        style="long",
        reason="flip",
        spot=22600.0 * 0.9975,  # -0.25%
        now=t,
    )
    assert ok_flip["ok"] is True
    assert bot.thesis_block_snapshot(now=t, spot=22600.0 * 0.9975) is None
    lessons = bot.lessons_today(now=t)
    assert any(les.get("kind") == "release" and les.get("side") == "pe" for les in lessons)
    # Restart inside original window must not revive the released PE block.
    wall = datetime.now(IST)
    bot._append_lesson(
        {
            "ts": wall.isoformat(),
            "day": wall.strftime("%Y-%m-%d"),
            "kind": "stop",
            "side": "pe",
            "style": "long",
            "pnl": -100.0,
            "spot": 22600.0,
            "until": (wall + timedelta(minutes=15)).isoformat(),
            "lesson": "stop",
        }
    )
    bot._append_lesson(
        {
            "ts": (wall + timedelta(seconds=1)).isoformat(),
            "day": wall.strftime("%Y-%m-%d"),
            "kind": "release",
            "side": "pe",
            "style": "long",
            "spot": 22600.0,
            "until": (wall + timedelta(seconds=1)).isoformat(),
            "reason": "favorable_flip",
            "lesson": "released",
        }
    )
    restored = PaperAgent(path=bot.path, lot_size=65, thesis_cooldown_min=15)
    assert restored._thesis_key("pe", "long") not in restored.thesis_blocks


def test_last_reject_thesis_clears_for_specific_key(tmp_path: Path) -> None:
    bot = PaperAgent(
        path=tmp_path / "paper_agent.jsonl",
        lot_size=65,
        thesis_cooldown_min=15,
        loss_cooldown_min=10,
        cooldown_min=1,
    )
    now = datetime(2026, 9, 30, 11, 0, tzinfo=IST)
    _stop_long_pe(bot, now=now, spot=22600.0)
    bot._upsert_thesis_block(
        side="ce",
        style="long",
        until=now + timedelta(minutes=10),
        spot=22600.0,
        reason="agent_exit",
    )
    blocked = bot.propose_entry(
        side="pe", style="long", reason="again", spot=22600.0, now=now + timedelta(minutes=2)
    )
    assert blocked["rejected"] == "same_thesis_cooldown"
    assert bot.last_reject == "same_thesis_cooldown"
    assert bot.last_reject_thesis_key == "long_pe"
    # Favorable flip releases PE only; CE block remains — last_reject for PE must clear.
    bot._scrub_stale_last_reject(now=now + timedelta(minutes=2), spot=22600.0 * 0.9975)
    assert bot._thesis_key("pe", "long") not in bot.thesis_blocks
    assert bot._thesis_key("ce", "long") in bot.thesis_blocks
    assert bot.last_reject is None
    assert bot.last_reject_thesis_key is None


def test_paper_agent_clears_stale_same_thesis_last_reject(tmp_path: Path) -> None:
    bot = PaperAgent(
        path=tmp_path / "paper_agent.jsonl",
        lot_size=65,
        thesis_cooldown_min=15,
        cooldown_min=1,
    )
    now = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    _stop_long_pe(bot, now=now, spot=22600.0)
    blocked = bot.propose_entry(
        side="pe",
        style="long",
        reason="again",
        spot=22600.0,
        now=now + timedelta(minutes=2),
    )
    assert blocked["rejected"] == "same_thesis_cooldown"
    assert bot.last_reject == "same_thesis_cooldown"
    # After block expires, snapshot must not keep dangling last_reject.
    snap = bot.snapshot(now=now + timedelta(minutes=20), spot=22600.0)
    assert snap["thesis_block"] is None
    assert snap["last_reject"] is None


def test_paper_agent_day_roll_clears_thesis_last_reject(tmp_path: Path) -> None:
    bot = PaperAgent(
        path=tmp_path / "paper_agent.jsonl",
        lot_size=65,
        thesis_cooldown_min=15,
        cooldown_min=1,
    )
    now = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    _stop_long_pe(bot, now=now, spot=22600.0)
    bot.propose_entry(
        side="pe",
        style="long",
        reason="again",
        spot=22600.0,
        now=now + timedelta(minutes=2),
    )
    assert bot.last_reject == "same_thesis_cooldown"
    bot._roll_to_day("2026-09-30")
    assert bot.thesis_block_snapshot(now=now + timedelta(days=1)) is None
    assert bot.last_reject is None


def test_paper_agent_scrubs_stale_cooldown_last_reject(tmp_path: Path) -> None:
    bot = PaperAgent(
        path=tmp_path / "paper_agent.jsonl",
        lot_size=65,
        cooldown_min=1,
        thesis_cooldown_min=0,
    )
    now = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    bot.last_exit_at = now
    bot.last_reject = "cooldown"
    # Still inside cooldown window.
    snap_hot = bot.snapshot(now=now + timedelta(seconds=30), spot=22600.0)
    assert snap_hot["last_reject"] == "cooldown"
    # 68 minutes later — condition gone; must not linger for the LLM.
    snap_cold = bot.snapshot(now=now + timedelta(minutes=68), spot=22600.0)
    assert snap_cold["last_reject"] is None


def test_paper_agent_scrubs_stale_outside_session_and_daily_loss(tmp_path: Path) -> None:
    bot = PaperAgent(path=tmp_path / "paper_agent.jsonl", lot_size=65, daily_loss_stop=-5000.0)
    # Outside session sticky reject clears once back in window.
    bot.last_reject = "outside_session"
    morning = datetime(2026, 9, 29, 10, 0, tzinfo=IST)
    assert bot.snapshot(now=morning, spot=22600.0)["last_reject"] is None
    # Daily loss sticky reject clears once PnL recovers above the stop.
    bot.last_reject = "daily_loss_stop"
    bot.day_pnl = -6000.0
    still = bot.snapshot(now=morning, spot=22600.0)
    assert still["last_reject"] == "daily_loss_stop"
    bot.day_pnl = -100.0
    recovered = bot.snapshot(now=morning, spot=22600.0)
    assert recovered["last_reject"] is None


def test_paper_agent_fee_floor_blocks_micro_exit(tmp_path: Path) -> None:
    class _Book:
        def __init__(self) -> None:
            self.px = 100.0

        def get(self, symbol: str):
            return {"last_price": self.px}

    bot = PaperAgent(
        path=tmp_path / "paper_agent.jsonl",
        lot_size=65,
        min_exit_net=140.0,
        max_cut_net=200.0,
        cooldown_min=0,
        thesis_cooldown_min=0,
    )
    now = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    book = _Book()
    bot.propose_entry(side="ce", style="long", reason="bull", spot=22600.0, now=now)
    opened = bot.on_frame(
        now=now + timedelta(seconds=5),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert opened is not None
    book.px = 100.5  # +0.5pt ≈ +₹32.5 gross — scratch band
    rejected = bot.propose_exit(reason="scratch", mark=book.px, now=now + timedelta(minutes=1))
    assert rejected["ok"] is False
    assert rejected["rejected"] == "fee_floor"
    assert rejected.get("net", 0) < 140.0
    # Mild loser still inside scratch band (−200 < net < 140) must stay blocked.
    book.px = 98.5  # ≈ −₹97.5 gross → net still > −200
    mild = bot.propose_exit(reason="mild", mark=book.px, now=now + timedelta(minutes=1, seconds=30))
    assert mild["ok"] is False
    assert mild["rejected"] == "fee_floor"
    # Even if a stale pending exit exists, on_frame must not scratch under the floor.
    from atlas_lite.paper_agent import PendingIntent

    book.px = 100.5
    bot.pending = PendingIntent(
        side="ce",
        style="long",
        ts=now + timedelta(minutes=1),
        spot=None,
        reason="scratch",
        want_exit=True,
    )
    still = bot.on_frame(
        now=now + timedelta(minutes=2),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert still is None
    assert bot.position is not None
    assert bot.pending is None
    assert bot.last_reject == "fee_floor"
    # Thesis-break cut: net ≤ −200 allowed (before hard stop).
    book.px = 95.0  # −5pt ≈ −₹325 gross → net ≪ −200
    cut_snap = bot.snapshot(book=book, now=now + timedelta(minutes=2, seconds=10), spot=22600.0)
    assert cut_snap["position"]["net"] is not None and cut_snap["position"]["net"] <= -200
    assert cut_snap["position"]["exit_allowed"] is True
    assert cut_snap["last_reject"] is None
    cut_ok = bot.propose_exit(reason="thesis broken", mark=book.px, now=now + timedelta(minutes=2, seconds=15))
    assert cut_ok["ok"] is True
    bot.clear_pending()
    # Mark recovers past the take-profit floor → exit allowed.
    book.px = 104.0  # +4pt ≈ +₹260 gross
    snap = bot.snapshot(book=book, now=now + timedelta(minutes=2, seconds=30), spot=22600.0)
    assert snap["last_reject"] is None
    assert snap["position"]["exit_allowed"] is True
    assert snap["position"]["net"] is not None and snap["position"]["net"] >= 140
    ok = bot.propose_exit(reason="take", mark=book.px, now=now + timedelta(minutes=3))
    assert ok["ok"] is True
    closed = bot.on_frame(
        now=now + timedelta(minutes=3, seconds=5),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert closed is not None
    assert closed["reason"] == "agent_exit"
    assert closed["pnl"] is not None and closed["pnl"] >= 140.0


def test_paper_agent_short_target(tmp_path: Path) -> None:
    class _Book:
        def __init__(self) -> None:
            self.px = 100.0

        def get(self, symbol: str):
            return {"last_price": self.px}

    bot = PaperAgent(path=tmp_path / "paper_agent.jsonl", lot_size=65)
    now = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    book = _Book()
    bot.propose_entry(side="pe", style="short", reason="rich puts", spot=22600.0, now=now)
    opened = bot.on_frame(
        now=now + timedelta(seconds=5),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert opened and opened["style"] == "short"
    assert bot.position is not None
    assert bot.position.target < bot.position.entry
    assert bot.position.stop > bot.position.entry
    book.px = bot.position.target
    closed = bot.on_frame(
        now=now + timedelta(minutes=1),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert closed is not None
    assert closed["reason"] == "target"
    assert closed["pnl_gross"] is not None and closed["pnl_gross"] > 0


def test_agent_advisor_tool_dispatch_without_llm(tmp_path: Path) -> None:
    gates = AgentGateStore(tmp_path / "gates.json")
    paper_bot = PaperAgent(path=tmp_path / "paper_agent.jsonl")

    def get_context():
        return {
            "as_of": datetime.now(IST).isoformat(),
            "spot": 22600,
            "atm": 22600,
            "ce": 50,
            "pe": 45,
            "adx": 18,
        }

    advisor = AgentAdvisor(
        data_dir=tmp_path,
        gates=gates,
        get_context=get_context,
        get_paper=lambda: {"agent": paper_bot.snapshot()},
        get_trades=lambda limit: {"ok": True, "trades": [], "count": 0},
        propose_entry=lambda **kw: paper_bot.propose_entry(**kw),
        propose_exit=lambda **kw: paper_bot.propose_exit(**kw),
        tape_ready=lambda: True,
        credentials=None,
    )
    out = advisor._dispatch_tool("set_book_gate", {"book": "combo", "mode": "pause", "reason": "adx 18"})
    assert out["ok"] is True
    # Wall clock may be after cash close; gate until is next session end.
    assert gates.entries_allowed("combo") is False or out["gate"].get("until")
    blocked = advisor._dispatch_tool(
        "set_book_gate",
        {"book": "agent", "mode": "pause", "reason": "tape failing on IV Percentile"},
    )
    assert blocked["ok"] is False
    assert blocked["error"] == "cannot_pause_agent_book"
    pack = advisor.build_context_pack()
    assert "scorecard" in pack
    assert "daily_review" in pack
    assert "recommended" in pack["scorecard"]
    recorded = advisor._dispatch_tool(
        "record_decision",
        {
            "regime": "trend",
            "chosen": {"action": "propose_entry", "side": "ce", "style": "long"},
            "rejected_alternatives": ["long_pe", "wait"],
            "why": "combo B + spot up",
        },
    )
    assert recorded["ok"] is True
    assert advisor.last_expert_choice is not None
    assert advisor.last_expert_choice["chosen"]["side"] == "ce"
    # Weak / non-recommended entry must be hard-rejected by scorecard gate.
    weak = advisor._dispatch_tool(
        "propose_entry",
        {"side": "pe", "style": "long", "reason": "guess"},
    )
    assert weak["ok"] is False
    assert weak["rejected"] in (
        "scorecard_wait",
        "weak_score",
        "no_agreement",
        "not_in_scorecard",
        "thesis_or_lesson_blocked",
    )
    assert gates.entries_allowed("agent", now=datetime(2026, 9, 29, 11, 0, tzinfo=IST)) is True
    cleared = advisor._dispatch_tool(
        "set_book_gate",
        {"book": "agent", "mode": "allow", "reason": "clear"},
    )
    assert cleared["ok"] is True
    entry = paper_bot.propose_entry(
        side="pe",
        reason="pcr",
        spot=22600.0,
        now=datetime(2026, 9, 29, 11, 0, tzinfo=IST),
    )
    assert entry["ok"] is True
    blocked = paper_bot.propose_entry(
        side="ce",
        reason="x",
        spot=22600.0,
        now=datetime(2026, 9, 29, 11, 1, tzinfo=IST),
    )
    # already pending / or ok pending again — pending replaced
    assert "ok" in blocked


def test_late_exit_profile_is_fixed_at_the_fill(tmp_path: Path) -> None:
    class _Book:
        def __init__(self) -> None:
            self.px = 100.0

        def get(self, symbol: str):
            return {"last_price": self.px}

    bot = PaperAgent(path=tmp_path / "paper_agent.jsonl", lot_size=65)
    now = datetime(2026, 9, 29, 11, 0, tzinfo=IST)
    book = _Book()
    bot.propose_entry(side="ce", style="long", reason="up", spot=22600.0, now=now)
    opened = bot.on_frame(
        now=now + timedelta(seconds=5),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert opened is not None
    assert opened["target"] == 115.0
    assert opened["stop"] == 90.0
    assert bot.position is not None
    assert bot.position.trail_arm_pct == 0.02
    assert bot.position.trail_pct == 0.02
    assert bot.position.trail_pts == 1.0
    book.px = 102.0
    bot.on_frame(
        now=now + timedelta(minutes=1),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert bot.position is not None
    assert bot.position.trail_armed is True
    assert bot.position.stop == 100.0
    # A fill before 10:30 keeps the morning profile even if it is still open later.
    morning = PaperAgent(path=tmp_path / "paper_agent_am.jsonl", lot_size=65)
    am = datetime(2026, 9, 29, 10, 0, tzinfo=IST)
    book.px = 100.0
    morning.propose_entry(side="ce", style="long", reason="up", spot=22600.0, now=am)
    opened_am = morning.on_frame(
        now=am + timedelta(seconds=5),
        feed={},
        book=book,
        ce_symbol="NFO:XCE",
        pe_symbol="NFO:XPE",
        atm=22600,
        allow_new_entries=True,
        spot=22600.0,
    )
    assert opened_am is not None
    assert opened_am["target"] == 110.0
    assert opened_am["stop"] == 94.0
    assert morning.position is not None
    assert morning.position.trail_arm_pct == 0.04
    assert morning.position.trail_pct == 0.03
