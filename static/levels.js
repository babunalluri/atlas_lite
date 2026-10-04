/* Price structure overlays: classic pivots, auto Fibonacci, fractal S/R,
   and RBR/DBD-style supply/demand. Chart only — not a trade engine. */
var LVL_FIB_RATIOS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];

function lvlIstDay(tsMs) {
  return new Date(Number(tsMs)).toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
}

function lvlPx(value, digits) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "n/a";
  return n.toFixed(digits == null ? 1 : digits);
}

function lvlClassicPivots(high, low, close) {
  const h = Number(high);
  const l = Number(low);
  const c = Number(close);
  if (![h, l, c].every(Number.isFinite) || !(h >= l)) return null;
  const p = (h + l + c) / 3;
  const range = h - l;
  return {
    p,
    r1: 2 * p - l,
    s1: 2 * p - h,
    r2: p + range,
    s2: p - range,
    r3: h + 2 * (p - l),
    s3: l - 2 * (h - p),
  };
}

function lvlSessions(dataList) {
  const map = new Map();
  for (let i = 0; i < dataList.length; i += 1) {
    const k = dataList[i];
    const day = lvlIstDay(k.timestamp);
    let s = map.get(day);
    if (!s) {
      s = {
        day,
        from: i,
        to: i,
        open: Number(k.open),
        high: Number(k.high),
        low: Number(k.low),
        close: Number(k.close),
      };
      map.set(day, s);
    } else {
      s.to = i;
      s.high = Math.max(s.high, Number(k.high));
      s.low = Math.min(s.low, Number(k.low));
      s.close = Number(k.close);
    }
  }
  return map;
}

function lvlAtrSeries(dataList, period) {
  const p = Math.max(1, Number(period) || 14);
  const out = new Array(dataList.length).fill(null);
  for (let i = 0; i < dataList.length; i += 1) {
    const h = Number(dataList[i].high);
    const l = Number(dataList[i].low);
    const prevClose = i > 0 ? Number(dataList[i - 1].close) : null;
    const tr = prevClose == null
      ? h - l
      : Math.max(h - l, Math.abs(h - prevClose), Math.abs(l - prevClose));
    if (i === 0) {
      out[i] = null;
    } else if (i < p) {
      let sum = 0;
      for (let j = 1; j <= i; j += 1) {
        const hh = Number(dataList[j].high);
        const ll = Number(dataList[j].low);
        const pc = Number(dataList[j - 1].close);
        sum += Math.max(hh - ll, Math.abs(hh - pc), Math.abs(ll - pc));
      }
      out[i] = sum / i;
    } else if (i === p) {
      let sum = 0;
      for (let j = 1; j <= p; j += 1) {
        const hh = Number(dataList[j].high);
        const ll = Number(dataList[j].low);
        const pc = Number(dataList[j - 1].close);
        sum += Math.max(hh - ll, Math.abs(hh - pc), Math.abs(ll - pc));
      }
      out[i] = sum / p;
    } else {
      out[i] = (out[i - 1] * (p - 1) + tr) / p;
    }
  }
  return out;
}

function lvlFractals(dataList, wing) {
  const w = Math.max(1, Number(wing) || 2);
  const highs = [];
  const lows = [];
  for (let i = w; i < dataList.length - w; i += 1) {
    const h = Number(dataList[i].high);
    const l = Number(dataList[i].low);
    let isHigh = true;
    let isLow = true;
    for (let k = 1; k <= w; k += 1) {
      if (!(h > Number(dataList[i - k].high) && h > Number(dataList[i + k].high))) isHigh = false;
      if (!(l < Number(dataList[i - k].low) && l < Number(dataList[i + k].low))) isLow = false;
      if (!isHigh && !isLow) break;
    }
    if (isHigh) highs.push({ i, price: h, kind: "h" });
    if (isLow) lows.push({ i, price: l, kind: "l" });
  }
  return { highs, lows };
}

