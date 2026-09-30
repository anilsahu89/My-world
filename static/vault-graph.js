/* ===========================================================================
 * Vault Globe — 3D map of the vault's [[wikilink]] network (dashboard page)
 * ---------------------------------------------------------------------------
 * Reads data/graph.json (built by build.py) and renders every note as a node
 * on a rotating globe; arcs between nodes are the wikilinks. Pure canvas,
 * zero libraries, zero network calls beyond the one JSON fetch.
 *
 *   drag            spin the globe (with inertia)
 *   scroll / pinch  zoom
 *   hover           trace a topic's connections
 *   click           open the note
 *   legend chips    show/hide a category
 *   search box      highlight notes by name
 *
 * SELF-CONTAINED FEATURE — to remove the globe from the site, delete:
 *   1. this file
 *   2. the <section id="vaultGraphSection"> + script tag in
 *      templates/dashboard.html
 *   3. the graph.json writer in build.py (or set VAULT_GRAPH_ENABLED=False)
 * Nothing else references any of these.
 * =========================================================================== */
(function () {
  "use strict";

  var section = document.getElementById("vaultGraphSection");
  if (!section) return;
  var canvas = document.getElementById("vaultGraphCanvas");
  var stage = document.getElementById("vaultGraphStage");
  var statusEl = document.getElementById("vgStatus");
  var statsEl = document.getElementById("vgStats");
  var tipEl = document.getElementById("vgTip");
  if (!canvas || !stage || !canvas.getContext) return;
  var ctx = canvas.getContext("2d");

  /* --- site base path (github.io subpath) — same trick app.js uses ------ */
  function siteBase() {
    var parts = window.location.pathname.replace(/^\/+/, "").split("/");
    if (parts.length > 1 && parts[0]) return "/" + parts[0];
    return "";
  }

  /* --- palette ----------------------------------------------------------- */
  var CAT_COLORS = {
    "Concept": [79, 156, 249],     // blue
    "Strategy": [63, 185, 80],     // green
    "Source": [240, 168, 96],      // orange
    "Trade Review": [180, 142, 240] // purple
  };
  var DEFAULT_COLOR = [139, 147, 167];
  function catColor(cat) { return CAT_COLORS[cat] || DEFAULT_COLOR; }
  function rgba(c, a) { return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")"; }
  function mix(c1, c2, t) {
    return [Math.round(c1[0] + (c2[0] - c1[0]) * t),
            Math.round(c1[1] + (c2[1] - c1[1]) * t),
            Math.round(c1[2] + (c2[2] - c1[2]) * t)];
  }

  /* --- scoped stylesheet (injected so the file is fully self-contained) -- */
  var css =
    "#vaultGraphSection{margin:0 0 1.8rem}" +
    "#vaultGraphSection .vg-head h2{margin:1.2rem 0 .15rem}" +
    ".vg-stage{position:relative;height:clamp(420px,52vw,560px);border:1px solid var(--border,#2a2f3a);" +
      "border-radius:12px;overflow:hidden;touch-action:none;user-select:none;-webkit-user-select:none;" +
      "background:radial-gradient(120% 120% at 50% 42%,#141a28 0%,#0f1115 60%,#0b0d12 100%)}" +
    "#vaultGraphCanvas{position:absolute;inset:0;width:100%;height:100%;display:block;cursor:grab}" +
    ".vg-stage.dragging #vaultGraphCanvas{cursor:grabbing}" +
    ".vg-stage.picking #vaultGraphCanvas{cursor:pointer}" +
    ".vg-status{position:absolute;top:10px;left:12px;font-size:.78rem;color:var(--muted,#8b93a7);" +
      "pointer-events:none;letter-spacing:.02em;text-shadow:0 1px 3px rgba(0,0,0,.8)}" +
    ".vg-controls{position:absolute;top:8px;right:8px;display:flex;gap:6px}" +
    ".vg-controls button,.vg-chip{background:rgba(23,26,33,.88);border:1px solid var(--border,#2a2f3a);" +
      "color:var(--muted,#8b93a7);border-radius:6px;padding:3px 9px;font-size:.76rem;font-weight:600;" +
      "cursor:pointer;transition:color .15s,border-color .15s}" +
    ".vg-controls button:hover,.vg-chip:hover{color:var(--text,#e6e8ee);border-color:var(--accent,#4f9cf9)}" +
    ".vg-controls button.active{color:var(--accent,#4f9cf9);border-color:var(--accent,#4f9cf9)}" +
    ".vg-legend{position:absolute;bottom:10px;left:12px;display:flex;gap:6px;flex-wrap:wrap;max-width:60%}" +
    ".vg-chip .vg-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;" +
      "vertical-align:0;background:var(--c,#888)}" +
    ".vg-chip.off{opacity:.38}" +
    ".vg-chip.off .vg-dot{background:var(--muted,#8b93a7)}" +
    ".vg-search{position:absolute;bottom:10px;right:12px;width:180px;padding:4px 10px;" +
      "background:rgba(23,26,33,.92);border:1px solid var(--border,#2a2f3a);border-radius:6px;" +
      "color:var(--text,#e6e8ee);font-size:.8rem}" +
    ".vg-search:focus{outline:none;border-color:var(--accent,#4f9cf9)}" +
    ".vg-tip{position:absolute;max-width:250px;background:rgba(15,17,21,.96);border:1px solid var(--border,#2a2f3a);" +
      "border-radius:8px;padding:8px 11px;font-size:.8rem;pointer-events:none;display:none;z-index:5;" +
      "box-shadow:0 6px 18px rgba(0,0,0,.5)}" +
    ".vg-tip-title{font-weight:700;color:var(--text,#e6e8ee)}" +
    ".vg-tip-cat{margin-top:2px;font-size:.74rem}" +
    ".vg-tip-meta{margin-top:3px;color:var(--muted,#8b93a7);font-size:.74rem}" +
    ".vg-tip-open{margin-top:5px;color:var(--accent,#4f9cf9);font-size:.74rem;font-weight:600}" +
    "@media (max-width:640px){" +
      ".vg-stage{height:380px}" +
      ".vg-search{width:132px}" +
      ".vg-legend{max-width:52%}" +
    "}";
  var styleTag = document.createElement("style");
  styleTag.textContent = css;
  document.head.appendChild(styleTag);

  /* =======================================================================
   * Data + layout
   * ======================================================================= */
  var nodes = [], links = [], byId = {};
  var neighbors = {};           // id -> Set of neighbor ids
  var catOn = {};               // category -> visible
  var hubIds = [];              // top nodes by degree (always labelled)

  var W = 1, H = 1, DPR = 1;
  var rotY = -0.7, rotX = 0.32; // current view rotation
  var velY = 0, velX = 0;       // drag inertia
  var zoom = 1;
  var autoRotate = !window.matchMedia ||
                   !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var labelsOn = true;
  var picked = null;            // hovered node
  var searchSet = null;         // Set of matched ids (or null)
  var running = false;
  var CAM = 3.4;

  /* Positions come precomputed in graph.json (build-time Fruchterman-
   * Reingold — see build.py) so the globe appears instantly with related
   * notes clustered together. Fibonacci-sphere spread is only a fallback
   * for older graph.json files that lack "pos". */
  function initLayout(done) {
    var N = nodes.length, golden = Math.PI * (3 - Math.sqrt(5));
    for (var i = 0; i < N; i++) {
      var n = nodes[i];
      if (n.pos && n.pos.length === 3) {
        n.x = n.pos[0]; n.y = n.pos[1]; n.z = n.pos[2];
      } else {
        var y = 1 - (i / Math.max(1, N - 1)) * 2;
        var r = Math.sqrt(Math.max(0, 1 - y * y));
        var th = golden * i;
        n.x = Math.cos(th) * r; n.y = y; n.z = Math.sin(th) * r;
      }
    }
    precomputeArcs();
    // Hubs (top 10 by degree) keep a permanent label
    var sorted = nodes.slice().sort(function (p, q) { return q.degree - p.degree; });
    hubIds = {};
    for (i = 0; i < Math.min(10, sorted.length); i++) hubIds[sorted[i].id] = true;
    done();
  }

  // Arc polylines between linked notes (quadratic bezier bulging outward
  // from the sphere, like flight paths)
  function precomputeArcs() {
    for (var li = 0; li < links.length; li++) {
      var L = links[li];
      var dot = L.a.x * L.b.x + L.a.y * L.b.y + L.a.z * L.b.z;
      var ang = Math.acos(Math.max(-1, Math.min(1, dot)));
      var h = Math.max(0.03, Math.min(0.42, ang * 0.22));
      var mx = (L.a.x + L.b.x) / 2, my = (L.a.y + L.b.y) / 2, mz = (L.a.z + L.b.z) / 2;
      var ml = Math.sqrt(mx * mx + my * my + mz * mz) + 1e-9;
      var lift = (1 + h) / ml;
      var cxp = mx * lift, cyp = my * lift, czp = mz * lift;
      var STEPS = 10, pts = [];
      for (var k = 0; k <= STEPS; k++) {
        var t = k / STEPS, u = 1 - t;
        pts.push([
          u * u * L.a.x + 2 * u * t * cxp + t * t * L.b.x,
          u * u * L.a.y + 2 * u * t * cyp + t * t * L.b.y,
          u * u * L.a.z + 2 * u * t * czp + t * t * L.b.z
        ]);
      }
      L.pts = pts;
      L.col = mix(catColor(L.a.category), catColor(L.b.category), 0.5);
    }
  }

  /* --- sphere wireframe (precomputed 3D polylines) ---------------------- */
  var wire = [];
  function initWire() {
    var i, j, t, pts;
    for (i = 0; i < 12; i++) {          // meridians
      var phi = (i / 12) * Math.PI * 2;
      pts = [];
      for (j = 0; j <= 32; j++) {
        t = (j / 32) * Math.PI * 2;
        pts.push([Math.cos(t) * Math.cos(phi), Math.sin(t), Math.cos(t) * Math.sin(phi)]);
      }
      wire.push(pts);
    }
    for (i = 1; i < 8; i++) {           // parallels
      var lat = (i / 8 - 0.5) * Math.PI;
      var r = Math.cos(lat), y = Math.sin(lat);
      pts = [];
      for (j = 0; j <= 48; j++) {
        t = (j / 48) * Math.PI * 2;
        pts.push([Math.cos(t) * r, y, Math.sin(t) * r]);
      }
      wire.push(pts);
    }
  }

  /* --- node sprites (glow dot per category, drawn scaled) --------------- */
  var sprites = {};
  function initSprites() {
    Object.keys(CAT_COLORS).forEach(function (cat) {
      var c = CAT_COLORS[cat];
      var s = document.createElement("canvas");
      s.width = s.height = 64;
      var g = s.getContext("2d");
      var grad = g.createRadialGradient(32, 32, 2, 32, 32, 30);
      grad.addColorStop(0, rgba(c, 0.95));
      grad.addColorStop(0.25, rgba(c, 0.55));
      grad.addColorStop(1, rgba(c, 0));
      g.fillStyle = grad;
      g.fillRect(0, 0, 64, 64);
      sprites[cat] = s;
    });
  }

  /* =======================================================================
   * Render loop
   * ======================================================================= */
  function resize() {
    var w = stage.clientWidth, h = stage.clientHeight;
    // a hidden/collapsed host pane reports 0×0 — keep the last good size
    // so the canvas doesn't blank out whenever the tab is backgrounded
    if (w < 10 || h < 10) return;
    W = w; H = h;
    DPR = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(W * DPR);
    canvas.height = Math.round(H * DPR);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }

  // rAF pauses entirely while the tab is hidden/backgrounded, so the
  // per-frame size check alone is not enough — poll as a fallback and
  // re-arm the draw chain if it stalled. Some embedded webviews never
  // deliver rAF at all; in those, this ticker keeps the globe animating.
  setInterval(function () {
    if (stage.clientWidth !== W || stage.clientHeight !== H) resize();
    if (running && Date.now() - lastFrameT > 300) frame();
  }, 33);
  setInterval(function () {
    if (running && Date.now() - lastFrameT > 3000) schedule();
  }, 1000);

  var cy, sy, cx, sx, scale;
  function beginFrame() {
    // self-healing size: the host pane can resize without firing window
    // resize (e.g. in-app browser pane), so verify every frame
    if (stage.clientWidth !== W || stage.clientHeight !== H) resize();
    if (autoRotate && !picked && !isDragging) rotY += 0.0021;
    if (!isDragging) {
      rotY += velY; rotX += velX;
      velY *= 0.94; velX *= 0.94;
    }
    rotX = Math.max(-1.25, Math.min(1.25, rotX));
    cy = Math.cos(rotY); sy = Math.sin(rotY); cx = Math.cos(rotX); sx = Math.sin(rotX);
    scale = Math.min(W, H) * 0.36 * zoom;
  }
  // rotate (Y then X) + perspective; writes sx/sy/sz/ps onto the target
  function proj(px, py, pz, out) {
    var x1 = px * cy - pz * sy, z1 = px * sy + pz * cy;
    var y2 = py * cx - z1 * sx, z2 = py * sx + z1 * cx;
    var p = CAM / (CAM - z2);
    out.x = W / 2 + x1 * scale * p;
    out.y = H / 2 - y2 * scale * p;
    out.z = z2; out.p = p;
  }

  function nodeRadius(n) {
    return (2.1 + 2.3 * Math.sqrt(n.degree)) * (n.pp || 1) * Math.pow(zoom, 0.55);
  }

  function focusSet() {
    // which node ids are "in focus" (hover neighbours or search matches)
    if (searchSet) return searchSet;
    if (picked) return neighbors[picked.id] || {};
    return null;
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    var focus = focusSet();
    var i, j, n, L;

    // glass ball
    var R = scale;
    var g = ctx.createRadialGradient(W / 2 - R * 0.25, H / 2 - R * 0.3, R * 0.1, W / 2, H / 2, R);
    g.addColorStop(0, "rgba(38,52,84,0.30)");
    g.addColorStop(0.7, "rgba(18,22,33,0.22)");
    g.addColorStop(1, "rgba(10,12,18,0.05)");
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(W / 2, H / 2, R, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = "rgba(79,156,249,0.16)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(W / 2, H / 2, R, 0, Math.PI * 2); ctx.stroke();

    // wireframe
    ctx.strokeStyle = "rgba(99,130,190,0.075)";
  var tmp = { x: 0, y: 0, z: 0, p: 1 };
  var PR = { x: 0, y: 0, z: 0, p: 1 };
    for (i = 0; i < wire.length; i++) {
      var pts3 = wire[i];
      ctx.beginPath();
      for (j = 0; j < pts3.length; j++) {
        proj(pts3[j][0], pts3[j][1], pts3[j][2], tmp);
        if (j === 0) ctx.moveTo(tmp.x, tmp.y); else ctx.lineTo(tmp.x, tmp.y);
      }
      ctx.stroke();
    }

    // project nodes — screen coords land in px/py/pz so the 3D layout
    // positions (x/y/z) survive for the next frame
    for (i = 0; i < nodes.length; i++) {
      n = nodes[i];
      if (n.x === undefined) continue;
      proj(n.x, n.y, n.z, PR);
      n.px = PR.x; n.py = PR.y; n.pz = PR.z; n.pp = PR.p;
    }

    // links (painter order back → front by mid z)
    var order = [];
    for (i = 0; i < links.length; i++) {
      L = links[i];
      L.hid = !catOn[L.a.category] || !catOn[L.b.category] || !L.pts;
      if (L.hid) continue;
      L.mz = (L.a.pz + L.b.pz) / 2;
      order.push(L);
    }
    order.sort(function (p, q) { return p.mz - q.mz; });
    for (i = 0; i < order.length; i++) {
      L = order[i];
      var inFocus = focus &&
        ((picked && (L.a === picked || L.b === picked)) ||
         (focus[L.a.id] && focus[L.b.id]));
      var front = L.mz > 0;
      var alpha = front ? 0.30 : 0.10;
      var col = L.col;
      if (focus) alpha = inFocus ? (front ? 0.85 : 0.35) : alpha * 0.22;
      if (inFocus && picked) col = mix(L.col, [79, 156, 249], 0.45);
      ctx.strokeStyle = rgba(col, alpha);
      ctx.lineWidth = (0.6 + 0.45 * Math.min(L.weight, 4)) * (front ? 1 : 0.7);
      ctx.beginPath();
      for (j = 0; j < L.pts.length; j++) {
        proj(L.pts[j][0], L.pts[j][1], L.pts[j][2], tmp);
        if (j === 0) ctx.moveTo(tmp.x, tmp.y); else ctx.lineTo(tmp.x, tmp.y);
      }
      ctx.stroke();
    }

    // nodes (back → front)
    var drawOrder = nodes.filter(function (n2) {
      return catOn[n2.category] && n2.x !== undefined;
    });
    drawOrder.sort(function (p, q) { return p.pz - q.pz; });
    for (i = 0; i < drawOrder.length; i++) {
      n = drawOrder[i];
      var r = nodeRadius(n);
      var dimmed = focus && !focus[n.id] && n !== picked;
      var a = n.pz > 0 ? 1 : 0.38 + n.pz * 0.16;      // fade on far side
      if (dimmed) a *= 0.13;
      var sp = sprites[n.category];
      if (sp) {
        ctx.globalAlpha = Math.max(0, Math.min(1, a));
        var gw = r * 4.2;
        ctx.drawImage(sp, n.px - gw / 2, n.py - gw / 2, gw, gw);
      }
      ctx.globalAlpha = 1;
      ctx.fillStyle = rgba(catColor(n.category), a);
      ctx.beginPath(); ctx.arc(n.px, n.py, r, 0, Math.PI * 2); ctx.fill();
      if (n === picked || (focus && focus[n.id] && searchSet)) {
        ctx.strokeStyle = "rgba(255,255,255," + (n === picked ? 0.95 : 0.55) + ")";
        ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.arc(n.px, n.py, r + 2.4, 0, Math.PI * 2); ctx.stroke();
      }
    }

    // labels
    ctx.font = "600 11px -apple-system,'Segoe UI',system-ui,sans-serif";
    ctx.textBaseline = "middle";
    for (i = 0; i < drawOrder.length; i++) {
      n = drawOrder[i];
      var show = n === picked ||
                  (focus && focus[n.id]) ||
                  (labelsOn && hubIds[n.id] && n.pz > -0.15 && !focus);
      if (!show) continue;
      var la = n.pz > 0 ? 0.92 : 0.4;
      if (focus && !focus[n.id] && n !== picked) continue;
      var label = n.title.length > 24 ? n.title.slice(0, 23) + "…" : n.title;
      var r2 = nodeRadius(n);
      var lx = n.px + r2 + 5, ly = n.py;
      ctx.lineWidth = 3;
      ctx.strokeStyle = "rgba(11,13,18,0.85)";
      ctx.strokeText(label, lx, ly);
      ctx.fillStyle = n === picked ? "rgba(255,255,255," + la + ")"
                                   : "rgba(214,222,238," + la + ")";
      ctx.fillText(label, lx, ly);
    }
  }

  var lastFrameT = 0, rafQueued = false;
  function schedule() {
    if (rafQueued) return;
    rafQueued = true;
    requestAnimationFrame(function () { rafQueued = false; frame(); });
  }
  function frame() {
    if (!running) return;
    lastFrameT = Date.now();
    beginFrame();
    draw();
    schedule();
  }
  function start() {
    if (running) return;
    running = true;
    schedule();
  }
  function stop() { running = false; }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stop(); else start();
  });

  /* =======================================================================
   * Interaction
   * ======================================================================= */
  var isDragging = false, moved = 0, lastX = 0, lastY = 0, downT = 0;
  var pointers = {}, pinchD = 0;

  function localXY(e) {
    var rect = canvas.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  function pickAt(mx, my) {
    var best = null, bd = 1e9;
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (!catOn[n.category] || n.px === undefined) continue;
      var r = nodeRadius(n) + 7;
      var dx = mx - n.px, dy = my - n.py;
      var d = dx * dx + dy * dy;
      if (d < r * r) {
        // prefer front-most among overlapping candidates
        if (!best || n.pz > best.pz || (n.pz === best.pz && d < bd)) { best = n; bd = d; }
      }
    }
    return best;
  }

  canvas.addEventListener("pointerdown", function (e) {
    try { canvas.setPointerCapture && canvas.setPointerCapture(e.pointerId); }
    catch (err) { /* synthetic/odd pointers can't be captured — fine */ }
    pointers[e.pointerId] = localXY(e);
    if (Object.keys(pointers).length === 2) {
      var ids = Object.keys(pointers);
      var p1 = pointers[ids[0]], p2 = pointers[ids[1]];
      pinchD = Math.hypot(p1.x - p2.x, p1.y - p2.y);
    }
    isDragging = true; moved = 0;
    lastX = e.clientX; lastY = e.clientY; downT = Date.now();
    velX = velY = 0;
    stage.classList.add("dragging");
  });

  canvas.addEventListener("pointermove", function (e) {
    var pos = localXY(e);
    if (pointers[e.pointerId]) pointers[e.pointerId] = pos;

    if (isDragging && Object.keys(pointers).length === 2) {
      var ids = Object.keys(pointers);
      var q1 = pointers[ids[0]], q2 = pointers[ids[1]];
      var d = Math.hypot(q1.x - q2.x, q1.y - q2.y);
      if (pinchD > 0) {
        zoom = Math.max(0.45, Math.min(3.2, zoom * (d / pinchD)));
      }
      pinchD = d;
      return;
    }
    if (isDragging) {
      var dx = e.clientX - lastX, dy = e.clientY - lastY;
      moved += Math.abs(dx) + Math.abs(dy);
      var k = 0.0044 / Math.sqrt(zoom);
      rotY += dx * k; rotX += dy * k;
      velY = dx * k * 0.22; velX = dy * k * 0.22;
      lastX = e.clientX; lastY = e.clientY;
      hideTip();
      return;
    }
    // hover picking
    var hit = pickAt(pos.x, pos.y);
    if (hit !== picked) { picked = hit; }
    stage.classList.toggle("picking", !!picked);
    if (picked) showTip(picked, pos.x, pos.y); else hideTip();
  });

  function endPointer(e) {
    delete pointers[e.pointerId];
    if (Object.keys(pointers).length === 0) {
      stage.classList.remove("dragging");
      var wasTap = moved < 6 && Date.now() - downT < 500;
      isDragging = false;
      pinchD = 0;
      if (wasTap && picked) {
        window.location.href = siteBase() + picked.url;
      }
    }
  }
  canvas.addEventListener("pointerup", endPointer);
  canvas.addEventListener("pointercancel", endPointer);
  canvas.addEventListener("pointerleave", function () {
    if (!isDragging) { picked = null; hideTip(); stage.classList.remove("picking"); }
  });

  canvas.addEventListener("wheel", function (e) {
    e.preventDefault();
    zoom = Math.max(0.45, Math.min(3.2, zoom * Math.exp(-e.deltaY * 0.0011)));
  }, { passive: false });

  /* --- tooltip ------------------------------------------------------------ */
  function showTip(n, mx, my) {
    var col = catColor(n.category);
    tipEl.innerHTML =
      '<div class="vg-tip-title"></div>' +
      '<div class="vg-tip-cat"></div>' +
      '<div class="vg-tip-meta"></div>' +
      '<div class="vg-tip-open">click to open ↗</div>';
    tipEl.firstChild.textContent = n.title;
    var catEl = tipEl.children[1];
    catEl.textContent = "● " + n.category;
    catEl.style.color = rgba(col, 1);
    var meta = (neighbors[n.id] ? Object.keys(neighbors[n.id]).length : 0) +
               " linked notes · " + n.degree + " link strength";
    if (n.broken) meta += " · " + n.broken + " unresolved";
    tipEl.children[2].textContent = meta;
    tipEl.style.display = "block";
    var tw = tipEl.offsetWidth, th = tipEl.offsetHeight;
    var tx = mx + 16, ty = my - th - 12;
    if (tx + tw > W - 8) tx = mx - tw - 16;
    if (tx < 8) tx = 8;
    if (ty < 8) ty = my + 18;
    tipEl.style.left = tx + "px";
    tipEl.style.top = ty + "px";
  }
  function hideTip() { tipEl.style.display = "none"; }

  /* =======================================================================
   * Controls, legend, search
   * ======================================================================= */
  function buildControls() {
    var controlsEl = document.getElementById("vgControls");
    if (!controlsEl) return;
    function btn(label, title, fn) {
      var b = document.createElement("button");
      b.textContent = label; b.title = title;
      b.addEventListener("click", fn);
      controlsEl.appendChild(b);
      return b;
    }
    var spinBtn = btn("⏸", "pause / resume rotation", function () {
      autoRotate = !autoRotate;
      spinBtn.textContent = autoRotate ? "⏸" : "▶";
      spinBtn.classList.toggle("active", !autoRotate);
    });
    spinBtn.title = "pause rotation";
    var lblBtn = btn("Labels", "show / hide hub labels", function () {
      labelsOn = !labelsOn;
      lblBtn.classList.toggle("active", labelsOn);
    });
    lblBtn.classList.toggle("active", labelsOn);
    btn("+", "zoom in", function () { zoom = Math.min(3.2, zoom * 1.18); });
    btn("−", "zoom out", function () { zoom = Math.max(0.45, zoom / 1.18); });
    btn("↺", "reset view", function () {
      zoom = 1; rotY = -0.7; rotX = 0.32; velX = velY = 0;
    });
  }

  function buildLegend(stats) {
    var legendEl = document.getElementById("vgLegend");
    if (!legendEl) return;
    Object.keys(stats.categories || {}).forEach(function (cat) {
      catOn[cat] = true;
      var chip = document.createElement("button");
      chip.className = "vg-chip";
      chip.style.setProperty("--c", rgba(catColor(cat), 1));
      chip.innerHTML = '<span class="vg-dot"></span>';
      chip.appendChild(document.createTextNode(
        cat + " · " + stats.categories[cat]));
      chip.addEventListener("click", function () {
        catOn[cat] = !catOn[cat];
        chip.classList.toggle("off", !catOn[cat]);
      });
      legendEl.appendChild(chip);
    });
    // unknown categories default on
    nodes.forEach(function (n) {
      if (!(n.category in catOn)) catOn[n.category] = true;
    });
  }

  function initSearch() {
    var searchEl = document.getElementById("vgSearch");
    if (!searchEl) return;
    searchEl.addEventListener("input", function () {
      var q = this.value.trim().toLowerCase();
      if (q.length < 2) {
        searchSet = null;
        statusEl.textContent = defaultStatus();
        return;
      }
      var set = {};
      var k = 0;
      nodes.forEach(function (n) {
        if (n.title.toLowerCase().indexOf(q) !== -1 ||
            n.id.toLowerCase().indexOf(q) !== -1) { set[n.id] = true; k++; }
      });
      searchSet = k ? set : {};
      statusEl.textContent = k + (k === 1 ? " note" : " notes") + ' match "' +
                             this.value.trim() + '"';
    });
    searchEl.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { this.value = ""; this.blur(); searchSet = null;
                                statusEl.textContent = defaultStatus(); }
      if (e.key === "Enter" && searchSet) {
        var first = Object.keys(searchSet)[0];
        if (first && byId[first]) window.location.href = siteBase() + byId[first].url;
      }
    });
  }

  function defaultStatus() {
    return statsText;
  }
  var statsText = "";

  /* =======================================================================
   * Boot
   * ======================================================================= */
  statusEl.textContent = "loading vault graph…";
  fetch(siteBase() + "/data/graph.json")
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      if (!data || !data.nodes || !data.nodes.length) throw new Error("empty graph");

      nodes = data.nodes;
      byId = {};
      nodes.forEach(function (n) { byId[n.id] = n; });
      links = (data.links || []).filter(function (l) {
        return byId[l.source] && byId[l.target];
      }).map(function (l) {
        return { a: byId[l.source], b: byId[l.target], weight: l.weight || 1 };
      });
      neighbors = {};
      links.forEach(function (l) {
        (neighbors[l.a.id] = neighbors[l.a.id] || {})[l.b.id] = true;
        (neighbors[l.b.id] = neighbors[l.b.id] || {})[l.a.id] = true;
      });

      statsText = data.stats.notes + " notes · " + data.stats.links +
                  " links · graph " + (data.generated || "");
      if (statsEl) statsEl.textContent = statsText;
      statusEl.textContent = statsText;

      buildLegend(data.stats);
      buildControls();
      initSearch();

      resize();
      initWire();
      initSprites();
      window.addEventListener("resize", resize);
      if (window.ResizeObserver) new ResizeObserver(resize).observe(stage);
      start();
      initLayout(function () { statusEl.textContent = statsText; });
    })
    .catch(function (err) {
      statusEl.textContent = "vault graph unavailable (" + err.message + ")";
      statusEl.style.color = "var(--muted,#8b93a7)";
      canvas.style.display = "none";
    });
})();
