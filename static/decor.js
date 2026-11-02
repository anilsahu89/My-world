/* decor.js — candlestick side-rails for the portal (added 2 Nov 2026).
   On wide screens the blank margins left/right of the 1100px content
   column get faint price-chart decorations: a seeded random-walk of
   candles with classic patterns folded in (engulfing, hammer, doji,
   morning/evening star, three soldiers/crows, trend runs), a soft
   trendline, dotted grid, and tiny pattern captions.
   Pure decoration: pointer-events none, aria-hidden, zero layout impact,
   hidden below 1420px viewport width, theme-aware via CSS variables.
   Undo: git revert <commit>. */
(function () {
  "use strict";
  var RAIL_W = 120, SLOT = 15, BODY = 9, MIN_VP = 1420, GEN_H = 2400;

  function hash(s) { var h = 7; for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return Math.abs(h) + 1; }
  function rng(seed) { return function () { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; }; }

  /* candle helpers in 0..100 price space */
  function C(o, c, h, l) { return { o: o, c: c, h: h == null ? Math.max(o, c) : h, l: l == null ? Math.min(o, c) : l }; }
  var PATTERNS = [
    { name: "bull engulf", n: 2, make: function (p, r) { var o1 = p - 3 - r() * 2; return [C(p, p - 4 - r() * 2, p + 1, o1 - 1), C(o1 - 1, o1 + 6 + r() * 3, o1 + 7.5, o1 - 2)]; } },
    { name: "bear engulf", n: 2, make: function (p, r) { var o1 = p + 3 + r() * 2; return [C(p, p + 4 + r() * 2, p + 1.5, p - 1), C(o1 + 1, o1 - 6 - r() * 3, o1 + 2, o1 - 7.5)]; } },
    { name: "hammer", n: 1, make: function (p, r) { var b = p + r() * 2; return [C(b + 1.5, b + 2.2, b + 3, b - 9 - r() * 3)]; } },
    { name: "doji", n: 1, make: function (p, r) { var m = p + (r() - 0.5) * 2; return [C(m, m + 0.3, m + 2.5, m - 2.5)]; } },
    { name: "morning star", n: 3, make: function (p, r) { return [C(p + 1, p - 5, p + 2, p - 6), C(p - 6, p - 6.4, p - 5, p - 9), C(p - 7, p - 1.5, p - 0.5, p - 8)]; } },
    { name: "evening star", n: 3, make: function (p, r) { return [C(p - 1, p + 5, p + 6, p - 2), C(p + 6, p + 6.4, p + 9, p + 5), C(p + 7, p + 1.5, p + 8, p + 0.5)]; } },
    { name: "3 soldiers", n: 3, make: function (p, r) { return [C(p - 1, p + 3, p + 4, p - 2), C(p + 3, p + 7, p + 8, p + 2), C(p + 7, p + 11, p + 12, p + 6)]; } },
    { name: "3 crows", n: 3, make: function (p, r) { return [C(p + 1, p - 3, p + 2, p - 4), C(p - 3, p - 7, p - 2, p - 8), C(p - 7, p - 11, p - 6, p - 12)]; } },
    { name: "uptrend", n: 4, make: function (p, r) { var out = [], x = p; for (var i = 0; i < 4; i++) { out.push(C(x, x + 2.5 + r() * 2, x + 4 + r() * 2, x - 1)); x += 3 + r() * 2; } return out; } },
    { name: "downtrend", n: 4, make: function (p, r) { var out = [], x = p; for (var i = 0; i < 4; i++) { out.push(C(x, x - 2.5 - r() * 2, x + 1, x - 4 - r() * 2)); x -= 3 + r() * 2; } return out; } }
  ];

  function buildSeries(seed) {
    var r = rng(seed), out = [], p = 55, guard = 0;
    while (out.length < 90 && guard++ < 200) {
      var pat = PATTERNS[Math.floor(r() * PATTERNS.length)];
      var cs = pat.make(p, r);
      cs.forEach(function (c) { out.push({ c: c, name: pat.name }); });
      p = cs[cs.length - 1].c;
      if (p < 22) p = 22 + r() * 10;
      if (p > 78) p = 78 - r() * 10;
      /* occasional plain drift candle */
      if (r() < 0.35) { var d = C(p, p + (r() - 0.5) * 3, 0, 0); out.push({ c: d, name: "" }); p = d.c; }
    }
    return out;
  }

  function railSVG(seed, w, h) {
    var series = buildSeries(seed);
    var lo = 100, hi = 0;
    series.forEach(function (s) { lo = Math.min(lo, s.c.l); hi = Math.max(hi, s.c.h); });
    var pad = 6;
    function Y(v) { return pad + (hi - v) / (hi - lo) * (h - pad * 2); }
    var cx = w / 2, s = '<svg class="decor-svg" width="' + w + '" height="' + h +
      '" viewBox="0 0 ' + w + " " + h + '" aria-hidden="true" focusable="false">';
    /* dotted grid */
    for (var gy = pad; gy < h - pad; gy += 46)
      s += '<line x1="6" x2="' + (w - 6) + '" y1="' + gy + '" y2="' + gy + '" stroke="var(--border)" stroke-width="1" stroke-dasharray="2 6" opacity="0.7"/>';
    /* soft trendline behind the candles */
    var pts = series.filter(function (_, i) { return i % 4 === 0; })
      .map(function (s2, i) { return (cx + (i % 2 ? 18 : -18)).toFixed(1) + "," + Y(s2.c.c).toFixed(1); }).join(" ");
    s += '<polyline points="' + pts + '" fill="none" stroke="var(--accent)" stroke-width="1.2" opacity="0.16"/>';
    /* candles */
    var cap = "";
    series.forEach(function (s2, i) {
      var yO = Y(s2.c.o), yC = Y(s2.c.c), yH = Y(s2.c.h), yL = Y(s2.c.l);
      var up = s2.c.c >= s2.c.o;
      var col = up ? "var(--green)" : "var(--red)";
      var x = cx - BODY / 2 + (i % 3 - 1) * 4;
      var top = Math.min(yO, yC), bh = Math.max(1.6, Math.abs(yO - yC));
      s += '<line x1="' + (x + BODY / 2) + '" x2="' + (x + BODY / 2) + '" y1="' + yH.toFixed(1) + '" y2="' + yL.toFixed(1) +
        '" stroke="' + col + '" stroke-width="1" opacity="0.34"/>';
      s += '<rect x="' + x.toFixed(1) + '" y="' + top.toFixed(1) + '" width="' + BODY + '" height="' + bh.toFixed(1) +
        '" rx="1.5" fill="' + col + '" opacity="0.30"/>';
      if (s2.name && i % 7 === 0)
        cap += '<text x="' + (w - 4) + '" y="' + (yL + 12).toFixed(1) + '" text-anchor="end" font-size="8.5" font-style="italic" fill="var(--muted)" opacity="0.75">' + s2.name + "</text>";
    });
    s += cap + "</svg>";
    return s;
  }

  function mount() {
    if (document.getElementById("decor-rails")) return;
    var host = document.createElement("div");
    host.id = "decor-rails";
    host.setAttribute("aria-hidden", "true");
    var st = document.createElement("style");
    st.textContent =
      "#decor-rails{position:fixed;inset:0;pointer-events:none;z-index:0;display:none;overflow:hidden}" +
      "@media (min-width:" + MIN_VP + "px){#decor-rails{display:block}}" +
      ".decor-rail{position:absolute;top:0;bottom:0;width:" + RAIL_W + "px;opacity:.6;" +
      "-webkit-mask-image:linear-gradient(180deg,transparent,#000 7%,#000 93%,transparent);" +
      "mask-image:linear-gradient(180deg,transparent,#000 7%,#000 93%,transparent)}" +
      ".decor-rail.l{left:16px}.decor-rail.r{right:16px}" +
      ".decor-rail .decor-svg{display:block}";
    host.appendChild(st);
    var l = document.createElement("div");
    l.className = "decor-rail l";
    l.innerHTML = railSVG(hash(location.pathname + "-L"), RAIL_W, GEN_H);
    var rt = document.createElement("div");
    rt.className = "decor-rail r";
    rt.innerHTML = railSVG(hash(location.pathname + "-R"), RAIL_W, GEN_H);
    host.appendChild(l);
    host.appendChild(rt);
    document.body.insertBefore(host, document.body.firstChild);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();
