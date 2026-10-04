"""OpenRouter tool-calling paper agent (no live Kite orders)."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

from atlas_lite.agent_gates import AgentGateStore, KNOWN_BOOKS, normalize_book
from atlas_lite.agent_memory import build_setup_memory
from atlas_lite.agent_review import build_daily_review
from atlas_lite.agent_scorecard import build_strategy_scorecard, entry_permitted
from atlas_lite.config import LlmCredentials, read_llm_credentials
from atlas_lite.log_util import get_logger
from atlas_lite.macro_sentiment import fetch_macro_snapshot
from atlas_lite.recorder import is_record_session

IST = ZoneInfo("Asia/Kolkata")
log = get_logger("atlas_lite.agent_advisor")

ADVISE_INTERVAL_S = float(os.environ.get("ATLAS_LITE_AGENT_INTERVAL_S", "120") or 120)
# When sparse: skip OpenRouter/OpenAI unless score/regime/exit wake (huge cost cut).
LLM_SPARSE = os.environ.get("ATLAS_LITE_AGENT_LLM_SPARSE", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
# Optional max silence between LLM calls (0 = only wake on signals). Default 15m.
LLM_HEARTBEAT_S = float(os.environ.get("ATLAS_LITE_AGENT_LLM_HEARTBEAT_S", "900") or 0)
# Scorecard-only executor: no OpenRouter/OpenAI. Entries from scorecard; exits in paper_agent.
SCORECARD_AUTOPILOT = os.environ.get(
    "ATLAS_LITE_AGENT_SCORECARD_AUTOPILOT", "1"
).strip().lower() not in ("0", "false", "no", "off")
# Cap agent_decisions.jsonl growth (0 = unlimited).
DECISIONS_MAX_LINES = max(
    0, int(os.environ.get("ATLAS_LITE_AGENT_DECISIONS_MAX_LINES", "1500") or 1500)
)


def llm_sparse_enabled() -> bool:
    return bool(LLM_SPARSE)


def scorecard_autopilot_enabled() -> bool:
    return bool(SCORECARD_AUTOPILOT)


def llm_wake_reasons(
    scorecard: dict[str, Any] | None,
    agent_book: dict[str, Any] | None,
    *,
    last_regime: str | None,
    last_llm_mono: float,
    now_mono: float | None = None,
    heartbeat_s: float | None = None,
) -> list[str]:
    """Why an LLM cycle is worth paying for (empty → skip)."""
    scorecard = scorecard if isinstance(scorecard, dict) else {}
    agent_book = agent_book if isinstance(agent_book, dict) else {}
    reasons: list[str] = []
    regime = str(scorecard.get("regime") or "").strip() or None
    if last_regime is None:
        reasons.append("cold_start")
    elif regime and regime != last_regime:
        reasons.append(f"regime_flip:{last_regime}->{regime}")

    pos = agent_book.get("position")
    pos_open = isinstance(pos, dict) and bool(pos)
    # Flat only: entry-score wakes. With an open book, exits are code-managed unless
    # exit_allowed (LLM mode) — do not burn tokens on candidate_above_threshold.
    if not pos_open:
        min_score = float(scorecard.get("min_entry_score") or 3.0)
        rec = (
            scorecard.get("recommended")
            if isinstance(scorecard.get("recommended"), dict)
            else {}
        )
        if rec.get("action") == "propose_entry":
            reasons.append("scorecard_entry")
        else:
            for row in scorecard.get("candidates") or []:
                if not isinstance(row, dict) or row.get("action") == "wait":
                    continue
                if row.get("blocked"):
                    continue
                try:
                    sc = float(row.get("score") or 0.0)
                except (TypeError, ValueError):
                    continue
                if row.get("agreement") and sc + 1e-9 >= min_score:
                    reasons.append("candidate_above_threshold")
                    break
    elif pos.get("exit_allowed") is True:
        reasons.append("exit_allowed")

    hb = float(LLM_HEARTBEAT_S if heartbeat_s is None else heartbeat_s)
    if hb > 0:
        mono = time.monotonic() if now_mono is None else float(now_mono)
        idle = mono - float(last_llm_mono or 0.0)
        if last_llm_mono <= 0:
            # cold_start already covers first call; no extra heartbeat reason
            pass
        elif idle >= hb:
            reasons.append("heartbeat")
    return reasons


SYSTEM_PROMPT = """You are Atlas Lite's expert PAPER options trader for NIFTY ATM options.
You are not a commentator and you do not rely on luck. You compare multiple strategies every cycle
and execute the single best expected-value decision for the agent paper book.

