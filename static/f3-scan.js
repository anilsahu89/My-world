/* 3-Candle momentum tab — renders data/f3_picks.json (written by the Mac's
 * daily 12:20 IST scan) with live P&L from data/paper_ol.json.
 * The 🔄 Refresh button re-fetches both instantly. */
(function () {
  "use strict";

  var base = "";
  if (location.hostname.endsWith(".github.io")) {
    var seg = location.pathname.replace(/^\/+/, "").split("/")[0];
    if (seg && !/\.html?$/i.test(seg)) base = "/" + seg;
  }

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  function inr(n, signed) {
    if (n == null || isNaN(n)) return "—";
    var neg = n < 0;
    var s = Math.abs(n).toLocaleString("en-IN",
      { minimumFractionDigits: 0, maximumFractionDigits: 0 });
    return (neg ? "−₹" : (signed ? "+₹" : "₹")) + s;
  }
  function cls(n) { return n > 0 ? "ok" : (n < 0 ? "bad" : ""); }

  window.F3Tab = {
    refresh: function (btn) {
      var el = document.getElementById("panel-f3");
      if (!el) return;
      if (btn) { btn.classList.add("loading"); btn.disabled = true; }
      function finish(label) {
        if (!btn) return;
        btn.classList.remove("loading");
        btn.disabled = false;
        btn.textContent = label;
        setTimeout(function () { btn.textContent = "🔄 Refresh"; }, 2500);
      }
      Promise.all([
        fetch(base + "/data/f3_picks.json?t=" + Date.now())
          .then(function (r) { return r.ok ? r.json() : null; }),
        fetch(base + "/data/paper_ol.json?t=" + Date.now())
          .then(function (r) { return r.ok ? r.json() : null; })
      ]).then(function (rs) {
        render(el, rs[0], rs[1]);
        finish("✅ Scanned");
      }).catch(function () {
        el.innerHTML = '<div class="flash">⚠️ Could not load the 3-Candle scan — the Mac publishes it daily at 12:20 IST.</div>';
        finish("⚠️ Failed");
      });
    }
  };

  function render(el, picks, paper) {
    if (!picks || !picks.signals) {
      el.innerHTML = '<div class="flash">No 3-Candle scan published yet today. The scan runs on the Mac at 12:20 IST (once the day\u2019s first 3 hourly candles complete) and this tab updates right after.</div>';
      return;
    }
    // live marks from the paper book
    var live = {};
    (paper && paper.open || []).concat(paper && paper.closed || []).forEach(function (t) {
      if (t.setup === "f3") live[t.symbol] = t;
    });
    var h = ['<div class="scan-results"><h2>🕯️ 3-Candle Momentum — '
             + esc(picks.date) + '</h2>'];
    h.push('<p class="hint">Scanned ' + esc(picks.scanned_at) + ' IST · '
           + esc(picks.rule) + ' · max ' + picks.max_trades
           + ' trades, ranked by drive strength · paper ₹10k/trade</p>');

    var open = 0, closedPnl = 0, unreal = 0;
    picks.signals.forEach(function (s) {
      var lt = live[s.symbol];
      if (lt) {
        if (lt.pnl != null) {
          if (lt.exit_price) closedPnl += lt.pnl; else unreal += lt.pnl;
        }
        if (!lt.exit_price) open++;
      }
    });
    h.push('<div class="summary-cards">');
    h.push('<div class="sum-card"><span class="sum-label">Signals today</span><span class="sum-value">' + picks.signals.length + "</span></div>");
    h.push('<div class="sum-card"><span class="sum-label">Open</span><span class="sum-value" style="color:var(--green)">' + open + "</span></div>");
    h.push('<div class="sum-card"><span class="sum-label">Realized P&L</span><span class="sum-value ' + (closedPnl >= 0 ? "pos" : "neg") + '">' + inr(closedPnl, true) + "</span></div>");
    h.push('<div class="sum-card"><span class="sum-label">Live P&L (open)</span><span class="sum-value ' + (unreal >= 0 ? "pos" : "neg") + '">' + inr(unreal, true) + "</span></div>");
    h.push("</div>");

    h.push('<div class="table-wrap"><table><thead><tr><th>#</th><th>Symbol</th>'
           + "<th>Drive</th><th>Qty</th><th>Entry ₹</th><th>SL ₹</th><th>Last ₹</th><th>Status</th><th>P&L</th>"
           + "</tr></thead><tbody>");
    picks.signals.forEach(function (s, i) {
      var lt = live[s.symbol] || {};
      var st = lt.exit_price
        ? '<span class="badge">' + esc(lt.reason || "CLOSED") + "</span>"
        : (lt.symbol ? '<span class="badge badge-green">OPEN</span>'
                     : '<span class="badge">PICK</span>');
      h.push("<tr><td>" + (i + 1) + "</td><td><strong>" + esc(s.symbol)
             + "</strong></td><td>+" + Number(s.drive_pct).toFixed(2) + "%</td>"
             + "<td>" + (lt.qty || s.qty || "—") + "</td>"
             + "<td>" + (lt.entry_price != null ? lt.entry_price : s.entry) + "</td>"
             + "<td>" + (lt.sl_price != null ? lt.sl_price : s.sl) + "</td>"
             + "<td>" + (lt.ltp != null ? lt.ltp : "—") + "</td>"
             + "<td>" + st + "</td>"
             + '<td class="' + cls(lt.pnl) + '">'
             + (lt.pnl != null ? inr(lt.pnl, true) : "—") + "</td></tr>");
    });
    h.push("</tbody></table></div>");
    h.push('<p class="hint">Backtest (2y): first-3-of-day LONG on NSE 200 — '
           + '53.5% WR, +0.22%/trade, PF 1.40. Rolling any-3-hours is noise; '
           + "BTC excluded (mean-reverts). Engine manages SL and 15:00 square-off.</p>");
    h.push("</div>");
    el.innerHTML = h.join("");
  }

  function maybeRender() {
    var active = document.querySelector(".setup-panel.active");
    if (active && active.id === "panel-f3") window.F3Tab.refresh(null);
  }
  document.addEventListener("DOMContentLoaded", function () { setTimeout(maybeRender, 400); });
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".setup-tab");
    if (b && b.getAttribute("data-tab") === "f3") setTimeout(maybeRender, 50);
  });
})();
