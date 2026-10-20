# Agent paper — live Kite orders

Status: implemented locally. Deploy only when asked. **Do not enable until ops unlock + token are set and the Kite console checks below are done.**

Off by default. The scorecard agent book (`agent_paper`) can place real Zerodha market orders when the **Live agent** checkbox in the Agent panel is on.

## Behaviour

There is **one** book: `agent_paper`. Live is not a second parallel book.

- **Live off (default):** same scorecard entry/exit rules, fills at LTP, ledger only.
- **Live on:** the **same** entry/exit rules decide when to trade; the fill is a Kite market order instead of LTP. One lot MIS.
- Turning live on does **not** run paper and live side by side. The next open is live; while a live position is open there is only that one position.
- A position opened live **always closes live**, even if you turn the checkbox off mid-trade.
- Turning live **off does not flatten** an open live lot — exits still follow stop/trail/target/time/square-off on the broker.
- Only `agent_paper`. Other books stay paper.
- UI toggle requires `X-Atlas-Token` (same as gates / advise).
- **Enabling** live also requires `ATLAS_LITE_API_TOKEN` to be set on the host (refuses with `api_token_required_for_live` if unset).
- Ops kill switch: `ATLAS_LITE_AGENT_LIVE_ALLOWED` defaults to **0** (locked). Set to `1` on the host to unlock the UI.

Exit **triggers** (stop / trail / target / time / square-off) still watch the live LTP mark. The broker fill can slip vs that mark.

## Safety

- `wait_fill` cancels on timeout, then **re-reads** status so a fill that raced the cancel is not treated as a miss.
- Transport / network errors raise `KiteOrderError`.
- `net_position_qty` / `position_row` read Kite **`net` only** (never sum `net` + `day`).
- Before every live open: broker must be **flat**. Pre-existing / manual lots are never taken over.
- After a failed live open with an `order_id`: adopt **only that order’s** fill (`filled_quantity` / `average_price`, capped at ordered qty) as `live_adopt`. Unreadable order status blocks further live opens for the day.
- Restart restores `live_adopt` like `open`, and restores a pending `live_close_submit` order id.
- Stale-day `broker_squared_off` uses **delta** of Kite realised since open (`pnl_source=kite_realised_delta`); otherwise PnL unknown.
- Close qty = `min(abs(broker net), book qty)` — never sells manual extras. Partial remain = book − filled (`live_partial_close` records chunk PnL).
- CANCELLED/REJECTED close with `filled_quantity > 0` books that chunk first, then requotes only the **remaining book** qty.
- An order is **finished only** on COMPLETE / CANCELLED / REJECTED. Any other Kite status (OPEN PENDING, CANCEL PENDING, …) keeps the saved id — never a second close.
- Timed-out opens that are still working block further live opens until that order_id finishes (then `live_adopt`).
- Pending close order id is kept across retries and restarts; do **not** place a second close.
- Live flag **auto-offs after square-off** (15:14, once flat). Turn it on each morning with the Kite token.
- Do not hand-trade the agent’s strike in the same account while live is on.
- Close failures use exponential backoff (cap 60s).
- Broker fills run via `asyncio.to_thread`.
- Ledger: `live_submit` / `live_submit_failed` / `live_close_submit` / `live_adopt` / `live_partial_close`.
- On restart, an orphan `live_submit` (no following open/failed/adopt) logs a loud error.

## Ops before first live day

Verify in the Kite developer console (not automated here):

1. OCI static IP is allowlisted for orders.
2. Whether NFO options market orders need `market_protection`.

## Files

| Piece | Path |
|-------|------|
| Flag file | `data/agent_live.json` |
| Orders | `atlas_lite/kite_orders.py` |
| Flag store | `atlas_lite/agent_live.py` |
| Book wiring | `atlas_lite/paper_agent.py` |
| API | `POST /api/agent/live?enabled=true\|false` |
| UI | Agent panel → Live agent checkbox |
| Env | `ATLAS_LITE_AGENT_LIVE_ALLOWED` (default 0), `ATLAS_LITE_API_TOKEN` |

## Enable checklist

1. Valid Kite access token on the host.
2. `ATLAS_LITE_API_TOKEN` set and known to the UI.
3. `ATLAS_LITE_AGENT_LIVE_ALLOWED=1` then recreate/restart the container.
4. Confirm `/health` and paper book first; only then tick Live agent.
