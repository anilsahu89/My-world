/* Gold & Bitcoin O=L/O=H tabs — renders data published by the goldcrypto
 * engine on the Mac (data/gc_scan.json + data/paper_gc.json). The engine
 * polls every ~5 min and publishes after each poll; the Refresh button
 * re-fetches instantly. */
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
  function usd(n, signed) {
    if (n == null || isNaN(n)) return "—";
    var neg = n < 0;
    return (neg ? "−$" : (signed ? "+$" : "$")) +
      Math.abs(n).toFixed(2);
  }
  function px(n) {
    return n == null ? "—" : "$" + Number(n).toLocaleString("en-US",
      { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function cls(n) { return n > 0 ? "ok" : (n < 0 ? "bad" : ""); }

  function badge(ok, label, good) {
    return '<span class="badge ' + (ok ? (good || "badge-green") : "") + '">' +
           (ok ? "✅ " : "") + esc(label) + "</span>";
  }

  function findMarket(scan, tab) {
    var want = tab === "btc" ? "BTC" : "GOLD";
    var ms = (scan && scan.markets) || [];
    for (var i = 0; i < ms.length; i++) if (ms[i].symbol === want) return ms[i];
    return null;
  }

  function renderMarket(el, scan, paper, tab) {
    var m = findMarket(scan, tab);
    if (!m) {
      el.innerHTML = '<div class="flash">No engine scan data yet — the Mac engine publishes every ~5 min. Click 🔄 Refresh again shortly.</div>';
      return;
    }
    var name = m.symbol === "BTC" ? "Bitcoin (BTC-USD · UTC session)" :
                                "Gold (COMEX GC=F · CME session)";
    var h = ['<div class="scan-results"><h2>' + (m.symbol === "BTC" ? "₿ " : "🥇 ") +
             esc(name) + "</h2>"];
    h.push('<p class="hint">Engine scan ' + esc(scan.updated_at) +
           " · session " + esc(m.session) +
           " · square-off " + esc(m.square_off) + " IST · $" +
           Math.round(m.capital_usd) + " notional/trade (≈ ₹5k) · paper only</p>");

    h.push('<div class="summary-cards">');
    h.push('<div class="sum-card"><span class="sum-label">Session Open</span><span class="sum-value">' + px(m.open) + "</span></div>");
    h.push('<div class="sum-card"><span class="sum-label">High</span><span class="sum-value">' + px(m.high) + "</span></div>");
    h.push('<div class="sum-card"><span class="sum-label">Low</span><span class="sum-value">' + px(m.low) + "</span></div>");
    h.push('<div class="sum-card"><span class="sum-label">Last</span><span class="sum-value ' + cls(m.chg_pct) + '">' + px(m.ltp) +
           " (" + (m.chg_pct > 0 ? "+" : "") + m.chg_pct + "%)</span></div>");
    h.push('<div class="sum-card"><span class="sum-label">Minutes since open</span><span class="sum-value">' + m.minutes_since_open + "</span></div>");
    h.push("</div>");

    h.push("<p>");
    h.push(badge(m.ol, "Open = Low holding (BUY setup)"));
    h.push(" ");
    h.push(badge(m.oh, "Open = High holding (SELL setup)"));
    h.push(" ");
    h.push(badge(m.entry_window_open, "Entry window OPEN (first " +
             m.minutes_since_open + "/" + 90 + " min)", "badge-red"));
    h.push("</p>");

    if (m.ol && m.oh) {
      h.push('<div class="flash">No signal yet — the session has barely moved ' +
             "off its open (both O=L and O=H technically hold). The engine " +
             "trades once price breaks clearly to one side within the entry window.</div>");
    } else if (m.ol || m.oh) {
      h.push('<div class="flash">' + (m.ol
        ? "O=L is holding — the engine takes a paper BUY when this state is fresh within the entry window."
        : "O=H is holding — the engine takes a paper SELL (short) when this state is fresh within the entry window.") + "</div>");
    } else {
      h.push('<div class="flash">Neither setup holds now — price has broken ' +
             "both below the open and above it this session. No new trade " +
             "until the next session.</div>");
    }

    if (m.position) {
      var p = m.position;
      h.push('<h3>Live paper position</h3><div class="table-wrap"><table><thead><tr>' +
             "<th>Setup</th><th>Side</th><th>Qty</th><th>Entry</th><th>SL</th><th>Entry Time</th><th>P&L</th>" +
             "</tr></thead><tbody><tr>");
      h.push("<td>" + badge(true, p.setup === "ol" ? "O=L" : "O=H") + "</td>");
      h.push("<td><strong>" + esc(p.side) + "</strong></td>");
      h.push("<td>" + p.qty + "</td><td>" + px(p.entry_price) + "</td><td>" + px(p.sl_price) + "</td>");
      h.push("<td>" + esc(p.entry_time) + "</td>");
      h.push('<td class="' + cls(p.pnl) + '">' + usd(p.pnl, true) + "</td></tr></tbody></table></div>");
    } else {
      h.push('<p class="muted">No open paper position for ' + esc(m.symbol) + " right now.</p>");
    }

    // session trades for this market from the paper ledger
    var trades = (paper && (paper.open || []).concat(paper.closed || [])).filter(function (t) {
      return t.symbol === m.symbol && t.session === m.session;
    });
    if (trades.length) {
      h.push("<h3>This session's trades</h3><div class=\"table-wrap\"><table><thead><tr>" +
             "<th>Setup</th><th>Side</th><th>Qty</th><th>Entry</th><th>SL</th><th>Exit</th><th>Reason</th><th>P&L</th>" +
             "</tr></thead><tbody>");
      trades.forEach(function (t) {
        h.push("<tr><td>" + esc(t.setup === "ol" ? "O=L" : "O=H") + "</td><td>" + esc(t.side) +
               "</td><td>" + t.qty + "</td><td>" + px(t.entry_price) + "</td><td>" + px(t.sl_price) +
               "</td><td>" + (t.exit_price != null ? px(t.exit_price) + " @ " + esc(t.exit_time || "") : "OPEN") +
               "</td><td>" + esc(t.reason || "—") + "</td>" +
               '<td class="' + cls(t.pnl) + '">' + usd(t.pnl, true) + "</td></tr>");
      });
      h.push("</tbody></table></div>");
    }
    h.push("</div>");
    el.innerHTML = h.join("");
  }

  window.GCTab = {
    refresh: function (btn, tab) {
      var el = document.getElementById("panel-" + tab);
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
        fetch(base + "/data/gc_scan.json?t=" + Date.now()).then(function (r) { return r.ok ? r.json() : null; }),
        fetch(base + "/data/paper_gc.json?t=" + Date.now()).then(function (r) { return r.ok ? r.json() : null; })
      ]).then(function (rs) {
        renderMarket(el, rs[0], rs[1], tab);
        finish("✅ Scanned");
      }).catch(function () {
        el.innerHTML = '<div class="flash">⚠️ Could not load engine data — retrying works once the Mac engine publishes (every ~5 min).</div>';
        finish("⚠️ Failed");
      });
    }
  };

  // render the tab immediately on load if it is (or becomes) active
  function maybeRender() {
    var panel = document.getElementById("panel-gold");
    if (!panel) return;
    var active = document.querySelector(".setup-panel.active");
    if (active && (active.id === "panel-gold" || active.id === "panel-btc")) {
      window.GCTab.refresh(null, active.id === "panel-btc" ? "btc" : "gold");
    }
  }
  document.addEventListener("DOMContentLoaded", function () { setTimeout(maybeRender, 400); });
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".setup-tab");
    if (b && (b.getAttribute("data-tab") === "gold" || b.getAttribute("data-tab") === "btc")) {
      setTimeout(maybeRender, 50);
    }
  });
})();
