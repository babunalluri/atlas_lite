# Agent paper — liquidity-sweep handling

Status: planned. **Not implemented.** See also [factor-paper-book.md](factor-paper-book.md) (2026-10-24), which plans a sweep *setup* in a separate book. No code in this repo was changed for this note.
Do not deploy, commit, or wire this until it is built and explicitly asked for.

Paper book only: **`agent_paper`** (`atlas_lite/paper_agent.py` and the scorecard path that opens it).
Do not apply these gates to the AI agent, iron fly, short straddle, short IC, theta, ICT, impulse, combo, or skew.

## Why

Large algos often show up as a fast premium sweep: a fill that is already late, then a stop a few seconds later. On 2026-10-19 the scorecard book stopped a long put in about 20 seconds (−₹501) and trailed a short call in seconds. The open sweep before 09:30 is already outside the entry clock. The remaining problem is entering while the option is still running, or right after a shock bar.

## In scope (two gates only)

### 1. Do not enter into a running premium

Skip a new entry when the option that would be traded is already moving hard in the direction that hurts that side.

Proposed rule (exact numbers to confirm before coding):

- Look back **15 minutes** on the **same strike** (same idea as the AI agent’s premium memory).
- **Long CE / long PE:** skip if that option’s premium is already **up** over the lookback (buying into a blow-off).
- **Short CE / short PE:** skip if that option’s premium is already **up** over the lookback (selling into a rising option).
- If the strike changed in the lookback window, the change is blank → **do not skip** on this rule (same as AI agent).
- Reject reason (suggested): `rising_premium` or `chasing_premium`.

This matches the live AI agent’s `skip_rising_short` idea for shorts, and extends the same “do not chase” idea to longs for this book only.

### 2. Quiet minute after a shock

After an unusually large short move in spot or ATM premium, block **new** entries for a few minutes. Exits (stop / trail / target / square-off) stay on every tick.

Proposed rule (exact numbers to confirm before coding):

- Measure the last **1–3 minutes** of spot range and/or ATM CE+PE (or the side under consideration).
- If that move is above a tape-backed threshold (to be set from recordings, not from one day alone), set `shock_until` = now + **N minutes** (suggested start: **3 minutes**).
- While `now < shock_until`, reject new entries. Suggested reason: `shock_cooldown`.
- Do not change stops, targets, trails, or the 09:30–12:00 entry window for this gate.

## Out of scope (do not do as part of this work)

- Do not add ICT order blocks, equal levels, PDH/PDL, premium/discount, PO3, OTE, or a fixed 15-point ICT stop.
- Do not widen the stop or trail off one bad day.
- Do not raise size above 1 lot / qty 65.
- Do not change capital (₹20k margin), `DAILY_LOSS_STOP` (−₹2,000), or the daily review stance rules (`agent_review.py`).
- Do not change AI agent entry rules beyond what is already deployed (closed-minute score + rising short skip).
- Do not retune fly, straddle, short IC, theta, or ICT off this document.

## What already helps (leave alone)

- Entry clock starts at **09:30** (open sweep mostly skipped).
- Scorecard needs **agreement** plus min score.
- After a stop, that setup is on the **day avoid** list.
- One position at a time; cooldown after exit.
- Daily loss stop for this book only.

## Expected files if / when implemented

| Area | Likely touch |
|------|----------------|
| Entry gate | `atlas_lite/paper_agent.py` (and/or the scorecard → propose_entry path that calls open) |
| Premium memory | Possibly shared helper, or a small mirror of AI agent’s 15-minute same-strike memory **for agent_paper only** |
| Tests | `tests/test_agent.py` (chase skip + shock cooldown; exits still fire during cooldown) |
| This doc | Update status to implemented / deployed when done |

No other books’ ledgers.

## How to revert after a future implementation

If the change is wrong after it is coded:

1. Revert the commit(s) that touch `paper_agent.py` / scorecard entry gating / related tests (git revert of those commits).
2. Or restore the pre-change entry path so `propose_entry` opens with only the existing clocks, cooldowns, avoid list, and day-loss stop.
3. Leave `agent_review.py` stance logic untouched unless a separate commit changed it.
4. Redeploy only when asked, after revert is verified with `tests/test_agent.py`.

Until something is committed, there is **nothing to revert** — this file is documentation only.

## Tape check before locking numbers

Before coding thresholds, replay recent `data/recordings/` for agent entry minutes:

- How often would gate (1) have blocked the 19 Oct long-PE stop and the short-CE scratch?
- How often would gate (1) have blocked winners the same week?
- What 1–3 minute spot / premium move size is rare on normal days but common on sweep minutes?

Do not fine-tune off a single expiry day alone.

## Decision log

| Date | Note |
|------|------|
| 2026-10-20 | User asked how to handle algo liquidity sweeps. Recommendation: agent_paper only; rising-premium skip + quiet minute after shock. |
| 2026-10-20 | User asked which book: **agent_paper only**. |
| 2026-10-20 | User asked for a document in docs, **no code changes**, so a bad future change can be reverted against this plan. |

## Status checklist

- [x] Written under `doc/` (repo docs folder)
- [ ] Numbers confirmed from multi-day tape
- [ ] Implemented in `agent_paper` only
- [ ] Tests added
- [ ] Explicit user ask to commit / deploy
