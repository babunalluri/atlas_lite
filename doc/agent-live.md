# Agent paper — live Kite orders

Status: implemented locally. Deploy only when asked.

Off by default. The scorecard agent book (`agent_paper`) can place real Zerodha market orders when the **Live agent** checkbox is on **and** the operator is signed in.

## Behaviour

There is **one** book: `agent_paper`. Live is not a second parallel book.

- **Live off (default):** same scorecard entry/exit rules, fills at LTP, ledger only.
- **Live on:** the same entry/exit rules decide when to trade; the fill is a Kite market order. One lot MIS.
- Turning live **off does not flatten** an open live lot.
- Live **auto-offs after square-off** (15:14, once flat).
- Only `agent_paper`. Other books stay paper.

## Auth (required)

- Turning **Live on** requires UI **Sign in** (HttpOnly session cookie, `SameSite=strict`). Turning **Live off** needs no login (kill switch).
- Set in host `.env` (never commit credentials). Login is disabled until all three are set:

```bash
ATLAS_LITE_UI_EMAIL=...
ATLAS_LITE_UI_PASSWORD=...
ATLAS_LITE_SESSION_SECRET=...   # or rely on ATLAS_LITE_API_TOKEN
```

- No hardcoded email or session-signing fallback. Missing secret ⇒ sign-in stays off.

- Unauthenticated `POST /api/agent/live` returns `401 login_required`.
- Ops kill switch: `ATLAS_LITE_AGENT_LIVE_ALLOWED` defaults to **0** (locked). Set to `1` to unlock the checkbox host-wide.
- Gates / advise may still use optional `ATLAS_LITE_API_TOKEN` / `X-Atlas-Token`.

## Safety

- Order finished only on COMPLETE / CANCELLED / REJECTED; intermediate statuses keep the saved id.
- Cancelled/rejected close with partial fills books the chunk, then requotes remaining **book** qty only.
- Never takes over a pre-existing/manual broker lot; adopt only own order fills.
- `live_open_pending` is written to the ledger and restored after restart (including across a calendar-day roll) so a watched open is not forgotten.
- Unreadable broker status still arms the pending watch so a restart keeps polling the same order id.
- `net` positions only (never sum `net` + `day`).

## Ops before first live day

Verify in the Kite developer console:

1. OCI static IP is allowlisted for orders.
2. Whether NFO options market orders need `market_protection`.

## Enable checklist

1. Valid Kite access token on the host.
2. `ATLAS_LITE_UI_EMAIL`, `ATLAS_LITE_UI_PASSWORD`, and `ATLAS_LITE_SESSION_SECRET` (or API token) in host `.env`.
3. `ATLAS_LITE_AGENT_LIVE_ALLOWED=1` then recreate the container.
4. Sign in → tick **Live agent** only when you want real orders.
5. Do not hand-trade the agent’s strike in the same account while live is on.

## Files

| Piece | Path |
|-------|------|
| Flag file | `data/agent_live.json` |
| Orders | `atlas_lite/kite_orders.py` |
| UI auth | `atlas_lite/ui_auth.py` |
| Flag store | `atlas_lite/agent_live.py` |
| Book | `atlas_lite/paper_agent.py` |
| API | `POST /api/auth/login`, `POST /api/agent/live` |
| UI | Agent panel → Sign in + Live agent |
