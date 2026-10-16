/* =============================================================================
 * TradeTemplates — ONE Open-trade template + ONE Closed-trade template for
 * every paper-trade tab on paper.html.
 *
 *   To add / remove / rename a column on EVERY tab at once, edit
 *   OPEN_COLUMNS or CLOSED_COLUMNS below. Nothing else needs to change.
 *
 * Each tab hands its raw engine rows through an adapter (T.adapt.*) that maps
 * them onto the common trade shape, then calls:
 *     TradeTemplates.open(wrapEl,   rows, { desk, empty, ... })
 *     TradeTemplates.closed(wrapEl, rows, { desk, empty, ... })
 *
 * Common trade shape (every field optional except symbol):
 *   id, desk, symbol, market, setup, side (BUY|SELL), status (OPEN|PENDING),
 *   qty, entry, ltp, exit, sl, tgt, invested, pnl, pnlPct, booked,
 *   entryDate, entryTime, exitDate, exitTime, held, reason,
 *   cur ('inr'|'usd'), detail (plain text under the symbol),
 *   slText / tgtText / entryText (override the displayed cell),
 *   canClose (false hides Close), onClose(trade) (custom close handler),
 *   extra (trusted HTML appended in the action cell), onDelete(trade)
 * ========================================================================== */
(function () {
  "use strict";
  var T = (window.TradeTemplates = {});

  /* ---------- global knobs ---------- */
  T.config = { closedLimit: 30 };

  /* ---------- tiny helpers (string based: no DOM needed) ---------- */
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function num(v) {
    if (v === null || v === undefined || v === "") return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }
  function sgnOf(t) {
    var s = String(t.side || "").toUpperCase();
    return (s.indexOf("SELL") === 0 || s.indexOf("SHORT") === 0) ? -1 : 1;
  }
  function sym(cur) { return cur === "usd" ? "$" : "₹"; }
  function price(n, cur) {
    n = num(n);
    if (n === null) return "—";
    var loc = cur === "usd" ? "en-US" : "en-IN";
    return sym(cur) + n.toLocaleString(loc, { maximumFractionDigits: 2 });
  }
  function money(n, cur, signed) {
    n = num(n);
    if (n === null) return "—";
    var loc = cur === "usd" ? "en-US" : "en-IN";
    var d = cur === "usd" ? 2 : 0;
    var s = Math.abs(n).toLocaleString(loc, { minimumFractionDigits: d, maximumFractionDigits: d });
    return (n < 0 ? "−" : (signed && n > 0 ? "+" : "")) + sym(cur) + s;
  }
  function pct(n) {
    n = num(n);
    if (n === null) return "—";
    return (n > 0 ? "+" : (n < 0 ? "−" : "")) + Math.abs(n).toFixed(2) + "%";
  }
  function pnlCls(n) { n = num(n); return n > 0 ? "ok" : (n < 0 ? "bad" : ""); }
  var DASH = '<span class="muted">—</span>';
  // compact "10-16 09:44" stamp (drops the year and seconds so the column stays narrow)
  function stamp(date, time) {
    var d = date ? String(date) : "", tm = time ? String(time).slice(0, 5) : "";
    if (/^\d{4}-\d{2}-\d{2}/.test(d)) d = d.slice(5);
    return esc((d + " " + tm).trim());
  }

  /* ---------- normalisation: fills in what can be derived ---------- */
  T.normalize = function (raw) {
    var t = {};
    for (var k in raw) if (Object.prototype.hasOwnProperty.call(raw, k)) t[k] = raw[k];
    t.cur = t.cur || "inr";
    t.pcur = t.pcur || t.cur;
    t.qty = num(t.qty); t.entry = num(t.entry); t.ltp = num(t.ltp); t.exit = num(t.exit);
    t.sl = num(t.sl); t.tgt = num(t.tgt); t.booked = num(t.booked) || 0;
    t.invested = num(t.invested); t.pnl = num(t.pnl); t.pnlPct = num(t.pnlPct);
    var s = sgnOf(t);
    if (t.invested === null && t.entry !== null && t.qty !== null) t.invested = t.entry * t.qty;
    var isClosed = t.exit !== null || t.exitDate || t.exitTime;
    if (t.pnl === null && !isClosed && t.entry !== null && t.qty !== null && t.ltp !== null && t.ltp > 0) {
      t.pnl = t.booked + s * (t.ltp - t.entry) * t.qty;
    }
    if (t.pnl === null && isClosed && t.entry !== null && t.qty !== null && t.exit !== null) {
      t.pnl = t.booked + s * (t.exit - t.entry) * t.qty;
    }
    if (t.pnlPct === null && t.pnl !== null && t.invested) t.pnlPct = t.pnl / t.invested * 100;
    // money at stake / money on the table, from the stop and target levels
    t.risk = (t.sl !== null && t.entry !== null && t.qty !== null) ? Math.abs(t.entry - t.sl) * t.qty : null;
    t.reward = (t.tgt !== null && t.entry !== null && t.qty !== null) ? Math.abs(t.tgt - t.entry) * t.qty : null;
    return t;
  };

  /* ---------- badges ---------- */
  var SETUP_LABEL = { ol: "O=L", oh: "O=H", f3: "3-Candle" };
  function setupBadge(t) {
    if (!t.setup) return DASH;
    var key = String(t.setup);
    var label = SETUP_LABEL[key] || key;
    var green = key === "ol" || key === "f3" || key.toUpperCase().indexOf("BUY") >= 0;
    return '<span class="badge ' + (green ? "badge-green" : "badge-red") + '">' + esc(label) + "</span>";
  }
  function sideBadge(t) {
    if (!t.side) return DASH;
    var c = sgnOf(t) > 0 ? "badge-green" : "badge-red";
    return '<span class="badge ' + c + '">' + esc(t.side) + "</span>";
  }

  /* ===========================================================================
   * COLUMN DEFINITIONS  —  edit here to change every tab.
   *   always:true  → shown even when no row has a value (shows "—")
   *   always:false → column is hidden automatically when every row is empty
   *   has(t)       → does this row carry a value? (used for auto-hide)
   * ======================================================================== */
  T.OPEN_COLUMNS = [
    { key: "opened", label: "Opened", has: function (t) { return !!(t.entryDate || t.entryTime); },
      cell: function (t) { return stamp(t.entryDate, t.entryTime); } },
    { key: "symbol", label: "Asset / Symbol", always: true,
      cell: function (t) {
        return "<strong>" + esc(t.symbol) + "</strong>" +
          (t.market ? ' <span class="muted" style="font-size:0.72rem">' + esc(t.market) + "</span>" : "") +
          (t.status === "PENDING" ? ' <span class="badge badge-grey">PENDING</span>' : "") +
          (t.detail ? '<div class="muted" style="font-size:0.72rem">' + esc(t.detail) + "</div>" : "");
      } },
    { key: "setup", label: "Setup", has: function (t) { return !!t.setup; }, cell: setupBadge },
    { key: "side", label: "Side", always: true, cell: sideBadge },
    { key: "qty", label: "Qty", always: true,
      cell: function (t) { return t.qty === null ? "—" : esc(t.qty.toLocaleString("en-IN")); } },
    { key: "entry", label: "Entry", always: true,
      cell: function (t) { return t.entryText != null ? esc(t.entryText) : (t.entry === null ? "next open" : price(t.entry, t.pcur)); } },
    { key: "ltp", label: "LTP (live)", always: true, title: "last traded / marked price from the engine",
      cell: function (t) { return t.ltp === null ? DASH : price(t.ltp, t.pcur); } },
    { key: "pnl", label: "P&L", always: true, title: "unrealised profit / loss at the LTP shown",
      cell: function (t) {
        return t.pnl === null ? DASH :
          '<strong class="' + pnlCls(t.pnl) + '">' + money(t.pnl, t.cur, true) + "</strong>";
      } },
    { key: "pnlPct", label: "P&L %", always: true,
      cell: function (t) { return t.pnlPct === null ? DASH : '<span class="' + pnlCls(t.pnlPct) + '">' + pct(t.pnlPct) + "</span>"; } },
    { key: "sl", label: "SL", always: true, title: "stop-loss",
      cell: function (t) { return t.slText != null ? esc(t.slText) : (t.sl === null ? "—" : price(t.sl, t.pcur)); } },
    { key: "tgt", label: "TGT", always: true, title: "target",
      cell: function (t) { return t.tgtText != null ? esc(t.tgtText) : (t.tgt === null ? "—" : price(t.tgt, t.pcur)); } },
    { key: "invested", label: "Invested", always: true, title: "capital deployed (entry × qty; for credit spreads, the max loss)",
      cell: function (t) { return t.invested === null ? "—" : money(t.invested, t.cur); } },
    { key: "risk", label: "Risk to SL", title: "money lost if the stop is hit (|entry − SL| × qty)",
      has: function (t) { return t.risk !== null; },
      cell: function (t) { return t.risk === null ? "—" : '<span class="bad">' + money(t.risk, t.cur) + "</span>"; } },
    { key: "booked", label: "Booked", title: "profit already booked on partial exits",
      has: function (t) { return !!t.booked; },
      cell: function (t) { return t.booked ? '<span class="' + pnlCls(t.booked) + '">' + money(t.booked, t.cur, true) + "</span>" : "—"; } },
    { key: "held", label: "Held", has: function (t) { return t.held != null && t.held !== ""; },
      cell: function (t) { return esc(t.held); } }
  ];

  T.CLOSED_COLUMNS = [
    { key: "closedOn", label: "Date", always: true,
      cell: function (t) { return stamp(t.exitDate || t.entryDate, t.exitTime) || "—"; } },
    { key: "symbol", label: "Asset / Symbol", always: true,
      cell: function (t) {
        return "<strong>" + esc(t.symbol) + "</strong>" +
          (t.market ? ' <span class="muted" style="font-size:0.72rem">' + esc(t.market) + "</span>" : "") +
          (t.detail ? '<div class="muted" style="font-size:0.72rem">' + esc(t.detail) + "</div>" : "");
      } },
    { key: "setup", label: "Setup", has: function (t) { return !!t.setup; }, cell: setupBadge },
    { key: "side", label: "Side", always: true, cell: sideBadge },
    { key: "qty", label: "Qty", always: true,
      cell: function (t) { return t.qty === null ? "—" : esc(t.qty.toLocaleString("en-IN")); } },
    { key: "entry", label: "Entry", always: true,
      cell: function (t) { return t.entryText != null ? esc(t.entryText) : price(t.entry, t.pcur); } },
    { key: "exit", label: "Exit", always: true,
      cell: function (t) { return price(t.exit, t.pcur); } },
    { key: "invested", label: "Invested", always: true,
      cell: function (t) { return t.invested === null ? "—" : money(t.invested, t.cur); } },
    { key: "pnl", label: "P&L", always: true,
      cell: function (t) { return t.pnl === null ? DASH : '<strong class="' + pnlCls(t.pnl) + '">' + money(t.pnl, t.cur, true) + "</strong>"; } },
    { key: "pnlPct", label: "P&L %", always: true,
      cell: function (t) { return t.pnlPct === null ? DASH : '<span class="' + pnlCls(t.pnlPct) + '">' + pct(t.pnlPct) + "</span>"; } },
    { key: "reason", label: "Reason", always: true, cell: function (t) { return esc(t.reason || "—"); } },
    { key: "held", label: "Held", has: function (t) { return t.held != null && t.held !== ""; },
      cell: function (t) { return esc(t.held); } },
    { key: "result", label: "", always: true,
      cell: function (t) {
        var win = (t.pnl || 0) > 0;
        return '<span class="badge ' + (win ? "badge-green" : "badge-red") + '">' + (win ? "WIN" : "LOSS") + "</span>";
      } }
  ];

  /* ---------- table builders (pure: return HTML) ---------- */
  // columns whose values should never wrap onto two lines
  var NOWRAP = { opened: 1, closedOn: 1, side: 1, setup: 1, qty: 1, entry: 1, ltp: 1, exit: 1, pnl: 1,
    pnlPct: 1, sl: 1, tgt: 1, invested: 1, risk: 1, booked: 1, held: 1, result: 1 };
  function td(c, t) {
    return "<td" + (NOWRAP[c.key] ? ' style="white-space:nowrap"' : "") + ">" + c.cell(t) + "</td>";
  }
  function visibleCols(cols, rows, opts) {
    var hide = (opts && opts.hide) || [];
    return cols.filter(function (c) {
      if (hide.indexOf(c.key) !== -1) return false;
      if (c.always) return true;
      return rows.some(function (t) { return c.has ? c.has(t) : true; });
    });
  }

  function totalsRow(cols, rows) {
    var byCur = {};
    rows.forEach(function (t) {
      var c = t.cur || "inr";
      var b = byCur[c] || (byCur[c] = { invested: 0, pnl: 0, anyPnl: false });
      b.invested += t.invested || 0;
      if (t.pnl !== null) { b.pnl += t.pnl; b.anyPnl = true; }
    });
    var curs = Object.keys(byCur);
    if (rows.length < 2) return "";
    function cell(key) {
      if (key === "symbol") return "<td><strong>Total (" + rows.length + ")</strong></td>";
      if (key === "invested") {
        return "<td>" + curs.map(function (c) { return money(byCur[c].invested, c); }).join(" + ") + "</td>";
      }
      if (key === "pnl") {
        return "<td>" + curs.map(function (c) {
          return byCur[c].anyPnl ? '<strong class="' + pnlCls(byCur[c].pnl) + '">' + money(byCur[c].pnl, c, true) + "</strong>" : "—";
        }).join(" + ") + "</td>";
      }
      return "<td></td>";
    }
    return '<tr class="tt-total" style="border-top:2px solid var(--border,#333)">' +
      cols.map(function (c) { return cell(c.key); }).join("") + "<td></td></tr>";
  }

  T.openHTML = function (rows, opts) {
    opts = opts || {};
    var cols = visibleCols(T.OPEN_COLUMNS, rows, opts);
    var head = cols.map(function (c) {
      return "<th" + (c.title ? ' title="' + esc(c.title) + '"' : "") + ">" + esc(c.label) + "</th>";
    }).join("") + "<th></th>";
    var body = rows.map(function (t, i) {
      var tds = cols.map(function (c) { return td(c, t); }).join("");
      var closeable = t.canClose !== false && t.status !== "PENDING" &&
        (typeof t.onClose === "function" || (t.desk && t.id != null));
      var act = (closeable
        ? '<button class="btn" style="padding:1px 8px;font-size:0.72rem" data-tt-close="' + i +
          '" title="close this trade' + (opts.closeHint ? " — " + esc(opts.closeHint) : "") + '">✕ Close</button>'
        : "") + (t.extra ? " " + t.extra : "");
      return "<tr>" + tds + '<td style="white-space:nowrap">' + act + "</td></tr>";
    }).join("");
    var out = "<table><thead><tr>" + head + "</tr></thead><tbody>" + body +
      (opts.noTotals ? "" : totalsRow(cols, rows)) + "</tbody></table>";
    return out + (opts.footer ? '<p class="hint" style="margin:0.5rem 0 0">' + opts.footer + "</p>" : "");
  };

  T.closedHTML = function (rows, opts, showAll) {
    opts = opts || {};
    var limit = opts.limit || T.config.closedLimit;
    var shown = showAll ? rows : rows.slice(0, limit);
    var cols = visibleCols(T.CLOSED_COLUMNS, shown, opts);
    var head = cols.map(function (c) { return "<th>" + esc(c.label) + "</th>"; }).join("") +
      (opts.deletable ? "<th></th>" : "");
    var body = shown.map(function (t, i) {
      var win = (t.pnl || 0) > 0;
      var tds = cols.map(function (c) { return td(c, t); }).join("");
      var del = opts.deletable
        ? '<td><button class="btn" data-tt-del="' + i + '" title="remove this entry from the book (wrong price, duplicate, misclick)">✖ Del</button></td>' : "";
      return '<tr class="' + (win ? "row-win" : "row-loss") + '">' + tds + del + "</tr>";
    }).join("");
    // quick stats line over ALL rows, not just the visible page
    var wins = rows.filter(function (t) { return (t.pnl || 0) > 0; }).length;
    var tot = {}; rows.forEach(function (t) { var c = t.cur || "inr"; tot[c] = (tot[c] || 0) + (t.pnl || 0); });
    var stats = '<p class="hint" style="margin:0 0 0.4rem">' + rows.length + " trades · " +
      Math.round(wins / rows.length * 100) + "% wins (" + wins + "W/" + (rows.length - wins) + "L) · net " +
      Object.keys(tot).map(function (c) {
        return '<strong class="' + pnlCls(tot[c]) + '">' + money(tot[c], c, true) + "</strong>";
      }).join(" + ") + "</p>";
    var more = rows.length > limit
      ? '<div style="padding:8px;text-align:center"><button class="btn" data-tt-more="1">' +
        (showAll ? "Show latest " + limit + " only" : "Show all " + rows.length + " trades") + "</button></div>" : "";
    return stats + "<table><thead><tr>" + head + "</tr></thead><tbody>" + body + "</tbody></table>" + more;
  };

  /* ---------- DOM wrappers ---------- */
  function bind(wrap) {
    if (wrap.__ttBound) return;
    wrap.__ttBound = true;
    wrap.addEventListener("click", function (e) {
      var el = e.target;
      while (el && el !== wrap && !(el.getAttribute && (el.getAttribute("data-tt-close") != null ||
             el.getAttribute("data-tt-del") != null || el.getAttribute("data-tt-more") != null))) el = el.parentNode;
      if (!el || el === wrap) return;
      var st = wrap.__tt || {};
      if (el.getAttribute("data-tt-more") != null) {
        st.showAll = !st.showAll; wrap.innerHTML = T.closedHTML(st.rows, st.opts, st.showAll); return;
      }
      var closeIdx = el.getAttribute("data-tt-close");
      if (closeIdx != null) {
        var t = st.rows[Number(closeIdx)];
        if (!t) return;
        if (typeof t.onClose === "function") t.onClose(t);
        else if (window.requestDeskClose) window.requestDeskClose(t.desk, t.id, t.symbol);
        return;
      }
      var delIdx = el.getAttribute("data-tt-del");
      var d = st.rows[Number(delIdx)];
      if (d && typeof d.onDelete === "function") d.onDelete(d);
    });
  }

  T.open = function (wrap, rawRows, opts) {
    if (!wrap) return;
    opts = opts || {};
    var rows = (rawRows || []).map(function (r) { return T.normalize(r); });
    if (opts.desk) rows.forEach(function (t) { if (!t.desk) t.desk = opts.desk; });
    bind(wrap);
    wrap.__tt = { rows: rows, opts: opts };
    wrap.innerHTML = rows.length ? T.openHTML(rows, opts)
      : '<div class="flash">' + (opts.empty || "No open positions right now.") + "</div>";
    return rows;
  };

  T.closed = function (wrap, rawRows, opts) {
    if (!wrap) return;
    opts = opts || {};
    var rows = (rawRows || []).map(function (r) { return T.normalize(r); });
    bind(wrap);
    var keep = wrap.__tt && wrap.__tt.showAll;
    wrap.__tt = { rows: rows, opts: opts, showAll: !!keep };
    wrap.innerHTML = rows.length ? T.closedHTML(rows, opts, !!keep)
      : '<div class="flash">' + (opts.empty || "No closed trades yet.") + "</div>";
    return rows;
  };

  /* ===========================================================================
   * ADAPTERS — map each desk's raw engine row onto the common trade shape.
   * ctx.ltp is an optional {SYMBOL: price} map of live quotes.
   * ======================================================================== */
  var A = (T.adapt = {});

  // Intraday O=L / O=H / 3-Candle (data/paper_ol.json)
  A.ol = function (t) {
    return { id: t.id, desk: "ol", symbol: t.symbol, setup: t.setup, side: t.side, qty: t.qty,
      entryDate: t.date, entryTime: t.entry_time, entry: t.entry_price, ltp: t.ltp,
      sl: t.sl_price, tgt: t.tgt_price != null ? t.tgt_price : t.tgt,
      exit: t.exit_price, exitDate: t.exit_price != null ? t.date : undefined,
      exitTime: t.exit_time, reason: t.reason,
      pnl: t.pnl, pnlPct: t.pnl_pct };
  };

  // Gold / BTC desk (data/paper_gc.json) — USD
  A.gc = function (t) {
    var desk = String(t.setup || "").indexOf("DESK") === 0;
    return { id: t.id, desk: "gc", symbol: t.symbol, setup: t.setup, side: t.side, qty: t.qty,
      entryTime: t.entry_time, entry: t.entry_price, ltp: t.ltp, sl: t.sl_price, tgt: t.tgt_price,
      exit: t.exit_price, exitDate: t.exit_price != null ? t.session : undefined,
      exitTime: t.exit_time, reason: t.reason,
      pnl: t.pnl, cur: "usd", detail: desk ? "premium desk (entry = credit)" : "" };
  };

  // HM BTC/Gold setup scanner (hm-btc-gold-latest.json) — capital in INR
  A.hmb = function (t) {
    return { symbol: t.asset, detail: t.frame, setup: t.setup, side: t.side,
      entry: t.entry_price, ltp: t.mark, entryDate: t.entry_date, exit: t.exit_price,
      exitDate: t.exit_date, reason: t.exit_reason, invested: t.capital,
      pnl: t.pnl_inr, pnlPct: t.pnl_pct, cur: "inr", pcur: "usd", canClose: false };
  };

  // HM setups currently open on the Gold/BTC tab (hm-btc-gold-latest.json) — USD prices
  A.hmgc = function (o) {
    return { symbol: o.asset, detail: o.frame, setup: o.setup, side: o.side,
      qty: o.lots, entry: o.entry_price, entryDate: o.entry_date,
      sl: o.stop_ref === "" ? null : o.stop_ref, cur: "usd", canClose: false };
  };

  // Intraday HM (htf-hm-latest.json)
  A.hm = function (t) {
    var open = t.status === "OPEN";
    var setup = String(t.setup || "");
    return { id: t.id, desk: "hm", symbol: t.symbol, setup: setup,
      side: t.side || (setup.indexOf("BUY") >= 0 ? "BUY" : "SELL"),
      qty: t.qty, entryDate: t.entry_date, entryTime: t.entry_time, entry: t.entry,
      ltp: t.ltp, sl: t.sl, tgt: t.tgt, invested: t.invested,
      pnl: open ? t.live_pnl : t.pnl, pnlPct: open ? t.live_pct : undefined,
      exit: open ? null : t.exit, exitDate: t.exit_date || undefined, exitTime: t.exit_time || undefined,
      reason: t.reason,
      detail: (t.htf_weekly || t.htf_daily)
        ? "HTF W/D: " + String(t.htf_weekly || "—").replace("-MOMENTUM", "") + " / " + String(t.htf_daily || "—").replace("-MOMENTUM", "") : "",
      held: t.weeks_held != null ? t.weeks_held + "w" : "" };
  };

  // QM swing (paper_qm.json). ctx.ltp = live NSE quotes
  A.qm = function (t, ctx) {
    var live = ctx && ctx.ltp ? Number(ctx.ltp[t.symbol]) : NaN;
    var hasLive = isFinite(live) && live > 0;
    var open = t.status === "OPEN";
    var pnl = t.pnl;
    if (open && t.entry != null) {
      if (hasLive) pnl = (t.booked || 0) + (t.qty || 0) * (live - t.entry);
      else if (pnl == null && t.mark != null) pnl = (t.booked || 0) + (t.qty || 0) * (t.mark - t.entry);
    }
    return { id: t.id, desk: "qm", symbol: t.symbol,
      status: t.status === "PENDING" ? "PENDING" : "OPEN",
      detail: (t.status === "PENDING" && t.note) ? t.note
        : ((t.trail ? "trailing stop" : "") + (t.avg_count ? " (averaged)" : "")).trim(),
      side: t.side || "BUY", qty: t.qty, entry: t.entry,
      entryDate: t.entry_date || t.signal_date,
      ltp: hasLive ? live : (t.mark != null ? t.mark : null),
      booked: t.booked, invested: t.invested, sl: t.sl, tgt: t.tgt, pnl: pnl,
      exit: t.exit, exitDate: t.exit_date, reason: t.reason,
      held: t.sessions != null ? t.sessions + "d" : "" };
  };

  // HM Swing (swing_picks.json): NSE in INR, others USD
  A.swing = function (t, ctx) {
    var nse = t.market === "NSE";
    var live = nse && ctx && ctx.ltp ? Number(ctx.ltp[t.symbol]) : NaN;
    var hasLive = isFinite(live) && live > 0;
    var pnl = t.pnl;
    if (t.status === "OPEN" && nse && t.entry != null && hasLive) {
      pnl = (t.qty || 0) * (t.side === "SELL" ? t.entry - live : live - t.entry);
    }
    return { id: t.id, desk: "swing", symbol: t.symbol, market: t.market, setup: t.setup,
      side: t.side, status: t.status === "PENDING" ? "PENDING" : "OPEN", qty: t.qty,
      entry: t.entry, entryDate: t.entry_date || t.signal_date,
      ltp: hasLive ? live : (t.mark != null ? t.mark : null),
      sl: t.sl, tgt: t.tgt, exit: t.exit, exitDate: t.exit_date, reason: t.reason,
      pnl: pnl, cur: nse ? "inr" : "usd",
      held: t.sessions != null ? t.sessions + "d" : "" };
  };

  // Manual book (browser localStorage). ctx.ltp = {SYMBOL: {ltp}}, ctx.optMark(t)
  A.manual = function (t, ctx) {
    var q = ctx && ctx.ltp && ctx.ltp[t.symbol];
    var l = q ? parseFloat(q.ltp) : null;
    if ((l === null || !isFinite(l)) && ctx && ctx.optMark) l = ctx.optMark(t);
    return { id: t.id, desk: "manual", symbol: t.symbol, side: t.side || "BUY", qty: t.quantity,
      entry: t.entry_price, entryDate: t.entry_date, ltp: l, sl: t.sl_price || null,
      tgt: t.tgt_price || null, invested: t.invested, exit: t.exit_price, exitDate: t.exit_date,
      reason: t.exit_reason, pnl: t.pnl,
      onClose: ctx && ctx.onClose, onDelete: ctx && ctx.onDelete };
  };

  // Credit-spread desks: NIFTY Theta ('theta') and Premium Mill ('mill')
  A.spread = function (t, ctx) {
    var desk = (ctx && ctx.desk) || "theta";
    var qty = Number(t.qty || t.lot) || 75;
    var cr = Number(t.net_credit);
    var width = Math.abs(Number(t.short_strike) - Number(t.long_strike));
    var maxLoss = (isFinite(width) && isFinite(cr)) ? Math.max(width - cr, 0) * qty : null;
    var closed = t.status === "CLOSED";
    return { id: "open", desk: desk, symbol: "NIFTY " + t.short_strike + "/" + t.long_strike + " " + (t.opt_type || ""),
      detail: "exp " + (t.expiry || "—") + (t.dte != null ? " (" + t.dte + "d)" : "") +
        " · spot " + (t.spot_entry != null ? t.spot_entry : "—") + " · VIX " + (t.vix_entry != null ? t.vix_entry : "—"),
      setup: t.direction, side: "SELL", qty: qty, entry: cr, entryDate: t.entry_date,
      ltp: null, pnl: closed ? t.pnl : t.live_pnl, invested: maxLoss,
      slText: ctx && ctx.slRule ? ctx.slRule(t) : null,
      tgtText: ctx && ctx.tgtRule ? ctx.tgtRule(t, qty) : null,
      exitDate: t.exit_date, reason: t.exit_reason,
      exit: null, held: t.holding_days != null ? t.holding_days + "d" : "" };
  };

  // NIFTY Long-Term Premium (paper_nifty_ltp.json): one structure open at a time
  A.ltpOpen = function (t) {
    var f = t.far || {}, call = t.side === "call";
    return { desk: "ltp", symbol: "NIFTY " + f.strike + (call ? " CE" : " PE") + " (far leg)",
      detail: "exp " + (f.expiry || "—") + " · day " + (t.days != null ? t.days : "—") + " of 4 · " +
        (t.points >= 0 ? "+" : "−") + Math.abs(t.points || 0) + " pts · " + ((t.shorts || []).length) + " short leg(s)",
      setup: call ? "CALL" : "PUT", side: "BUY", qty: f.qty, entry: f.entry, ltp: f.mark,
      pnl: t.pnl != null ? t.pnl : ((t.points != null && f.qty) ? t.points * f.qty : null), slText: "−70 pts", tgtText: "+70 pts", canClose: false };
  };
  A.ltpClosed = function (t) {
    return { symbol: "NIFTY Long-Term Premium", setup: t.side === "call" ? "CALL" : "PUT", side: "BUY",
      detail: "discount at entry " + Math.round((t.discount_at_entry || 0) * 100) + "% · " +
        (t.points >= 0 ? "+" : "−") + Math.abs(t.points || 0) + " pts",
      entryDate: t.entry, exitDate: t.exit, reason: t.reason, pnl: t.pnl };
  };

  // BTC Theta (paper_btc_theta.json): USD marks, INR closed P&L
  A.btcOpen = function (t) {
    var k = t.strikes || {}, lot = Number(t.lot);
    var cal = t.structure === "weekly-cal", call = t.side === "call";
    var mark = Number(t.mark_usd);
    return { id: t.id, desk: "btc", symbol: cal ? "BTC " + (call ? "call" : "put") + " weekly calendar" : "BTC iron condor",
      detail: cal ? "strike " + k.k + (call ? "C" : "P") + " · exp " + t.expiry + " ⇢ fence " + t.hedge_expiry
                  : k.sc + "C / " + k.sp + "P · wings " + k.wc + "C / " + k.wp + "P · exp " + t.expiry,
      side: "SELL", qty: lot, entryDate: t.entry_at,
      entry: cal ? t.hawa_usd : t.credit_usd,
      ltp: t.spot_now != null ? t.spot_now : null,
      pnl: isFinite(mark) && isFinite(lot) ? mark * lot : null, cur: "usd",
      slText: cal ? "none (risk pre-paid)" : "wings cap loss",
      entryText: undefined };
  };
  A.btcClosed = function (t) {
    var k = t.strikes || {};
    return { symbol: "BTC iron condor", detail: k.sc + "C / " + k.sp + "P",
      side: "SELL", entryDate: t.entry_at, entry: t.credit_usd, reason: t.reason,
      pnl: t.pnl_inr, cur: "inr", pcur: "usd" };
  };

  // Future Arbitrage calendar spreads (paper_future_arb.json)
  A.arb = function (t) {
    return { symbol: t.symbol, setup: "CAL SPREAD", side: "BUY",
      detail: "BUY " + t.current_expiry + " @ " + t.entry_current + " / SELL " + t.next_expiry + " @ " + t.entry_next,
      qty: t.lot, entry: t.spread, entryDate: t.entry_date, invested: t.spread_value,
      exitDate: t.exit_date, reason: String(t.reason || "").replace("SL_current_future_matched_or_crossed_spot", "convergence"),
      pnl: t.pnl, canClose: false,
      tgtText: "basis converges", slText: "—" };
  };

  /* expose helpers that adapters/tabs may want */
  T.fmt = { price: price, money: money, pct: pct, esc: esc };
})();