function lvlLastSwingPair(dataList, opts) {
  const wing = Math.max(1, Number(opts && opts.wing) || 2);
  const minAtrMult = opts && opts.minAtrMult != null ? Number(opts.minAtrMult) : 0.5;
  const atr = lvlAtrSeries(dataList, 14);
  const { highs, lows } = lvlFractals(dataList, wing);
  const swings = highs.concat(lows).sort((a, b) => a.i - b.i);
  for (let j = swings.length - 1; j >= 1; j -= 1) {
    const later = swings[j];
    for (let k = j - 1; k >= 0; k -= 1) {
      const earlier = swings[k];
      if (earlier.kind === later.kind || earlier.i === later.i) continue;
      const span = Math.abs(later.price - earlier.price);
      if (!(span > 0)) continue;
      const a = atr[later.i] || atr[earlier.i];
      if (a && minAtrMult > 0 && span < a * minAtrMult) continue;
      return { earlier, later, up: later.kind === "h" };
    }
  }
  return null;
}

function lvlFibLevels(pair) {
  if (!pair) return [];
  return LVL_FIB_RATIOS.map((ratio) => ({
    ratio,
    label: ratio === 0 ? "0" : ratio === 1 ? "100" : String(Math.round(ratio * 1000) / 10),
    price: pair.later.price + (pair.earlier.price - pair.later.price) * ratio,
  }));
}

function calcPivotRows(dataList) {
  if (!dataList || !dataList.length) return [];
  const sessions = lvlSessions(dataList);
  const days = [...sessions.keys()].sort();
  const byDay = new Map();
  days.forEach((day, idx) => {
    if (idx === 0) return;
    const prev = sessions.get(days[idx - 1]);
    const piv = lvlClassicPivots(prev.high, prev.low, prev.close);
    if (piv) byDay.set(day, piv);
  });
  return dataList.map((k) => {
    const day = lvlIstDay(k.timestamp);
    const piv = byDay.get(day);
    return piv ? Object.assign({ day }, piv) : { day };
  });
}

function calcFibRows(dataList, opts) {
  if (!dataList || !dataList.length) return [];
  const pair = lvlLastSwingPair(dataList, opts || {});
  const levels = lvlFibLevels(pair);
  const meta = pair
    ? {
        from: pair.earlier.i,
        to: pair.later.i,
        up: pair.up,
        start: pair.earlier.price,
        end: pair.later.price,
        levels,
      }
    : null;
  return dataList.map(() => ({ fib: meta }));
}

function lvlClusterSwings(swings, thresh) {
  const sorted = [...swings].sort((a, b) => a.price - b.price);
  const groups = [];
  sorted.forEach((s) => {
    const g = groups[groups.length - 1];
    if (g && s.price - g.lo <= thresh) {
      g.members.push(s);
      g.price = g.members.reduce((sum, m) => sum + m.price, 0) / g.members.length;
      g.from = Math.min(g.from, s.i);
      if (s.kind === "h") g.highs += 1;
      else g.lows += 1;
    } else {
      groups.push({
        lo: s.price,
        price: s.price,
        members: [s],
        from: s.i,
        highs: s.kind === "h" ? 1 : 0,
        lows: s.kind === "l" ? 1 : 0,
      });
    }
  });
  return groups.map((g) => ({
    price: g.price,
    from: g.from,
    touches: g.members.length,
    // Origin hint only — calcSrRows re-labels by polarity vs last close.
    type: g.highs >= g.lows ? "R" : "S",
  }));
}

function calcSrRows(dataList, opts) {
  if (!dataList || !dataList.length) return [];
  const wing = Math.max(1, Number(opts && opts.wing) || 2);
  const maxLevels = Math.max(2, Number(opts && opts.maxLevels) || 8);
  const atr = lvlAtrSeries(dataList, 14);
  const last = dataList[dataList.length - 1];
  const lastClose = last ? Number(last.close) : 0;
  const lastAtr = atr[atr.length - 1] || Math.abs(lastClose) * 0.002 || 1;
  const thresh = Math.max(lastAtr * 0.25, Math.abs(lastClose) * 0.0006);
  const { highs, lows } = lvlFractals(dataList, wing);
  const swings = highs.concat(lows);
  let levels = lvlClusterSwings(swings, thresh);
  // Polarity: overhead = resistance, underneath = support (not fractal-kind majority).
  levels = levels.map((lv) => ({
    ...lv,
    type: lv.price >= lastClose ? "R" : "S",
  }));
  levels.sort((a, b) => {
    if (b.touches !== a.touches) return b.touches - a.touches;
    return Math.abs(a.price - lastClose) - Math.abs(b.price - lastClose);
  });
  levels = levels.slice(0, maxLevels);
  let nearestS = null;
  let nearestR = null;
  levels.forEach((lv) => {
    if (lv.type === "S" && lv.price <= lastClose) {
      if (!nearestS || lv.price > nearestS) nearestS = lv.price;
    }
    if (lv.type === "R" && lv.price >= lastClose) {
      if (!nearestR || lv.price < nearestR) nearestR = lv.price;
    }
  });
  const meta = { levels, nearestS, nearestR };
  return dataList.map(() => ({
    sr: meta,
    s: nearestS,
    r: nearestR,
  }));
}

