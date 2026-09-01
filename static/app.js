/**
 * Mahi Portal — Client-side interactivity for the static site.
 *
 * Handles: search, paper trades (localStorage), alerts, link fixing,
 * GitHub Actions integration (Run Scanner, Refresh Alerts, Job Log),
 * and settings (GitHub token + repo).
 *
 * Works at any URL depth on GitHub Pages.
 */
(function () {
  "use strict";

  // Detect the base path (e.g. "/mahi-portal" or "/" for root hosting).
  // Try explicit hints first, then auto-detect from URL.
  var base = (document.querySelector('meta[name="base-url"]') || {}).content || "";
  if (!base && document.documentElement.dataset.base) {
    base = document.documentElement.dataset.base;
  }
  if (!base) {
    var baseEl = document.querySelector("base");
    if (baseEl) {
      base = baseEl.getAttribute("href") || "";
    }
  }
  // Auto-detect subpath for GitHub Pages (e.g. /mahi-portal/strategy/foo.html)
  // Only triggers on *.github.io where first path segment is the repo name.
  if (!base) {
    var host = window.location.hostname;
    if (host.endsWith('.github.io')) {
      var path = window.location.pathname;
      var parts = path.replace(/^\/+/, "").split("/");
      if (parts.length > 1) {
        base = "/" + parts[0];
      }
    }
  }
  base = base.replace(/\/+$/, "") || "";

  // ---------------------------------------------------------------------------
  // Utility helpers
  // ---------------------------------------------------------------------------
  function escHtml(s) {
    var d = document.createElement("div");
    d.textContent = s || "";
    return d.innerHTML;
  }

  function url(path) {
    return base + path;
  }

  function fetchJSON(path) {
    return fetch(url(path)).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  // ---------------------------------------------------------------------------
  // GitHub Settings — token & repo in localStorage
  // ---------------------------------------------------------------------------
  var GH_TOKEN_KEY = "mahi_gh_token";
  var GH_REPO_KEY = "mahi_gh_repo";

  // Tokens/repo names pasted from GitHub often carry stray whitespace or a
  // full URL. Whitespace inside the Authorization header makes fetch() throw
  // a bare "Failed to fetch", so normalize aggressively before use.
  function normalizeGHToken(t) {
    return (t || "").replace(/\s+/g, "");
  }

  function normalizeGHRepo(r) {
    return (r || "")
      .replace(/\s+/g, "")
      .replace(/^https?:\/\/(www\.)?github\.com\//i, "")
      .replace(/^git@github\.com:/i, "")
      .replace(/\.git$/i, "")
      .replace(/\/+$/, "");
  }

  function getGHToken() {
    try { return normalizeGHToken(localStorage.getItem(GH_TOKEN_KEY)); } catch (e) { return ""; }
  }

  function getGHRepo() {
    try { return normalizeGHRepo(localStorage.getItem(GH_REPO_KEY)); } catch (e) { return ""; }
  }

  function ghApi(path, options) {
    var token = getGHToken();
    var repo = getGHRepo();
    if (!token || !repo) {
      return Promise.reject(new Error("GitHub token or repo not configured. Click ⚙️ Settings to set up."));
    }
    if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) {
      return Promise.reject(new Error('Repository "' + repo + '" is not in owner/repo format (e.g. anilsahu89/My-world). Fix it in ⚙️ Settings.'));
    }
    var url = "https://api.github.com/repos/" + repo + path;
    var headers = {
      "Authorization": "Bearer " + token,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28"
    };
    return fetch(url, Object.assign({}, options || {}, { headers: headers }))
      .catch(function (e) {
        throw new Error("Could not reach api.github.com (" + (e && e.message ? e.message : e) + "). Check your connection, or any content/ad blocker.");
      });
  }

  // Settings modal
  window.openSettings = function () {
    var modal = document.getElementById("settingsModal");
    if (!modal) return;
    document.getElementById("ghTokenInput").value = getGHToken();
    document.getElementById("ghRepoInput").value = getGHRepo();
    document.getElementById("settingsStatus").textContent = "";
    modal.style.display = "flex";
  };

  window.closeSettings = function () {
    var modal = document.getElementById("settingsModal");
    if (modal) modal.style.display = "none";
  };

  window.saveSettings = function () {
    var token = normalizeGHToken(document.getElementById("ghTokenInput").value);
    var repo = normalizeGHRepo(document.getElementById("ghRepoInput").value);
    var status = document.getElementById("settingsStatus");

    if (!token) {
      status.innerHTML = '<span class="bad">Token is required.</span>';
      return;
    }
    if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) {
      status.innerHTML = '<span class="bad">Repository must be "owner/repo" format (e.g. anilsahu89/My-world).</span>';
      return;
    }

    try {
      localStorage.setItem(GH_TOKEN_KEY, token);
      localStorage.setItem(GH_REPO_KEY, repo);
    } catch (e) {
      status.innerHTML = '<span class="bad">Failed to save: ' + escHtml(e.message) + '</span>';
      return;
    }

    status.innerHTML = '<span class="ok">✅ Saved. You can now run scanners.</span>';
    setTimeout(function () { window.closeSettings(); }, 1500);
  };

  // Show settings prompt if no token configured
  function checkSettingsPrompt() {
    if (!getGHToken() || !getGHRepo()) {
      // Show a subtle prompt near the settings button
      var settingsBtn = document.querySelector(".settings-btn");
      if (settingsBtn) {
        settingsBtn.style.animation = "pulse 2s infinite";
        settingsBtn.title = "⚠️ Click to configure GitHub token (required for scanners)";
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Timestamp — live JS clock
  // ---------------------------------------------------------------------------
  function initClock() {
    var el = document.querySelector(".topbar .now");
    if (!el) return;
    function tick() {
      el.textContent = new Date().toLocaleString();
    }
    tick();
    setInterval(tick, 30000);
  }

  // ---------------------------------------------------------------------------
  // Global search — loads pre-built search-index.json once, filters locally
  // ---------------------------------------------------------------------------
  var searchIndex = null;

  function initSearch() {
    var input = document.getElementById("globalSearch");
    var dropdown = document.getElementById("searchDropdown");
    if (!input || !dropdown) return;

    var debounceTimer = null;
    var activeIdx = -1;
    var items = [];

    var indexLoaded = false;
    input.addEventListener("focus", function () {
      if (!indexLoaded) {
        indexLoaded = true;
        fetchJSON("/static/search-index.json")
          .then(function (data) { searchIndex = data || []; })
          .catch(function () { searchIndex = []; });
      }
    });

    input.addEventListener("input", function () {
      var q = this.value.trim();
      if (q.length < 2) { dropdown.style.display = "none"; return; }
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(function () { doLocalSearch(q); }, 150);
    });

    input.addEventListener("keydown", function (e) {
      if (dropdown.style.display === "none") {
        if (e.key === "Enter") {
          var q = this.value.trim();
          if (q.length >= 2) { window.location.href = url("/search.html?q=" + encodeURIComponent(q)); e.preventDefault(); }
        }
        return;
      }
      if (e.key === "Escape") { dropdown.style.display = "none"; input.blur(); e.preventDefault(); }
      else if (e.key === "ArrowDown") { activeIdx = Math.min(activeIdx + 1, items.length - 1); highlightActive(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { activeIdx = Math.max(activeIdx - 1, -1); highlightActive(); e.preventDefault(); }
      else if (e.key === "Enter") {
        if (activeIdx >= 0 && items[activeIdx]) { window.location.href = items[activeIdx].url; }
        else { window.location.href = url("/search.html?q=" + encodeURIComponent(input.value.trim())); }
        e.preventDefault();
      }
    });

    document.addEventListener("click", function (e) {
      if (!e.target.closest(".search-wrap")) dropdown.style.display = "none";
    });

    function doLocalSearch(q) {
      if (!searchIndex || searchIndex.length === 0) {
        dropdown.innerHTML = '<div class="search-empty">Index loading…</div>';
        dropdown.style.display = "block";
        return;
      }
      var ql = q.toLowerCase();
      var scored = [];
      for (var i = 0; i < searchIndex.length; i++) {
        var entry = searchIndex[i];
        var title_l = (entry.title || "").toLowerCase();
        var slug_l = (entry.slug || "").toLowerCase();
        var snippet_l = (entry.snippet || "").toLowerCase();
        var score = 0;
        if (ql === slug_l) score = 100;
        else if (slug_l.indexOf(ql) !== -1) score = 90;
        else if (title_l.indexOf(ql) !== -1) score = 80;
        else if (snippet_l.indexOf(ql) !== -1) score = 50;
        else continue;
        scored.push({ title: entry.title, url: url(entry.url), cat_label: entry.cat_label, category: entry.category, snippet: entry.snippet, score: score });
      }
      scored.sort(function (a, b) { return b.score - a.score || a.title.localeCompare(b.title); });
      renderResults(scored.slice(0, 15));
    }

    function renderResults(results) {
      if (!results.length) {
        dropdown.innerHTML = '<div class="search-empty">No results found</div>';
        dropdown.style.display = "block"; items = []; activeIdx = -1; return;
      }
      var html = ""; var lastCat = ""; items = [];
      for (var i = 0; i < results.length; i++) {
        var r = results[i];
        if (r.category !== lastCat) { html += '<div class="search-group-label">' + escHtml(r.cat_label) + "</div>"; lastCat = r.category; }
        items.push(r);
        html += '<a class="search-item" href="' + escHtml(r.url) + '" data-idx="' + i + '">' +
          '<div class="search-item-title">' + escHtml(r.title) + "</div>" +
          (r.snippet ? '<div class="search-item-snippet">' + escHtml(r.snippet) + "</div>" : "") + "</a>";
      }
      dropdown.innerHTML = html;
      dropdown.style.display = "block";
      activeIdx = -1;
      dropdown.querySelectorAll(".search-item").forEach(function (el) {
        el.addEventListener("mouseenter", function () { activeIdx = parseInt(this.dataset.idx); highlightActive(); });
      });
    }

    function highlightActive() {
      dropdown.querySelectorAll(".search-item").forEach(function (el, idx) {
        el.classList.toggle("active", idx === activeIdx);
        if (idx === activeIdx) el.scrollIntoView({ block: "nearest" });
      });
    }
  }

  // ---------------------------------------------------------------------------
  // Search results page — client-side filtering
  // ---------------------------------------------------------------------------
  function initSearchPage() {
    var params = new URLSearchParams(window.location.search);
    var query = (params.get("q") || "").trim();
    if (!query) return;

    var h1 = document.querySelector("h1");
    if (h1) h1.innerHTML = '🔍 Search Results <span class="muted">for "' + escHtml(query) + '"</span>';

    var searchInput = document.querySelector(".search-input-bottom");
    if (searchInput) searchInput.value = query;

    fetchJSON("/static/search-index.json")
      .then(function (index) {
        var ql = query.toLowerCase();
        var scored = [];
        for (var i = 0; i < index.length; i++) {
          var entry = index[i];
          var title_l = (entry.title || "").toLowerCase();
          var slug_l = (entry.slug || "").toLowerCase();
          var snippet_l = (entry.snippet || "").toLowerCase();
          var score = 0;
          if (ql === slug_l) score = 100;
          else if (slug_l.indexOf(ql) !== -1) score = 90;
          else if (title_l.indexOf(ql) !== -1) score = 80;
          else if (snippet_l.indexOf(ql) !== -1) score = 50;
          else continue;
          scored.push({ title: entry.title, url: url(entry.url), cat_label: entry.cat_label, category: entry.category, snippet: entry.snippet, score: score });
        }
        scored.sort(function (a, b) { return b.score - a.score || a.title.localeCompare(b.title); });

        var main = document.querySelector("main");
        var flash = main.querySelector(".flash"); if (flash) flash.remove();
        main.querySelectorAll("section").forEach(function (s) { s.remove(); });
        var countEl = main.querySelector("p.muted"); if (countEl) countEl.remove();

        var countP = document.createElement("p");
        countP.className = "muted";
        countP.textContent = scored.length + " result" + (scored.length !== 1 ? "s" : "") + " found";
        main.appendChild(countP);

        var groups = []; var seenCat = {};
        for (var j = 0; j < scored.length; j++) {
          var r = scored[j];
          if (seenCat[r.category] !== undefined) { groups[seenCat[r.category]].rows.push(r); }
          else { seenCat[r.category] = groups.length; groups.push({ cat_label: r.cat_label, rows: [r] }); }
        }
        for (var g = 0; g < groups.length; g++) {
          var grp = groups[g];
          var section = document.createElement("section");
          section.style.marginTop = "1.5rem";
          section.innerHTML = "<h2>" + escHtml(grp.cat_label) + ' <span class="muted">(' + grp.rows.length + ")</span></h2>" +
            '<div class="table-wrap"><table><thead><tr><th>#</th><th>Title</th><th>Details</th></tr></thead><tbody></tbody></table></div>';
          var tbody = section.querySelector("tbody");
          for (var k = 0; k < grp.rows.length; k++) {
            var row = grp.rows[k];
            var tr = document.createElement("tr");
            tr.innerHTML = "<td>" + (k + 1) + "</td>" +
              '<td><a href="' + escHtml(row.url) + '">' + escHtml(row.title) + "</a></td>" +
              '<td class="muted" style="font-size:0.85rem">' + escHtml(row.snippet) + "</td>";
            tbody.appendChild(tr);
          }
          main.appendChild(section);
        }
        if (scored.length === 0) {
          var noRes = document.createElement("div"); noRes.className = "flash";
          noRes.innerHTML = 'No results found for "<strong>' + escHtml(query) + '</strong>". Try a different term.';
          main.appendChild(noRes);
        }
      })
      .catch(function () {});
  }

  // ---------------------------------------------------------------------------
  // Paper Trades — localStorage-based CRUD
  // ---------------------------------------------------------------------------
  var PAPER_TRADES_KEY = "mahi_paper_trades";

  function getPaperTrades() {
    try { var data = localStorage.getItem(PAPER_TRADES_KEY); return data ? JSON.parse(data) : []; } catch (e) { return []; }
  }

  function savePaperTrades(trades) {
    try { localStorage.setItem(PAPER_TRADES_KEY, JSON.stringify(trades)); } catch (e) { console.error("Failed to save paper trades:", e); }
  }

  function seedPaperTrades() {
    var existing = getPaperTrades();
    if (existing.length > 0) return;
    fetchJSON("/data/papertrades.json")
      .then(function (trades) {
        if (trades && trades.length > 0) { savePaperTrades(trades); renderPaperTradesPage(); }
      })
      .catch(function () {});
  }

  window.addPaperTrade = function (trade) {
    var trades = getPaperTrades();
    trade.id = trades.length > 0 ? Math.max.apply(null, trades.map(function (t) { return t.id; })) + 1 : 1;
    trade.status = "OPEN";
    trade.created_at = new Date().toISOString();
    trades.unshift(trade);
    savePaperTrades(trades);
    var flash = document.createElement("div");
    flash.className = "flash";
    flash.textContent = "✅ Trade added: " + trade.symbol + " (saved locally)";
    document.querySelector("main").insertBefore(flash, document.querySelector("main").firstChild);
    setTimeout(function () { flash.remove(); }, 5000);
  };

  window.closePaperTrade = function (tradeId, exitPrice, exitDate, exitReason) {
    var trades = getPaperTrades();
    for (var i = 0; i < trades.length; i++) {
      if (trades[i].id === tradeId && trades[i].status === "OPEN") {
        var entryPrice = trades[i].entry_price;
        var quantity = trades[i].quantity;
        trades[i].exit_price = parseFloat(exitPrice);
        trades[i].exit_date = exitDate;
        trades[i].exit_reason = exitReason;
        trades[i].pnl = parseFloat((quantity * (exitPrice - entryPrice)).toFixed(2));
        trades[i].pnl_pct = parseFloat(((exitPrice - entryPrice) / entryPrice * 100).toFixed(2));
        trades[i].status = "CLOSED";
        trades[i].closed_at = new Date().toISOString();
        savePaperTrades(trades);
        return true;
      }
    }
    return false;
  };

  function renderPaperTradesPage() {
    if (!document.querySelector("h1")) return;
    var h1 = document.querySelector("h1");
    if (!h1 || h1.textContent.indexOf("Paper Trades") === -1) return;

    var trades = getPaperTrades();
    var opens = trades.filter(function (t) { return t.status === "OPEN"; });
    var closes = trades.filter(function (t) { return t.status === "CLOSED"; });

    var totalRealized = 0; var wins = 0;
    for (var i = 0; i < closes.length; i++) { totalRealized += closes[i].pnl || 0; if ((closes[i].pnl || 0) > 0) wins++; }
    var winRate = closes.length > 0 ? (wins / closes.length * 100).toFixed(1) : 0;

    var summary = document.querySelector(".summary-cards");
    if (summary) {
      var vals = summary.querySelectorAll(".sum-value");
      if (vals.length >= 4) {
        vals[0].textContent = opens.length;
        vals[1].textContent = closes.length;
        var pnlEl = vals[2];
        pnlEl.textContent = "₹" + (totalRealized >= 0 ? "+" : "") + totalRealized.toLocaleString("en-IN", { minimumFractionDigits: 2 });
        pnlEl.className = "sum-value " + (totalRealized >= 0 ? "pos" : "neg");
        vals[3].textContent = winRate + "%";
      }
    }
  }

  // ---------------------------------------------------------------------------
  // Alerts page — setup tabs (O=L / O=H / BB Trap), Take Trade buttons
  // ---------------------------------------------------------------------------
  function initAlertsPage() {
    if (!document.querySelector("h1")) return;
    var h1 = document.querySelector("h1");
    if (!h1 || h1.textContent.indexOf("Market Alerts") === -1) return;

    // Setup tabs
    var tabs = document.querySelectorAll(".setup-tab");
    if (tabs.length) {
      tabs.forEach(function (b) {
        b.addEventListener("click", function () { window.switchAlertTab(b.getAttribute("data-tab")); });
      });
      // Restore cached scans for O=H / BB Trap so tabs aren't empty on revisit
      ["oh", "bb"].forEach(function (k) {
        if (window.DirectScan && window.DirectScan.loadCacheFor) {
          var cached = window.DirectScan.loadCacheFor(k);
          if (cached) {
            (k === "oh" ? window.DirectScan.renderOpenHighPage : window.DirectScan.renderBBTrapPage)(cached.result);
          }
        }
      });
      window.switchAlertTab(activeAlertTab());
    }

    // Wire Take Trade buttons
    document.querySelectorAll(".take-trade-btn").forEach(function (btn) {
      var form = btn.closest("form");
      if (!form) return;
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        var trade = {
          strategy: "open-low",
          symbol: form.querySelector('input[name="symbol"]').value,
          entry_date: form.querySelector('input[name="entry_date"]').value,
          entry_price: parseFloat(form.querySelector('input[name="entry_price"]').value),
          sl_price: parseFloat(form.querySelector('input[name="sl_price"]').value),
          quantity: parseInt(form.querySelector('input[name="quantity"]').value),
          invested: parseFloat(form.querySelector('input[name="invested"]').value),
        };
        window.addPaperTrade(trade);
        btn.textContent = "✅ Taken";
        btn.style.opacity = "0.6";
        btn.style.pointerEvents = "none";
      });
    });

    // If the scheduled workflow has committed a fresher scan than the baked
    // HTML (the build may be days old), render it right away — same-origin
    // fetch, no proxies, instant. Works for whichever of O=L / O=H is the
    // active tab on load.
    var SCHEDULED_RENDERERS = {
      ol: function (repo) { window.DirectScan.renderAlertsPage(repo); },
      oh: function (repo) { window.DirectScan.renderOpenHighPage(repo); }
    };
    if (window.DirectScan && window.DirectScan.fetchRepoAlerts) {
      var initialTab = activeAlertTab();
      if (SCHEDULED_RENDERERS[initialTab]) {
        window.DirectScan.fetchRepoAlerts(initialTab).then(function (repo) {
          if (repo && repo.is_today) SCHEDULED_RENDERERS[initialTab](repo);
        });
      }
    }

    // BB Trap tab: same scheduled-scan treatment — render data/bbtrap.json
    // instantly on load unless a fresher browser scan is already showing.
    // (No intraday auto-poll: positional signals change once a day.)
    var bbPanel = document.getElementById("panel-bb");
    if (bbPanel && window.DirectScan && window.DirectScan.fetchRepoBBTrap &&
        (!bbPanel.textContent.trim() || /Click 🔄 Refresh/.test(bbPanel.textContent))) {
      window.DirectScan.fetchRepoBBTrap().then(function (repo) {
        if (repo && repo.is_recent) window.DirectScan.renderBBTrapPage(repo);
      });
    }

    // Auto-poll the scheduled O=L / O=H scan every 2 minutes while the
    // market is open (refresh-alerts.yml commits a fresh one every 15 min),
    // so new signals show up on their own — no manual Refresh click needed.
    // Only re-renders when a genuinely newer scan has landed, and only for
    // whichever of the two tabs is currently showing.
    if (window.DirectScan && window.DirectScan.fetchRepoAlerts && window.DirectScan.sessionPct) {
      var lastAutoScanAt = { ol: "", oh: "" };
      setInterval(function () {
        var tab = activeAlertTab();
        if (!SCHEDULED_RENDERERS[tab]) return;
        var pct = window.DirectScan.sessionPct();
        if (!(pct > 0 && pct < 1)) return; // before open / after close / weekend
        window.DirectScan.fetchRepoAlerts(tab).then(function (repo) {
          if (repo && repo.is_today && repo.scanned_at !== lastAutoScanAt[tab]) {
            lastAutoScanAt[tab] = repo.scanned_at;
            SCHEDULED_RENDERERS[tab](repo);
          }
        });
      }, 120000);
    }
  }

  // Refresh — runs the scan for the ACTIVE setup tab (O=L / O=H / BB Trap),
  // entirely in the browser. No token, no GitHub Actions.
  var ALERT_TAB_KEY = "mahi_alert_tab";

  function activeAlertTab() {
    try { return localStorage.getItem(ALERT_TAB_KEY) || "ol"; } catch (e) { return "ol"; }
  }

  window.switchAlertTab = function (tab) {
    try { localStorage.setItem(ALERT_TAB_KEY, tab); } catch (e) {}
    document.querySelectorAll(".setup-tab").forEach(function (b) {
      b.classList.toggle("active", b.getAttribute("data-tab") === tab);
    });
    document.querySelectorAll(".setup-panel").forEach(function (p) {
      p.classList.toggle("active", p.id === "panel-" + tab);
    });
    var placeholder = "Click 🔄 Refresh to scan this setup now.";
    if (tab === "oh" && !document.getElementById("panel-oh").innerHTML.trim()) {
      document.getElementById("panel-oh").innerHTML = '<p class="muted" style="padding:1rem 0">' + placeholder + "</p>";
    }
    if (tab === "bb" && !document.getElementById("panel-bb").innerHTML.trim()) {
      document.getElementById("panel-bb").innerHTML = '<p class="muted" style="padding:1rem 0">' + placeholder + "</p>";
    }
    // Kite tab: auto-detect the local terminal on first activation, then
    // keep it fresh while visible.
    if (tab === "kite" && window.KiteScan) {
      var panel = document.getElementById("panel-kite");
      if (panel && /Checking for local|Click 🔄 Refresh/.test(panel.textContent)) {
        window.KiteScan.refresh(null);
      }
      window.KiteScan.startAutoRefresh();
    }
  };

  window.refreshAlerts = function (btn) {
    if (!window.DirectScan) {
      alert("Scan engine failed to load — reload the page.");
      return;
    }
    var tab = activeAlertTab();

    // Kite tab runs through the local terminal bridge, not the Yahoo engine
    if (tab === "kite") {
      if (window.KiteScan) window.KiteScan.refresh(btn);
      return;
    }

    var runners = { ol: window.DirectScan.runOpenLow, oh: window.DirectScan.runOpenHigh, bb: window.DirectScan.runBBTrap };
    var renderers = { ol: window.DirectScan.renderAlertsPage, oh: window.DirectScan.renderOpenHighPage, bb: window.DirectScan.renderBBTrapPage };
    var runner = runners[tab];
    var render = renderers[tab];
    if (!runner || !render) return;

    btn.classList.add("loading");
    btn.disabled = true;

    function finish(label) {
      btn.classList.remove("loading");
      btn.disabled = false;
      btn.textContent = label;
      setTimeout(function () { btn.textContent = "🔄 Refresh"; }, 2500);
    }

    var SCHEDULE_FETCHERS = {
      ol: function () { return window.DirectScan.fetchRepoAlerts("ol"); },
      oh: function () { return window.DirectScan.fetchRepoAlerts("oh"); },
      bb: function () { return window.DirectScan.fetchRepoBBTrap(); }
    };
    function scheduledIsFresh(tab, repo) {
      if (!repo) return false;
      return tab === "bb" ? !!repo.is_recent : !!repo.is_today;
    }

    function runLiveBrowserScan() {
      btn.textContent = "⏳ Scanning…";
      runner(function (pct, msg) {
        btn.textContent = "⏳ " + msg;
      })
        .then(function (result) {
          window.DirectScan.saveCacheFor(tab, result);
          render(result);
          finish("✅ Scanned live");
        })
        .catch(function (err) {
          // Live proxies failed — fall back to the scheduled server scan for
          // this tab if it's current; every tab then falls back further to
          // its last successful browser scan.
          var flash = document.createElement("div");
          var h1 = document.querySelector("main h1");
          var fetcher = SCHEDULE_FETCHERS[tab];
          (fetcher ? fetcher() : Promise.resolve(null)).then(function (repo) {
            if (scheduledIsFresh(tab, repo)) {
              render(repo);
              flash.className = "flash";
              flash.textContent = "Live browser scan failed (" + err.message +
                ") — showing today's scheduled server scan from " + repo.scanned_at +
                ({ ol: " (auto-updates every 15 min during market hours).", oh: " (auto-updates every 15 min during market hours).", bb: "." }[tab] || ".");
            } else {
              var cached = window.DirectScan.loadCacheFor(tab);
              if (cached) {
                render(cached.result);
                flash.className = "flash bad";
                flash.textContent = "Live scan failed (" + err.message + ") — showing your last successful " +
                  ({ ol: "Open=Low", oh: "Open=High", bb: "BB Trap" }[tab] || "") + " scan from " +
                  cached.result.scanned_at + ". Retry in a minute.";
              } else {
                flash.className = "flash bad";
                flash.textContent = "Live browser scan failed: " + err.message +
                  " — public CORS proxies are rate-limited, retry in a minute.";
              }
            }
            if (h1 && h1.parentNode) h1.parentNode.insertBefore(flash, h1.nextSibling);
          }).then(function () {
            finish("⚠️ Live scan failed");
          });
        });
    }

    // O=L, O=H, and now BB Trap all have a scheduled server scan behind them
    // that's far more reliable (and much faster) than scraping Yahoo through
    // public CORS proxies from the browser. Check that FIRST and use it
    // whenever it's current — only fall to the live browser scan if it
    // isn't (most often: today's scheduled run hasn't landed yet).
    if (SCHEDULE_FETCHERS[tab]) {
      btn.textContent = "⏳ Checking scheduled scan…";
      SCHEDULE_FETCHERS[tab]().then(function (repo) {
        if (scheduledIsFresh(tab, repo)) {
          window.DirectScan.saveCacheFor(tab, repo);
          render(repo);
          finish("✅ Scanned (server, " + repo.scanned_at.replace(" IST", "") + ")");
        } else {
          runLiveBrowserScan();
        }
      }).catch(function () {
        runLiveBrowserScan();
      });
    } else {
      runLiveBrowserScan();
    }
  };

  // ---------------------------------------------------------------------------
  // Paper trades page — wire close modal to localStorage
  // ---------------------------------------------------------------------------
  function initPaperTradePage() {
    if (!document.querySelector("h1")) return;
    var h1 = document.querySelector("h1");
    if (!h1 || h1.textContent.indexOf("Paper Trades") === -1) return;

    var closeForm = document.getElementById("closeForm");
    if (closeForm) {
      closeForm.addEventListener("submit", function (e) {
        e.preventDefault();
        var tradeId = parseInt(document.getElementById("closeTradeId").value);
        var exitPrice = this.querySelector('input[name="exit_price"]').value;
        var exitDate = this.querySelector('input[name="exit_date"]').value;
        var exitReason = this.querySelector('select[name="exit_reason"]').value;
        if (!exitPrice || !exitDate) { alert("Exit price and date are required"); return; }
        var success = window.closePaperTrade(tradeId, exitPrice, exitDate, exitReason);
        if (success) { document.getElementById("closeModal").style.display = "none"; window.location.reload(); }
        else { alert("Trade not found or already closed"); }
      });
    }

    var closeDate = document.getElementById("closeDate");
    if (closeDate) closeDate.value = new Date().toISOString().split("T")[0];
  }

  // ---------------------------------------------------------------------------
  // Run Scanner — dispatch GitHub Actions workflow, poll, show results
  // ---------------------------------------------------------------------------
  window.runScanner = function (btn) {
    var slug = btn.getAttribute("data-slug");
    if (!slug) return;

    // Open=Low runs entirely in the browser — no token, no GitHub Actions.
    if (slug === "open-low-intraday" && window.DirectScan && window.DirectScan.runOpenLow) {
      var dStatusEl = btn.parentElement.querySelector(".run-status");
      btn.disabled = true;
      btn.textContent = "⏳ Running…";
      if (dStatusEl) dStatusEl.innerHTML = '<span class="running">Scanning in browser…</span>';

      window.DirectScan.runOpenLow(function (pct, msg) {
        if (dStatusEl) dStatusEl.innerHTML = '<span class="running">' + escHtml(msg) + "</span>";
      })
        .then(function (result) {
          if (window.DirectScan.saveCache) window.DirectScan.saveCache(result);
          if (dStatusEl) dStatusEl.innerHTML = '<span class="ok">Scan complete</span>';
          var resEl = document.getElementById("scanResults");
          if (resEl) {
            resEl.style.display = "block";
            resEl.innerHTML = window.renderScanResults(window.DirectScan.buildScannerJSON(result));
          }
          btn.textContent = "✅ Done — live results";
          setTimeout(function () {
            btn.disabled = false;
            btn.textContent = "▶ Run scan now";
          }, 30000);
        })
        .catch(function (err) {
          btn.disabled = false;
          btn.textContent = "▶ Run scan now";
          var resEl2 = document.getElementById("scanResults");
          window.DirectScan.fetchRepoAlerts().then(function (repo) {
            var result = null, note = "";
            if (repo && repo.is_today) {
              result = repo;
              note = "⚠ Live browser scan failed (" + escHtml(err.message) +
                ") — showing today's scheduled server scan from " + escHtml(repo.scanned_at) + ".";
              if (dStatusEl) dStatusEl.innerHTML = '<span class="bad">Live failed — server scan shown</span>';
            } else {
              var cached = window.DirectScan.loadCache ? window.DirectScan.loadCache() : null;
              if (cached) {
                result = cached.result;
                note = "⚠ Live scan failed (" + escHtml(err.message) +
                  ") — showing last successful scan from " + escHtml(cached.result.scanned_at) + ".";
                if (dStatusEl) dStatusEl.innerHTML = '<span class="bad">Showing cached scan</span>';
              }
            }
            if (result && resEl2) {
              resEl2.style.display = "block";
              resEl2.innerHTML = window.renderScanResults(window.DirectScan.buildScannerJSON(result)) +
                '<div class="flash">' + note + "</div>";
            }
            if (!result) {
              if (dStatusEl) dStatusEl.innerHTML = '<span class="bad">Error: ' + escHtml(err.message) + "</span>";
              alert("Scan failed: " + err.message +
                "\n\nPublic CORS proxies are rate-limited; retry in a minute.");
            }
          });
        });
      return;
    }

    if (!getGHToken() || !getGHRepo()) {
      alert("GitHub token not configured. Click ⚙️ Settings in the top bar to set up your token and repository.");
      window.openSettings();
      return;
    }

    var statusEl = btn.parentElement.querySelector(".run-status");
    btn.disabled = true;
    btn.textContent = "⏳ Running…";
    if (statusEl) statusEl.innerHTML = '<span class="running">Triggering workflow…</span>';

    // Dispatch the workflow
    ghApi("/actions/workflows/run-scanner.yml/dispatches", {
      method: "POST",
      body: JSON.stringify({ ref: "main", inputs: { strategy: slug } })
    })
      .then(function (r) {
        if (r.status === 204) {
          if (statusEl) statusEl.innerHTML = '<span class="running">Dispatched — waiting for CI (~30s)…</span>';
          // Poll for workflow run to complete
          pollWorkflow("run-scanner.yml", function (status, run) {
            if (status === "completed") {
              btn.textContent = "✅ Done — Results updated";
              if (statusEl) statusEl.innerHTML = '<span class="ok">Scan complete</span>';
              // Fetch and display results
              fetchScannerResults(slug);
              // Re-enable button after a delay
              setTimeout(function () {
                btn.disabled = false;
                btn.textContent = "▶ Run scan now";
              }, 30000);
            } else if (status === "failed") {
              btn.textContent = "❌ Run failed";
              if (statusEl) statusEl.innerHTML = '<span class="bad">Workflow failed — check GitHub Actions</span>';
              setTimeout(function () { btn.disabled = false; btn.textContent = "▶ Run scan now"; }, 10000);
            }
          });
        } else {
          return r.json().then(function (d) { throw new Error(d.message || "Unexpected response"); });
        }
      })
      .catch(function (err) {
        btn.disabled = false;
        btn.textContent = "▶ Run scan now";
        if (statusEl) statusEl.innerHTML = '<span class="bad">Error: ' + escHtml(err.message) + '</span>';
        alert("Failed to run scanner: " + err.message + "\n\nThis strategy needs the GitHub Actions backend. Fine-grained token: Actions must be 'Read and write'. Classic token: tick 'repo'.");
      });
  };

  // Poll a workflow's latest run until it completes
  function pollWorkflow(workflowFile, callback) {
    var pollCount = 0;
    var maxPolls = 40; // 40 * 15s = 10 minutes max

    function poll() {
      if (pollCount >= maxPolls) {
        callback("timeout");
        return;
      }
      pollCount++;

      ghApi("/actions/workflows/" + workflowFile + "/runs?per_page=1")
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then(function (data) {
          var runs = data.workflow_runs || [];
          if (runs.length === 0) { setTimeout(poll, 5000); return; }

          var run = runs[0];
          var status = run.status; // queued, in_progress, completed
          var conclusion = run.conclusion; // success, failure, null

          if (status === "completed") {
            callback(conclusion === "success" ? "completed" : "failed", run);
          } else {
            setTimeout(poll, 15000); // poll every 15s
          }
        })
        .catch(function (err) {
          // Retry on network errors
          setTimeout(poll, 10000);
        });
    }

    // Wait a bit for the dispatch to register (GitHub API delay)
    setTimeout(poll, 5000);
  }

  // Fetch and display scanner results from data/scanners/<slug>-latest.json
  function fetchScannerResults(slug) {
    var resultsEl = document.getElementById("scanResults");
    if (!resultsEl) return;

    resultsEl.style.display = "block";
    resultsEl.innerHTML = '<div class="flash">Loading results…</div>';

    fetchJSON("/data/scanners/" + slug + "-latest.json")
      .then(function (data) {
        resultsEl.innerHTML = renderScanResults(data);
      })
      .catch(function (err) {
        resultsEl.innerHTML = '<div class="flash bad">Failed to load results: ' + escHtml(err.message) + '</div>';
      });
  }

  // Render scanner results as HTML
  function renderScanResults(data) {
    var html = '<div class="scan-results-inner">';
    html += '<h2>📊 Scanner Results</h2>';

    // Status badge
    var statusClass = "ok";
    var statusText = data.status || "unknown";
    if (data.status === "error" || data.status === "failed") { statusClass = "bad"; }
    if (data.status === "never_run") { statusClass = ""; statusText = "not yet run"; }

    html += '<div class="kv">';
    if (data.scanned_at) html += '<dt>Scanned at</dt><dd>' + escHtml(data.scanned_at) + '</dd>';
    if (data.scan_date) html += '<dt>Scan date</dt><dd>' + escHtml(data.scan_date) + '</dd>';
    html += '<dt>Status</dt><dd class="' + statusClass + '">' + escHtml(statusText) + '</dd>';

    // Signal count
    if (data.signals !== undefined) {
      if (typeof data.signals === "object") {
        html += '<dt>Signals</dt><dd>';
        if (data.signals.short !== undefined) html += 'Short: ' + data.signals.short + ' | ';
        if (data.signals.long !== undefined) html += 'Long: ' + data.signals.long;
        html += '</dd>';
      } else {
        html += '<dt>Signals</dt><dd class="' + (data.signals > 0 ? "ok" : "") + '">' + data.signals + '</dd>';
      }
    }

    // Summary section
    if (data.summary && typeof data.summary === "object") {
      var keys = Object.keys(data.summary);
      for (var i = 0; i < keys.length; i++) {
        html += '<dt>' + escHtml(keys[i]) + '</dt><dd>' + escHtml(String(data.summary[keys[i]])) + '</dd>';
      }
    }

    // Trades count for premium mill / daily theta
    if (data.trade_count !== undefined) {
      html += '<dt>Total trades</dt><dd>' + data.trade_count + '</dd>';
    }
    if (data.log_entries !== undefined) {
      html += '<dt>Log entries</dt><dd>' + data.log_entries + '</dd>';
    }

    html += '</div>'; // end .kv

    // Table data
    if (data.table && data.table.headers && data.table.rows && data.table.rows.length > 0) {
      html += '<h3>Signals Table</h3>';
      html += '<div class="table-wrap"><table><thead><tr>';
      for (var h = 0; h < data.table.headers.length; h++) {
        html += '<th>' + escHtml(data.table.headers[h]) + '</th>';
      }
      html += '</tr></thead><tbody>';
      for (var r = 0; r < data.table.rows.length; r++) {
        html += '<tr>';
        for (var c = 0; c < data.table.rows[r].length; c++) {
          html += '<td>' + escHtml(data.table.rows[r][c]) + '</td>';
        }
        html += '</tr>';
      }
      html += '</tbody></table></div>';
    }

    // Trades array (for premium mill / daily theta)
    if (data.trades && data.trades.length > 0) {
      html += '<h3>Trade History</h3>';
      html += '<div class="table-wrap"><table><thead><tr>';
      var tradeKeys = Object.keys(data.trades[0]);
      for (var tk = 0; tk < tradeKeys.length; tk++) {
        html += '<th>' + escHtml(tradeKeys[tk]) + '</th>';
      }
      html += '</tr></thead><tbody>';
      for (var ti = 0; ti < data.trades.length; ti++) {
        html += '<tr>';
        for (var tv = 0; tv < tradeKeys.length; tv++) {
          var val = data.trades[ti][tradeKeys[tv]];
          html += '<td>' + escHtml(String(val || "")) + '</td>';
        }
        html += '</tr>';
      }
      html += '</tbody></table></div>';
    }

    // Text report
    if (data.text_report && data.status !== "never_run") {
      html += '<h3>Report</h3>';
      html += '<pre class="log">' + escHtml(data.text_report) + '</pre>';
    }

    // Error display
    if (data.error) {
      html += '<div class="flash bad">Error: ' + escHtml(data.error) + '</div>';
    }

    html += '</div>'; // end .scan-results-inner
    return html;
  }

  // Exposed for direct-scan.js (browser-direct results use the same renderer)
  window.renderScanResults = renderScanResults;

  // ---------------------------------------------------------------------------
  // Job Log — fetch GitHub Actions run history
  // ---------------------------------------------------------------------------
  function initJobLogPage() {
    if (!document.querySelector("h1")) return;
    var h1 = document.querySelector("h1");
    if (!h1 || h1.textContent.indexOf("Job Log") === -1) return;

    loadJobLog();
  }

  window.refreshJobLog = function (btn) {
    btn.textContent = "⏳ Refreshing…";
    btn.classList.add("loading");
    btn.disabled = true;
    loadJobLog(function () {
      btn.textContent = "🔄 Refresh";
      btn.classList.remove("loading");
      btn.disabled = false;
    });
  };

  function loadJobLog(done) {
    var container = document.getElementById("jobLogContainer");
    if (!container) return;

    if (!getGHToken() || !getGHRepo()) {
      container.innerHTML =
        '<div class="flash">⚠️ GitHub token not configured. <a href="javascript:openSettings()" style="color:var(--accent);cursor:pointer">Click here to open Settings</a>.</div>';
      if (done) done();
      return;
    }

    container.innerHTML = '<div class="flash">Fetching run history from GitHub…</div>';

    ghApi("/actions/runs?per_page=30")
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        var runs = data.workflow_runs || [];
        if (runs.length === 0) {
          container.innerHTML = '<div class="flash">No workflow runs found.</div>';
          if (done) done();
          return;
        }

        var html = '<div class="table-wrap"><table>';
        html += '<thead><tr><th>#</th><th>Workflow</th><th>Status</th><th>Conclusion</th><th>Started</th><th>Duration</th><th>Actions</th></tr></thead>';
        html += '<tbody>';

        for (var i = 0; i < runs.length; i++) {
          var run = runs[i];
          var statusClass = "running";
          var conclusionText = run.conclusion || "—";
          if (run.status === "completed") {
            statusClass = run.conclusion === "success" ? "ok" : "bad";
          }

          var duration = "";
          if (run.updated_at && run.created_at) {
            var start = new Date(run.created_at);
            var end = new Date(run.updated_at);
            var diff = Math.round((end - start) / 1000);
            var mins = Math.floor(diff / 60);
            var secs = diff % 60;
            duration = mins + "m " + secs + "s";
          }

          // Extract strategy name from run name or display name
          var name = run.display_title || run.name || "Unknown";
          // Clean up the name
          if (name === "Run Scanner") {
            // Try to get the strategy from inputs (not available in list API)
            name = "Scanner";
          }

          html += '<tr>';
          html += '<td>' + run.run_number + '</td>';
          html += '<td><strong>' + escHtml(name) + '</strong></td>';
          html += '<td class="' + (run.status === "completed" ? "" : "running") + '">' + escHtml(run.status) + '</td>';
          html += '<td class="' + statusClass + '">' + escHtml(conclusionText) + '</td>';
          html += '<td class="muted" style="font-size:0.82rem">' + new Date(run.created_at).toLocaleString() + '</td>';
          html += '<td class="muted">' + escHtml(duration) + '</td>';
          html += '<td><a href="' + escHtml(run.html_url) + '" target="_blank" rel="noopener">View ↗</a></td>';
          html += '</tr>';
        }

        html += '</tbody></table></div>';
        container.innerHTML = html;
        if (done) done();
      })
      .catch(function (err) {
        container.innerHTML = '<div class="flash bad">Failed to load runs: ' + escHtml(err.message) +
          '<br>Check your token and repo in <a href="javascript:openSettings()" style="color:var(--accent)">Settings</a>.</div>';
        if (done) done();
      });
  }

  // ---------------------------------------------------------------------------
  // Fix internal links — convert /wiki/strategies/X to /strategies/X.html
  // ---------------------------------------------------------------------------
  function fixWikiLinks() {
    document.querySelectorAll("a").forEach(function (a) {
      var href = a.getAttribute("href");
      if (!href) return;
      if (href.indexOf("://") !== -1 || href.indexOf("#") === 0 || href.indexOf("javascript:") === 0) return;

      // /wiki/strategies/foo → /strategies/foo.html
      var m = href.match(/^\/wiki\/(.+)$/);
      if (m) { a.setAttribute("href", url("/" + m[1] + ".html")); return; }

      // /strategy/foo → /strategy/foo.html
      var m2 = href.match(/^\/strategy\/(.+)$/);
      if (m2) { a.setAttribute("href", url("/strategy/" + m2[1] + ".html")); return; }

      // /strategies/foo → /strategies/foo.html
      var m3 = href.match(/^\/strategies\/(.+)$/);
      if (m3) { a.setAttribute("href", url("/strategies/" + m3[1] + ".html")); return; }

      // /concepts/foo → /concepts/foo.html
      var m4 = href.match(/^\/concepts\/(.+)$/);
      if (m4) { a.setAttribute("href", url("/concepts/" + m4[1] + ".html")); return; }

      // /sources/foo → /sources/foo.html
      var m5 = href.match(/^\/sources\/(.+)$/);
      if (m5) { a.setAttribute("href", url("/sources/" + m5[1] + ".html")); return; }

      // /trade-reviews/foo → /trade-reviews/foo.html
      var m6 = href.match(/^\/trade-reviews\/(.+)$/);
      if (m6) { a.setAttribute("href", url("/trade-reviews/" + m6[1] + ".html")); return; }

      // /concepts → /concepts.html
      if (href === "/concepts") { a.setAttribute("href", url("/concepts.html")); return; }
      // /active → /active.html
      if (href === "/active") { a.setAttribute("href", url("/active.html")); return; }
      // /stocks → /stocks.html
      if (href === "/stocks") { a.setAttribute("href", url("/stocks.html")); return; }
      // /alerts → /alerts.html
      if (href === "/alerts") { a.setAttribute("href", url("/alerts.html")); return; }
      // /paper → /paper.html
      if (href === "/paper") { a.setAttribute("href", url("/paper.html")); return; }
      // /jobs → /jobs.html
      if (href === "/jobs") { a.setAttribute("href", url("/jobs.html")); return; }
      // / → index
      if (href === "/" && base) { a.setAttribute("href", base + "/"); }
    });
  }

  // ---------------------------------------------------------------------------
  // Load previous scan results on strategy pages (if data exists)
  // ---------------------------------------------------------------------------
  function loadPreviousScanResults() {
    var runBtn = document.querySelector(".run-scan-btn");
    if (!runBtn) return; // Not a strategy page with a runnable scanner
    var slug = runBtn.getAttribute("data-slug");
    if (!slug) return;

    fetchJSON("/data/scanners/" + slug + "-latest.json")
      .then(function (data) {
        if (data && data.status && data.status !== "never_run") {
          fetchScannerResults(slug);
        }
      })
      .catch(function () {
        // No results file yet — that's fine
      });
  }

  // ---------------------------------------------------------------------------
  // Dashboard strategy tabs — Active / All / Rules-only (default: Active)
  // ---------------------------------------------------------------------------
  function initStratTabs() {
    var bar = document.getElementById("stratTabs");
    if (!bar) return;
    var cards = Array.prototype.slice.call(document.querySelectorAll(".grid .card"));
    var TAB_KEY = "mahi_strat_tab";

    function apply(filter) {
      try { localStorage.setItem(TAB_KEY, filter); } catch (e) {}
      bar.querySelectorAll("button").forEach(function (b) {
        b.classList.toggle("active", b.getAttribute("data-filter") === filter);
      });
      cards.forEach(function (c) {
        var match = filter === "all" || c.classList.contains(filter);
        c.classList.toggle("hidden-by-tab", !match);
      });
    }

    bar.querySelectorAll("button").forEach(function (b) {
      b.addEventListener("click", function () { apply(b.getAttribute("data-filter")); });
    });

    var saved = "";
    try { saved = localStorage.getItem(TAB_KEY) || ""; } catch (e) {}
    if (!saved || !bar.querySelector('button[data-filter="' + saved + '"]')) saved = "runnable";
    apply(saved);
  }

  // ---------------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", function () {
    initClock();
    initSearch();
    fixWikiLinks();
    checkSettingsPrompt();
    initStratTabs();

    // Page-specific init
    if (window.location.pathname.indexOf("/search") !== -1) {
      initSearchPage();
    }
    initAlertsPage();
    initPaperTradePage();
    initJobLogPage();
    seedPaperTrades();
    renderPaperTradesPage();
    loadPreviousScanResults();
  });

})();
