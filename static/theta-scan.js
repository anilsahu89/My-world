/* 🛡️ Nifty Daily Theta tab — renders data/scanners/nifty-daily-theta-latest.json
 * (hedged credit spread: VIX<22 gate, 20-SMA direction, 300/400 OTM spread,
 * 50% profit / strike-hit / expiry exits, max 1 open). Runs daily on the relay. */
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
  function cls(n) { return n > 0 ? "ok" : (n < 0 ? "bad" : ""); }
  function inr(n) {
    if (n === "" || n == null || isNaN(n)) return "—";
    n = Number(n);
    var s = Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 0 });
    return (n < 0 ? "−₹" : "+₹") + s;
  }

  function render(d) {
    var el = document.getElementById("panel-theta");
    if (!el) return;
    var trades = d.trades || [];
    var open = trades.filter(function (t) { return t.status === "OPEN"; });
    var closed = trades.filter(function (t) { return t.status === "CLOSED"; });
    var wins = closed.filter(function (t) { return Number(t.pnl) > 0; }).length;
    var realized = closed.reduce(function (a, t) { return a + (Number(t.pnl) || 0); }, 0);

    var h = '<p class="hint">Nifty Daily Theta — hedged credit spread · VIX &lt; 22 · ' +
      'above 20-SMA → Bull Put, below → Bear Call · sell 300 OTM / buy 400 OTM (100 pt) · ' +
      'credit ≥ ₹3 · exits: 50% profit / strike hit / expiry · max 1 open · Mon–Wed entries, ' +
      'daily management on the cloud relay (backtested 92.9% WR, PF 1.77).</p>';

    if (!d.scanned_at) {
      h += '<p class="muted" style="padding:1rem 0">Not run yet — first scheduled run is the ' +
        'next weekday morning (~09:15 IST), or hit 🔄 Refresh after clicking ▶ Run scan.</p>';
      el.innerHTML = h;
      return;
    }

    h += '<p class="muted" style="font-size:0.82rem">Last scan ' + esc(d.scanned_at) +
      " · status " + esc(d.status) + " · open " + open.length + " · closed " +
      closed.length + " · realized " + inr(realized) + " · WR " +
      (closed.length ? Math.round(wins / closed.length * 100) + "%" : "—") + "</p>";

    if (open.length) {
      h += '<h3 style="color:var(--green)">🟢 Open position</h3><div class="table-wrap"><table>' +
        "<thead><tr><th>Entry</th><th>Direction</th><th>Short/Long</th><th>Lot</th>" +
        "<th>Expiry (DTE)</th><th>Net Credit ₹</th><th>Spot@Entry</th><th>VIX@Entry</th>" +
        "</tr></thead><tbody>";
      open.forEach(function (t) {
        h += "<tr><td>" + esc(t.entry_date) + "</td><td><strong>" + esc(t.direction) +
          "</strong></td><td>" + esc(t.short_strike) + " / " + esc(t.long_strike) +
          " " + esc(t.opt_type) + "</td><td>75</td><td>" + esc(t.expiry) + " (" +
          esc(t.dte) + "d)</td><td>" + esc(t.net_credit) + "</td><td>" +
          esc(t.spot_entry) + "</td><td>" + esc(t.vix_entry) + "</td></tr>";
      });
      h += "</tbody></table></div>";
    } else {
      h += '<p class="muted" style="padding:0.6rem 0">No open position — waiting for the next ' +
        "Mon/Tue/Wed entry window with VIX &lt; 22.</p>";
    }

    if (closed.length) {
      h += '<h3 style="margin-top:1.2rem">🔴 Closed trades</h3><div class="table-wrap"><table>' +
        "<thead><tr><th>Entry</th><th>Exit</th><th>Direction</th><th>Strikes</th>" +
        "<th>Reason</th><th>P&L</th><th>Held</th></tr></thead><tbody>";
      closed.slice().reverse().forEach(function (t) {
        var win = Number(t.pnl) > 0;
        h += '<tr class="' + (win ? "row-win" : "row-loss") + '"><td>' + esc(t.entry_date) +
          "</td><td>" + esc(t.exit_date || "—") + "</td><td>" + esc(t.direction) +
          "</td><td>" + esc(t.short_strike) + "/" + esc(t.long_strike) + " " +
          esc(t.opt_type) + "</td><td>" + esc(t.exit_reason) + '</td><td class="' +
          cls(t.pnl) + '">' + inr(t.pnl) + "</td><td>" + esc(t.holding_days) +
          "d</td></tr>";
      });
      h += "</tbody></table></div>";
    }
    el.innerHTML = h;
  }

  function load() {
    fetch(base + "/data/scanners/nifty-daily-theta-latest.json?t=" + Date.now())
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) render(d); })
      .catch(function () {});
  }

  window.ThetaTab = { refresh: function (btn) { load(); if (btn) { btn.textContent = "✅ Refreshed"; setTimeout(function () { btn.textContent = "🔄 Refresh"; }, 2000); } } };
  load();
  setInterval(load, 60000);
})();