function lvlBarTag(k, atr, impulseMult, baseMult) {
  if (atr == null || !(atr > 0)) return "mix";
  const range = Math.max(0, Number(k.high) - Number(k.low));
  const body = Number(k.close) - Number(k.open);
  const absBody = Math.abs(body);
  if ((range >= impulseMult * atr || absBody >= impulseMult * atr * 0.8) && body > 0) return "up";
  if ((range >= impulseMult * atr || absBody >= impulseMult * atr * 0.8) && body < 0) return "dn";
  if (range <= baseMult * atr) return "base";
  if (range > 0 && absBody / range <= 0.4) return "base";
  return "mix";
}

function lvlIsBaseTag(tag) {
  return tag === "base" || tag === "mix";
}

function lvlSupplyDemandZones(dataList, opts) {
  if (!dataList || !dataList.length) return [];
  const impulseMult = Math.max(0.7, Number(opts && opts.impulseMult) || 1.2);
  const baseMult = Math.min(1.2, Number(opts && opts.baseMult) || 0.8);
  const minBase = Math.max(1, Number(opts && opts.minBase) || 1);
  const maxBase = Math.max(minBase, Number(opts && opts.maxBase) || 8);
  const atr = lvlAtrSeries(dataList, 14);
  const n = dataList.length;
  const tag = dataList.map((k, i) => lvlBarTag(k, atr[i], impulseMult, baseMult));
  const zones = [];
  for (let i = 0; i < n; i += 1) {
    if (tag[i] !== "up" && tag[i] !== "dn") continue;
    const leftDir = tag[i];
    let leftEnd = i;
    while (leftEnd + 1 < n && tag[leftEnd + 1] === leftDir) leftEnd += 1;
    const b0 = leftEnd + 1;
    if (b0 >= n || !lvlIsBaseTag(tag[b0])) continue;
    let b1 = b0;
    while (b1 + 1 < n && lvlIsBaseTag(tag[b1 + 1]) && (b1 + 1 - b0 + 1) <= maxBase) b1 += 1;
    const baseLen = b1 - b0 + 1;
    if (baseLen < minBase || baseLen > maxBase) continue;
    const r0 = b1 + 1;
    if (r0 >= n || (tag[r0] !== "up" && tag[r0] !== "dn")) continue;
    const rightDir = tag[r0];
    let r1 = r0;
    while (r1 + 1 < n && tag[r1 + 1] === rightDir) r1 += 1;
    let pattern = null;
    let type = null;
    if (leftDir === "up" && rightDir === "up") {
      pattern = "RBR";
      type = "demand";
    } else if (leftDir === "dn" && rightDir === "dn") {
      pattern = "DBD";
      type = "supply";
    } else if (leftDir === "dn" && rightDir === "up") {
      pattern = "DBR";
      type = "demand";
    } else if (leftDir === "up" && rightDir === "dn") {
      pattern = "RBD";
      type = "supply";
    }
    if (!type) continue;
    let lo = Infinity;
    let hi = -Infinity;
    for (let j = b0; j <= b1; j += 1) {
      lo = Math.min(lo, Number(dataList[j].low));
      hi = Math.max(hi, Number(dataList[j].high));
    }
    if (!(hi > lo)) continue;
    let broken = null;
    for (let j = r1 + 1; j < n; j += 1) {
      const close = Number(dataList[j].close);
      if (type === "demand" && close < lo) {
        broken = j;
        break;
      }
      if (type === "supply" && close > hi) {
        broken = j;
        break;
      }
    }
    zones.push({
      type,
      pattern,
      from: b0,
      to: b1,
      out: r1,
      lo,
      hi,
      broken,
    });
    i = leftEnd;
  }
  const fresh = zones.filter((z) => z.broken == null).slice(-12);
  const faded = zones.filter((z) => z.broken != null).slice(-6);
  return fresh.concat(faded);
}