HARD RULE — NO HALLUCINATION:
- Use ONLY fields in the JSON context / scorecard / tool results.
- Every price, %, signal, or fill you cite MUST appear there. Missing → UNKNOWN (do not invent).
- Agent entry tape is ONLY ``required_tape_ok``. Ignore iron-fly tape_failing / IVP / RV-vs-IV.
- If required_tape_ok is false → do not propose_entry. NEVER pause book=agent.
- Reasons: facts only (field names/values). ≤2 sentences.

You never place live broker orders. Paper fills only, via tools.

DECISION LOOP (every cycle — do this mentally, then call tools):
1) Read ``scorecard`` (forced in payload): regime, ranked candidates, recommended.
2) Read ``structure`` (1m candles/traps), ``recent_agent_closes`` (today),
   ``daily_review.setup_memory``, ``lessons_today``, ``thesis_blocks``, open ``position.net``.
3) Choose ONE action:
   - flat + recommended.action=propose_entry + required_tape_ok → propose_entry with that side/style
     (unless you have a stronger fact-based override; say which scorecard row you beat and why).
   - flat + recommended.action=wait → do NOT force a trade; optionally set_book_gate on other books.
   - open + position.exit_allowed → propose_exit only if thesis broken (net≤−max_cut_net) or
     take-profit (net≥min_exit_net). Else HOLD for trail/target/stop/time.
4) Call record_decision with {regime, chosen, rejected_alternatives, why} BEFORE or WITH the trade tool.
5) Other books are independent — do not assume combo/impulse are on. Prefer set_book_gate
   only when you intentionally want to pause/skip another book; never pause book=agent.

CANDIDATES you must consider (ATM):
- long CE, long PE, short CE, short PE, wait
Playbook: trend → long premium with spot/ADX; range/chop → short premium or wait;
combo/impulse are optional soft cues only (those books may be paused).

STRUCTURE / LIQUIDITY (forced field ``structure`` — code from 1m→5m/15m + volume):
- Cite only: bias, trap, pin, engulfing, sweep, order_blocks, liquidity_pools,
  volume_profile, intent_proxy, cues, recent_bars.
- Traps: bull_trap / bear_trap / eqh_liquidity_grab / eql_liquidity_grab / vp_reject_*.
- order_blocks: multi-TF demand/supply zones; spot_rel inside/above/below.
- liquidity_pools: equal highs/lows; do not invent pools not listed.
- intent_proxy is VOLUME/OI PROXY ONLY — never claim true institutional intent.
- pin/engulfing = soft confirmation with ADX/spot; compression → short premium or wait.
- Never invent candle names or traps absent from ``structure``.

COSTS:
- Round-trip charges ~₹65–75. +0.5pt is usually a net loss.
- Use position.net / charges_est / exit_allowed. Scratch band → fee_floor (code rejects).
- Exit only when exit_allowed (net ≥ min_exit_net ~₹140 OR net ≤ −max_cut_net ~₹200).

MEMORY / LEARNING:
- Honor thesis_block and ``daily_review.avoid_today`` (hard — code rejects).
  Includes multi-day MEMORY AVOID when sample size is enough.
- Obey ``daily_review.rules`` and stance (defensive/cautious → fewer trades, higher bar).
- ``setup_memory`` is decayed multi-day stats — prefer/avoid only when n is sufficient.
- Do not flip CE↔PE every cycle. One thesis until stop, block expiry, or favorable flip.
- If today's net is deeply red or stops≥2, prefer wait unless scorecard recommends strongly.

GATES:
- set_book_gate only on OTHER books (allow / pause / skip_entries). User may turn any book
  off at any time — honor that. NEVER pause/skip agent.

