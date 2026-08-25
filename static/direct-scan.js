/**
 * Mahi Portal — Direct browser scan engine.
 *
 * Runs the Open=Low NSE-200 scan entirely in the browser using Yahoo Finance
 * public endpoints through the same CORS-proxy chain as stocks.js. No GitHub
 * token, no Actions dispatch — clicking Refresh just works on the static site.
 *
 * Pipeline:
 *   1. spark 1d/5m (20 symbols/batch) → rough O≈L candidates
 *   2. spark 1d/1m (batched)           → sharper open, strict ₹0.10 rule
 *   3. chart 2mo/1d per survivor       → 20-day average volume → Vol×
 *
 * Exposed as window.DirectScan.
 */
(function () {
  "use strict";

  // Base path (GitHub Pages subpath aware) — same detection as app.js.
  var base = "";
  var baseEl = document.querySelector("base");
  if (baseEl) base = (baseEl.getAttribute("href") || "").replace(/\/+$/, "");
  if (!base && location.hostname.endsWith(".github.io")) {
    var seg = location.pathname.replace(/^\/+/, "").split("/")[0];
    if (seg) base = "/" + seg;
  }

  // Scan config — mirrors refresh-alerts scanner defaults.
  var CFG = {
    OL_DIFF_MAX: 0.10,   // ₹ — strict O=L rule (after 1m refinement)
    OL_ROUGH_TOL: 0.003, // 0.3% — pre-filter tolerance for the 5m open approx
    MIN_VOL_MULT: 1.5,
    PRICE_MIN: 50,
    SL_PCT: 0.5,
    INVESTMENT: 10000,
    VOL_LOOKBACK: 20,
    BATCH: 30,   // 30 symbols/request → 7 quote calls for NSE-200 (fewer proxy hits)
    WORKERS: 2   // gentle on r.jina.ai — 3 concurrent bursts trigger its throttling
  };

  // CORS proxy chain for Yahoo (same idea as stocks.js, retuned):
  // r.jina.ai is primary; direct Yahoo fails instantly in browsers (no CORS
  // headers), which is cheaper than waiting on the occasionally-dead public
  // proxies — so it comes second, and the slow ones go last.
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
      wrap: function (u) { return u; }, // direct (browsers fail this fast on CORS; harmless)
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

  // r.jina.ai's response shape is unstable: sometimes clean JSON, sometimes
  // {data:{content:"<json string>"}}, sometimes markdown ("Title:/URL Source:")
  // with the JSON embedded after it. This digests all three.
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

  // Fetch a Yahoo URL through the proxy chain. Proxies upstream of Yahoo get
  // briefly throttled (surface as 404/429 for a minute or so), so retries are
  // patient: 3 tries per proxy with growing backoff, then the next proxy.
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
  // IST helpers (Yahoo epoch seconds are exchange-local) — same as stocks.js
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
  // Fraction (0..1) of the 9:15–15:30 IST session elapsed; 1 once closed.
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
  // Universe
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

  // -------------------------------------------------------------------------
  // Stage 1 — spark quotes (batch of 20, same shape as stocks.js)
  // -------------------------------------------------------------------------
  function fetchSparkBatch(symbols) {
    var yurl = "https://query1.finance.yahoo.com/v7/finance/spark?symbols=" +
      symbols.map(encodeURIComponent).join(",") + "&range=1d&interval=5m";
    return fetchYahoo(yurl, 12000).then(function (data) {
      var rows = (data && data.spark && data.spark.result) || null;
      return rows && rows.length ? rows : null;
    });
  }

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
      open: open5m,                      // approx until refined
      high: m.regularMarketDayHigh != null ? m.regularMarketDayHigh : ltp,
      low: m.regularMarketDayLow != null ? m.regularMarketDayLow : ltp,
      volume: m.regularMarketVolume != null ? m.regularMarketVolume : 0,
      market_time: m.regularMarketTime || 0,
      approx: true
    };
  }

  // -------------------------------------------------------------------------
  // Stage 2 — refine candidates with a batched 1m spark call (true-ish open:
  // first 1-minute close, 20 symbols per request — no per-symbol round trips)
  // -------------------------------------------------------------------------
  function fetchSpark1mBatch(symbols) {
    var yurl = "https://query1.finance.yahoo.com/v7/finance/spark?symbols=" +
      symbols.map(encodeURIComponent).join(",") + "&range=1d&interval=1m";
    return fetchYahoo(yurl, 15000).then(function (data) {
      var rows = (data && data.spark && data.spark.result) || null;
      return rows && rows.length ? rows : null;
    });
  }

  function refineCandidates(candidates, onTick) {
    var chunks = [];
    for (var i = 0; i < candidates.length; i += CFG.BATCH) chunks.push(candidates.slice(i, i + CFG.BATCH));
    var idx = 0, workers = [], done = 0, refined = [];

    function nextChunk() {
      if (idx >= chunks.length) return Promise.resolve();
      var my = idx++;
      // 1m spark needs the .NS suffix
      var syms = chunks[my].map(function (q) { return q.symbol + ".NS"; });
      return fetchSpark1mBatch(syms).then(function (rows) {
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
            // else: keep 5m approximation, q.approx stays true
            if (Math.abs(q.open - q.low) <= CFG.OL_DIFF_MAX) refined.push(q);
          });
        } else {
          // whole batch unreachable — keep 5m approximations that already
          // satisfy the strict rule so the scan degrades instead of emptying
          chunks[my].forEach(function (q) {
            if (Math.abs(q.open - q.low) <= CFG.OL_DIFF_MAX) refined.push(q);
          });
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
  // Stage 3 — 20-day average volume from the daily series
  // -------------------------------------------------------------------------
  function avgVol20d(sym, todayStr) {
    var yurl = "https://query1.finance.yahoo.com/v8/finance/chart/" +
      encodeURIComponent(sym + ".NS") + "?range=2mo&interval=1d";
    return fetchYahoo(yurl, 12000).then(function (data) {
      var res = data && data.chart && data.chart.result && data.chart.result[0];
      if (!res || !res.timestamp) return null;
      var q = res.indicators && res.indicators.quote && res.indicators.quote[0];
      if (!q || !q.volume) return null;
      var vols = [];
      for (var i = 0; i < res.timestamp.length; i++) {
        var dstr = istDateStr(res.timestamp[i]);
        if (dstr === todayStr) continue; // exclude today's partial bar
        if (q.volume[i] != null) vols.push(q.volume[i]);
      }
      if (!vols.length) return null;
      var tail = vols.slice(-CFG.VOL_LOOKBACK);
      var sum = 0;
      tail.forEach(function (v) { sum += v; });
      return Math.round(sum / tail.length);
    });
  }

  // -------------------------------------------------------------------------
  // The scan
  // -------------------------------------------------------------------------
  function runOpenLow(onProgress) {
    var progress = typeof onProgress === "function" ? onProgress : function () {};
    var quotes = [];

    return loadUniverse()
      .then(function (syms) {
        var chunks = [];
        for (var i = 0; i < syms.length; i += CFG.BATCH) {
          chunks.push(syms.slice(i, i + CFG.BATCH).map(function (s) { return s + ".NS"; }));
        }
        var done = 0, poolIdx = 0, workers = [];

        function nextChunk() {
          if (poolIdx >= chunks.length) return Promise.resolve();
          var idx = poolIdx++;
          return fetchSparkBatch(chunks[idx]).then(function (rows) {
            (rows || []).forEach(function (r) {
              var q = quoteFromSpark(r);
              if (q) quotes.push(q);
            });
            done++;
            progress(Math.round((done / chunks.length) * 60),
              "Quotes " + done + "/" + chunks.length + " — " + quotes.length + " symbols");
            return nextChunk();
          });
        }
        for (var w = 0; w < CFG.WORKERS; w++) workers.push(nextChunk());
        return Promise.all(workers);
      })
      .then(function () {
        if (!quotes.length) throw new Error("no quotes — CORS proxies unreachable, retry in a minute");

        // Rough O≈L pre-filter with the 5m-approx open.
        var candidates = quotes.filter(function (q) {
          if (q.open == null || !q.low) return false;
          var tol = Math.max(CFG.OL_DIFF_MAX, CFG.OL_ROUGH_TOL * q.low);
          return Math.abs(q.open - q.low) <= tol;
        });
        progress(65, candidates.length + " O≈L candidates — refining…");

        // Batched 1m refine, then the strict ₹0.10 rule.
        return refineCandidates(candidates, function (done, total, found) {
          progress(65 + Math.round((done / Math.max(total, 1)) * 15),
            "Refine " + done + "/" + total + " — " + found + " confirmed");
        });
      })
      .then(function (refined) {
        // 20-day volume for each survivor.
        var todayStr = null;
        refined.forEach(function (q) {
          var s = istDateStr(q.market_time);
          if (!todayStr) todayStr = s;
        });
        var idx = 0, workers3 = [], doneR = 0;
        function nextRef() {
          if (idx >= refined.length) return Promise.resolve();
          var i = idx++;
          return avgVol20d(refined[i].symbol, todayStr).then(function (avg) {
            var q = refined[i];
            q.avg_vol_20d = avg || 0;
            doneR++;
            progress(65 + Math.round((doneR / Math.max(refined.length, 1)) * 30),
              "Volume history " + doneR + "/" + refined.length);
            return nextRef();
          });
        }
        for (var w3 = 0; w3 < CFG.WORKERS; w3++) workers3.push(nextRef());
        return Promise.all(workers3).then(function () { return refined; });
      })
      .then(function (refined) {
        progress(97, "Classifying…");
        var sess = sessionPct();
        var rows = refined.map(function (q) {
          var volRatio = q.avg_vol_20d ? +(q.volume / q.avg_vol_20d).toFixed(2) : 0;
          var estFull = sess > 0 ? +(volRatio / sess).toFixed(1) : volRatio;
          var shares = q.open > 0 ? Math.floor(CFG.INVESTMENT / q.open) : 0;
          return {
            symbol: q.symbol,
            name: q.name,
            open: q.open,
            low: q.low,
            ol_diff: +(Math.abs(q.open - q.low).toFixed(2)),
            ltp: q.ltp,
            high: q.high,
            prev_close: q.prev_close,
            volume: q.volume,
            avg_vol_20d: q.avg_vol_20d,
            vol_ratio: volRatio,
            est_full_vol_ratio: estFull,
            shares: shares,
            invested: +(shares * q.open).toFixed(0),
            sl_price: +(q.open * (1 - CFG.SL_PCT / 100)).toFixed(2),
            pnl: +((q.ltp - q.open) * shares).toFixed(0),
            pnl_pct: q.open ? +(((q.ltp - q.open) / q.open) * 100).toFixed(2) : 0,
            gap_pct: q.prev_close ? +(((q.open - q.prev_close) / q.prev_close) * 100).toFixed(2) : 0,
            day_high_pct: q.open ? +(((q.high - q.open) / q.open) * 100).toFixed(2) : 0,
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
          with_volume: withVolume,
          without_volume: withoutVolume,
          scanned_at: fmtTs(null),
          universe_count: quotes.length,
          session_pct: +(sess * 100).toFixed(1),
          approx_count: rows.filter(function (r) { return r.approx; }).length
        };
      });
  }

  // -------------------------------------------------------------------------
  // Alerts page renderer — updates the baked-in tables in place
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

  function wireTakeTrades() {
    document.querySelectorAll(".take-trade-btn").forEach(function (btn) {
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

  function renderAlertsPage(result) {
    var total = result.with_volume.length + result.without_volume.length;

    // Page title count + last-refresh line
    var h1muted = document.querySelector("h1 .muted");
    if (h1muted) h1muted.textContent = "(" + total + " from NSE 200)";
    var lr = document.getElementById("lastRefresh");
    if (lr) lr.textContent = "Last refresh: " + result.scanned_at +
      (result.source === "scheduled" ? " (scheduled server scan — updates every 15 min in market hours)" : " (live browser scan — no server needed)");

    // Sections
    var withSec = null, withoutSec = null;
    document.querySelectorAll("main section").forEach(function (s) {
      var h = s.querySelector("h2");
      if (!h) return;
      var t = h.textContent || "";
      if (t.indexOf("With Volume") >= 0) withSec = s;
      else if (t.indexOf("Without Volume") >= 0) withoutSec = s;
    });

    // ---- Section 1: with volume ----
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
            "<td>" + pctStr(r.day_high_pct) + "</td>" +
            '<td class="' + (r.pnl >= 0 ? "ok" : "bad") + '">' + (r.pnl >= 0 ? "PROFIT" : "LOSS") + "</td>" +
            "<td>" + takeTradeFormHtml(r) + "</td></tr>";
        });
        if (!html) html = '<tr><td colspan="14" class="muted" style="text-align:center;padding:1.2rem">No O=L + volume signals right now (' + result.universe_count + ' scanned).</td></tr>';
        tb1.innerHTML = html;
      }
      // Trade cards
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
          "<dt>Day High</dt><dd>" + pctStr(r.day_high_pct) + "</dd>" +
          "<dt>Exit</dt><dd>1:00 PM</dd>" +
          "</div>" +
          '<div class="trade-action">' + takeTradeFormHtml(r, "📝 Take This Trade on Paper") + "</div></div></div>";
      });
      if (anchor && cards) anchor.insertAdjacentHTML("afterend", cards);
    }

    // ---- Section 2: without volume ----
    if (withoutSec) {
      var h2m2 = withoutSec.querySelector("h2 .muted");
      if (h2m2) h2m2.textContent = "(" + result.without_volume.length + " watch list)";
      var hint = withoutSec.querySelector("p.hint");
      if (hint) hint.innerHTML = "These stocks have confirmed Open=Low but volume hasn't reached " + CFG.MIN_VOL_MULT +
        "× yet (session " + result.session_pct + "% complete). Estimated full-day volume shown — stocks marked 🔥 may qualify by 1 PM exit.";
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

    wireTakeTrades();
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
  // Last-successful-scan cache (localStorage) — graceful degradation when the
  // public proxies are all down: re-render the most recent good scan.
  // -------------------------------------------------------------------------
  var CACHE_KEY = "mahi_olscan_cache_v1";

  function saveCache(result) {
    try { localStorage.setItem(CACHE_KEY, JSON.stringify({ savedAt: Date.now(), result: result })); } catch (e) {}
  }

  function loadCache(maxAgeMs) {
    try {
      var c = JSON.parse(localStorage.getItem(CACHE_KEY) || "null");
      if (c && c.result && c.result.with_volume && (!maxAgeMs || Date.now() - c.savedAt <= maxAgeMs)) return c;
    } catch (e) {}
    return null;
  }

  // -------------------------------------------------------------------------
  // Scheduled-scan fallback — the GitHub workflow commits data/alerts.json
  // every ~15 min during market hours. Same-origin fetch: no proxies, no
  // rate limits, always works. Used on page load and when the live browser
  // scan can't get through.
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
    renderAlertsPage: renderAlertsPage,
    buildScannerJSON: buildScannerJSON,
    saveCache: saveCache,
    loadCache: loadCache,
    fetchRepoAlerts: fetchRepoAlerts
  };
})();