function lvlZoneAt(close, zones) {
  if (!zones || !zones.length || !Number.isFinite(close)) return null;
  let hit = null;
  zones.forEach((z) => {
    if (!(close >= z.lo && close <= z.hi)) return;
    if (!hit) {
      hit = z;
      return;
    }
    const hitFresh = hit.broken == null;
    const zFresh = z.broken == null;
    if (zFresh !== hitFresh) {
      if (zFresh) hit = z;
      return;
    }
    if (z.from > hit.from) hit = z;
  });
  return hit;
}

function calcSdRows(dataList, opts) {
  if (!dataList || !dataList.length) return [];
  const zones = lvlSupplyDemandZones(dataList, opts || {});
  return dataList.map((k) => {
    const close = Number(k.close);
    const zone = lvlZoneAt(close, zones);
    return {
      sd: zones,
      zone: zone ? zone.type : null,
      pattern: zone ? zone.pattern : null,
      lo: zone ? zone.lo : null,
      hi: zone ? zone.hi : null,
    };
  });
}

function lvlDrawHLine(ctx, xAxis, yAxis, bounding, fromIdx, toIdx, price, color, dash, label) {
  if (!Number.isFinite(price) || !bounding) return;
  const y = yAxis.convertToPixel(price);
  if (y < -8 || y > bounding.height + 8) return;
  let x0 = xAxis.convertToPixel(fromIdx);
  let x1 = toIdx == null ? bounding.width : xAxis.convertToPixel(toIdx);
  if (!Number.isFinite(x0)) x0 = 0;
  if (!Number.isFinite(x1)) x1 = bounding.width;
  if (toIdx == null) {
    x1 = bounding.width;
    x0 = x0 >= bounding.width ? 0 : Math.max(0, x0);
  } else {
    x0 = Math.max(0, Math.min(bounding.width, x0));
    x1 = Math.max(0, Math.min(bounding.width, x1));
    if (x1 < x0) {
      const swap = x0;
      x0 = x1;
      x1 = swap;
    }
  }
  if (x1 - x0 < 1) return;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  if (dash) ctx.setLineDash(dash);
  ctx.beginPath();
  ctx.moveTo(x0, y);
  ctx.lineTo(x1, y);
  ctx.stroke();
  if (label) {
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.font = "10px ui-monospace, monospace";
    ctx.textAlign = "right";
    ctx.textBaseline = "bottom";
    ctx.fillText(label, x1 - 2, y - 1);
  }
  ctx.restore();
}

function drawPivotOverlay({ ctx, kLineDataList, indicator, visibleRange, bounding, xAxis, yAxis }) {
  if (!xAxis || !yAxis || !kLineDataList.length || !visibleRange || !bounding) return false;
  const result = indicator.result || [];
  const from = Math.max(0, visibleRange.from | 0);
  const to = Math.min(kLineDataList.length - 1, visibleRange.to | 0);
  const lastDay = lvlIstDay(kLineDataList[kLineDataList.length - 1].timestamp);
  const seen = new Set();
  const specs = [
    ["p", "PP", "#fbbf24", null],
    ["r1", "R1", "#f87171", [4, 3]],
    ["r2", "R2", "#fb7185", [6, 4]],
    ["r3", "R3", "#fda4af", [2, 4]],
    ["s1", "S1", "#34d399", [4, 3]],
    ["s2", "S2", "#6ee7b7", [6, 4]],
    ["s3", "S3", "#a7f3d0", [2, 4]],
  ];
  for (let i = from; i <= to; i += 1) {
    const day = result[i]?.day || lvlIstDay(kLineDataList[i].timestamp);
    if (seen.has(day)) continue;
    seen.add(day);
    const row = result[i];
    if (!row || row.p == null) continue;
    let a = i;
    while (a > 0 && lvlIstDay(kLineDataList[a - 1].timestamp) === day) a -= 1;
    let b = i;
    while (b < kLineDataList.length - 1 && lvlIstDay(kLineDataList[b + 1].timestamp) === day) b += 1;
    const extend = day === lastDay;
    specs.forEach(([key, lab, color, dash]) => {
      lvlDrawHLine(ctx, xAxis, yAxis, bounding, a, extend ? null : b, row[key], color, dash, `${lab} ${lvlPx(row[key])}`);
    });
  }
  return true;
}

