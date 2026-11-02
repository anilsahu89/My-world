/* decor.js v2 — interactive candlestick side-rails for the portal.
   On wide screens the blank margins beside the 1100px content column show
   faint price-chart columns: seeded random-walk candles with classic
   patterns folded in (engulfing, hammer, doji, morning/evening star,
   three soldiers/crows, trend runs), soft trendline, dotted grid, and
   pattern captions.
   v2: candles read clearly at rest, and hovering a pattern brightens it
   and shows a tooltip card — pattern name, meaning, and its OHLC.
   Pure decoration: pointer-transparent except the rails themselves,
   aria-hidden, zero layout impact, hidden below 1420px, theme-aware.
   Undo: git revert <commit>. */
(function () {
  "use strict";
  var RAIL_W = 124, SLOT = 15, BODY = 11, MIN_VP = 1420, GEN_H = 2400;

  function hash(s) { var h = 7; for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return Math.abs(h) + 1; }
  function rng(seed) { return function () { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; }; }
  function px(v) { return (v * 7.35).toFixed(2); }

  function C(o, c, h, l) { return { o: o, c: c, h: h == null ? Math.max(o, c) : h, l: l == null ? Math.min(o, c) : l }; }
  var PATTERNS = [
    { name: "Bullish engulfing", desc: "buyers take control", n: 2, make: function (p, r) { var o1 = p - 3 - r() * 2; return [C(p, p - 4 - r() * 2, p + 1, o1 - 1), C(o1 - 1, o1 + 6 + r() * 3, o1 + 7.5, o1 - 2)]; } },
    { name: "Bearish engulfing", desc: "sellers take control", n: 2, make: function (p, r) { var o1 = p + 3 + r() * 2; return [C(p, p + 4 + r() * 2, p + 1.5, p - 1), C(o1 + 1, o1 - 6 - r() * 3, o1 + 2, o1 - 7.5)]; } },
    { name: "Hammer", desc: "rejection of the lows", n: 1, make: function (p, r) { var b = p + r() * 2; return [C(b + 1.5, b + 2.2, b + 3, b - 9 - r() * 3)]; } },
    { name: "Doji", desc: "indecision", n: 1, make: function (p, r) { var m = p + (r() - 0.5) * 2; return [C(m, m + 0.3, m + 2.5, m - 2.5)]; } },
    { name: "Morning star", desc: "bottom reversal", n: 3, make: function (p, r) { return [C(p + 1, p - 5, p + 2, p - 6), C(p - 6, p - 6.4, p - 5, p - 9), C(p - 7, p - 1.5, p - 0.5, p - 8)]; } },
    { name: "Evening star", desc: "top reversal", n: 3, make: function (p, r) { return [C(p - 1, p + 5, p + 6, p - 2), C(p + 6, p + 6.4, p + 9, p + 5), C(p + 7, p + 1.5, p + 8, p + 0.5)]; } },
    { name: "Three white soldiers", desc: "strong uptrend", n: 3, make: function (p, r) { return [C(p - 1, p + 3, p + 4, p - 2), C(p + 3, p + 7, p + 8, p + 2), C(p + 7, p + 11, p + 12, p + 6)]; } },
    { name: "Three black crows", desc: "strong downtrend", n: 3, make: function (p, r) { return [C(p + 1, p - 3, p + 2, p - 4), C(p - 3, p - 7, p - 2, p - 8), C(p - 7, p - 11, p - 6, p - 12)]; } },
    { name: "Uptrend run", desc: "higher highs, higher lows", n: 4, make: function (p, r) { var out = [], x = p; for (var i = 0; i < 4; i++) { out.push(C(x, x + 2.5 + r() * 2, x + 4 + r() * 2, x - 1)); x += 3 + r() * 2; } return out; } },
    { name: "Downtrend run", desc: "lower highs, lower lows", n: 4, make: function (p, r) { var out = [], x = p; for (var i = 0; i < 4; i++) { out.push(C(x, x - 2.5 - r() * 2, x + 1, x - 4 - r() * 2)); x -= 3 + r() * 2; } return out; } }
  ];

  function buildSeries(seed) {
    var r = rng(seed), out = [], p = 55, guard = 0;
    while (out.length < 90 && guard++ < 200) {
      var pat = PATTERNS[Math.floor(r() * PATTERNS.length)];
      var cs = pat.make(p, r);
      out.push({ candles: cs, name: pat.name, desc: pat.desc });
      p = cs[cs.length - 1].c;
      if (p < 22) p = 22 + r() * 10;
      if (p > 78) p = 78 - r() * 10;
      if (r() < 0.35) { var d = C(p, p + (r() - 0.5) * 3, 0, 0); out.push({ candles: [d], name: "", desc: "" }); p = d.c; }
    }
    return out;
  }

  function railSVG(seed, w, h, railId) {
    var series = buildSeries(seed);
    var lo = 100, hi = 0;
    series.forEach(function (g) { g.candles.forEach(function (c) { lo = Math.min(lo, c.l); hi = Math.max(hi, c.h); }); });
    var pad = 6;
    function Y(v) { return pad + (hi - v) / (hi - lo) * (h - pad * 2); }
    var cx = w / 2, s = '<svg class="decor-svg" width="' + w + '" height="' + h +
      '" viewBox="0 0 ' + w + " " + h + '" aria-hidden="true" focusable="false">';
    for (var gy = pad; gy < h - pad; gy += 46)
      s += '<line x1="6" x2="' + (w - 6) + '" y1="' + gy + '" y2="' + gy + '" stroke="var(--border)" stroke-width="1" stroke-dasharray="2 6" opacity="0.9"/>';
    var flat = [];
    series.forEach(function (g) { g.candles.forEach(function (c) { flat.push(c.c); }); });
    var pts = flat.filter(function (_, i) { return i % 3 === 0; })
      .map(function (v, i) { return (cx + (i % 2 ? 20 : -20)).toFixed(1) + "," + Y(v).toFixed(1); }).join(" ");
    s += '<polyline points="' + pts + '" fill="none" stroke="var(--accent)" stroke-width="1.2" opacity="0.2"/>';
    var idx = 0;
    series.forEach(function (g) {
      var inner = "", yMin = 1e9, yMax = -1e9;
      g.candles.forEach(function (c, j) {
        var yO = Y(c.o), yC = Y(c.c), yH = Y(c.h), yL = Y(c.l);
        var up = c.c >= c.o;
        var col = up ? "var(--green)" : "var(--red)";
        var x = cx - BODY / 2 + (idx % 3 - 1) * 3;
        var top = Math.min(yO, yC), bh = Math.max(2, Math.abs(yO - yC));
        yMin = Math.min(yMin, yH); yMax = Math.max(yMax, yL);
        inner += '<line class="wk" x1="' + (x + BODY / 2) + '" x2="' + (x + BODY / 2) + '" y1="' + yH.toFixed(1) + '" y2="' + yL.toFixed(1) +
          '" stroke="' + col + '" stroke-width="1.2" opacity="0.55"/>';
        inner += '<rect class="bc" x="' + x.toFixed(1) + '" y="' + top.toFixed(1) + '" width="' + BODY + '" height="' + bh.toFixed(1) +
          '" rx="1.5" fill="' + col + '" opacity="0.55"/>';
        idx++;
      });
      if (g.name) {
        var hitTop = Math.max(0, yMin - 8), hitH = Math.max(18, yMax - yMin + 16);
        inner += '<rect class="hit" x="2" y="' + hitTop.toFixed(1) + '" width="' + (w - 4) + '" height="' + hitH.toFixed(1) + '" fill="transparent"/>';
        s += '<g class="decor-pat" data-rail="' + railId + '" data-name="' + g.name + '" data-desc="' + g.desc + '" data-ohlc=\'' +
          JSON.stringify(g.candles) + '\'>' + inner + "</g>";
      } else {
        s += "<g>" + inner + "</g>";
      }
      if (g.name && idx % 9 < 5) {
        s += '<text x="' + (w - 3) + '" y="' + (yMax + 13).toFixed(1) + '" text-anchor="end" font-size="8.5" font-style="italic" fill="var(--muted)" opacity="0.9">' +
          g.name.toLowerCase() + "</text>";
      }
    });
    s += "</svg>";
    return s;
  }

  var CSS =
    "#decor-rails{position:fixed;inset:0;pointer-events:none;z-index:0;display:none;overflow:hidden}" +
    "@media (min-width:" + MIN_VP + "px){#decor-rails{display:block}}" +
    ".decor-rail{position:absolute;top:0;bottom:0;width:" + RAIL_W + "px;opacity:.85;pointer-events:auto;" +
    "-webkit-mask-image:linear-gradient(180deg,transparent,#000 7%,#000 93%,transparent);" +
    "mask-image:linear-gradient(180deg,transparent,#000 7%,#000 93%,transparent)}" +
    ".decor-rail.l{left:14px}.decor-rail.r{right:14px}" +
    ".decor-rail .decor-svg{display:block}" +
    ".decor-pat .hit{cursor:crosshair}" +
    ".decor-pat.hover .bc{opacity:1}" +
    ".decor-pat.hover .wk{opacity:.95}" +
    ".decor-tip{position:absolute;width:186px;background:var(--panel);color:var(--text);" +
    "border:1px solid var(--border);border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.18);" +
    "padding:8px 10px;font-size:11px;line-height:1.35;pointer-events:none;display:none;z-index:6}" +
    ".decor-tip b{display:block;font-size:11.5px;margin-bottom:1px}" +
    ".decor-tip .desc{color:var(--muted);font-size:10px;margin-bottom:5px}" +
    ".decor-tip table{width:100%;border-collapse:collapse}" +
    ".decor-tip td{padding:0 2px;color:var(--muted)}" +
    ".decor-tip td.v{text-align:right;color:var(--text);font-variant-numeric:tabular-nums}" +
    ".decor-tip .up{color:var(--green);font-weight:600}.decor-tip .dn{color:var(--red);font-weight:600}";

  function tipHTML(g) {
    var rows = "";
    g.candles.forEach(function (c, i) {
      var up = c.c >= c.o;
      rows += "<tr><td>O</td><td class='v'>" + px(c.o) + "</td><td>H</td><td class='v'>" + px(c.h) + "</td></tr>" +
        "<tr><td>L</td><td class='v'>" + px(c.l) + "</td><td>C</td><td class='v " + (up ? "up" : "dn") + "'>" + px(c.c) + "</td></tr>";
    });
    return "<b>" + g.name + "</b><span class='desc'>" + g.desc + " · hover out to hide</span><table>" + rows + "</table>";
  }

  function wireTip(rail, side) {
    var tip = document.createElement("div");
    tip.className = "decor-tip";
    rail.appendChild(tip);
    var groups = rail.querySelectorAll("g.decor-pat");
    Array.prototype.forEach.call(groups, function (g) {
      g.addEventListener("pointerenter", function () {
        g.classList.add("hover");
        var bb = g.getBBox();
        var railH = rail.clientHeight;
        var top = Math.max(4, Math.min(bb.y - rail.clientHeight / 2 + railH / 2, railH - 160));
        top = Math.max(4, Math.min(bb.y + rail.scrollTop - 60, railH - 170));
        tip.innerHTML = tipHTML({ name: g.getAttribute("data-name"), desc: g.getAttribute("data-desc"), candles: JSON.parse(g.getAttribute("data-ohlc")) });
        tip.style.display = "block";
        tip.style.top = top.toFixed(0) + "px";
        if (side === "l") tip.style.left = (RAIL_W + 10) + "px";
        else { tip.style.left = "auto"; tip.style.right = (RAIL_W + 10) + "px"; }
      });
      g.addEventListener("pointerleave", function () {
        g.classList.remove("hover");
        tip.style.display = "none";
      });
    });
  }

  function mount() {
    if (document.getElementById("decor-rails")) return;
    var host = document.createElement("div");
    host.id = "decor-rails";
    host.setAttribute("aria-hidden", "true");
    var st = document.createElement("style");
    st.textContent = CSS;
    host.appendChild(st);
    var l = document.createElement("div");
    l.className = "decor-rail l";
    l.innerHTML = railSVG(hash(location.pathname + "-L"), RAIL_W, GEN_H, "l");
    var rt = document.createElement("div");
    rt.className = "decor-rail r";
    rt.innerHTML = railSVG(hash(location.pathname + "-R"), RAIL_W, GEN_H, "r");
    host.appendChild(l);
    host.appendChild(rt);
    document.body.insertBefore(host, document.body.firstChild);
    wireTip(l, "l");
    wireTip(rt, "r");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
