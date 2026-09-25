/* QM (Quantity Model) tab — renders data/qm_picks.json (written by the
 * cloud QM desk's evening scan) with the desk's open positions from
 * data/paper_qm.json. The 🔄 Refresh button re-fetches both instantly. */
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

  function fmtPct(n) {
    if (n == null || isNaN(n)) return "—";
    return (n > 0 ? "+" : "") + n.toFixed(1);
  }

  function renderSignals(d) {
    var sigs = d.signals || [];
    var gate = d.nifty_gate || {};
    var h = '<p class="hint">' + esc(d.rule || "") + "</p>";
    h += '<p class="muted" style="font-size:0.82rem">Scan ' +
      esc(d.updated_at || d.date) +
      " · NIFTY gate — HM buy state: <strong>" + (gate.hm_buy_state ? "ON" : "off") +
      "</strong>, above SMA20: <strong>" + (gate.above_sma20 ? "ON" : "off") +
      "</strong> · " + sigs.length + " candidate" + (sigs.length === 1 ? "" : "s") +
      " (desk takes max 2/day)</p>";
    if (!sigs.length) {
      h += '<p class="muted" style="padding:1rem 0">No QM candidates today — the gates ' +
        "(NIFTY direction + squeeze/high conditions) filtered everything.</p>";
      return h;
    }
    h += '<div class="table-wrap"><table><thead><tr><th>#</th><th>Family</th>' +
      "<th>Symbol</th><th>Close ₹</th><th>RSI9</th><th>Why</th><th>Action</th>" +
      "</tr></thead><tbody>";
    sigs.forEach(function (s, i) {
      h += "<tr><td>" + (i + 1) + "</td>" +
        "<td><strong>" + esc(s.setup) + "</strong></td>" +
        "<td><strong>" + esc(s.sym) + "</strong></td>" +
        "<td class='num'>" + s.close + "</td>" +
        "<td class='num'>" + s.rsi + "</td>" +
        "<td class='muted'>" + esc(s.note || "") + "</td>" +
        "<td>" + (window.addPaperTrade
          ? '<button type="button" class="take-trade-btn" data-qm-symbol="' +
            esc(s.sym) + '" data-qm-price="' + s.close + '">📝 Take</button>'
          : "") + "</td></tr>";
    });
    return h + "</tbody></table></div>";
  }

  function renderDesk(q) {
    var s = q.summary || {};
    if (!s.open && !s.pending && !s.closed) return "";
    var h = '<p class="hint" style="margin-top:1.2rem">QM desk book — ₹30k lots, ' +
      "max 2 new/day, +12% book half &amp; trail, RSI86 exit-all, never book " +
      "losses (average once at −50%).</p>";
    h += '<p class="muted" style="font-size:0.82rem">Desk ' + esc(q.updated_at) +
      " · open " + s.open + " · pending " + s.pending + " · closed " + s.closed +
      " · realized ₹" + (s.realized || 0) + " · unrealized ₹" + (s.unrealized || 0) +
      (s.circuit_breaker ? " · <strong style='color:#f66'>CIRCUIT BREAKER ON</strong>" : "") +
      "</p>";
    var rows = (q.pending || []).concat(q.open || []).concat((q.closed || []).slice(0, 10));
    if (rows.length) {
      h += '<div class="table-wrap"><table><thead><tr><th>Signal</th><th>Symbol</th>' +
        "<th>Family</th><th>Status</th><th>Entry</th><th>Mark</th><th>Booked</th>" +
        "<th>Exit</th><th>P&L ₹</th></tr></thead><tbody>";
      rows.forEach(function (t) {
        h += "<tr><td>" + esc(t.signal_date) + "</td>" +
          "<td><strong>" + esc(t.symbol) + "</strong></td>" +
          "<td>" + esc(t.setup) + "</td>" +
          "<td>" + esc(t.status) + (t.trail ? " 🚩" : "") + (t.avg_count ? " (avg)" : "") + "</td>" +
          "<td>" + (t.entry != null ? t.entry : "next open") + "</td>" +
          "<td>" + (t.mark != null ? t.mark : "—") + "</td>" +
          '<td class="' + cls(t.booked) + '">' + (t.booked || "—") + "</td>" +
          "<td>" + (t.exit != null ? t.exit + " " + esc(t.reason || "") : "—") + "</td>" +
          '<td class="' + cls(t.pnl) + '">' + (t.pnl != null ? t.pnl : "—") + "</td></tr>";
      });
      h += "</tbody></table></div>";
    }
    return h;
  }

  function wireTake() {
    document.querySelectorAll("[data-qm-symbol]").forEach(function (btn) {
      if (btn._wired) return;
      btn._wired = true;
      btn.addEventListener("click", function () {
        if (!window.addPaperTrade) return;
        var sym = btn.getAttribute("data-qm-symbol");
        var px = parseFloat(btn.getAttribute("data-qm-price"));
        var qty = px > 0 ? Math.floor(30000 / px) : 0;   // ₹30k lot, like the desk
        window.addPaperTrade({
          strategy: "qm-manual", symbol: sym,
          entry_date: new Date().toISOString().slice(0, 10) + " 00:00:00",
          entry_price: px, sl_price: null, quantity: qty,
          invested: Math.round(qty * px)
        });
        btn.textContent = "✅ Taken";
        btn.style.opacity = "0.6";
        btn.style.pointerEvents = "none";
      });
    });
  }

  function loadBoth(btn) {
    var el = document.getElementById("panel-qm");
    if (!el) return;
    if (btn) { btn.classList.add("loading"); btn.disabled = true; btn.textContent = "⏳ Scanning…"; }
    Promise.all([
      fetch(base + "/data/qm_picks.json?t=" + Date.now()).then(function (r) { return r.ok ? r.json() : null; }),
      fetch(base + "/data/paper_qm.json?t=" + Date.now()).then(function (r) { return r.ok ? r.json() : null; })
    ]).then(function (res) {
      var h = res[0] ? renderSignals(res[0])
        : '<p class="muted" style="padding:1rem 0">No QM scan yet — the desk scans NIFTY-500 daily after 17:30 IST.</p>';
      if (res[1]) h += renderDesk(res[1]);
      el.innerHTML = h;
      wireTake();
      if (btn) { btn.classList.remove("loading"); btn.disabled = false; btn.textContent = "✅ Refreshed"; }
    }).catch(function () {
      if (btn) { btn.classList.remove("loading"); btn.disabled = false; btn.textContent = "⚠️ Failed"; }
    }).then(function () {
      if (btn) setTimeout(function () { btn.textContent = "🔄 Refresh"; }, 2500);
    });
  }

  window.QMTab = { refresh: function (btn) { loadBoth(btn); } };
  var active = document.querySelector(".setup-tab.active");
  if (active && active.getAttribute("data-tab") === "qm") window.QMTab.refresh(null);
})();
