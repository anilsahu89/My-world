/**
 * Mahi Portal — Live Stocks page.
 *
 * On-demand, same-day NSE 200 quotes fetched entirely in the browser from
 * Yahoo Finance's spark endpoint. GitHub Pages is static hosting, so live
 * requests go through public CORS proxies with a fallback chain and retries.
 * A scheduled GitHub Actions workflow also commits a snapshot to
 * data/live_stocks.json; that snapshot is loaded instantly on page open.
 *
 * Exposed as window.StocksPage.
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

  var BATCH = 20;
  var OL_TOL = 0.003; // open within 0.3% of day low => O≈L flag
  var state = { stocks: [], snapshot: [], sort: "pct-desc", filter: "", olOnly: false, fetchedAt: null, live: false };
  var fetching = false;

  // CORS proxy chain for Yahoo (tried in order per batch, with retries).
  // r.jina.ai echoes CORS headers and needs no key — but its payload shape is
  // unstable: clean JSON, {data:{content:"<json>"}}, or markdown with the JSON
  // embedded ("Title:/URL Source:"). The unwrap + text-parse below digests all.
  var PROXIES = [
    {
      wrap: function (u) { return "https://r.jina.ai/" + u; },
      unwrap: function (d) {
        if (d && d.data && d.data.content != null) {
          var c = d.data.content;
          if (typeof c === "string") {
            try { return JSON.parse(c); } catch (e) {
              var i = c.indexOf("{");
              if (i > -1) {
                try { return JSON.parse(c.slice(i, c.lastIndexOf("}") + 1)); } catch (e2) { /* fall through */ }
              }
            }
          }
        }
        return d;
      }
    },
    {
      wrap: function (u) { return "https://api.allorigins.win/raw?url=" + encodeURIComponent(u); },
      unwrap: function (d) { return d; }
    },
    {
      wrap: function (u) { return "https://api.codetabs.com/v1/proxy?quest=" + encodeURIComponent(u); },
      unwrap: function (d) { return d; }
    },
    {
      wrap: function (u) { return u; }, // direct (works if Yahoo ever sends CORS headers)
      unwrap: function (d) { return d; }
    }
  ];

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
        .then(function (text) {
          try { return JSON.parse(text); } catch (e) {}
          var j = text.indexOf("{");
          if (j > -1) {
            try { return JSON.parse(text.slice(j, text.lastIndexOf("}") + 1)); } catch (e2) {}
          }
          throw new Error("bad payload");
        })
        .then(resolve, function (e) {
          clearTimeout(timer);
          reject(e);
        });
    });
  }

  function url(path) { return base + path; }

  function escHtml(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function el(id) { return document.getElementById(id); }

  // -------------------------------------------------------------------------
  // IST helpers (Yahoo returns epoch seconds in IST exchange tz)
  // -------------------------------------------------------------------------
  function istNow() {
    var now = new Date();
    return new Date(now.getTime() + (330 + now.getTimezoneOffset()) * 60000);
  }
  function istDateStr(ts) {
    var d = ts ? new Date(ts * 1000) : istNow();
    var ist = new Date(d.getTime() + (330 + d.getTimezoneOffset()) * 60000);
    return ist.toISOString().slice(0, 10);
  }
  function fmtTime(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    var ist = new Date(d.getTime() + (330 + d.getTimezoneOffset()) * 60000);
    return ist.toISOString().slice(0, 16).replace("T", " ") + " IST";
  }
  function marketOpen() {
    var d = istNow();
    var day = d.getDay();
    if (day === 0 || day === 6) return false;
    var mins = d.getHours() * 60 + d.getMinutes();
    return mins >= 555 && mins <= 930; // 9:15 – 15:30
  }
  function updateMarketPill() {
    var p = el("marketStatus");
    if (!p) return;
    if (marketOpen()) { p.textContent = "● MARKET OPEN"; p.className = "meta-pill ok"; }
    else { p.textContent = "● MARKET CLOSED"; p.className = "meta-pill"; }
  }

  // -------------------------------------------------------------------------
  // Data plumbing
  // -------------------------------------------------------------------------
  function loadUniverse() {
    return fetch(url("/data/nse200_symbols.csv"))
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

  function loadSnapshot() {
    fetch(url("/data/live_stocks.json?t=" + Date.now()))
      .then(function (r) { if (r.ok) return r.json(); throw new Error("no snapshot"); })
      .then(function (data) {
        if (fetching) return; // a live fetch is already running
        if (data && data.stocks && data.stocks.length) {
          state.snapshot = data.stocks;
          state.stocks = data.stocks;
          state.fetchedAt = data.fetched_at || "";
          state.live = false;
          render();
          setStatus("Showing committed snapshot" + (data.fetched_at ? " from " + escHtml(String(data.fetched_at).slice(0, 16)) : "") + " — click ⟳ for live data.");
        }
      })
      .catch(function () { /* no snapshot yet — fine */ });
  }

  function fetchChunk(symbols) {
    // 5m interval keeps payloads small (the 1m series is ~5x bigger and makes
    // the free r.jina.ai proxy dramatically slower). The committed snapshot
    // from the cloud job still uses 1m for a sharper open approximation.
    var yurl = "https://query1.finance.yahoo.com/v7/finance/spark?symbols=" +
      symbols.map(encodeURIComponent).join(",") + "&range=1d&interval=5m";

    function attempt(proxyIdx, tryNum, delay) {
      return new Promise(function (resolve) {
        setTimeout(function () {
          fetchWithTimeout(PROXIES[proxyIdx].wrap(yurl), 12000)
            .then(function (data) {
              var rows = PROXIES[proxyIdx].unwrap(data);
              rows = (rows && rows.spark && rows.spark.result) || null;
              if (!rows || !rows.length) throw new Error("empty spark result");
              resolve(rows);
            })
            .catch(function () {
              // next try of same proxy, then next proxy
              if (tryNum < 2) resolve(attempt(proxyIdx, tryNum + 1, 500));
              else if (proxyIdx + 1 < PROXIES.length) resolve(attempt(proxyIdx + 1, 1, 200));
              else resolve(null);
            });
        }, delay);
      });
    }
    return attempt(0, 1, 0);
  }

  // Retry-pass variant: one quick attempt on the primary proxy only.
  function fetchChunkLight(symbols) {
    var yurl = "https://query1.finance.yahoo.com/v7/finance/spark?symbols=" +
      symbols.map(encodeURIComponent).join(",") + "&range=1d&interval=5m";
    return fetchWithTimeout(PROXIES[0].wrap(yurl), 10000)
      .then(function (data) {
        var rows = PROXIES[0].unwrap(data);
        rows = (rows && rows.spark && rows.spark.result) || null;
        return rows && rows.length ? rows : null;
      })
      .catch(function () { return null; });
  }

  function parseRows(rows, out) {
    (rows || []).forEach(function (r) {
      var resp = (r.response || [])[0];
      if (!resp || !resp.meta) return;
      var m = resp.meta;
      var ltp = m.regularMarketPrice;
      var prev = m.chartPreviousClose != null ? m.chartPreviousClose : m.previousClose;
      if (ltp == null || prev == null) return;

      // Approximate open from the first 1-minute close of the day.
      var opens = [];
      var q = resp.indicators && resp.indicators.quote && resp.indicators.quote[0];
      if (q && q.close) {
        for (var i = 0; i < q.close.length; i++) {
          if (q.close[i] != null) { opens.push(q.close[i]); break; }
        }
      }
      var open = opens.length ? opens[0] : null;

      var high = m.regularMarketDayHigh != null ? m.regularMarketDayHigh : ltp;
      var low = m.regularMarketDayLow != null ? m.regularMarketDayLow : ltp;
      var ol = open != null && low > 0 && Math.abs(open - low) / low <= OL_TOL;

      out.push({
        symbol: (m.symbol || r.symbol || "").replace(/\.NS$/, ""),
        name: m.shortName || m.longName || "",
        ltp: ltp,
        prev_close: prev,
        chg: +(ltp - prev).toFixed(2),
        pct: prev ? +(((ltp - prev) / prev) * 100).toFixed(2) : 0,
        open: open,
        high: high,
        low: low,
        volume: m.regularMarketVolume != null ? m.regularMarketVolume : 0,
        ol: ol,
        market_time: m.regularMarketTime || 0
      });
    });
  }

  // Overlay live rows on top of the committed snapshot so a partial fetch
  // still shows the full universe (snapshot rows just look older).
  function mergeWithSnapshot(liveRows) {
    if (!state.snapshot.length) return liveRows.slice();
    var map = {};
    state.snapshot.forEach(function (r) { map[r.symbol] = r; });
    liveRows.forEach(function (r) { map[r.symbol] = r; });
    return Object.keys(map).map(function (k) { return map[k]; });
  }

  // -------------------------------------------------------------------------
  // Main on-demand fetch
  // -------------------------------------------------------------------------
  function fetchLive() {
    if (fetching) return;
    fetching = true;
    var btn = el("fetchLiveBtn");
    btn.disabled = true;
    btn.textContent = "⏳ Fetching…";
    showProgress(0, "Loading universe…");

    loadUniverse()
      .then(function (syms) {
        var chunks = [];
        for (var i = 0; i < syms.length; i += BATCH) chunks.push(syms.slice(i, i + BATCH).map(function (s) { return s + ".NS"; }));

        // Small worker pool: 3 batches in flight. The primary proxy (r.jina.ai)
        // handles this fine and it turns a ~60s sequential run into ~10s.
        var collected = [];
        var done = 0;
        var missed = [];
        var poolIdx = 0;
        var workers = [];

        function nextChunk() {
          if (poolIdx >= chunks.length) return Promise.resolve();
          var idx = poolIdx++;
          return fetchChunk(chunks[idx]).then(function (rows) {
            if (rows) {
              parseRows(rows, collected);
            } else {
              missed.push(idx); // retried in a light second pass below
            }
            done++;
            showProgress(Math.round((done / chunks.length) * 100),
              "Batch " + done + "/" + chunks.length + " — " + collected.length + " live quotes");
            state.stocks = mergeWithSnapshot(collected);
            state.live = true;
            state.fetchedAt = new Date().toISOString();
            render();
            return nextChunk();
          });
        }
        for (var w = 0; w < 3; w++) workers.push(nextChunk());
        return Promise.all(workers)
          .then(function () {
            // Light second pass for batches every proxy dropped: one quick
            // attempt on the primary proxy only, so this can't drag on.
            var retryChain = Promise.resolve();
            missed.forEach(function (idx) {
              retryChain = retryChain.then(function () {
                return fetchChunkLight(chunks[idx]).then(function (rows) {
                  if (rows) {
                    parseRows(rows, collected);
                    state.stocks = mergeWithSnapshot(collected);
                    render();
                  }
                });
              });
            });
            return retryChain;
          })
          .then(function () {
            if (!collected.length) throw new Error("all CORS proxies unreachable");
            return collected.length;
          });
      })
      .then(function (liveCount) {
        hideProgress();
        var total = state.stocks.length;
        if (liveCount >= total || !state.snapshot.length) {
          setStatus(total + " quotes fetched live from Yahoo Finance at " +
            escHtml(fmtTime(Date.now() / 1000)) + ". Same-day data: " +
            (isSameDay() ? "yes ✓" : "no — market may have been closed"));
        } else {
          setStatus(liveCount + " live quotes merged onto the " + state.snapshot.length +
            "-stock snapshot (dimmed rows = older data). Click fetch again to top up, or use ☁ Server Refresh.");
        }
      })
      .catch(function (err) {
        hideProgress();
        setStatus("Live fetch failed (" + escHtml(err.message) + "). Public CORS proxies are rate-limited — try again in a minute, or use ☁ Server Refresh.", true);
      })
      .then(function () {
        fetching = false;
        btn.disabled = false;
        btn.textContent = "⟳ Fetch Live Data Now";
      });
  }

  function isSameDay() {
    if (!state.stocks.length) return false;
    var todayIst = istDateStr(null);
    return state.stocks.some(function (s) { return istDateStr(s.market_time) === todayIst; });
  }

  // -------------------------------------------------------------------------
  // Server-side refresh via GitHub Actions (token from ⚙️ Settings)
  // -------------------------------------------------------------------------
  function serverRefresh(btn) {
    var token = "", repo = "";
    try {
      // Same normalization as app.js — stray whitespace in a token makes the
      // Authorization header invalid and fetch fails with "Failed to fetch".
      token = (localStorage.getItem("mahi_gh_token") || "").replace(/\s+/g, "");
      repo = (localStorage.getItem("mahi_gh_repo") || "")
        .replace(/\s+/g, "")
        .replace(/^https?:\/\/(www\.)?github\.com\//i, "")
        .replace(/\.git$/i, "")
        .replace(/\/+$/, "");
    } catch (e) {}
    if (!token || !/^[\w.-]+\/[\w.-]+$/.test(repo)) {
      setStatus("Server Refresh needs a GitHub token + repo (owner/repo, e.g. anilsahu89/My-world). Click ⚙️ Settings to configure.", true);
      if (window.openSettings) openSettings();
      return;
    }
    btn.disabled = true;
    btn.textContent = "⏳ Running…";
    setStatus("Triggered cloud refresh job. It fetches quotes server-side and commits a fresh snapshot (~1 min)…");

    fetch("https://api.github.com/repos/" + repo + "/actions/workflows/refresh-stocks.yml/dispatches", {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json"
      },
      body: JSON.stringify({ ref: "main" })
    })
      .then(function (r) {
        if (!r.ok && r.status !== 409) throw new Error("dispatch HTTP " + r.status);
        // Poll workflow completion
        return new Promise(function (resolve, reject) {
          var tries = 0;
          (function poll() {
            tries++;
            fetch("https://api.github.com/repos/" + repo + "/actions/workflows/refresh-stocks.yml/runs?per_page=1", {
              headers: { "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json" }
            })
              .then(function (r) { return r.json(); })
              .then(function (data) {
                var run = data.workflow_runs && data.workflow_runs[0];
                if (run && run.status === "completed") {
                  if (run.conclusion === "success") resolve();
                  else reject(new Error("workflow " + run.conclusion));
                } else if (tries > 24) reject(new Error("timed out waiting"));
                else setTimeout(poll, 5000);
              })
              .catch(function () { setTimeout(poll, 5000); });
          })();
        });
      })
      .then(function () {
        setStatus("Cloud refresh done ✓ reloading snapshot…");
        return new Promise(function (res) { setTimeout(res, 15000); }); // let Pages CDN pick up the commit
      })
      .then(function () { loadSnapshot(); })
      .catch(function (err) {
        setStatus("Server refresh failed: " + escHtml(err.message), true);
      })
      .then(function () {
        btn.disabled = false;
        btn.textContent = "☁ Server Refresh";
      });
  }

  // -------------------------------------------------------------------------
  // Rendering
  // -------------------------------------------------------------------------
  function setStatus(msg, isErr) {
    var t = el("progressText");
    var w = el("progressWrap");
    if (!t || !w) return;
    w.style.display = "flex";
    el("progressFill").style.width = "100%";
    t.style.color = isErr ? "var(--red)" : "";
    t.textContent = msg;
  }

  function showProgress(pct, text) {
    el("progressWrap").style.display = "flex";
    el("progressFill").style.width = pct + "%";
    var t = el("progressText");
    t.style.color = "";
    t.textContent = text;
  }

  function hideProgress() {
    el("progressWrap").style.display = "none";
  }

  function fmtVol(v) {
    if (!v) return "—";
    if (v >= 1e7) return (v / 1e7).toFixed(2) + " Cr";
    if (v >= 1e5) return (v / 1e5).toFixed(2) + " L";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + " K";
    return String(v);
  }

  function visibleStocks() {
    var f = state.filter.toLowerCase();
    var list = state.stocks.filter(function (s) {
      if (state.olOnly && !s.ol) return false;
      if (f && s.symbol.toLowerCase().indexOf(f) === -1 &&
          (s.name || "").toLowerCase().indexOf(f) === -1) return false;
      return true;
    });
    var key = state.sort.replace(/-(asc|desc)$/, "");
    var dir = state.sort.endsWith("-asc") ? 1 : -1;
    if (key === "sym") key = "symbol";
    list.sort(function (a, b) {
      var av = a[key], bv = b[key];
      if (key === "symbol" || key === "name") return String(av).localeCompare(String(bv)) * dir;
      if (key === "ol") return (bv ? 1 : 0) - (av ? 1 : 0);
      if (av == null) return 1;
      if (bv == null) return -1;
      return (av - bv) * dir;
    });
    return list;
  }

  function renderStats() {
    var box = el("stocksStats");
    var all = state.stocks;
    if (!all.length) { box.innerHTML = ""; return; }
    var up = 0, down = 0, flat = 0, ol = 0;
    all.forEach(function (s) {
      if (s.pct > 0) up++; else if (s.pct < 0) down++; else flat++;
      if (s.ol) ol++;
    });
    var best = null, worst = null;
    all.forEach(function (s) {
      if (!best || s.pct > best.pct) best = s;
      if (!worst || s.pct < worst.pct) worst = s;
    });
    box.innerHTML =
      '<div class="stat-tile"><div class="v">' + all.length + '</div><div class="k">quotes</div></div>' +
      '<div class="stat-tile"><div class="v pct-pos">' + up + '</div><div class="k">advancing</div></div>' +
      '<div class="stat-tile"><div class="v pct-neg">' + down + '</div><div class="k">declining</div></div>' +
      (best ? '<div class="stat-tile"><div class="v pct-pos">+' + best.pct + '%</div><div class="k">best: ' + escHtml(best.symbol) + '</div></div>' : "") +
      (worst ? '<div class="stat-tile"><div class="v pct-neg">' + worst.pct + '%</div><div class="k">worst: ' + escHtml(worst.symbol) + '</div></div>' : "") +
      '<div class="stat-tile"><div class="v" style="color:var(--green)">' + ol + '</div><div class="k">Open≈Low</div></div>';
  }

  function render() {
    updateMarketPill();
    renderStats();
    var tbody = el("stocksBody");
    var list = visibleStocks();
    if (!list.length) {
      tbody.innerHTML = '<tr><td colspan="11" class="muted" style="text-align:center;padding:1.5rem">' +
        (state.stocks.length ? "No rows match the filter." : "No data yet — click ⟳ Fetch Live Data Now.") +
        "</td></tr>";
      return;
    }
    var newestTs = 0;
    list.forEach(function (s) { if (s.market_time > newestTs) newestTs = s.market_time; });
    var html = "";
    list.forEach(function (s) {
      // Dim rows lagging the freshest quote by >2h (snapshot rows under a
      // partial live fetch). On weekends everything shares one timestamp,
      // so nothing dims.
      var stale = newestTs && s.market_time && (newestTs - s.market_time) > 7200;
      html += '<tr class="' + (stale ? "stale-row" : "") + '">' +
        "<td><strong>" + escHtml(s.symbol) + "</strong></td>" +
        "<td class='muted'>" + escHtml(s.name) + "</td>" +
        "<td class='num'>" + (s.ltp != null ? s.ltp.toFixed(2) : "—") + "</td>" +
        "<td class='num " + (s.chg > 0 ? "pct-pos" : s.chg < 0 ? "pct-neg" : "") + "'>" + (s.chg > 0 ? "+" : "") + s.chg.toFixed(2) + "</td>" +
        "<td class='num " + (s.pct > 0 ? "pct-pos" : s.pct < 0 ? "pct-neg" : "") + "'>" + (s.pct > 0 ? "+" : "") + s.pct.toFixed(2) + "%</td>" +
        "<td class='num'>" + (s.open != null ? s.open.toFixed(2) : "—") + "</td>" +
        "<td class='num'>" + (s.high != null ? s.high.toFixed(2) : "—") + "</td>" +
        "<td class='num'>" + (s.low != null ? s.low.toFixed(2) : "—") + "</td>" +
        "<td class='num'>" + (s.prev_close != null ? s.prev_close.toFixed(2) : "—") + "</td>" +
        "<td class='num'>" + fmtVol(s.volume) + "</td>" +
        "<td>" + (s.ol ? '<span class="ol-pill">O≈L</span>' : "") + "</td>" +
        "</tr>";
    });
    tbody.innerHTML = html;
  }

  // -------------------------------------------------------------------------
  // Init + events
  // -------------------------------------------------------------------------
  function init() {
    if (!el("stocksTable")) return; // not on the stocks page

    var search = el("stockSearch");
    var sortSel = el("sortSel");
    var olOnly = el("olOnly");

    search.addEventListener("input", function () {
      state.filter = search.value.trim();
      render();
    });
    sortSel.addEventListener("change", function () {
      state.sort = sortSel.value;
      render();
    });
    olOnly.addEventListener("change", function () {
      state.olOnly = olOnly.checked;
      render();
    });

    // Click-to-sort on column headers
    document.querySelectorAll("#stocksTable th[data-key]").forEach(function (th) {
      th.addEventListener("click", function () {
        var key = th.getAttribute("data-key");
        var current = state.sort;
        var next = key + (current === key + "-asc" ? "-desc" : "-asc");
        if (key === "pct") next = current === "pct-asc" ? "pct-desc" : "pct-asc";
        state.sort = next;
        sortSel.value = ["pct-desc", "pct-asc", "ltp-desc", "ltp-asc", "vol-desc", "sym-asc"].indexOf(next) >= 0 ? next : "pct-desc";
        document.querySelectorAll("#stocksTable th").forEach(function (t) { t.classList.remove("sorted-asc", "sorted-desc"); });
        th.classList.add(next.endsWith("-asc") ? "sorted-asc" : "sorted-desc");
        render();
      });
    });

    updateMarketPill();
    loadSnapshot();
  }

  window.StocksPage = { fetchLive: fetchLive, serverRefresh: serverRefresh, render: render };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
