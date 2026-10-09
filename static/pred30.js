/* 30-minute target, range, and band helpers. This file is what the chart renders.
   Do not keep a second copy of the price path in Python. Band width helpers live
   here too so index.html does not drift. */
var PRED30_HORIZON_MIN = 30;
var PRED30_SESSION_START_MIN = 9 * 60 + 15;
var PRED30_SESSION_END_MIN = 15 * 60 + 30;
var PRED30_SESSION_MINUTES = PRED30_SESSION_END_MIN - PRED30_SESSION_START_MIN;
var PRED30_TRADING_MINUTES_YEAR = PRED30_SESSION_MINUTES * 252;

function pred30Px(value) {
  return Math.round(value);
}

function pred30Num(x) {
  const n = Number(x);
  return Number.isFinite(n) ? n : null;
}

function pred30IstClock(ts) {
  if (ts == null) return null;
  const day = new Date(ts).toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
  const hm = new Date(ts).toLocaleTimeString("en-GB", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const parts = String(hm).split(":");
  const hour = Number(parts[0]);
  const minute = Number(parts[1]);
  if (!day || !Number.isFinite(hour) || !Number.isFinite(minute)) return null;
  return { day, clock: hour * 60 + minute };
}

function pred30AddDays(isoDay, n) {
  const [y, m, d] = String(isoDay).split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}

function pred30IstWeekday(isoDay) {
  const [y, m, d] = String(isoDay).split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).getUTCDay();
}

function pred30TradingMinutesToExpiry(expiryIso, ts) {
  if (!expiryIso) return null;
  const clock = pred30IstClock(ts);
  if (!clock) return null;
  const start = clock.day;
  const end = String(expiryIso).slice(0, 10);
  if (end < start) return null;
  let minutes = 0;
  let cursor = start;
  while (cursor <= end) {
    const wd = pred30IstWeekday(cursor);
    if (wd >= 1 && wd <= 5) {
      if (cursor === start) {
        if (clock.clock < PRED30_SESSION_END_MIN) {
          minutes += clock.clock <= PRED30_SESSION_START_MIN
            ? PRED30_SESSION_MINUTES
            : PRED30_SESSION_END_MIN - clock.clock;
        }
      } else {
        minutes += PRED30_SESSION_MINUTES;
      }
    }
    if (cursor === end) break;
    cursor = pred30AddDays(cursor, 1);
  }
  return minutes >= PRED30_HORIZON_MIN ? minutes : null;
}

function pred30IvWidth(spot, ivPct) {
  if (spot == null || ivPct == null || spot <= 0 || ivPct <= 0) return null;
  return spot * (ivPct / 100) * Math.sqrt(PRED30_HORIZON_MIN / PRED30_TRADING_MINUTES_YEAR);
}

function pred30StraddleWidth(straddle, minutesLeft) {
  if (straddle == null || minutesLeft == null || straddle <= 0 || minutesLeft < PRED30_HORIZON_MIN) return null;
  return straddle * Math.sqrt(PRED30_HORIZON_MIN / minutesLeft);
}

function pred30AtrWidth(atr, barMinutes) {
  if (atr == null || atr <= 0 || barMinutes <= 0) return null;
  return atr * Math.sqrt(PRED30_HORIZON_MIN / barMinutes);
}

function pred30Linreg(closes) {
  const n = closes.length;
  if (n < 5) return { slope: null, sigma: null };
  const meanX = (n - 1) / 2;
  let meanY = 0;
  for (let i = 0; i < n; i += 1) meanY += closes[i];
  meanY /= n;
  let varX = 0;
  let cov = 0;
  for (let i = 0; i < n; i += 1) {
    const dx = i - meanX;
    varX += dx * dx;
    cov += dx * (closes[i] - meanY);
  }
  if (varX <= 0) return { slope: null, sigma: null };
  const slope = cov / varX;
  const intercept = meanY - slope * meanX;
  let varR = 0;
  for (let i = 0; i < n; i += 1) {
    const resid = closes[i] - (intercept + slope * i);
    varR += resid * resid;
  }
  varR /= Math.max(n - 2, 1);
  const sigma = Math.sqrt(Math.max(varR, 0));
  return { slope, sigma: sigma > 0 ? sigma : null };
}

function pred30IchimokuSpanA(highs, lows) {
  if (highs.length < 26 || lows.length < 26) return null;
  const tenkan = (Math.max(...highs.slice(-9)) + Math.min(...lows.slice(-9))) / 2;
  const kijun = (Math.max(...highs.slice(-26)) + Math.min(...lows.slice(-26))) / 2;
  return (tenkan + kijun) / 2;
}

function pred30Ensemble(widths) {
  const vals = widths.filter((w) => w != null && Number.isFinite(w) && w > 0).sort((a, b) => a - b);
  if (!vals.length) return { n: 0, median: null, tight: null };
  const mid = vals.length >> 1;
  const median = vals.length % 2 ? vals[mid] : (vals[mid - 1] + vals[mid]) / 2;
  return { n: vals.length, median, tight: vals[0] };
}

function pred30Vote(votes) {
  const up = votes.filter((v) => v > 0).length;
  const down = votes.filter((v) => v < 0).length;
  const total = up + down;
  if (!total) return { up: 0, down: 0, total: 0, side: "flat", label: "0/0" };
  const side = up > down ? "up" : down > up ? "down" : "flat";
  const winning = up >= down ? up : down;
  return { up, down, total, side, label: `${winning}/${total} ${side}` };
}

function pred30VoteLean(median, side, up, down) {
  const total = up + down;
  if ((side !== "up" && side !== "down") || total <= 0 || median <= 0) return 0;
  const strength = Math.max(up, down) / total;
  const mag = median * (strength - 0.5);
  return side === "up" ? mag : -mag;
}

function pred30SlopeDrift(slope, barMinutes, cap) {
  if (slope == null || !barMinutes || barMinutes <= 0 || cap <= 0) return null;
  const raw = slope * (PRED30_HORIZON_MIN / barMinutes);
  return Math.max(-cap, Math.min(cap, raw));
}

function pred30Forecast(close, median, tight, slope, barMinutes, votes) {
  // Slope and the four votes do not predict the next 30 minutes (about 50%
  // either way, including a unanimous vote). The path is a range around the
  // last price. On Jul–Oct 2026 1m Nifty, that range held the later close
  // about two times in three. Arguments after `tight` stay so callers do not change.
  void slope;
  void barMinutes;
  void votes;
  if (close == null || median == null || median <= 0) return null;
  const span = tight != null && tight > 0 ? tight : median;
  const round2 = (n) => Math.round(n * 100) / 100;
  const lo = close - median;
  const hi = close + median;
  return {
    drift: 0,
    target: round2(close),
    lo: round2(lo),
    hi: round2(hi),
    tlo: round2(close - span),
    thi: round2(close + span),
    conflict: false,
    text: `→ ${pred30Px(close)} · ${pred30Px(lo)}–${pred30Px(hi)}`,
  };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    PRED30_HORIZON_MIN,
    PRED30_TRADING_MINUTES_YEAR,
    pred30Px,
    pred30Num,
    pred30IvWidth,
    pred30StraddleWidth,
    pred30AtrWidth,
    pred30Linreg,
    pred30IchimokuSpanA,
    pred30Ensemble,
    pred30Vote,
    pred30VoteLean,
    pred30SlopeDrift,
    pred30Forecast,
    pred30TradingMinutesToExpiry,
  };
}
