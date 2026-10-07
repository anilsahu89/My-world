/* ui-v2.js — optional look-and-feel layer for paper.html.
   Read-only with respect to the page: it only adds classes and one new
   status bar. It never moves, removes or rewrites existing elements, and
   every step is wrapped so a failure here cannot affect the trading desks.
   Switch back: add ?classic=1 to the URL, press "Classic view", or run
   `bash ui-v2.sh off` to remove it from the page. */
(function () {
  "use strict";
  var KEY = "ui_v2_off";
  var root = document.documentElement;
  function on() { return root.classList.contains("ui-v2"); }
  function safe(fn) { try { fn(); } catch (e) { if (window.console) console.warn("[ui-v2]", e); } }
  function setOff(v) {
    try { if (v) localStorage.setItem(KEY, "1"); else localStorage.removeItem(KEY); } catch (e) {}
    location.reload();
  }

  /* --- classic mode: only offer the way back ----------------------------- */
  if (!on()) {
    document.addEventListener("DOMContentLoaded", function () {
      safe(function () {
        var b = document.createElement("button");
        b.id = "ui-restore"; b.type = "button"; b.textContent = "Try new look";
        b.addEventListener("click", function () { setOff(false); });
        document.body.appendChild(b);
      });
    });
    return;
  }

  function ready(fn) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn); else fn();
  }

  ready(function () {
    /* 1. keep the desk tabs just below the sticky top bar */
    safe(function () {
      var bar = document.querySelector(".topbar");
      if (!bar) return;
      function set() { root.style.setProperty("--ui-topbar-h", bar.offsetHeight + "px"); }
      set();
      window.addEventListener("resize", set);
      if (window.ResizeObserver) new ResizeObserver(set).observe(bar);
    });

    /* 2. fold the long rule text under each desk title */
    safe(function () {
      document.querySelectorAll(".pt-panel > h2 > span.muted").forEach(function (s) {
        s.classList.add("ui-rules");
        s.setAttribute("tabindex", "0");
        s.setAttribute("role", "button");
        s.title = "Show or hide the full rules";
        function toggle() { s.classList.toggle("open"); }
        s.addEventListener("click", toggle);
        s.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
        });
      });
    });

    /* 3. keep the active tab visible in the scrolling tab row */
    safe(function () {
      var row = document.querySelector(".pt-tabs");
      if (!row) return;
      function reveal() {
        var a = row.querySelector(".pt-tab.active");
        if (a && row.scrollWidth > row.clientWidth) {
          row.scrollLeft = Math.max(0, a.offsetLeft - (row.clientWidth - a.offsetWidth) / 2);
        }
      }
      row.addEventListener("click", function () { setTimeout(reveal, 0); });
      setTimeout(reveal, 0);
    });

    /* 4. status bar: NSE session state + how fresh each desk's data is
       (inserts before the desk tabs on paper.html, at the top of main
       everywhere else) */
    safe(function () {
      var row = document.querySelector(".pt-tabs");
      var anchor, mode;
      if (row && row.parentNode) { anchor = row; mode = "before"; }
      else {
        anchor = document.querySelector("main");
        if (!anchor) return;
        mode = "first";
      }
      var barEl = document.createElement("div");
      barEl.id = "ui-statusbar";
      barEl.innerHTML =
        '<span id="ui-mkt"></span><span id="ui-fresh"></span>' +
        '<span class="ui-spacer"></span>' +
        '<button type="button" id="ui-fresh-btn" aria-expanded="false">Data details</button>' +
        '<button type="button" id="ui-classic">Classic view</button>' +
        '<ul id="ui-fresh-list" hidden></ul>';
      if (mode === "before") row.parentNode.insertBefore(barEl, row);
      else anchor.insertBefore(barEl, anchor.firstChild);
      document.getElementById("ui-classic").addEventListener("click", function () { setOff(true); });
      var list = document.getElementById("ui-fresh-list"), btn = document.getElementById("ui-fresh-btn");
      btn.addEventListener("click", function () {
        list.hidden = !list.hidden; btn.setAttribute("aria-expanded", String(!list.hidden));
      });

      function istParts(d) {
        var p = {};
        new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", weekday: "short", hour: "2-digit",
          minute: "2-digit", hour12: false }).formatToParts(d).forEach(function (x) { p[x.type] = x.value; });
        return p;
      }
      function market() {
        var p = istParts(new Date()), mins = (+p.hour % 24) * 60 + +p.minute;
        var wk = ["Mon", "Tue", "Wed", "Thu", "Fri"].indexOf(p.weekday) >= 0;
        var open = wk && mins >= 555 && mins < 930;           /* 09:15 – 15:30 IST */
        var el = document.getElementById("ui-mkt");
        el.innerHTML = '<span class="ui-dot ' + (open ? "on" : "") + '"></span>NSE ' + (open ? "open" : "closed") +
          " · " + p.hour + ":" + p.minute + " IST";
        el.title = "Based on 09:15–15:30 IST, Mon–Fri. Exchange holidays are not checked.";
      }
      market(); setInterval(market, 30000);

      var FILES = [["Intraday O=L / O=H", "data/paper_ol.json"], ["Gold / BTC", "data/paper_gc.json"],
        ["QM swing", "data/paper_qm.json"], ["Future arbitrage", "data/paper_future_arb.json"],
        ["BTC theta", "data/paper_btc_theta.json"], ["Long-term premium", "data/paper_nifty_ltp.json"]];
      var MON = { Jan: 0, Feb: 1, Mar: 2, Apr: 3, May: 4, Jun: 5, Jul: 6, Aug: 7, Sep: 8, Oct: 9, Nov: 10, Dec: 11 };
      function parseIST(s) {
        var m = /(\d{1,2}) (\w{3}) (\d{4}) (\d{2}):(\d{2})(?::(\d{2}))?/.exec(s || "");
        if (!m || !(m[2] in MON)) return null;
        return Date.UTC(+m[3], MON[m[2]], +m[1], +m[4], +m[5], +(m[6] || 0)) - 19800000;  /* IST = UTC+5:30 */
      }
      function ago(ms) {
        var m = Math.max(0, Math.round(ms / 60000));
        if (m < 1) return "just now";
        if (m < 90) return m + " min ago";
        var h = Math.round(m / 60);
        return h < 48 ? h + " h ago" : Math.round(h / 24) + " d ago";
      }
      function refresh() {
        if (document.hidden) return;
        Promise.all(FILES.map(function (f) {
          return fetch(f[1], { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; })
            .then(function (j) { return { name: f[0], t: j ? parseIST(j.updated_at) : null }; })
            .catch(function () { return { name: f[0], t: null }; });
        })).then(function (res) {
          var now = Date.now(), known = res.filter(function (x) { return x.t; });
          var wk = ["Mon", "Tue", "Wed", "Thu", "Fri"].indexOf(istParts(new Date()).weekday) >= 0;
          function cls(t) { var h = (now - t) / 3600000; return !wk ? "on" : h < 6 ? "on" : h < 30 ? "amber" : "bad"; }
          list.innerHTML = res.map(function (x) {
            return "<li><span>" + x.name + "</span><span>" + (x.t
              ? '<span class="ui-dot ' + cls(x.t) + '"></span>' + ago(now - x.t) : "unavailable") + "</span></li>";
          }).join("");
          var f = document.getElementById("ui-fresh");
          if (!known.length) {
            f.innerHTML = '<span class="ui-dot bad"></span>Could not read data files — showing whatever the page already loaded';
            return;
          }
          var newest = Math.max.apply(null, known.map(function (x) { return x.t; }));
          var stale = known.filter(function (x) { return cls(x.t) === "bad"; }).length + (res.length - known.length);
          f.innerHTML = '<span class="ui-dot ' + (stale ? "amber" : "on") + '"></span>Latest data ' + ago(now - newest) +
            (stale ? " · " + stale + " desk" + (stale > 1 ? "s" : "") + " need a look (see Data details)" : "");
        });
      }
      refresh(); setInterval(refresh, 120000);
    });
  });
})();