function drawFibOverlay({ ctx, kLineDataList, indicator, visibleRange, bounding, xAxis, yAxis }) {
  if (!xAxis || !yAxis || !kLineDataList.length || !bounding) return false;
  const meta = (indicator.result || [])[kLineDataList.length - 1]?.fib
    || (indicator.result || []).find((r) => r && r.fib)?.fib;
  if (!meta || !meta.levels) return false;
  const colors = {
    0: "#64748b",
    0.236: "#94a3b8",
    0.382: "#cbd5e1",
    0.5: "#e2e8f0",
    0.618: "#fbbf24",
    0.786: "#fdba74",
    1: "#64748b",
  };
  meta.levels.forEach((lv) => {
    const color = colors[lv.ratio] || "#94a3b8";
    const dash = lv.ratio === 0.5 || lv.ratio === 0.618 ? null : [4, 3];
    lvlDrawHLine(
      ctx, xAxis, yAxis, bounding,
      meta.to, null, lv.price, color, dash,
      `${lv.label} ${lvlPx(lv.price)}`,
    );
  });
  return true;
}

function drawSrOverlay({ ctx, kLineDataList, indicator, visibleRange, bounding, xAxis, yAxis }) {
  if (!xAxis || !yAxis || !kLineDataList.length || !bounding) return false;
  const meta = (indicator.result || [])[kLineDataList.length - 1]?.sr;
  if (!meta || !meta.levels) return false;
  meta.levels.forEach((lv) => {
    const support = lv.type === "S";
    const color = support ? "#34d399" : "#f87171";
    lvlDrawHLine(
      ctx, xAxis, yAxis, bounding,
      lv.from, null, lv.price, color, [5, 4],
      `${lv.type} ${lvlPx(lv.price)}`,
    );
  });
  return true;
}

function drawSdOverlay({ ctx, kLineDataList, indicator, bounding, xAxis, yAxis }) {
  if (!ctx || !xAxis || !yAxis || !kLineDataList.length) return false;
  const box = bounding || (ctx.canvas
    ? { width: ctx.canvas.width, height: ctx.canvas.height }
    : null);
  if (!box) return false;
  const rows = indicator.result || [];
  const zones = rows[kLineDataList.length - 1]?.sd
    || rows[rows.length - 1]?.sd
    || [];
  if (!zones.length) return false;
  ctx.save();
  zones.forEach((z) => {
    const faded = z.broken != null;
    let x0 = xAxis.convertToPixel(z.from);
    let x1 = faded ? xAxis.convertToPixel(z.broken) : box.width;
    if (!Number.isFinite(x0)) x0 = 0;
    if (!Number.isFinite(x1)) x1 = box.width;
    if (!faded) x1 = box.width;
    x0 = Math.max(0, Math.min(box.width, x0));
    x1 = Math.max(0, Math.min(box.width, x1));
    if (x1 - x0 < 1) return;
    const y1 = yAxis.convertToPixel(z.hi);
    const y2 = yAxis.convertToPixel(z.lo);
    if (!Number.isFinite(y1) || !Number.isFinite(y2)) return;
    let top = Math.min(y1, y2);
    let bot = Math.max(y1, y2);
    if (bot < 0 || top > box.height) return;
    top = Math.max(0, top);
    bot = Math.min(box.height, bot);
    const h = Math.max(4, bot - top);
    const w = x1 - x0;
    const y = top;
    const demand = z.type === "demand";
    ctx.fillStyle = faded
      ? (demand ? "rgba(16,185,129,0.12)" : "rgba(244,63,94,0.12)")
      : (demand ? "rgba(16,185,129,0.28)" : "rgba(244,63,94,0.28)");
    ctx.strokeStyle = faded
      ? (demand ? "rgba(16,185,129,0.55)" : "rgba(244,63,94,0.55)")
      : (demand ? "#34d399" : "#fb7185");
    ctx.lineWidth = 1.5;
    ctx.fillRect(x0, y, w, h);
    ctx.strokeRect(x0, y, w, h);
    ctx.fillStyle = ctx.strokeStyle;
    ctx.font = "10px ui-monospace, monospace";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    const tag = faded ? `${z.pattern} ×` : z.pattern;
    ctx.fillText(tag, x0 + 4, y + 3);
  });
  ctx.restore();
  return true;
}

