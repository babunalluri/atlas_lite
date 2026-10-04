/* Order flow from FUT volume × close location. Not bid/ask footprint — Kite
   has no aggressor side. Higher timeframes sum 1m deltas inside the bucket. */
function ofIstDay(tsMs) {
  return new Date(Number(tsMs)).toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}

function ofBarDelta(k) {
  const high = Number(k.high);
  const low = Number(k.low);
  const close = Number(k.close);
  const vol = Number(k.volume) || 0;
  const range = high - low;
  if (!(range > 0) || !(vol > 0)) return 0;
  return vol * ((2 * close - high - low) / range);
}

function ofBucketStartSec(timeSec, minutes) {
  const t = Number(timeSec);
  if (!minutes || minutes <= 1) return t;
  const bucket = minutes * 60;
  return Math.floor(t / bucket) * bucket;
}

function ofDataKey(tsMs, interval) {
  if (interval === 0) return ofIstDay(tsMs);
  const t = Math.floor(Number(tsMs) / 1000);
  return ofBucketStartSec(t, interval);
}

function ofRawDeltaByKey(rawBars, interval) {
  const map = new Map();
  if (!rawBars || !rawBars.length) return map;
  for (const b of rawBars) {
    const t = Number(b.time);
    if (!Number.isFinite(t)) continue;
    const key = interval === 0
      ? ofIstDay(t * 1000)
      : ofBucketStartSec(t, interval);
    const prev = map.get(key) || { delta: 0, vol: 0 };
    prev.delta += ofBarDelta(b);
    prev.vol += Number(b.volume) || 0;
    map.set(key, prev);
  }
  return map;
}

function calcOrderFlowSeries(dataList, opts) {
  const period = Math.max(5, Number(opts && opts.period) || 20);
  const impulseMult = Math.max(0.5, Number(opts && opts.impulseMult) || 1.5);
  const interval = opts && opts.interval != null ? Number(opts.interval) : 1;
  const tf = interval === 0 ? 0 : (interval || 1);
  const lookback = tf <= 1 ? period : Math.max(5, Math.round(period / Math.max(tf, 1)));
  const packed = opts && opts.rawBars && tf !== 1
    ? ofRawDeltaByKey(opts.rawBars, tf)
    : null;

  let day = "";
  let cvd = 0;
  let side = null;
  let divSide = null;
  const absWin = [];
  const volWin = [];
  const out = [];

  for (let i = 0; i < dataList.length; i += 1) {
    const k = dataList[i];
    const key = ofIstDay(k.timestamp);
    if (tf !== 0 && key !== day) {
      day = key;
      cvd = 0;
      side = null;
      divSide = null;
      absWin.length = 0;
      volWin.length = 0;
    } else if (tf === 0) {
      day = key;
    }

    const raw = packed ? packed.get(ofDataKey(k.timestamp, tf)) : null;
    const delta = raw ? raw.delta : ofBarDelta(k);
    const vol = raw ? raw.vol : (Number(k.volume) || 0);
    cvd += delta;
    if (vol > 0) {
      absWin.push(Math.abs(delta));
      volWin.push(vol);
      if (absWin.length > lookback) absWin.shift();
      if (volWin.length > lookback) volWin.shift();
    }
    const avgAbs = absWin.length
      ? absWin.reduce((sum, x) => sum + x, 0) / absWin.length
      : 0;
    const avgVol = volWin.length
      ? volWin.reduce((sum, x) => sum + x, 0) / volWin.length
      : 0;
    const ready = absWin.length >= lookback && avgAbs > 0;
    const impulse = ready && Math.abs(delta) >= impulseMult * avgAbs;

    let next = null;
    if (impulse && delta > 0 && cvd > 0) next = "B";
    else if (impulse && delta < 0 && cvd < 0) next = "S";
    let signal = null;
    if (next && next !== side) {
      signal = next;
      side = next;
    }

    const high = Number(k.high);
    const low = Number(k.low);
    const close = Number(k.close);
    const range = high - low;
    const loc = range > 0 ? (close - low) / range : 0.5;
    // Same-bar CLV makes (delta>0 && loc<=0.35) impossible. On 1m, high volume
    // with a mid close is churn. Higher TFs keep 1m-sum Δ vs bucket location.
    const absorb = ready
      && avgVol > 0
      && vol >= 1.5 * avgVol
      && (raw
        ? ((delta > 0 && loc <= 0.35) || (delta < 0 && loc >= 0.65))
        : range > 0 && Math.abs(delta) / vol <= 0.35);

    let liveDiv = null;
    if (i >= lookback) {
      const prev = dataList[i - lookback];
      const prevRow = out[i - lookback];
      const sameSession = tf === 0 || (prev && ofIstDay(prev.timestamp) === key);
      if (sameSession && prev && prevRow) {
        const dPx = close - Number(prev.close);
        const dCvd = cvd - prevRow.cvd;
        if (dPx > 0 && dCvd < 0) liveDiv = "S";
        else if (dPx < 0 && dCvd > 0) liveDiv = "B";
      }
    }
    let div = null;
    if (liveDiv !== divSide) {
      if (liveDiv) div = liveDiv;
      divSide = liveDiv;
    }

    out.push({
      delta,
      cvd,
      vol,
      avgAbs,
      avgVol,
      impulse,
      absorb,
      div,
      divSide,
      signal,
      side,
      buy: signal === "B",
      sell: signal === "S",
      source: raw ? "1m" : "bar",
    });
  }
  return out;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    ofBarDelta,
    ofBucketStartSec,
    ofRawDeltaByKey,
    calcOrderFlowSeries,
  };
}
