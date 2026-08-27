/**
 * Mahi Portal — Kite Screener tab (local bridge to the trading terminal).
 *
 * The alerts page's other tabs scan Yahoo Finance through public CORS
 * proxies. This tab talks to the local trading terminal
 * (127.0.0.1:8000 — FastAPI, CORS open), which screens NSE 200 through
 * Kite Connect: two bulk-quote calls, TRUE exchange opens, no proxies.
 *
 * Terminal absent → friendly panel with start instructions; nothing else
 * on the page is affected.
 *
 * Exposed as window.KiteScan.
 */
(function () {
  "use strict";

  var TERM = "http://127.0.0.1:8000";
  var AUTO_REFRESH_MS = 30000;
  var timer = null;

  function fetchTimeout(url, ms) {
    var ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
    var t = setTimeout(function () { if (ctrl) ctrl.abort(); }, ms || 15000);
    return fetch(url, ctrl ? { signal: ctrl.signal } : undefined)
      .then(function (r) {
        clearTimeout(t);
        if (!r.ok) {
          return r.json().catch(function () { return {}; }).then(function (body) {
            var err = new Error(body.detail || "HTTP " + r.status);
            err.status = r.status;
            throw err;
          });
        }
        return r.json();
      })
      .catch(function (e) {
        clearTimeout(t);
        throw e;
      });
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

  function ping(ms) {
    return fetchTimeout(TERM + "/api/health", ms || 1500)
      .then(function (h) { return !!(h && h.status === "ok"); })
      .catch(function () { return false; });
  }

  // -------------------------------------------------------------------------
  // Rendering
  // -------------------------------------------------------------------------
  function olRow(r, i, withVol) {
    var watch = r.est_full_vol_ratio >= 1.5 ? "🔥" : (r.est_full_vol_ratio >= 1.0 ? "⚡" : "—");
    return "<tr><td>" + (i + 1) + "</td>" +
      "<td><strong>" + escHtml(r.symbol) + "</strong></td>" +
      "<td>" + inr(r.open) + "</td>" +
      "<td>" + inr(r.low) + "</td>" +
      "<td>" + inr(r.ol_diff) + "</td>" +
      "<td>" + inr(r.ltp) + "</td>" +
      "<td>" + r.vol_ratio.toFixed(2) + "×</td>" +
      (withVol
        ? "<td>" + inr(r.sl_price) + "</td>" +
          '<td class="' + (r.pnl >= 0 ? "ok" : "bad") + '">' + pctStr(r.pnl_pct) + "</td>" +
          "<td>" + takeBtn(r) + "</td>"
        : "<td>" + r.est_full_vol_ratio.toFixed(1) + "×</td>" +
          '<td class="' + (r.pnl_pct >= 0 ? "ok" : "bad") + '">' + pctStr(r.pnl_pct) + "</td>" +
          "<td>" + watch + "</td>") +
      "</tr>";
  }

  function ohRow(r, i, withVol) {
    return "<tr><td>" + (i + 1) + "</td>" +
      "<td><strong>" + escHtml(r.symbol) + "</strong></td>" +
      "<td>" + inr(r.open) + "</td>" +
      "<td>" + inr(r.high) + "</td>" +
      "<td>" + inr(r.ol_diff) + "</td>" +
      "<td>" + inr(r.ltp) + "</td>" +
      "<td>" + r.vol_ratio.toFixed(2) + "×</td>" +
      (withVol
        ? "<td>" + inr(r.sl_price) + "</td>" +
          '<td class="' + (r.pnl >= 0 ? "ok" : "bad") + '">' + pctStr(r.pnl_pct) + "</td>" +
          "<td>" + pctStr(r.ext_pct) + "</td>"
        : "<td>" + r.est_full_vol_ratio.toFixed(1) + "×</td>" +
          '<td class="' + (r.pnl_pct >= 0 ? "ok" : "bad") + '">' + pctStr(r.pnl_pct) + "</td>") +
      "</tr>";
  }

  function takeBtn(r) {
    return '<button class="take-trade-btn kite-take" data-symbol="' + escHtml(r.symbol) + '" ' +
      'data-open="' + r.open + '" data-sl="' + r.sl_price + '" data-shares="' + r.shares + '" ' +
      'data-invested="' + r.invested + '" data-date="' + escHtml(r.date) + '">📝 Take</button>';
  }

  function wireTakeTrade(scope) {
    (scope || document).querySelectorAll(".kite-take").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (!window.addPaperTrade) return;
        window.addPaperTrade({
          strategy: "open-low",
          symbol: btn.getAttribute("data-symbol"),
          entry_date: btn.getAttribute("data-date"),
          entry_price: parseFloat(btn.getAttribute("data-open")),
          sl_price: parseFloat(btn.getAttribute("data-sl")),
          quantity: parseInt(btn.getAttribute("data-shares"), 10),
          invested: parseFloat(btn.getAttribute("data-invested"))
        });
        btn.textContent = "✅ Taken";
        btn.style.opacity = "0.6";
        btn.style.pointerEvents = "none";
      });
    });
  }

  function section(title, color, head, rows, emptyMsg) {
    return '<section><h2 style="color:' + color + '">' + title + "</h2>" +
      '<div class="table-wrap"><table><thead><tr>' + head + "</tr></thead><tbody>" +
      (rows || '<tr><td colspan="13" class="muted" style="text-align:center;padding:1.2rem">' + emptyMsg + "</td></tr>") +
      "</tbody></table></div></section>";
  }

  function render(result) {
    var panel = document.getElementById("panel-kite");
    if (!panel) return;
    var ol = result.setups.ol || { with_volume: [], without_volume: [], session_pct: result.session_pct };
    var oh = result.setups.oh || { with_volume: [], without_volume: [] };

    var olHead = "<th>#</th><th>Symbol</th><th>Open</th><th>Low</th><th>O-L</th><th>LTP</th><th>Vol×</th><th>SL</th><th>P&L%</th><th>Take</th>";
    var olWatchHead = "<th>#</th><th>Symbol</th><th>Open</th><th>Low</th><th>O-L</th><th>LTP</th><th>Vol×</th><th>Est Full×</th><th>P&L%</th><th>Watch</th>";
    var ohHead = "<th>#</th><th>Symbol</th><th>Open</th><th>High</th><th>H-O</th><th>LTP</th><th>Vol×</th><th>SL</th><th>P&L%</th><th>Fall%</th>";
    var ohWatchHead = "<th>#</th><th>Symbol</th><th>Open</th><th>High</th><th>H-O</th><th>LTP</th><th>Vol×</th><th>Est Full×</th><th>P&L%</th>";

    panel.innerHTML =
      '<p class="hint">⚡ Live via Kite Connect — local trading terminal (127.0.0.1:8000), true exchange opens, no proxies. Auto-refreshes every ' + (AUTO_REFRESH_MS / 1000) + "s.</p>" +
      '<p class="muted" style="font-size:0.82rem">Scanned: ' + escHtml(result.scanned_at) +
      " · " + result.universe_count + " NSE-200 symbols · session " + (ol.session_pct || 0) + "% complete</p>" +
      section("🟢 O=L With Volume (" + ol.with_volume.length + " tradeable)", "var(--green,#3fb950)", olHead,
        ol.with_volume.map(function (r, i) { return olRow(r, i, true); }).join(""),
        "No O=L + volume signals right now (" + result.universe_count + " scanned).") +
      '<section style="margin-top:2.5rem">' +
      section("⚠️ O=L Without Volume (" + ol.without_volume.length + " watch)", "#d29922", olWatchHead,
        ol.without_volume.map(function (r, i) { return olRow(r, i, false); }).join(""), "No watch-list stocks.") +
      "</section>" +
      '<section style="margin-top:2.5rem">' +
      section("🔻 O=H With Volume (" + oh.with_volume.length + " tradeable shorts)", "var(--red,#e5534b)", ohHead,
        oh.with_volume.map(function (r, i) { return ohRow(r, i, true); }).join(""), "No O=H signals right now.") +
      "</section>" +
      '<section style="margin-top:2.5rem">' +
      section("⚠️ O=H Without Volume (" + oh.without_volume.length + " watch)", "#d29922", ohWatchHead,
        oh.without_volume.map(function (r, i) { return ohRow(r, i, false); }).join(""), "No watch-list stocks.") +
      "</section>";
    wireTakeTrade(panel);
  }

  function renderOffline(note) {
    var panel = document.getElementById("panel-kite");
    if (!panel) return;
    panel.innerHTML =
      '<div class="flash">' + (note || "Local trading terminal not detected at 127.0.0.1:8000.") + "</div>" +
      '<div class="kv" style="font-size:0.9rem;margin-top:1rem">' +
      "<dt>Start it</dt><dd><code>cd ~/ZCodeProject/trading-terminal && ./start.sh</code></dd>" +
      "<dt>Then</dt><dd>press 🔄 Refresh (this tab auto-detects the terminal)</dd>" +
      "<dt>Kite login</dt><dd><code>cd ~/ZCodeProject/kite-mcp-server && python3 login_from_url.py \"<final url>\"</code> — paste the browser URL after login, once each morning</dd>" +
      "</div>";
  }

  function renderBusy(msg) {
    var panel = document.getElementById("panel-kite");
    if (panel) panel.innerHTML = '<p class="muted" style="padding:1.2rem 0">' + msg + "</p>";
  }

  // -------------------------------------------------------------------------
  // Orchestration
  // -------------------------------------------------------------------------
  function refresh(btn) {
    if (btn) { btn.textContent = "⏳ Scanning…"; btn.disabled = true; btn.classList.add("loading"); }
    renderBusy("Checking for local trading terminal…");
    return ping()
      .then(function (ok) {
        if (!ok) { renderOffline(); return false; }
        renderBusy("⚡ Kite terminal found — screening NSE 200…");
        return fetchTimeout(TERM + "/api/scanner/kite-screen?setups=ol,oh", 60000)
          .then(function (result) { render(result); return true; })
          .catch(function (e) {
            renderOffline(e.status === 503 ? escHtml(e.message) : "Terminal error: " + escHtml(e.message));
            return false;
          });
      })
      .catch(function () { renderOffline(); return false; })
      .then(function () {
        if (btn) {
          btn.classList.remove("loading");
          btn.disabled = false;
          setTimeout(function () { btn.textContent = "🔄 Refresh"; }, 1500);
        }
      });
  }

  function isActive() {
    var p = document.getElementById("panel-kite");
    return !!(p && p.classList.contains("active"));
  }

  function startAutoRefresh() {
    if (timer) return;
    timer = setInterval(function () {
      if (isActive() && document.visibilityState === "visible") refresh(null);
    }, AUTO_REFRESH_MS);
  }

  window.KiteScan = {
    refresh: refresh,
    startAutoRefresh: startAutoRefresh,
    renderOffline: renderOffline
  };
})();