Execute with tools. No analysis-only endings. Code risk gates may still reject.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_market_context",
            "description": "Return NIFTY tape context already assembled by Atlas Lite.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_macro_snapshot",
            "description": "Return timestamped oil/gold/FX/US futures/VIX (+ optional headlines).",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_paper_snapshot",
            "description": "Return paper book snapshots and active agent gates.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_trades",
            "description": "Return recent paper fills.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_book_gate",
            "description": (
                "Allow, pause, or skip new entries for OTHER paper books "
                "(not the agent book). For book=agent only mode=allow is permitted."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "book": {
                        "type": "string",
                        "enum": list(KNOWN_BOOKS),
                    },
                    "mode": {"type": "string", "enum": ["allow", "pause", "skip_entries"]},
                    "until": {
                        "type": "string",
                        "description": "ISO timestamp or HH:MM IST; defaults to today 15:30 IST for pause/skip",
                    },
                    "reason": {"type": "string"},
                },
                "required": ["book", "mode"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_decision",
            "description": (
                "Record the expert comparison for this cycle: regime, chosen action, "
                "rejected alternatives, and why. Call before propose_entry/propose_exit/wait."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "regime": {
                        "type": "string",
                        "enum": ["trend", "range", "mixed", "unknown"],
                    },
                    "chosen": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["propose_entry", "propose_exit", "wait", "hold"],
                            },
                            "side": {"type": "string", "enum": ["ce", "pe"]},
                            "style": {"type": "string", "enum": ["long", "short"]},
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                    "rejected_alternatives": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "e.g. ['long_pe', 'short_ce', 'wait']",
                    },
                    "why": {"type": "string"},
                },
                "required": ["regime", "chosen", "why"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_entry",
            "description": (
                "Propose ATM CE/PE long (buy) or short (sell) for the agent paper book. "
                "Prefer scorecard.recommended. Code may reject; trail/target/stop in code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "side": {"type": "string", "enum": ["ce", "pe"]},
                    "style": {
                        "type": "string",
                        "enum": ["long", "short"],
                        "description": "long=buy premium, short=sell premium. Default long.",
                    },
                    "reason": {"type": "string"},
                },
                "required": ["side"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_exit",
            "description": (
                "Propose exit of the open agent paper position when position.exit_allowed."
            ),
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    },
]


def agent_enabled() -> bool:
    raw = os.environ.get("ATLAS_LITE_AGENT", "1").strip().lower()
    return raw in ("1", "true", "yes")


def _field(value: Any, *, as_of: str | None = None, stale: bool = False) -> dict[str, Any]:
    return {"value": value, "as_of": as_of, "stale": bool(stale)}