function pivotTooltipValues(row) {
  const keys = [
    ["p", "PP", "#fbbf24"],
    ["r1", "R1", "#f87171"],
    ["s1", "S1", "#34d399"],
    ["r2", "R2", "#fb7185"],
    ["s2", "S2", "#6ee7b7"],
  ];
  if (!row || row.p == null) {
    return [{ title: { text: "Pivots: ", color: "#e2e8f0" }, value: { text: "need prior IST day", color: "#94a3b8" } }];
  }
  return keys.map(([key, lab, color]) => ({
    title: { text: `${lab}: `, color: "#e2e8f0" },
    value: { text: lvlPx(row[key]), color },
  }));
}

function fibTooltipValues(row) {
  const meta = row?.fib;
  if (!meta || !meta.levels) {
    return [{ title: { text: "Fib: ", color: "#e2e8f0" }, value: { text: "no swing pair", color: "#94a3b8" } }];
  }
  const want = new Set(["0", "50", "61.8", "100"]);
  const values = [
    {
      title: { text: "Swing: ", color: "#e2e8f0" },
      value: { text: meta.up ? "low → high" : "high → low", color: meta.up ? "#16a34a" : "#dc2626" },
    },
  ];
  meta.levels.forEach((lv) => {
    if (!want.has(lv.label)) return;
    values.push({
      title: { text: `${lv.label}: `, color: "#e2e8f0" },
      value: { text: lvlPx(lv.price), color: lv.ratio === 0.618 ? "#fbbf24" : "#e2e8f0" },
    });
  });
  return values;
}

function srTooltipValues(row) {
  const meta = row?.sr;
  if (!meta) {
    return [{ title: { text: "S/R: ", color: "#e2e8f0" }, value: { text: "n/a", color: "#94a3b8" } }];
  }
  return [
    {
      title: { text: "Support: ", color: "#e2e8f0" },
      value: { text: meta.nearestS == null ? "—" : lvlPx(meta.nearestS), color: "#34d399" },
    },
    {
      title: { text: "Resist: ", color: "#e2e8f0" },
      value: { text: meta.nearestR == null ? "—" : lvlPx(meta.nearestR), color: "#f87171" },
    },
  ];
}

function sdTooltipValues(row) {
  const zones = row?.sd || [];
  const fresh = zones.filter((z) => z.broken == null);
  if (!row || !row.zone) {
    const n = fresh.length;
    return [{
      title: { text: "Zone: ", color: "#e2e8f0" },
      value: {
        text: n ? `${n} on chart` : "none",
        color: n ? "#94a3b8" : "#94a3b8",
      },
    }];
  }
  const demand = row.zone === "demand";
  return [
    {
      title: { text: "Zone: ", color: "#e2e8f0" },
      value: {
        text: `${row.pattern || ""} ${row.zone}`.trim(),
        color: demand ? "#10b981" : "#f43f5e",
      },
    },
    {
      title: { text: "Band: ", color: "#e2e8f0" },
      value: { text: `${lvlPx(row.lo)}–${lvlPx(row.hi)}`, color: "#e2e8f0" },
    },
  ];
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    LVL_FIB_RATIOS,
    lvlIstDay,
    lvlClassicPivots,
    lvlSessions,
    lvlAtrSeries,
    lvlFractals,
    lvlLastSwingPair,
    lvlFibLevels,
    lvlClusterSwings,
    lvlBarTag,
    lvlSupplyDemandZones,
    lvlZoneAt,
    calcPivotRows,
    calcFibRows,
    calcSrRows,
    calcSdRows,
  };
}
