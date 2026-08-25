/**
 * Mahi Portal — Direct browser scan engine (three setups, no backend needed).
 *
 * Setups (each shown in its own sub-tab on the Alerts page):
 *   ol — Open=Low  intraday (bullish):  |open − low|  ≤ ₹0.10, Vol tiers
 *   oh — Open=High intraday (bearish):  |high − open| ≤ ₹0.10, Vol tiers
 *   bb — BB Trap v2 positional:         primary candle outside BB(20, 2σ),
 *                                       rejection wick ≥50%, Vol ≥1.5×
 *
 * All data comes from Yahoo Finance public endpoints through a CORS-proxy
 * chain. Intraday quotes and daily closes are batched (30 symbols/request)
 * and shared between setups with a short TTL so one pull feeds every tab.
 *
 * Exposed as window.DirectScan.
 */
(function () {
  "use strict";

  var base = "";
  var baseEl = document.querySelector("base");
  if (baseEl) base = (baseEl.getAttribute("href") || "").replace(/\/+$/, "");
  if (!base && location.hostname.endsWith(".github.io")) {
    var seg = location.pathname.replace(/^\/+/, "").split("/")[0];
    if (seg) base = "/" + seg;
  }

  // Scan config — mirrors the Python scanners' defaults.
  var CFG = {
    OL_DIFF_MAX: 0.10,
    OL_ROUGH_TOL: 0.003,
    MIN_VOL_MULT: 1.5,
    PRICE_MIN: 50,
    SL_PCT: 0.5,
    INVESTMENT: 10000,
    VOL_LOOKBACK: 20,
    BATCH: 30,
    WORKERS: 2,
    // BB Trap v2 (from scan_bb_trap_v2.py)
    BB_PERIOD: 20,
    BB_STD: 2.0,
    BB_MIN_WICK: 0.50,
    BB_MIN_VOL_MULT: 1.5,
    BB_RSI_PERIOD: 14,
    BB_RSI_THRESHOLD: 70,
    BB_MIN_AVG_VOL: 100000
  };

  var TTL = 180000; // shared-data cache lifetime (3 min)
  var shared = { quotes: null, quotesAt: 0, daily: null, dailyAt: 0 };

  // r.jina.ai's response shape is unstable: sometimes clean JSON, sometimes
  // {data:{content:"<json string>"}}, sometimes markdown with the JSON
  // embedded. This digests all three.
  function extractJson(text) {
    if (text && typeof text === "object") return text;
    var s = String(text);
    try { return JSON.parse(s); } catch (e) {}
    var i = s.indexOf("{");
    while (i !== -1) {
      var end = s.lastIndexOf("}");
      while (end > i) {
        try { return JSON.parse(s.slice(i, end + 1)); } catch (e) {}
        end = s.lastIndexOf("}", end - 1);
      }
      i = s.indexOf("{", i + 1);
    }
    throw new Error("unparseable proxy payload");
  }

  function fetchWithTimeout(url, ms) {
    return new Promise(function (resolve, reject) {
      var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
      var timer = setTimeout(function () {
        if (ctrl) ctrl.abort();
        reject(new Error("timeout"));
      }, ms);
      fetch(url, ctrl ? { signal: ctrl.signal } : undefined)
        .then(function (r) {
          clearTimeout(timer);
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.text();
        })
        .then(resolve, function (e) {
          clearTimeout(timer);
          reject(e);
        });
    });
  }

  // Fetch a Yahoo URL through the proxy chain. r.jina.ai is primary; direct
  // Yahoo fails instantly in browsers (no CORS headers), which is cheaper
  // than waiting on the occasionally-dead public proxies.
  var PROXIES = [
    {
      wrap: function (u) { return "https://r.jina.ai/" + u; },
      unwrap: function (d) {
        if (d && d.data && d.data.content != null) {
          try { return extractJson(d.data.content); } catch (e) { /* wrapped differently */ }
        }
        return d;
      }
    },
    {
      wrap: function (u) { return u; },
      unwrap: function (d) { return d; }
    },
    {
      wrap: function (u) { return "https://api.allorigins.win/raw?url=" + encodeURIComponent(u); },
      unwrap: function (d) { return d; }
    },
    {
      wrap: function (u) { return "https://api.codetabs.com/v1/proxy?quest=" + encodeURIComponent(u); },
      unwrap: function (d) { return d; }
    }
  ];

  function fetchYahoo(yurl, timeoutMs) {
    function attempt(proxyIdx, tryNum, delay) {
      return new Promise(function (resolve) {
        setTimeout(function () {
          fetchWithTimeout(PROXIES[proxyIdx].wrap(yurl), timeoutMs || 15000)
            .then(function (text) {
              resolve(PROXIES[proxyIdx].unwrap(extractJson(text)));
            })
            .catch(function () {
              if (tryNum < 3) resolve(attempt(proxyIdx, tryNum + 1, tryNum === 1 ? 2500 : 6000));
              else if (proxyIdx + 1 < PROXIES.length) resolve(attempt(proxyIdx + 1, 1, 800));
              else resolve(null);
            });
        }, delay);
      });
    }
    return attempt(0, 1, 0);
  }

  // -------------------------------------------------------------------------
  // IST helpers (Yahoo epoch seconds are exchange-local)
  // -------------------------------------------------------------------------
  function istShift(d) { return new Date(d.getTime() + (330 + d.getTimezoneOffset()) * 60000); }
  function istNow() { return istShift(new Date()); }
  function istDateStr(ts) {
    var d = ts ? istShift(new Date(ts * 1000)) : istNow();
    return d.toISOString().slice(0, 10);
  }
  function fmtTs(ts) {
    var d = ts ? istShift(new Date(ts * 1000)) : istNow();
    return d.toISOString().slice(0, 16).replace("T", " ") + " IST";
  }
  function sessionPct() {
    var d = istNow();
    var day = d.getDay();
    var mins = d.getHours() * 60 + d.getMinutes();
    if (day === 0 || day === 6) return 1;
    if (mins <= 555) return 0;
    if (mins >= 930) return 1;
    return +(((mins - 555) / 375) * 100).toFixed(1) / 100;
  }

  function escHtml(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  function inr(v, dec) {
    if (v == null || isNaN(v)) return "—";
    return "₹" + (+v).toLocaleString("en-IN", { minimumFractionDigits: dec || 0, maximumFractionDigits: dec == null ? 2 : dec });
  }
  function pctStr(v) { return (v >= 0 ? "+" : "") + (+v).toFixed(2) + "%"; }

  // -------------------------------------------------------------------------
  // Universe + batch fetching
  // -------------------------------------------------------------------------
  function loadUniverse() {
    return fetch(base + "/data/nse200_symbols.csv")
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (text) {
        var syms = [];
        text.split(/\r?\n/).forEach(function (line) {
          var s = line.trim().split(",")[0];
          if (s && s.toLowerCase() !== "symbol") syms.push(s);
        });
        if (!syms.length) throw new Error("empty universe CSV");
        return syms;
      });
  }

  // Batched spark fetch. interval "5m" → today's intraday quotes;
  // interval "1d" → ~2 months of daily bars per symbol.
  function fetchBatches(interval, rowParser, onTick) {
    return loadUniverse().then(function (syms) {
      var chunks = [];
      for (var i = 0; i < syms.length; i += CFG.BATCH) chunks.push(syms.slice(i, i + CFG.BATCH));
      var out = [], idx = 0, workers = [], done = 0;

      function next() {
        if (idx >= chunks.length) return Promise.resolve();
        var my = idx++;
        var yurl = "https://query1.finance.yahoo.com/v7/finance/spark?symbols=" +
          chunks[my].map(encodeURIComponent).join(",") +
          "&range=" + (interval === "1d" ? "2mo" : "1d") + "&interval=" + interval;
        return fetchYahoo(yurl, 15000).then(function (data) {
          var rows = (data && data.spark && data.spark.result) || null;
          (rows || []).forEach(function (r) { var q = rowParser(r); if (q) out.push(q); });
          done++;
          if (onTick) onTick(done, chunks.length, out.length);
          return next();
        });
      }
      for (var w = 0; w < CFG.WORKERS; w++) workers.push(next());
      return Promise.all(workers).then(function () { return out; });
    });
  }

  // Intraday quote from a 5m spark row (open ≈ first 5m close until refined)
  function quoteFromSpark(r) {
    var resp = (r.response || [])[0];
    if (!resp || !resp.meta) return null;
    var m = resp.meta;
    var ltp = m.regularMarketPrice;
    var prev = m.chartPreviousClose != null ? m.chartPreviousClose : m.previousClose;
    if (ltp == null || prev == null) return null;
    var open5m = null;
    var q = resp.indicators && resp.indicators.quote && resp.indicators.quote[0];
    if (q && q.close) {
      for (var i = 0; i < q.close.length; i++) {
        if (q.close[i] != null) { open5m = q.close[i]; break; }
      }
    }
    return {
      symbol: (m.symbol || r.symbol || "").replace(/\.NS$/, ""),
      name: m.shortName || m.longName || "",
      ltp: ltp,
      prev_close: prev,
      open: open5m,
      high: m.regularMarketDayHigh != null ? m.regularMarketDayHigh : ltp,
      low: m.regularMarketDayLow != null ? m.regularMarketDayLow : ltp,
      volume: m.regularMarketVolume != null ? m.regularMarketVolume : 0,
      market_time: m.regularMarketTime || 0,
      approx: true
    };
  }

  // Daily history from a 1d spark row: full close series + timestamps
  function dailyFromSpark(r) {
    var resp = (r.response || [])[0];
    if (!resp || !resp.meta) return null;
    var q = resp.indicators && resp.indicators.quote && resp.indicators.quote[0];
    if (!q || !q.close || !resp.timestamp) return null;
    var closes = [], ts = [];
    for (var i = 0; i < q.close.length; i++) {
      if (q.close[i] != null) { closes.push(q.close[i]); ts.push(resp.timestamp[i]); }
    }
    if (closes.length < 2) return null;
    return {
      symbol: (resp.meta.symbol || r.symbol || "").replace(/\.NS$/, ""),
      closes: closes,
      ts: ts
    };
  }

  function getQuotes(onProgress) {
    if (shared.quotes && Date.now() - shared.quotesAt < TTL) return Promise.resolve(shared.quotes);
    return fetchBatches("5m", quoteFromSpark, function (done, total, got) {
      onProgress(Math.round((done / total) * 60), "Quotes " + done + "/" + total + " — " + got + " symbols");
    }).then(function (quotes) {
      if (!quotes.length) throw new Error("no quotes — CORS proxies unreachable");
      shared.quotes = quotes; shared.quotesAt = Date.now();
      return quotes;
    });
  }

  function getDaily(onProgress) {
    if (shared.daily && Date.now() - shared.dailyAt < TTL) return Promise.resolve(shared.daily);
    return fetchBatches("1d", dailyFromSpark, function (done, total, got) {
      onProgress(Math.round((done / total) * 70), "Daily history " + done + "/" + total + " — " + got + " symbols");
    }).then(function (rows) {
      if (!rows.length) throw new Error("no daily data — CORS proxies unreachable");
      var map = {};
      rows.forEach(function (d) { map[d.symbol] = d; });
      shared.daily = map; shared.dailyAt = Date.now();
      return map;
    });
  }

  // -------------------------------------------------------------------------
  // 1m refinement for O=L / O=H candidates (batched, strict ₹0.10 rule is
  // applied by the caller via ruleFn)
  // -------------------------------------------------------------------------
  function refineCandidates(candidates, ruleFn, onTick) {
    var chunks = [];
    for (var i = 0; i < candidates.length; i += CFG.BATCH) chunks.push(candidates.slice(i, i + CFG.BATCH));
    var idx = 0, workers = [], done = 0, refined = [];

    function nextChunk() {
      if (idx >= chunks.length) return Promise.resolve();
      var my = idx++;
      var syms = chunks[my].map(function (q) { return q.symbol + ".NS"; });
      var yurl = "https://query1.finance.yahoo.com/v7/finance/spark?symbols=" +
        syms.map(encodeURIComponent).join(",") + "&range=1d&interval=1m";
      return fetchYahoo(yurl, 15000).then(function (data) {
        var rows = (data && data.spark && data.spark.result) || null;
        if (rows) {
          var bySym = {};
          rows.forEach(function (r) {
            var resp = (r.response || [])[0];
            if (resp && resp.meta) bySym[(resp.meta.symbol || r.symbol || "").replace(/\.NS$/, "")] = resp;
          });
          chunks[my].forEach(function (q) {
            var resp = bySym[q.symbol];
            if (resp) {
              var m = resp.meta;
              var open1m = null;
              var qq = resp.indicators && resp.indicators.quote && resp.indicators.quote[0];
              if (qq && qq.close) {
                for (var i2 = 0; i2 < qq.close.length; i2++) {
                  if (qq.close[i2] != null) { open1m = qq.close[i2]; break; }
                }
              }
              if (open1m != null) q.open = open1m;
              if (m.regularMarketDayLow != null) q.low = m.regularMarketDayLow;
              if (m.regularMarketDayHigh != null) q.high = m.regularMarketDayHigh;
              if (m.regularMarketVolume != null) q.volume = m.regularMarketVolume;
              if (m.regularMarketPrice != null) q.ltp = m.regularMarketPrice;
              var pc = m.chartPreviousClose != null ? m.chartPreviousClose : m.previousClose;
              if (pc != null) q.prev_close = pc;
              if (m.regularMarketTime) q.market_time = m.regularMarketTime;
              q.approx = false;
            }
            if (ruleFn(q)) refined.push(q);
          });
        } else {
          chunks[my].forEach(function (q) { if (ruleFn(q)) refined.push(q); });
        }
        done++;
        if (onTick) onTick(done, chunks.length, refined.length);
        return nextChunk();
      });
    }
    for (var w = 0; w < CFG.WORKERS; w++) workers.push(nextChunk());
    return Promise.all(workers).then(function () { return refined; });
  }

  // -------------------------------------------------------------------------
  // Per-symbol extras (few calls): 20d avg volume, full daily OHLCV
  // -------------------------------------------------------------------------
  var histVolCache = {};

  function chartDaily(sym) {
    var yurl = "https://query1.finance.yahoo.com/v8/finance/chart/" +
      encodeURIComponent(sym + ".NS") + "?range=3mo&interval=1d";
    return fetchYahoo(yurl, 15000).then(function (data) {
      var res = data && data.chart && data.chart.result && data.chart.result[0];
      if (!res || !res.timestamp) return null;
      var q = res.indicators && res.indicators.quote && res.indicators.quote[0];
      if (!q || !q.close) return null;
      var bars = [];
      for (var i = 0; i < res.timestamp.length; i++) {
        if (q.close[i] == null || q.open[i] == null) continue;
        bars.push({
          ts: res.timestamp[i],
          o: q.open[i], h: q.high[i], l: q.low[i], c: q.close[i],
          v: q.volume[i] != null ? q.volume[i] : 0
        });
      }
      return bars.length ? bars : null;
    });
  }

  function avgVol20dFor(sym, todayStr) {
    var key = sym + "|" + todayStr;
    if (histVolCache[key] !== undefined) return Promise.resolve(histVolCache[key]);
    return chartDaily(sym).then(function (bars) {
      var vols = [];
      for (var i = 0; i < bars.length; i++) {
        if (istDateStr(bars[i].ts) === todayStr) continue;
        vols.push(bars[i].v);
      }
      var avg = 0;
      if (vols.length) {
        var tail = vols.slice(-CFG.VOL_LOOKBACK);
        var sum = 0;
        tail.forEach(function (v) { sum += v; });
        avg = Math.round(sum / tail.length);
      }
      histVolCache[key] = avg;
      return avg;
    });
  }

  // -------------------------------------------------------------------------
  // O=L / O=H intraday scans (shared machinery)
  // -------------------------------------------------------------------------
  function runIntradayScan(kind, onProgress) {
    var progress = typeof onProgress === "function" ? onProgress : function () {};
    var quotes = [];

    return getQuotes(progress)
      .then(function (q) {
        quotes = q;
        // Rough pre-filter with the 5m-approx open
        var candidates = quotes.filter(function (x) {
          if (x.open == null) return false;
          if (kind === "ol") {
            if (!x.low) return false;
            return Math.abs(x.open - x.low) <= Math.max(CFG.OL_DIFF_MAX, CFG.OL_ROUGH_TOL * x.low);
          }
          if (!x.high) return false;
          return Math.abs(x.high - x.open) <= Math.max(CFG.OL_DIFF_MAX, CFG.OL_ROUGH_TOL * x.high);
        });
        progress(65, candidates.length + " candidates — refining…");

        var rule = kind === "ol"
          ? function (x) { return Math.abs(x.open - x.low) <= CFG.OL_DIFF_MAX; }
          : function (x) { return Math.abs(x.high - x.open) <= CFG.OL_DIFF_MAX; };

        return refineCandidates(candidates, rule, function (done, total, found) {
          progress(65 + Math.round((done / Math.max(total, 1)) * 10),
            "Refine " + done + "/" + total + " — " + found + " confirmed");
        });
      })
      .then(function (refined) {
        var todayStr = null;
        refined.forEach(function (q) {
          var s = istDateStr(q.market_time);
          if (!todayStr) todayStr = s;
        });
        var idx = 0, workers = [], doneR = 0;
        function nextRef() {
          if (idx >= refined.length) return Promise.resolve();
          var i = idx++;
          return avgVol20dFor(refined[i].symbol, todayStr).then(function (avg) {
            refined[i].avg_vol_20d = avg || 0;
            doneR++;
            progress(75 + Math.round((doneR / Math.max(refined.length, 1)) * 20),
              "Volume history " + doneR + "/" + refined.length);
            return nextRef();
          });
        }
        for (var w = 0; w < CFG.WORKERS; w++) workers.push(nextRef());
        return Promise.all(workers).then(function () { return refined; });
      })
      .then(function (refined) {
        progress(97, "Classifying…");
        var sess = sessionPct();
        var rows = refined.map(function (q) {
          var volRatio = q.avg_vol_20d ? +(q.volume / q.avg_vol_20d).toFixed(2) : 0;
          var estFull = sess > 0 ? +(volRatio / sess).toFixed(1) : volRatio;
          var shares = q.open > 0 ? Math.floor(CFG.INVESTMENT / q.open) : 0;
          var bullish = kind === "ol";
          return {
            kind: kind,
            symbol: q.symbol,
            name: q.name,
            open: q.open,
            low: q.low,
            high: q.high,
            ol_diff: kind === "ol" ? +Math.abs(q.open - q.low).toFixed(2) : +Math.abs(q.high - q.open).toFixed(2),
            ltp: q.ltp,
            prev_close: q.prev_close,
            volume: q.volume,
            avg_vol_20d: q.avg_vol_20d,
            vol_ratio: volRatio,
            est_full_vol_ratio: estFull,
            shares: shares,
            invested: +(shares * q.open).toFixed(0),
            sl_price: +(q.open * (bullish ? 1 - CFG.SL_PCT / 100 : 1 + CFG.SL_PCT / 100)).toFixed(2),
            pnl: +(((bullish ? 1 : -1) * (q.ltp - q.open)) * shares).toFixed(0),
            pnl_pct: q.open ? +(((bullish ? 1 : -1) * (q.ltp - q.open) / q.open) * 100).toFixed(2) : 0,
            gap_pct: q.prev_close ? +(((q.open - q.prev_close) / q.prev_close) * 100).toFixed(2) : 0,
            ext_pct: bullish
              ? (q.open ? +(((q.high - q.open) / q.open) * 100).toFixed(2) : 0)
              : (q.open ? +(((q.open - q.low) / q.open) * 100).toFixed(2) : 0),
            approx: q.approx,
            in_nse200: true,
            date: istDateStr(q.market_time)
          };
        });

        var withVolume = rows
          .filter(function (r) { return r.vol_ratio >= CFG.MIN_VOL_MULT && r.ltp >= CFG.PRICE_MIN; })
          .sort(function (a, b) { return b.vol_ratio - a.vol_ratio; });
        var withoutVolume = rows
          .filter(function (r) { return withVolume.indexOf(r) === -1; })
          .sort(function (a, b) { return b.est_full_vol_ratio - a.est_full_vol_ratio; });

        return {
          kind: kind,
          with_volume: withVolume,
          without_volume: withoutVolume,
          scanned_at: fmtTs(null),
          universe_count: quotes.length,
          session_pct: +(sess * 100).toFixed(1),
          approx_count: rows.filter(function (r) { return r.approx; }).length
        };
      });
  }

  function runOpenLow(onProgress) { return runIntradayScan("ol", onProgress); }
  function runOpenHigh(onProgress) { return runIntradayScan("oh", onProgress); }

  // -------------------------------------------------------------------------
  // BB Trap v2 (positional) — pre-filter on batched daily closes, exact OHLC
  // rules verified per surviving symbol
  // -------------------------------------------------------------------------
  function computeBB(closes) {
    if (closes.length < CFG.BB_PERIOD) return null;
    var w = closes.slice(-CFG.BB_PERIOD);
    var sma = w.reduce(function (a, b) { return a + b; }, 0) / w.length;
    var vari = w.reduce(function (a, b) { return a + (b - sma) * (b - sma); }, 0) / w.length;
    var sd = Math.sqrt(vari);
    return { sma: sma, upper: sma + CFG.BB_STD * sd, lower: sma - CFG.BB_STD * sd };
  }

  function computeRSI(closes) {
    var p = CFG.BB_RSI_PERIOD;
    if (closes.length < p + 1) return null;
    var gains = 0, losses = 0;
    for (var i = closes.length - p; i < closes.length; i++) {
      var ch = closes[i] - closes[i - 1];
      if (ch > 0) gains += ch; else losses -= ch;
    }
    var ag = gains / p, al = losses / p;
    return al === 0 ? 100 : 100 - 100 / (1 + ag / al);
  }

  function wickPct(o, h, l, c, upper) {
    var rng = h - l;
    if (rng <= 0) return 0;
    return upper ? (h - Math.max(o, c)) / rng : (Math.min(o, c) - l) / rng;
  }

  function runBBTrap(onProgress) {
    var progress = typeof onProgress === "function" ? onProgress : function () {};
    var preShort = [], preLong = [];

    return getDaily(progress)
      .then(function (daily) {
        Object.keys(daily).forEach(function (sym) {
          var d = daily[sym];
          var closes = d.closes;
          if (closes.length < CFG.BB_PERIOD + 2) return;
          if (closes[closes.length - 1] < CFG.PRICE_MIN) return;
          var bb = computeBB(closes.slice(0, closes.length - 2));
          if (!bb) return;
          // Necessary condition: primary close beyond the band (close ≥ low,
          // close ≤ high), so this pre-filter never misses a real signal.
          var cP = closes[closes.length - 3];
          if (cP > bb.upper) preShort.push({ symbol: sym, closes: closes, ts: d.ts });
          else if (cP < bb.lower) preLong.push({ symbol: sym, closes: closes, ts: d.ts });
        });
        var cands = preShort.concat(preLong);
        progress(72, cands.length + " BB candidates — verifying candles…");

        // Exact rules per candidate from full daily OHLCV
        var idx = 0, workers = [], shorts = [], longs = [], done = 0;
        function nextCand() {
          if (idx >= cands.length) return Promise.resolve();
          var i = idx++;
          var cand = cands[i];
          return chartDaily(cand.symbol).then(function (bars) {
            if (bars && bars.length >= CFG.BB_PERIOD + 2) {
              var closes = bars.map(function (b) { return b.c; });
              var bb = computeBB(closes.slice(0, closes.length - 2));
              if (bb) {
                var p = bars[bars.length - 3], a = bars[bars.length - 2];
                var volMult = p.v > 0 ? a.v / p.v : 999;
                var avg10 = bars.slice(-12, -2).reduce(function (s, b) { return s + b.v; }, 0) / 10;
                var isShort = preShort.indexOf(cand) >= 0;

                var passesWickVol = volMult >= CFG.BB_MIN_VOL_MULT && avg10 >= CFG.BB_MIN_AVG_VOL;
                if (isShort && passesWickVol && p.l > bb.upper &&
                    wickPct(a.o, a.h, a.l, a.c, true) >= CFG.BB_MIN_WICK) {
                  var entry = closes[closes.length - 1];
                  var rng = p.h - p.l;
                  var sl = entry + rng * 0.30, tgt = entry - rng * 0.80;
                  var risk = sl - entry, reward = entry - tgt;
                  var rsi = computeRSI(closes);
                  var uw = wickPct(a.o, a.h, a.l, a.c, true);
                  shorts.push({
                    kind: "bb", type: "SHORT", symbol: cand.symbol,
                    entry_price: +entry.toFixed(2), sl_price: +sl.toFixed(2), target_price: +tgt.toFixed(2),
                    rr: risk > 0 ? +(reward / risk).toFixed(1) : 0,
                    wick_pct: +(uw * 100).toFixed(0), vol_multiple: +volMult.toFixed(1),
                    rsi: rsi != null ? +rsi.toFixed(0) : null, rsi_pass: rsi != null && rsi > CFG.BB_RSI_THRESHOLD,
                    primary_range: +rng.toFixed(2),
                    primary_date: istDateStr(p.ts), alert_date: istDateStr(a.ts),
                    score: +(((reward / risk) * 10) + volMult * 2 + uw * 5 + (rsi != null && rsi > CFG.BB_RSI_THRESHOLD ? 10 : 0)).toFixed(1)
                  });
                }
                if (!isShort && passesWickVol && p.h < bb.lower &&
                    wickPct(a.o, a.h, a.l, a.c, false) >= CFG.BB_MIN_WICK) {
                  var entryL = closes[closes.length - 1];
                  var rngL = p.h - p.l;
                  var slL = entryL - rngL * 0.50, tgtL = entryL + rngL * 1.00;
                  var riskL = entryL - slL, rewardL = tgtL - entryL;
                  var lw = wickPct(a.o, a.h, a.l, a.c, false);
                  longs.push({
                    kind: "bb", type: "LONG", symbol: cand.symbol,
                    entry_price: +entryL.toFixed(2), sl_price: +slL.toFixed(2), target_price: +tgtL.toFixed(2),
                    rr: riskL > 0 ? +(rewardL / riskL).toFixed(1) : 0,
                    wick_pct: +(lw * 100).toFixed(0), vol_multiple: +volMult.toFixed(1),
                    rsi: null, rsi_pass: false,
                    primary_range: +rngL.toFixed(2),
                    primary_date: istDateStr(p.ts), alert_date: istDateStr(a.ts),
                    score: +(((rewardL / riskL) * 10) + volMult * 2 + lw * 5).toFixed(1)
                  });
                }
              }
            }
            done++;
            progress(72 + Math.round((done / Math.max(cands.length, 1)) * 25),
              "Verify " + done + "/" + cands.length);
            return nextCand();
          });
        }
        for (var w = 0; w < CFG.WORKERS; w++) workers.push(nextCand());
        return Promise.all(workers).then(function () {
          return { shorts: shorts, longs: longs };
        });
      })
      .then(function (found) {
        var shorts = found.shorts, longs = found.longs;
        progress(97, "Classifying…");
        shorts.sort(function (a, b) { return b.score - a.score; });
        longs.sort(function (a, b) { return b.score - a.score; });
        return {
          kind: "bb",
          shorts: shorts,
          longs: longs,
          with_volume: [],
          without_volume: [],
          scanned_at: fmtTs(null),
          universe_count: Object.keys(shared.daily || {}).length,
          session_pct: +(sessionPct() * 100).toFixed(1)
        };
      });
  }

  // -------------------------------------------------------------------------
  // Renderers — one panel per setup on the Alerts page, nothing mixed
  // -------------------------------------------------------------------------
  function takeTradeFormHtml(r, label) {
    return '<form method="POST" action="/paper-trade" style="display:inline">' +
      '<input type="hidden" name="symbol" value="' + escHtml(r.symbol) + '">' +
      '<input type="hidden" name="entry_price" value="' + r.open + '">' +
      '<input type="hidden" name="sl_price" value="' + r.sl_price + '">' +
      '<input type="hidden" name="quantity" value="' + r.shares + '">' +
      '<input type="hidden" name="invested" value="' + r.invested + '.0">' +
      '<input type="hidden" name="entry_date" value="' + r.date + ' 00:00:00">' +
      '<button type="submit" class="take-trade-btn">' + (label || "📝 Take") + '</button></form>';
  }

  function wireTakeTrades(scope) {
    (scope || document).querySelectorAll(".take-trade-btn").forEach(function (btn) {
      if (btn._directWired) return;
      btn._directWired = true;
      var form = btn.closest("form");
      if (!form) return;
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        if (!window.addPaperTrade) return;
        var trade = {
          strategy: "open-low",
          symbol: form.querySelector('input[name="symbol"]').value,
          entry_date: form.querySelector('input[name="entry_date"]').value,
          entry_price: parseFloat(form.querySelector('input[name="entry_price"]').value),
          sl_price: parseFloat(form.querySelector('input[name="sl_price"]').value),
          quantity: parseInt(form.querySelector('input[name="quantity"]').value, 10),
          invested: parseFloat(form.querySelector('input[name="invested"]').value)
        };
        window.addPaperTrade(trade);
        btn.textContent = "✅ Taken";
        btn.style.opacity = "0.6";
        btn.style.pointerEvents = "none";
      });
    });
  }

  function setH1Count(text) {
    var m = document.querySelector("h1 .muted");
    if (m) m.textContent = text;
  }

  // -- O=L panel (renders into the baked sections when present, else into panel)
  function renderAlertsPage(result) {
    var scope = document.getElementById("panel-ol") || document;
    var total = result.with_volume.length + result.without_volume.length;
    setH1Count("(" + total + " from NSE 200)");

    var lr = document.getElementById("lastRefresh");
    if (lr) lr.textContent = "Last refresh: " + result.scanned_at +
      (result.source === "scheduled" ? " (scheduled server scan — updates every 15 min in market hours)" : " (live browser scan — no server needed)");

    var withSec = null, withoutSec = null;
    scope.querySelectorAll("section").forEach(function (s) {
      var h = s.querySelector("h2");
      if (!h) return;
      var t = h.textContent || "";
      if (t.indexOf("With Volume") >= 0) withSec = s;
      else if (t.indexOf("Without Volume") >= 0) withoutSec = s;
    });

    if (withSec) {
      var h2m = withSec.querySelector("h2 .muted");
      if (h2m) h2m.textContent = "(" + result.with_volume.length + " tradeable)";
      var vals = withSec.querySelectorAll(".sum-value");
      if (vals.length >= 3) {
        var invested = 0, pnl = 0;
        result.with_volume.forEach(function (r) { invested += r.invested || 0; pnl += r.pnl || 0; });
        vals[0].textContent = result.with_volume.length;
        vals[1].textContent = inr(invested, 0);
        vals[2].textContent = inr(pnl, 0).replace("₹", "₹" + (pnl >= 0 ? "+" : "-"));
        vals[2].className = "sum-value " + (pnl >= 0 ? "pos" : "neg");
      }
      var tb1 = withSec.querySelector("tbody");
      if (tb1) {
        var html = "";
        result.with_volume.forEach(function (r, i) {
          html += "<tr><td>" + (i + 1) + "</td>" +
            "<td><strong>" + escHtml(r.symbol) + "</strong></td>" +
            "<td>" + inr(r.open) + "</td>" +
            "<td>" + inr(r.low) + "</td>" +
            "<td>" + inr(r.ol_diff) + "</td>" +
            "<td>" + inr(r.ltp) + "</td>" +
            "<td>" + r.vol_ratio.toFixed(2) + "×</td>" +
            "<td>✅</td>" +
            "<td>" + inr(r.sl_price) + "</td>" +
            '<td class="' + (r.pnl >= 0 ? "ok" : "bad") + '">' + inr(r.pnl, 0).replace("₹", "₹" + (r.pnl >= 0 ? "+" : "-")) + "</td>" +
            '<td class="' + (r.pnl_pct >= 0 ? "ok" : "bad") + '">' + pctStr(r.pnl_pct) + "</td>" +
            "<td>" + pctStr(r.ext_pct) + "</td>" +
            '<td class="' + (r.pnl >= 0 ? "ok" : "bad") + '">' + (r.pnl >= 0 ? "PROFIT" : "LOSS") + "</td>" +
            "<td>" + takeTradeFormHtml(r) + "</td></tr>";
        });
        if (!html) html = '<tr><td colspan="14" class="muted" style="text-align:center;padding:1.2rem">No O=L + volume signals right now (' + result.universe_count + ' scanned).</td></tr>';
        tb1.innerHTML = html;
      }
      withSec.querySelectorAll(".trade-card").forEach(function (c) { c.remove(); });
      var anchor = withSec.querySelector(".table-wrap");
      var cards = "";
      result.with_volume.forEach(function (r) {
        cards += '<div class="trade-card"><div class="trade-head">' +
          '<span class="trade-sym">' + escHtml(r.symbol) + "</span>" +
          '<span class="badge badge-green">O=L + VOL</span>' +
          '<span class="badge badge-green">NSE 200</span></div>' +
          '<div class="trade-body"><div class="kv">' +
          "<dt>Entry (Open)</dt><dd>" + inr(r.open) + "</dd>" +
          "<dt>Low</dt><dd>" + inr(r.low) + " (O-L: " + inr(r.ol_diff) + ")</dd>" +
          "<dt>LTP</dt><dd>" + inr(r.ltp) + "</dd>" +
          "<dt>Volume</dt><dd>" + r.vol_ratio.toFixed(2) + "× 20d avg (" + r.volume.toLocaleString("en-IN") + " / " + (r.avg_vol_20d || 0).toLocaleString("en-IN") + ")</dd>" +
          "<dt>Stop Loss</dt><dd>" + inr(r.sl_price) + " (" + CFG.SL_PCT + "% below open)</dd>" +
          "<dt>Shares</dt><dd>" + r.shares + " (" + inr(r.invested, 0) + " invested)</dd>" +
          '<dt>P&L</dt><dd class="' + (r.pnl >= 0 ? "ok" : "bad") + '">' + inr(r.pnl, 0) + " (" + pctStr(r.pnl_pct) + ")</dd>" +
          "<dt>Gap</dt><dd>" + pctStr(r.gap_pct) + "</dd>" +
          "<dt>Day High</dt><dd>" + pctStr(r.ext_pct) + "</dd>" +
          "<dt>Exit</dt><dd>1:00 PM</dd>" +
          "</div>" +
          '<div class="trade-action">' + takeTradeFormHtml(r, "📝 Take This Trade on Paper") + "</div></div></div>";
      });
      if (anchor && cards) anchor.insertAdjacentHTML("afterend", cards);
    }

    if (withoutSec) {
      var h2m2 = withoutSec.querySelector("h2 .muted");
      if (h2m2) h2m2.textContent = "(" + result.without_volume.length + " watch list)";
      var hint = withoutSec.querySelector("p.hint");
      if (hint) hint.innerHTML = "Confirmed Open=Low but volume below " + CFG.MIN_VOL_MULT +
        "× so far (session " + result.session_pct + "% complete). Estimated full-day volume shown — 🔥 may qualify by 1 PM exit.";
      var tb2 = withoutSec.querySelector("tbody");
      if (tb2) {
        var html2 = "";
        result.without_volume.forEach(function (r, i) {
          var watch = r.est_full_vol_ratio >= CFG.MIN_VOL_MULT ? "🔥" : (r.est_full_vol_ratio >= 1.0 ? "⚡" : "");
          html2 += "<tr><td>" + (i + 1) + "</td>" +
            "<td><strong>" + escHtml(r.symbol) + "</strong></td>" +
            "<td>" + inr(r.open) + "</td>" +
            "<td>" + inr(r.low) + "</td>" +
            "<td>" + inr(r.ol_diff) + "</td>" +
            "<td>" + inr(r.ltp) + "</td>" +
            "<td>" + r.vol_ratio.toFixed(2) + "×</td>" +
            "<td>" + r.est_full_vol_ratio.toFixed(1) + "×</td>" +
            '<td class="' + (r.pnl_pct >= 0 ? "ok" : "bad") + '">' + pctStr(r.pnl_pct) + "</td>" +
            "<td>✅</td>" +
            "<td>" + watch + "</td></tr>";
        });
        if (!html2) html2 = '<tr><td colspan="11" class="muted" style="text-align:center;padding:1.2rem">No watch-list stocks right now.</td></tr>';
        tb2.innerHTML = html2;
      }
    }

    wireTakeTrades(scope);
  }

  // -- O=H panel (built entirely from a scan result)
  function renderOpenHighPage(result) {
    var panel = document.getElementById("panel-oh");
    if (!panel) return;
    var total = result.with_volume.length + result.without_volume.length;
    setH1Count("(" + total + " from NSE 200)");
    var lr = document.getElementById("ohLastRefresh");
    if (lr) lr.textContent = "Last refresh: " + result.scanned_at + " (live browser scan — no server needed)";

    function tableRows(rows, withVol) {
      var html = "";
      rows.forEach(function (r, i) {
        html += "<tr><td>" + (i + 1) + "</td>" +
          "<td><strong>" + escHtml(r.symbol) + "</strong></td>" +
          "<td>" + inr(r.open) + "</td>" +
          "<td>" + inr(r.high) + "</td>" +
          "<td>" + inr(r.ol_diff) + "</td>" +
          "<td>" + inr(r.ltp) + "</td>" +
          "<td>" + r.vol_ratio.toFixed(2) + "×</td>" +
          (withVol ? "<td>" + inr(r.sl_price) + "</td>" +
            '<td class="' + (r.pnl >= 0 ? "ok" : "bad") + '">' + pctStr(r.pnl_pct) + "</td>" +
            "<td>" + pctStr(r.ext_pct) + "</td>" :
            "<td>" + r.est_full_vol_ratio.toFixed(1) + "×</td>" +
            '<td class="' + (r.pnl_pct >= 0 ? "ok" : "bad") + '">' + pctStr(r.pnl_pct) + "</td>") +
          "</tr>";
      });
      return html || '<tr><td colspan="' + (withVol ? 11 : 10) + '" class="muted" style="text-align:center;padding:1.2rem">No signals right now (' + result.universe_count + ' scanned).</td></tr>';
    }

    panel.innerHTML =
      '<p class="hint">Bearish mirror of Open=Low: the stock opened at the day high and traded below all session (H−O ≤ ₹' + CFG.OL_DIFF_MAX + ') · short-side watch · SL ' + CFG.SL_PCT + '% above open.</p>' +
      '<p class="muted" id="ohLastRefresh" style="font-size:0.82rem">Last refresh: ' + escHtml(result.scanned_at) + ' (live browser scan)</p>' +
      '<section><h2 style="color:var(--red,#e5534b);border-bottom-color:rgba(229,83,75,0.3)">🔻 O=H With Volume <span class="muted">(' + result.with_volume.length + ' tradeable shorts)</span></h2>' +
      '<div class="table-wrap"><table><thead><tr><th>#</th><th>Symbol</th><th>Open</th><th>High</th><th>H−O</th><th>LTP</th><th>Vol×</th><th>SL</th><th>P&L%</th><th>Fall%</th></tr></thead><tbody>' +
      tableRows(result.with_volume, true) + "</tbody></table></div></section>" +
      '<section style="margin-top:2.5rem"><h2 style="color:#d29922;border-bottom-color:rgba(210,153,34,0.3)">⚠️ O=H Without Volume <span class="muted">(' + result.without_volume.length + ' watch list)</span></h2>' +
      '<p class="hint">Session ' + result.session_pct + '% complete — 🔥 rows may reach ' + CFG.MIN_VOL_MULT + '× volume.</p>' +
      '<div class="table-wrap"><table><thead><tr><th>#</th><th>Symbol</th><th>Open</th><th>High</th><th>H−O</th><th>LTP</th><th>Cur Vol×</th><th>Est Full×</th><th>P&L%</th></tr></thead><tbody>' +
      tableRows(result.without_volume, false) + "</tbody></table></div></section>";
  }

  // -- BB Trap panel
  function renderBBTrapPage(result) {
    var panel = document.getElementById("panel-bb");
    if (!panel) return;
    setH1Count("(" + (result.shorts.length + result.longs.length) + " setups)");

    function bbTable(rows) {
      var html = "";
      rows.forEach(function (r, i) {
        html += "<tr><td>" + (i + 1) + "</td>" +
          "<td><strong>" + escHtml(r.symbol) + "</strong></td>" +
          "<td>" + inr(r.entry_price) + "</td>" +
          "<td>" + inr(r.sl_price) + "</td>" +
          "<td>" + inr(r.target_price) + "</td>" +
          "<td>" + r.rr.toFixed(1) + "×</td>" +
          "<td>" + r.wick_pct + "%</td>" +
          "<td>" + r.vol_multiple.toFixed(1) + "×</td>" +
          "<td>" + (r.rsi != null ? r.rsi + (r.rsi_pass ? " ✓" : "") : "—") + "</td>" +
          "<td>" + inr(r.primary_range) + "</td>" +
          "<td>" + escHtml(r.alert_date) + "</td>" +
          "<td><strong>" + r.score.toFixed(1) + "</strong></td></tr>";
      });
      return html;
    }

    var emptyNote = '<p class="muted" style="padding:1rem 0">No BB Trap v2 signals right now. This is normal — shorts average ~4 per month.</p>';
    panel.innerHTML =
      '<p class="hint">Positional setup, daily timeframe · Primary candle fully outside BB(20, 2σ) → alert candle rejection wick ≥ 50% of range → volume ≥ ' + CFG.BB_MIN_VOL_MULT + '× · price ≥ ₹' + CFG.PRICE_MIN + '.</p>' +
      '<p class="muted" id="bbLastRefresh" style="font-size:0.82rem">Last refresh: ' + escHtml(result.scanned_at) + ' (live browser scan)</p>' +
      '<section><h2 style="color:var(--red,#e5534b);border-bottom-color:rgba(229,83,75,0.3)">🔻 SHORT Setups <span class="muted">(' + result.shorts.length + ', PF 2.10 backtested)</span></h2>' +
      (result.shorts.length ? '<div class="table-wrap"><table><thead><tr><th>#</th><th>Symbol</th><th>Entry</th><th>SL</th><th>Target</th><th>R:R</th><th>Wick</th><th>Vol×</th><th>RSI</th><th>P.Range</th><th>Alert Date</th><th>Score</th></tr></thead><tbody>' + bbTable(result.shorts) + "</tbody></table></div>" : emptyNote) + "</section>" +
      '<section style="margin-top:2.5rem"><h2 style="color:var(--green,#3fb950);border-bottom-color:rgba(63,185,80,0.3)">🔺 LONG Setups <span class="muted">(' + result.longs.length + ', PF 1.06 — marginal)</span></h2>' +
      (result.longs.length ? '<div class="table-wrap"><table><thead><tr><th>#</th><th>Symbol</th><th>Entry</th><th>SL</th><th>Target</th><th>R:R</th><th>Wick</th><th>Vol×</th><th>RSI</th><th>P.Range</th><th>Alert Date</th><th>Score</th></tr></thead><tbody>' + bbTable(result.longs) + "</tbody></table></div>" : emptyNote) + "</section>";
  }

  // -------------------------------------------------------------------------
  // Strategy-page result object (same shape as data/scanners/*-latest.json)
  // -------------------------------------------------------------------------
  function buildScannerJSON(result) {
    var headers = ["#", "Symbol", "Open", "Low", "O-L", "LTP", "Vol×", "Est Full×", "SL", "Shares", "P&L%", "Gap%", "Status"];
    var rows = [];
    var all = result.with_volume.concat(result.without_volume);
    all.forEach(function (r, i) {
      rows.push([
        i + 1, r.symbol, inr(r.open), inr(r.low), inr(r.ol_diff), inr(r.ltp),
        r.vol_ratio.toFixed(2) + "×", r.est_full_vol_ratio.toFixed(1) + "×",
        inr(r.sl_price), r.shares, pctStr(r.pnl_pct), pctStr(r.gap_pct),
        result.with_volume.indexOf(r) >= 0 ? "PASS — tradeable" : "WATCH — volume pending"
      ]);
    });
    var lines = [
      "Open=Low intraday scan — ran in your browser (Yahoo Finance via CORS proxies)",
      "Universe scanned:      " + result.universe_count + " NSE-200 symbols",
      "O=L signals found:     " + all.length,
      "  With volume (>=" + CFG.MIN_VOL_MULT + "x):  " + result.with_volume.length,
      "  Watch list:           " + result.without_volume.length,
      "Session progress:      " + result.session_pct + "%",
      "Scanned at:            " + result.scanned_at,
      "",
      "Rules: O=L diff <= " + CFG.OL_DIFF_MAX + " | Vol >= " + CFG.MIN_VOL_MULT +
        "x 20d avg | Price >= " + CFG.PRICE_MIN + " | SL " + CFG.SL_PCT + "% | Exit 1 PM"
    ];
    if (result.approx_count) {
      lines.push("Note: " + result.approx_count + " signal(s) used 5m-approx open (1m refine unavailable).");
    }
    return {
      strategy: "open-low-intraday",
      scanned_at: result.scanned_at,
      status: "ok",
      signals: result.with_volume.length,
      summary: {
        "Universe": result.universe_count,
        "O=L found": all.length,
        "With volume": result.with_volume.length,
        "Watch list": result.without_volume.length,
        "Data source": "Yahoo Finance (browser-direct)"
      },
      table: { headers: headers, rows: rows },
      text_report: lines.join("\n")
    };
  }

  // -------------------------------------------------------------------------
  // Last-successful-scan caches per setup (localStorage)
  // -------------------------------------------------------------------------
  var CACHE_KEYS = { ol: "mahi_olscan_cache_v1", oh: "mahi_ohscan_cache_v1", bb: "mahi_bb_cache_v1" };

  function saveCacheFor(kind, result) {
    try { localStorage.setItem(CACHE_KEYS[kind] || CACHE_KEYS.ol, JSON.stringify({ savedAt: Date.now(), result: result })); } catch (e) {}
  }
  function loadCacheFor(kind, maxAgeMs) {
    try {
      var c = JSON.parse(localStorage.getItem(CACHE_KEYS[kind] || CACHE_KEYS.ol) || "null");
      if (c && c.result && (!maxAgeMs || Date.now() - c.savedAt <= maxAgeMs)) return c;
    } catch (e) {}
    return null;
  }
  function saveCache(result) { saveCacheFor("ol", result); }
  function loadCache(maxAgeMs) { return loadCacheFor("ol", maxAgeMs); }

  // -------------------------------------------------------------------------
  // Scheduled-scan fallback for O=L (data/alerts.json from the workflow)
  // -------------------------------------------------------------------------
  function fetchRepoAlerts() {
    return fetch(base + "/data/alerts.json?t=" + Date.now())
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.with_volume || !d.without_volume) return null;
        var rows = d.with_volume.concat(d.without_volume);
        var latest = "";
        rows.forEach(function (r) { if (r.date && String(r.date) > latest) latest = String(r.date); });
        var fetchedAt = String(d.fetched_at || "").replace("T", " ").slice(0, 16);
        var today = istDateStr(null);
        return {
          with_volume: d.with_volume,
          without_volume: d.without_volume,
          scanned_at: (fetchedAt || latest.slice(0, 16)) + " IST",
          universe_count: d.chartink_raw_count || (rows.length ? 200 : 0),
          session_pct: +(sessionPct() * 100).toFixed(1),
          approx_count: 0,
          source: "scheduled",
          is_today: latest.slice(0, 10) === today || fetchedAt.slice(0, 10) === today
        };
      })
      .catch(function () { return null; });
  }

  window.DirectScan = {
    runOpenLow: runOpenLow,
    runOpenHigh: runOpenHigh,
    runBBTrap: runBBTrap,
    renderAlertsPage: renderAlertsPage,
    renderOpenHighPage: renderOpenHighPage,
    renderBBTrapPage: renderBBTrapPage,
    buildScannerJSON: buildScannerJSON,
    saveCache: saveCache,
    loadCache: loadCache,
    saveCacheFor: saveCacheFor,
    loadCacheFor: loadCacheFor,
    fetchRepoAlerts: fetchRepoAlerts
  };
})();