class AgentAdvisor:
    """Periodic agent executor: scorecard autopilot (default) or OpenRouter tool loop."""

    def __init__(
        self,
        *,
        data_dir: Path,
        gates: AgentGateStore,
        get_context: Callable[[], dict[str, Any]],
        get_paper: Callable[[], dict[str, Any]],
        get_trades: Callable[[int], dict[str, Any]],
        propose_entry: Callable[..., dict[str, Any]],
        propose_exit: Callable[..., dict[str, Any]],
        tape_ready: Callable[[], bool],
        credentials: LlmCredentials | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.gates = gates
        self._get_context = get_context
        self._get_paper = get_paper
        self._get_trades = get_trades
        self._propose_entry = propose_entry
        self._propose_exit = propose_exit
        self._tape_ready = tape_ready
        self.credentials = credentials if credentials is not None else read_llm_credentials()
        self.decisions_path = self.data_dir / "agent_decisions.jsonl"
        self.last_decision: dict[str, Any] | None = None
        self.last_expert_choice: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.last_provider: str | None = None
        self.last_skip_reason: str | None = None
        self._last_run_mono = 0.0
        self._last_llm_mono = 0.0
        self._last_regime: str | None = None
        self._decision_writes = 0

    def status(self) -> dict[str, Any]:
        creds = self.credentials
        return {
            "ok": True,
            "enabled": agent_enabled(),
            "has_credentials": bool(creds and creds.api_key),
            "model": creds.model if creds else None,
            "base_url": creds.base_url if creds else None,
            "openai_failover": bool(creds and creds.has_openai_failover),
            "openai_model": creds.openai_model if creds and creds.has_openai_failover else None,
            "last_provider": self.last_provider,
            "interval_s": ADVISE_INTERVAL_S,
            "executor": "scorecard" if scorecard_autopilot_enabled() else "llm",
            "scorecard_autopilot": scorecard_autopilot_enabled(),
            "llm_sparse": llm_sparse_enabled(),
            "llm_heartbeat_s": LLM_HEARTBEAT_S,
            "last_regime": self._last_regime,
            "last_skip_reason": self.last_skip_reason,
            "last_decision": self.last_decision,
            "last_error": self.last_error,
            "gates": self.gates.snapshot().get("gates"),
        }

    def _decisions_archive_path(self, *, now: datetime | None = None) -> Path:
        day = (now or datetime.now(IST)).strftime("%Y-%m-%d")
        return self.data_dir / f"agent_decisions-{day}.jsonl"

    def _trim_decision_log(self) -> None:
        """Keep newest DECISIONS_MAX_LINES; archive overflow atomically (best-effort)."""
        max_lines = int(DECISIONS_MAX_LINES)
        if max_lines <= 0 or not self.decisions_path.is_file():
            return
        try:
            lines = [
                ln
                for ln in self.decisions_path.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
            if len(lines) <= max_lines:
                return
            drop = lines[:-max_lines]
            keep = lines[-max_lines:]
            archive = self._decisions_archive_path()
            # Append overflow to dated archive first (safe if live rewrite fails later).
            with archive.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(drop) + "\n")
            # Atomic replace of the live log.
            tmp = self.decisions_path.with_suffix(self.decisions_path.suffix + ".tmp")
            tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
            os.replace(tmp, self.decisions_path)
        except OSError as exc:
            log.warning("agent decision trim failed: %s", exc)
            try:
                tmp = self.decisions_path.with_suffix(self.decisions_path.suffix + ".tmp")
                if tmp.is_file():
                    tmp.unlink()
            except OSError:
                pass

    def _append_decision(self, row: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        row = dict(row)
        row.setdefault("ts", datetime.now(IST).isoformat())
        try:
            with self.decisions_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
            self._decision_writes += 1
            # Periodic trim — avoid rewriting the file on every quiet cycle.
            if self._decision_writes % 50 == 0:
                self._trim_decision_log()
        except OSError as exc:
            log.warning("agent decision log failed: %s", exc)
        self.last_decision = row

    def build_context_pack(self) -> dict[str, Any]:
        ctx = self._get_context() or {}
        as_of = str(ctx.get("as_of") or datetime.now(IST).isoformat())
        paper = self._get_paper() if callable(self._get_paper) else {}
        agent_book = (paper or {}).get("agent") if isinstance(paper, dict) else {}
        if not isinstance(agent_book, dict):
            agent_book = {}
        clock = ctx.get("clock") if isinstance(ctx.get("clock"), dict) else {}
        ist = str(clock.get("ist") or as_of or "")
        # Accept ISO datetime or bare YYYY-MM-DD.
        day = ist.split("T", 1)[0][:10] if ist else None
        if day and len(day) < 10:
            day = None
        all_closes = list(agent_book.get("recent_closes") or [])
        daily_review = build_daily_review(
            all_closes,
            lessons=agent_book.get("lessons_today") or [],
            day=day,
        )
        setup_memory = build_setup_memory(all_closes, as_of_day=day)
        # Merge multi-day avoids into today's hard list (dedupe by side+style).
        seen_avoid = {
            (str(a.get("side")), str(a.get("style")))
            for a in (daily_review.get("avoid_today") or [])
            if isinstance(a, dict)
        }
        for row in setup_memory.get("avoid_setups") or []:
            if not isinstance(row, dict):
                continue
            mark = (str(row.get("side")), str(row.get("style")))
            if mark in seen_avoid:
                continue
            daily_review.setdefault("avoid_today", []).append(row)
            seen_avoid.add(mark)
        # Only AVOID memory rules — PREFER must not inflate wait (scorecard +0.5/rule).
        for rule in setup_memory.get("rules") or []:
            if not str(rule).startswith("MEMORY AVOID"):
                continue
            daily_review.setdefault("rules", []).append(rule)
        daily_review["rules"] = list(daily_review.get("rules") or [])[:16]
        daily_review["setup_memory"] = {
            "closes_used": setup_memory.get("closes_used"),
            "setups": setup_memory.get("setups"),
            "score_adjust": setup_memory.get("score_adjust"),
            "prefer_setups": setup_memory.get("prefer_setups"),
        }
        daily_review["score_adjust"] = setup_memory.get("score_adjust") or {}
        scorecard_ctx = {
            "spot": ctx.get("spot"),
            "atm": ctx.get("atm"),
            "fut": ctx.get("fut"),
            "forward": ctx.get("forward"),
            "days_to_expiry": ctx.get("days_to_expiry"),
            "fut_days_to_expiry": ctx.get("fut_days_to_expiry"),
            "carry_pts": ctx.get("carry_pts"),
            "spot_chg_pct": ctx.get("spot_chg_pct"),
            "spot_chg_open_pct": ctx.get("spot_chg_open_pct"),
            "adx": ctx.get("adx"),
            "pdi": ctx.get("pdi"),
            "mdi": ctx.get("mdi"),
            "adx_regime": ctx.get("adx_regime"),
            "ce_pe_skew": ctx.get("ce_pe_skew"),
            "ce_pe_skew_rich": ctx.get("ce_pe_skew_rich"),
            "pcr": ctx.get("pcr"),
            "combo": ctx.get("combo"),
            "impulse": ctx.get("impulse"),
            "structure": ctx.get("structure"),
        }
        scorecard = build_strategy_scorecard(
            scorecard_ctx,
            agent_book=agent_book,
            thesis_block=agent_book.get("thesis_block"),
            daily_review=daily_review,
        )
        return {
            "as_of": as_of,
            "clock": ctx.get("clock"),
            "spot": _field(ctx.get("spot"), as_of=as_of, stale=ctx.get("spot") is None),
            "spot_chg_pct": _field(ctx.get("spot_chg_pct"), as_of=as_of),
            "atm": _field(ctx.get("atm"), as_of=as_of, stale=ctx.get("atm") is None),
            "adx": _field(ctx.get("adx"), as_of=as_of, stale=ctx.get("adx") is None),
            "adx_hint": _field(ctx.get("adx_hint"), as_of=as_of),
            "ivp": _field(ctx.get("ivp"), as_of=as_of),
            "pcr": _field(ctx.get("pcr"), as_of=as_of),
            "max_pain": _field(ctx.get("max_pain"), as_of=as_of),
            "ce": _field(ctx.get("ce"), as_of=as_of, stale=ctx.get("ce") is None),
            "pe": _field(ctx.get("pe"), as_of=as_of, stale=ctx.get("pe") is None),
            "ce_pe_skew": _field(ctx.get("ce_pe_skew"), as_of=as_of),
            "combo": ctx.get("combo"),
            "impulse": ctx.get("impulse"),
            "structure": ctx.get("structure"),
            "daily_review": daily_review,
            "scorecard": scorecard,
            "required_tape_ok": bool(self._tape_ready()),
        }

    def _dispatch_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "get_market_context":
            return {"ok": True, "context": self.build_context_pack()}
        if name == "get_macro_snapshot":
            return fetch_macro_snapshot()
        if name == "get_paper_snapshot":
            return {
                "ok": True,
                "paper": self._get_paper(),
                "gates": self.gates.snapshot().get("gates"),
            }
        if name == "get_recent_trades":
            limit = int(args.get("limit") or 20)
            return self._get_trades(max(1, min(limit, 50)))
        if name == "set_book_gate":
            book = normalize_book(str(args.get("book") or ""))
            mode = str(args.get("mode") or "").strip().lower()
            reason = str(args.get("reason") or "")[:300]
            until = args.get("until")
            # LLM was pausing its own book on iron-fly tape_failing — block that hard.
            if book in ("agent", "paper_agent") and mode != "allow":
                return {
                    "ok": False,
                    "error": "cannot_pause_agent_book",
                    "hint": "Use required_tape_ok; pause other books only. Set agent mode=allow to clear.",
                }
            try:
                row = self.gates.set_gate(
                    book,
                    mode,  # type: ignore[arg-type]
                    until=str(until) if until else None,
                    reason=reason,
                    source="agent",
                )
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "gate": row}
        if name == "record_decision":
            chosen = args.get("chosen") if isinstance(args.get("chosen"), dict) else {}
            row = {
                "ok": True,
                "recorded": True,
                "regime": str(args.get("regime") or "unknown")[:32],
                "chosen": {
                    "action": str(chosen.get("action") or "")[:32],
                    "side": chosen.get("side"),
                    "style": chosen.get("style"),
                },
                "rejected_alternatives": list(args.get("rejected_alternatives") or [])[:12],
                "why": str(args.get("why") or "")[:400],
                "ts": datetime.now(IST).isoformat(),
            }
            self.last_expert_choice = row
            return row
        if name == "propose_entry":
            if not self._tape_ready():
                return {"ok": False, "rejected": "tape_stale_or_missing"}
            side = str(args.get("side") or "").lower()
            style = str(args.get("style") or "long").lower()
            reason = str(args.get("reason") or "")[:300]
            ctx = self.build_context_pack()
            scorecard = ctx.get("scorecard") if isinstance(ctx.get("scorecard"), dict) else {}
            ok_score, score_why = entry_permitted(scorecard, side=side, style=style)
            if not ok_score:
                return {
                    "ok": False,
                    "rejected": score_why,
                    "scorecard_recommended": scorecard.get("recommended"),
                    "min_entry_score": scorecard.get("min_entry_score"),
                    "max_entries_today": scorecard.get("max_entries_today"),
                    "entries_today": scorecard.get("entries_today"),
                    "daily_review_stance": (ctx.get("daily_review") or {}).get("stance"),
                }
            spot = (ctx.get("spot") or {}).get("value")
            return self._propose_entry(side=side, style=style, reason=reason, spot=spot)
        if name == "propose_exit":
            reason = str(args.get("reason") or "")[:300]
            return self._propose_exit(reason=reason)
        return {"ok": False, "error": f"unknown_tool:{name}"}

    @staticmethod
    def _failover_status(status_code: int) -> bool:
        """Retry on auth / billing / rate-limit / upstream outages."""
        return status_code in (401, 402, 403, 429) or status_code >= 500

    def _provider_targets(self) -> list[tuple[str, str, str, str]]:
        """Ordered (name, api_key, base_url, model) targets."""
        creds = self.credentials
        if not creds or not creds.api_key:
            return []
        targets = [("primary", creds.api_key, creds.base_url, creds.model)]
        if creds.has_openai_failover and creds.openai_api_key:
            targets.append(
                (
                    "openai_failover",
                    creds.openai_api_key,
                    creds.openai_base_url,
                    creds.openai_model,
                )
            )
        return targets

    def _chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        targets = self._provider_targets()
        if not targets:
            raise RuntimeError("LLM credentials missing")
        last_exc: Exception | None = None
        with httpx.Client(timeout=60.0) as client:
            for idx, (name, api_key, base_url, model) in enumerate(targets):
                url = f"{base_url.rstrip('/')}/chat/completions"
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://atlas-lite.local",
                    "X-Title": "Atlas Lite Agent",
                }
                body = {
                    "model": model,
                    "messages": messages,
                    "tools": TOOLS,
                    "tool_choice": "auto",
                    "temperature": 0.1,
                }
                try:
                    resp = client.post(url, headers=headers, json=body)
                    resp.raise_for_status()
                    self.last_provider = name
                    if name != "primary":
                        log.warning("LLM using %s model=%s after primary failure", name, model)
                    return resp.json()
                except httpx.HTTPStatusError as exc:
                    last_exc = exc
                    code = exc.response.status_code
                    can_failover = idx + 1 < len(targets) and self._failover_status(code)
                    if can_failover:
                        log.warning(
                            "LLM primary failed HTTP %s; trying openai_failover",
                            code,
                        )
                        continue
                    raise
                except httpx.RequestError as exc:
                    last_exc = exc
                    if idx + 1 < len(targets):
                        log.warning("LLM primary network error; trying openai_failover: %s", exc)
                        continue
                    raise
        assert last_exc is not None
        raise last_exc

    def _advise_scorecard(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Zero-LLM cycle: follow scorecard.recommended; exits stay in paper_agent code."""
        context = self.build_context_pack()
        paper = self._get_paper() if callable(self._get_paper) else {}
        agent_book = (paper or {}).get("agent") if isinstance(paper, dict) else {}
        if not isinstance(agent_book, dict):
            agent_book = {}
        scorecard = context.get("scorecard") if isinstance(context.get("scorecard"), dict) else {}
        daily_review = (
            context.get("daily_review") if isinstance(context.get("daily_review"), dict) else {}
        )
        regime = str(scorecard.get("regime") or "unknown")[:32]
        rec = scorecard.get("recommended") if isinstance(scorecard.get("recommended"), dict) else {}
        tape_ok = bool(context.get("required_tape_ok"))
        tools: list[dict[str, Any]] = []
        chosen: dict[str, Any] = {"action": "wait", "side": None, "style": None}
        why = "scorecard wait"
        rejected: list[str] = []

        pos = agent_book.get("position")
        if isinstance(pos, dict) and pos:
            chosen = {
                "action": "hold",
                "side": pos.get("side"),
                "style": pos.get("style") or "long",
            }
            why = (
                "position open — scorecard autopilot holds; "
                "trail/target/stop/time managed in code"
            )
            rejected = ["propose_entry", "propose_exit"]
        elif str(rec.get("action") or "") == "propose_entry":
            side = str(rec.get("side") or "").lower()
            style = str(rec.get("style") or "long").lower() or "long"
            if not tape_ok:
                why = "scorecard entry blocked: required_tape_ok=false"
                rejected = [f"propose_entry_{side}_{style}"]
            else:
                ok_score, score_why = entry_permitted(scorecard, side=side, style=style)
                if not ok_score:
                    why = f"scorecard entry rejected: {score_why}"
                    rejected = [f"propose_entry_{side}_{style}"]
                else:
                    reason = (
                        f"scorecard autopilot {style} {side.upper()} "
                        f"score={rec.get('score')} regime={regime}"
                    )[:300]
                    if dry_run:
                        entry_out = {
                            "ok": True,
                            "dry_run": True,
                            "would_call": "propose_entry",
                            "side": side,
                            "style": style,
                        }
                    else:
                        entry_out = self._propose_entry(
                            side=side, style=style, reason=reason, spot=(context.get("spot") or {}).get("value")
                        )
                    tools.append(
                        {
                            "tool": "propose_entry",
                            "args": {"side": side, "style": style, "reason": reason},
                            "result": entry_out,
                        }
                    )
                    if entry_out.get("ok"):
                        chosen = {"action": "propose_entry", "side": side, "style": style}
                        why = reason
                        rejected = ["wait", "propose_exit"]
                    else:
                        why = f"propose_entry failed: {entry_out.get('rejected') or entry_out.get('error')}"
                        rejected = [f"propose_entry_{side}_{style}"]
        else:
            why = f"scorecard recommended {rec.get('action') or 'wait'}"
            rejected = ["propose_entry_ce_long", "propose_entry_pe_long", "propose_entry_ce_short", "propose_entry_pe_short"]

        expert = {
            "ok": True,
            "recorded": True,
            "regime": regime,
            "chosen": chosen,
            "rejected_alternatives": rejected[:12],
            "why": why[:400],
            "ts": datetime.now(IST).isoformat(),
            "executor": "scorecard",
        }
        self.last_expert_choice = expert
        tools.insert(0, {"tool": "record_decision", "args": expert, "result": expert})

        self.last_error = None
        self.last_skip_reason = None
        self.last_provider = "scorecard"
        self._last_regime = regime
        self._last_run_mono = time.monotonic()
        # Treat as "llm done" for sparse idle bookkeeping if mode flips later.
        self._last_llm_mono = self._last_run_mono
        row = {
            "ok": True,
            "dry_run": dry_run,
            "executor": "scorecard",
            "skipped_llm": True,
            "provider": "scorecard",
            "model": None,
            "wake_reasons": ["scorecard_autopilot"],
            "reason": why[:500],
            "tools": tools,
            "context_spot": (context.get("spot") or {}).get("value"),
            "required_tape_ok": tape_ok,
            "scorecard_recommended": rec,
            "daily_review": {
                "stance": daily_review.get("stance"),
                "net_pnl": daily_review.get("net_pnl"),
                "stops": daily_review.get("stops"),
                "avoid_today": daily_review.get("avoid_today"),
                "rules": daily_review.get("rules"),
                "max_entries_today": daily_review.get("max_entries_today"),
                "min_entry_score_hint": daily_review.get("min_entry_score_hint"),
                "setup_memory": daily_review.get("setup_memory"),
                "score_adjust": daily_review.get("score_adjust"),
            },
            "expert_choice": expert,
        }
        self._append_decision(row)
        return row

    def advise(self, *, dry_run: bool = False, force: bool = False) -> dict[str, Any]:
        """Run one advise cycle (scorecard autopilot or LLM tool loop)."""
        if not agent_enabled():
            return {"ok": False, "error": "agent_disabled"}
        if scorecard_autopilot_enabled():
            return self._advise_scorecard(dry_run=dry_run)
        if not self.credentials or not self.credentials.api_key:
            self.last_error = "missing_llm_credentials"
            return {"ok": False, "error": "missing_llm_credentials"}

        context = self.build_context_pack()
        paper = self._get_paper()
        agent_book = (paper or {}).get("agent") if isinstance(paper, dict) else None
        if not isinstance(agent_book, dict):
            agent_book = {}
        scorecard = context.get("scorecard") or {}
        daily_review = context.get("daily_review") or {}
        regime = str(scorecard.get("regime") or "").strip() or None

        wake = ["force"] if force or dry_run else []
        if not wake and llm_sparse_enabled():
            wake = llm_wake_reasons(
                scorecard if isinstance(scorecard, dict) else {},
                agent_book,
                last_regime=self._last_regime,
                last_llm_mono=self._last_llm_mono,
            )
        elif not wake:
            wake = ["sparse_off"]

        if not wake:
            # Quiet tape — pay nothing; keep interval cadence.
            self._last_regime = regime or self._last_regime
            self._last_run_mono = time.monotonic()
            self.last_skip_reason = "scorecard_quiet"
            self.last_error = None
            row = {
                "ok": True,
                "skipped_llm": True,
                "wake_reasons": [],
                "reason": "skipped_llm:scorecard_quiet",
                "context_spot": (context.get("spot") or {}).get("value"),
                "required_tape_ok": context.get("required_tape_ok"),
                "scorecard_recommended": (
                    scorecard.get("recommended") if isinstance(scorecard, dict) else None
                ),
                "daily_review": {
                    "stance": daily_review.get("stance"),
                    "net_pnl": daily_review.get("net_pnl"),
                    "stops": daily_review.get("stops"),
                    "min_entry_score_hint": daily_review.get("min_entry_score_hint"),
                }
                if isinstance(daily_review, dict)
                else None,
                "tools": [],
            }
            self._append_decision(row)
            return row

        self.last_skip_reason = None
        macro = fetch_macro_snapshot()
        day = str(daily_review.get("day") or "")
        # LLM sees today's closes once; multi-day history stays server-side for setup_memory.
        recent_all = list(agent_book.get("recent_closes") or [])
        recent_today = (
            [c for c in recent_all if str(c.get("day") or "") == day] if day else recent_all[-20:]
        )
        agent_slim = {
            k: v
            for k, v in agent_book.items()
            if k not in ("recent_closes", "lessons_today")
        }
        # Avoid duplicating scorecard/daily_review inside context (saves a few KB/cycle).
        context_slim = {
            k: v for k, v in context.items() if k not in ("scorecard", "daily_review")
        }
        user_payload = {
            "context": context_slim,
            "scorecard": scorecard,
            "daily_review": daily_review,
            "macro": macro,
            "paper_summary": {
                "gates": self.gates.snapshot().get("gates"),
                "agent": agent_slim,
            },
            # Forced memory — today only; multi-day edge is in daily_review.setup_memory.
            "recent_agent_closes": recent_today,
            "lessons_today": agent_book.get("lessons_today") or [],
            "thesis_block": agent_book.get("thesis_block"),
            "thesis_blocks": agent_book.get("thesis_blocks") or [],
            "last_decision": self.last_decision,
            "last_expert_choice": self.last_expert_choice,
            "wake_reasons": wake,
            "dry_run": dry_run,
            "instruction": (
                "Expert cycle: (1) read structure (candles/traps) + daily_review "
                "(stance/rules/avoid_today/setup_memory) + scorecard.recommended, "
                "(2) record_decision with regime/chosen/rejected/why, "
                "(3) propose_entry ONLY if recommended.action=propose_entry and "
                "required_tape_ok — code rejects weak_score / no_agreement / avoid_today, "
                "(4) if open → HOLD unless position.exit_allowed then propose_exit, "
                "(5) other books are independent — gate them only if you mean to. "
                "Never pause book=agent. No analysis-only text."
            ),
        }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, default=str)},
        ]
        tool_trace: list[dict[str, Any]] = []
        final_text = ""
        try:
            for _ in range(6):
                data = self._chat(messages)
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                tool_calls = message.get("tool_calls") or []
                content = message.get("content") or ""
                if content:
                    final_text = str(content)
                messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls or None,
                    }
                )
                if not tool_calls:
                    break
                for call in tool_calls:
                    fn = call.get("function") or {}
                    name = str(fn.get("name") or "")
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except json.JSONDecodeError:
                        args = {}
                    if dry_run and name in (
                        "set_book_gate",
                        "propose_entry",
                        "propose_exit",
                    ):
                        result = {"ok": True, "dry_run": True, "would_call": name, "args": args}
                    else:
                        result = self._dispatch_tool(name, args if isinstance(args, dict) else {})
                    tool_trace.append({"tool": name, "args": args, "result": result})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id") or name,
                            "content": json.dumps(result, default=str),
                        }
                    )
            self.last_error = None
            used_model = self.credentials.model
            if self.last_provider == "openai_failover" and self.credentials:
                used_model = self.credentials.openai_model
            self._last_regime = regime or self._last_regime
            self._last_llm_mono = time.monotonic()
            self._last_run_mono = self._last_llm_mono
            row = {
                "ok": True,
                "dry_run": dry_run,
                "skipped_llm": False,
                "wake_reasons": wake,
                "provider": self.last_provider or "primary",
                "model": used_model,
                "reason": (final_text or "")[:500],
                "tools": tool_trace,
                "context_spot": (context.get("spot") or {}).get("value"),
                "required_tape_ok": context.get("required_tape_ok"),
                "scorecard_recommended": (
                    scorecard.get("recommended") if isinstance(scorecard, dict) else None
                ),
                "daily_review": {
                    "stance": daily_review.get("stance"),
                    "net_pnl": daily_review.get("net_pnl"),
                    "stops": daily_review.get("stops"),
                    "avoid_today": daily_review.get("avoid_today"),
                    "rules": daily_review.get("rules"),
                    "max_entries_today": daily_review.get("max_entries_today"),
                    "min_entry_score_hint": daily_review.get("min_entry_score_hint"),
                    "setup_memory": daily_review.get("setup_memory"),
                    "score_adjust": daily_review.get("score_adjust"),
                }
                if isinstance(daily_review, dict)
                else None,
                "expert_choice": self.last_expert_choice,
            }
            self._append_decision(row)
            return row
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)[:300]
            # Still advance cadence + regime so a dead LLM does not tight-loop burns.
            self._last_regime = regime or self._last_regime
            self._last_run_mono = time.monotonic()
            row = {
                "ok": False,
                "error": self.last_error,
                "wake_reasons": wake,
                "tools": tool_trace,
            }
            self._append_decision(row)
            log.warning("agent advise failed: %s", exc)
            return row

    def due(self, now: datetime | None = None) -> bool:
        if not agent_enabled():
            return False
        if not scorecard_autopilot_enabled() and not self.credentials:
            return False
        if not is_record_session(now):
            return False
        return (time.monotonic() - self._last_run_mono) >= ADVISE_INTERVAL_S
