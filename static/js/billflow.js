/* Bill flowchart: tap a bill number anywhere to see who picked what and when.
   One bar per grab on a shared time axis, coloured by phase -- normal time,
   extra time to find, report only, late -- with missing items marked, then
   the same story as a list. Used by the assigner, picker, admin picking page
   and staff profile. Data: GET /pick/bill/<id>.json. */
(function () {
  "use strict";
  var css = ".bf-modal{position:fixed;inset:0;z-index:9990;background:rgba(15,23,42,.5);display:flex;align-items:flex-end;justify-content:center}" +
    ".bf-modal[hidden]{display:none}" +
    ".bf-sheet{background:var(--card,#fff);color:var(--ink,#0f172a);width:100%;max-width:760px;max-height:88vh;overflow:auto;border-radius:16px 16px 0 0;padding:16px 16px 20px;font:14px/1.45 system-ui,-apple-system,'Segoe UI',sans-serif}" +
    "@media(min-width:760px){.bf-modal{align-items:center}.bf-sheet{border-radius:16px}}" +
    ".bf-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:6px}.bf-top h3{margin:0;font-size:18px}" +
    ".bf-x{border:1.5px solid var(--line,#dfe4ec);background:transparent;border-radius:10px;padding:6px 12px;font-weight:700;cursor:pointer;color:inherit}" +
    ".bf-sub{color:var(--ink-3,#64748b);font-size:13px}.bf-u{background:#b91c1c;color:#fff;font-size:11px;font-weight:800;padding:1px 6px;border-radius:6px;margin-left:6px}" +
    ".bf-sum{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:12px 0}" +
    ".bf-sum div{border:1px solid var(--line,#dfe4ec);border-radius:10px;padding:8px}.bf-sum b{display:block;font-size:18px;font-variant-numeric:tabular-nums}" +
    ".bf-sum span{font-size:10.5px;font-weight:700;color:var(--ink-3,#64748b);text-transform:uppercase;letter-spacing:.4px}" +
    ".bf-chart{margin:8px 0 4px}.bf-row{display:grid;grid-template-columns:96px 1fr;gap:8px;align-items:center;margin:7px 0}" +
    ".bf-name{font-weight:700;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.bf-name small{display:block;font-weight:500;color:var(--ink-3,#64748b)}" +
    ".bf-track{position:relative;height:22px;background:repeating-linear-gradient(90deg,transparent 0 calc(25% - 1px),var(--line,#dfe4ec) calc(25% - 1px) 25%);border-radius:6px}" +
    ".bf-seg{position:absolute;top:3px;height:16px;border-radius:4px}.bf-seg.n{background:#146b4a}.bf-seg.f{background:#e8a13a}.bf-seg.r{background:#7c3aed}.bf-seg.l{background:#b91c1c}" +
    ".bf-seg.live{background-image:repeating-linear-gradient(45deg,rgba(255,255,255,.35) 0 6px,transparent 6px 12px)}" +
    ".bf-dot{position:absolute;top:1px;width:4px;height:20px;border-radius:2px;background:#0f172a}" +
    ".bf-miss{position:absolute;top:-6px;width:0;height:0;border-left:6px solid transparent;border-right:6px solid transparent;border-top:9px solid #7c3aed;transform:translateX(-6px)}" +
    ".bf-axis{display:grid;grid-template-columns:96px 1fr;gap:8px;font-size:11.5px;color:var(--ink-3,#64748b)}.bf-axis div:last-child{display:flex;justify-content:space-between}" +
    ".bf-legend{display:flex;flex-wrap:wrap;gap:10px;font-size:12px;color:var(--ink-2,#334155);margin:8px 0 2px}.bf-legend i{display:inline-block;width:12px;height:10px;border-radius:2px;margin-right:4px;vertical-align:middle}" +
    ".bf-list{border-left:3px solid var(--line,#dfe4ec);margin:12px 0 0 6px;padding-left:14px}" +
    ".bf-list div{position:relative;padding:5px 0}.bf-list div::before{content:'';position:absolute;left:-21px;top:10px;width:11px;height:11px;border-radius:50%;background:#1e40af}" +
    ".bf-list div.late::before{background:#b91c1c}.bf-list div.miss::before{background:#7c3aed}.bf-list div.end::before{background:#15803d}" +
    ".bf-hit{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px;width:100%;text-align:left;border:1px solid var(--line,#dfe4ec);background:var(--surface,#fff);color:inherit;border-radius:10px;padding:9px 12px;margin:0 0 6px;cursor:pointer;font:inherit}" +
    ".bf-hit:hover{border-color:var(--brand,#1e40af)}.bf-hit b{font-size:15px}.bf-hit span{font-size:12.5px;color:var(--ink-3,#64748b)}" +
    "@media(max-width:560px){.bf-sum{grid-template-columns:repeat(2,minmax(0,1fr))}.bf-row,.bf-axis{grid-template-columns:74px 1fr}}";
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
  // Indian time whatever the device clock says, to match the rest of the app
  function t12(ms) { var d = new Date(ms + 330 * 60000), h = d.getUTCHours(), m = d.getUTCMinutes(); return ((h % 12) || 12) + ":" + (m < 10 ? "0" : "") + m + " " + (h < 12 ? "AM" : "PM"); }
  function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }
  var modal, lastFocus;
  function ensure() {
    if (modal) return;
    var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
    modal = document.createElement("div"); modal.className = "bf-modal"; modal.hidden = true;
    modal.setAttribute("role", "dialog"); modal.setAttribute("aria-modal", "true"); modal.setAttribute("aria-labelledby", "bfTitle");
    modal.innerHTML = '<div class="bf-sheet"><div class="bf-top"><div><h3 id="bfTitle"></h3><div class="bf-sub" id="bfSub"></div></div>' +
      '<button type="button" class="bf-x" id="bfX">Close</button></div><div id="bfBody"></div></div>';
    document.body.appendChild(modal);
    modal.addEventListener("click", function (e) { if (e.target === modal || e.target.id === "bfX") close(); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !modal.hidden) close(); });
  }
  function close() { modal.hidden = true; if (lastFocus && lastFocus.focus) lastFocus.focus(); }

  function draw(b) {
    var t0 = b.t_added, t1 = b.t_done || b.t_now;
    b.grabs.forEach(function (g) { t1 = Math.max(t1, g.e || b.t_now); });
    b.missing.forEach(function (m) { if (m.t) t1 = Math.max(t1, m.t); });
    if (t1 <= t0) t1 = t0 + 60000;
    var span = t1 - t0, pct = function (x) { return Math.max(0, Math.min(100, 100 * (x - t0) / span)); };
    function seg(cls, a, z, live) { if (z <= a) return ""; return '<i class="bf-seg ' + cls + (live ? " live" : "") + '" style="left:' + pct(a) + "%;width:" + (pct(z) - pct(a)) + '%"></i>'; }
    var picked = 0, late = 0;
    var rows = b.grabs.filter(function (g) { return g.status !== "cancelled"; }).map(function (g) {
      var end = g.e || b.t_now, live = !g.e, h = "";
      h += seg("n", g.s, Math.min(g.n, end), live) + seg("f", g.n, Math.min(g.f, end), live) + seg("r", g.f, Math.min(g.r, end), live) + seg("l", g.r, end, live);
      if (g.e) h += '<i class="bf-dot" style="left:calc(' + pct(g.e) + '% - 2px)" title="Done ' + t12(g.e) + '"></i>';
      b.missing.forEach(function (m) { if (m.by === g.picker && m.t && m.t >= g.s && m.t <= end + 1000) h += '<i class="bf-miss" style="left:' + pct(m.t) + '%" title="' + esc(m.item) + " ×" + m.qty + '"></i>'; });
      if (g.picked != null) picked += g.picked; if (g.outcome === "late") late++;
      return '<div class="bf-row"><div class="bf-name">' + esc(g.picker) + "<small>" + plural(g.items, "item") + (g.e ? " · " + g.minutes + " min" : " · picking") + "</small></div>" +
        '<div class="bf-track" role="img" aria-label="' + esc(g.picker) + " took " + g.items + " at " + esc(g.took) + (g.done ? ", done " + esc(g.done) : "") + '">' + h + "</div></div>";
    }).join("");
    var sum = '<div class="bf-sum"><div><span>Items</span><b>' + b.items + "</b></div><div><span>Picked</span><b>" + picked + "</b></div>" +
      "<div><span>Missing</span><b>" + b.missing.reduce(function (s, m) { return s + m.qty; }, 0) + "</b></div><div><span>Total time</span><b>" +
      (b.minutes != null ? b.minutes + " min" : Math.round((b.t_now - t0) / 6000) / 10 + " min") + "</b></div></div>";
    var chart = rows ? '<div class="bf-chart">' + rows + '<div class="bf-axis"><div></div><div><span>' + t12(t0) + "</span><span>" + t12(t0 + span / 2) + "</span><span>" + t12(t1) + "</span></div></div>" +
      '<div class="bf-legend"><span><i style="background:#146b4a"></i>Normal time</span><span><i style="background:#e8a13a"></i>Extra time</span>' +
      '<span><i style="background:#7c3aed"></i>Report only</span><span><i style="background:#b91c1c"></i>Late</span><span><i style="background:#0f172a;width:4px"></i>Done</span>' +
      '<span><i style="background:#7c3aed;clip-path:polygon(0 0,100% 0,50% 100%)"></i>Not found</span></div></div>' : '<div class="bf-sub" style="margin:10px 0">Nobody has taken items from this bill yet.</div>';
    var list = '<div class="bf-list"><div>Added at <b>' + esc(b.added) + "</b></div>" + b.grabs.map(function (g) {
      var lt = g.outcome === "late";
      return '<div class="' + (lt ? "late" : "") + '"><b>' + esc(g.picker) + "</b> took " + plural(g.items, "item") + " at " + esc(g.took) + " · due " + esc(g.due) +
        (g.done ? " · done " + esc(g.done) + " (" + g.minutes + " min)" : g.status === "cancelled" ? " · released by the assigner" : " · <i>picking now</i>") +
        (g.missing ? " · " + g.missing + " not found" : "") + (lt ? ' · <b style="color:#991b1b">' + g.late_min + " min late</b>" : "") +
        (g.done ? " · " + (g.points > 0 ? "+" : "") + g.points + " pts" : "") + "</div>";
    }).join("") + b.missing.map(function (m) {
      return '<div class="miss">' + esc(m.by) + " could not find <b>" + esc(m.item) + " ×" + m.qty + "</b> at " + esc(m.at) + " · " +
        ({open: "waiting for the purchaser", sorted: "done by purchaser", reassigned: "reassigned", unavailable: "not available"}[m.status] || esc(m.status)) + "</div>";
    }).join("") + (b.completed ? '<div class="end">Completed at <b>' + esc(b.completed) + "</b> · total " + b.minutes + " min</div>" : "") + "</div>";
    return sum + chart + list;
  }

  function open(id) {
    ensure(); lastFocus = document.activeElement;
    fetch("/pick/bill/" + id + ".json", {credentials: "same-origin"}).then(function (r) { return r.json(); }).then(function (b) {
      if (!b || !b.bill_no) return;
      document.getElementById("bfTitle").innerHTML = esc(b.bill_no) + (b.urgent ? '<span class="bf-u">URGENT</span>' : "");
      document.getElementById("bfSub").textContent = plural(b.items, "item") + " · " +
        ({done: "complete", open: "open", cancelled: "cancelled", scheduled: "rescheduled"}[b.status] || b.status) + (late(b) ? " · " + late(b) + " late" : "");
      document.getElementById("bfBody").innerHTML = draw(b);
      modal.hidden = false; document.getElementById("bfX").focus();
    }).catch(function () {});
  }
  function late(b) { return b.grabs.filter(function (g) { return g.outcome === "late"; }).length; }
  /* Bill search (assigner and admin): type part of a bill number, matching
     bills of any date are listed, tap one to open everything about it.
     Markup: <input data-bill-search="resultsId"> <div id="resultsId"></div> */
  var sq = {};
  function searchInto(input) {
    var out = document.getElementById(input.getAttribute("data-bill-search"));
    if (!out) return;
    var q = (input.value || "").replace(/\D/g, "");
    clearTimeout(sq[out.id]);
    if (!q) { out.innerHTML = ""; return; }
    ensure();                                   // styles for the result list
    sq[out.id] = setTimeout(function () {
      fetch("/pick/bills/find.json?q=" + encodeURIComponent(q), {credentials: "same-origin"})
        .then(function (r) { return r.json(); }).then(function (d) {
          if ((input.value || "").replace(/\D/g, "") !== q) return;      // typed on since
          var bl = (d && d.bills) || [];
          out.innerHTML = bl.length ? bl.map(function (b) {
            var st = b.status === "done" ? "complete" : b.status === "cancelled" ? "cancelled" :
              b.status === "scheduled" ? "rescheduled" : b.left + " left";
            return '<button type="button" class="bf-hit" data-bill-flow="' + b.id + '"><b>' + esc(b.bill_no) + "</b>" +
              (b.urgent ? '<span class="bf-u">URGENT</span>' : "") + "<span>" + esc(b.date) + " · " + esc(b.added) + " · " +
              plural(b.items, "item") + " · " + st + "</span></button>";
          }).join("") : '<div class="bf-sub" style="padding:6px 2px">No bill with ' + esc(q) + " in its number.</div>";
        }).catch(function () { out.innerHTML = '<div class="bf-sub">No connection. Try again.</div>'; });
    }, 220);
  }
  document.addEventListener("input", function (e) {
    if (e.target && e.target.hasAttribute && e.target.hasAttribute("data-bill-search")) searchInto(e.target);
  });
  window.BillFlow = {open: open, draw: draw, search: searchInto};
  // any element with data-bill-flow="<id>" opens it
  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest("[data-bill-flow]");
    if (el) { e.preventDefault(); open(el.getAttribute("data-bill-flow")); }
  });
})();
