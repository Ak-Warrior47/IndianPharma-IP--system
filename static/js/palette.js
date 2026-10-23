/* ============================================================================
   Command palette — Cmd+K / Ctrl+K
   One entry point to every screen and every staff member, for an admin who
   spends the day moving between them. Patterned on the Linear/Superhuman
   convention: open with a chord, type to filter, arrows to move, Enter to go.

   Self-contained: no dependencies, no build step, no globals beyond one guard.
   Fetches its items once, lazily, on first open.
   ========================================================================= */
(function () {
  "use strict";
  if (window.__ipPalette) return;
  window.__ipPalette = true;

  var items = null, loading = false, open = false;
  var root, input, list, lastFocus, active = 0, shown = [];

  /* ---- Subsequence match, so "ak" finds "Arjun Kumar" and "adch" finds
     "Admin Dashboard > Checklists". Scores exact and prefix hits higher. ---- */
  function score(item, q) {
    if (!q) return 1;
    var hay = (item.t + " " + (item.s || "") + " " + (item.g || "")).toLowerCase();
    var t = item.t.toLowerCase();
    if (t === q) return 1000;
    if (t.indexOf(q) === 0) return 500 - t.length;
    if (t.indexOf(q) > -1) return 300 - t.length;
    if (hay.indexOf(q) > -1) return 150;
    var qi = 0;
    for (var i = 0; i < hay.length && qi < q.length; i++) {
      if (hay.charAt(i) === q.charAt(qi)) qi++;
    }
    return qi === q.length ? 50 : 0;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function build() {
    root = document.createElement("div");
    root.className = "ipk";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-label", "Command palette");
    root.hidden = true;
    root.innerHTML =
      '<div class="ipk__scrim" data-close></div>' +
      '<div class="ipk__panel">' +
        '<div class="ipk__search">' +
          '<svg class="ipk__icon" width="15" height="15" viewBox="0 0 24 24" fill="none" ' +
            'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true">' +
            '<circle cx="11" cy="11" r="7"></circle><path d="M20 20l-3.6-3.6"></path></svg>' +
          '<input class="ipk__input" type="text" autocomplete="off" spellcheck="false" ' +
            'placeholder="Search screens and staff" aria-label="Search screens and staff" ' +
            'aria-controls="ipk-list" aria-autocomplete="list">' +
          '<kbd class="ipk__kbd">Esc</kbd>' +
        '</div>' +
        '<div class="ipk__list" id="ipk-list" role="listbox" aria-label="Results"></div>' +
        '<div class="ipk__foot">' +
          '<span><kbd class="ipk__kbd">&#8593;</kbd><kbd class="ipk__kbd">&#8595;</kbd> move</span>' +
          '<span><kbd class="ipk__kbd">&#8629;</kbd> open</span>' +
          '<span><kbd class="ipk__kbd">Esc</kbd> close</span>' +
        '</div>' +
      '</div>';
    document.body.appendChild(root);
    input = root.querySelector(".ipk__input");
    list = root.querySelector(".ipk__list");

    root.addEventListener("click", function (e) {
      if (e.target.hasAttribute("data-close")) hide();
      var row = e.target.closest("[data-url]");
      if (row) go(row.getAttribute("data-url"));
    });
    input.addEventListener("input", function () { active = 0; render(); });
    input.addEventListener("keydown", onKeys);
  }

  function onKeys(e) {
    if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, shown.length - 1); render(true); }
    else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); render(true); }
    else if (e.key === "Enter") { e.preventDefault(); if (shown[active]) go(shown[active].u); }
    else if (e.key === "Escape") { e.preventDefault(); hide(); }
    else if (e.key === "Tab") { e.preventDefault(); }   /* keep focus inside */
  }

  function go(url) { hide(); window.location.href = url; }

  function render(keepQuery) {
    var q = (input.value || "").trim().toLowerCase();
    var pool = items || [];
    shown = pool.map(function (it) { return { it: it, sc: score(it, q) }; })
                .filter(function (r) { return r.sc > 0; })
                .sort(function (a, b) { return b.sc - a.sc; })
                .slice(0, 40)
                .map(function (r) { return r.it; });

    if (loading) {
      list.innerHTML = '<div class="ipk__msg">Loading…</div>';
      return;
    }
    if (!shown.length) {
      list.innerHTML = '<div class="ipk__msg">Nothing matches “' + esc(q) + '”</div>';
      return;
    }
    if (active >= shown.length) active = shown.length - 1;

    var html = "", lastGroup = null;
    for (var i = 0; i < shown.length; i++) {
      var it = shown[i];
      if (it.g !== lastGroup) {
        html += '<div class="ipk__group">' + esc(it.g) + "</div>";
        lastGroup = it.g;
      }
      html += '<div class="ipk__row' + (i === active ? " is-active" : "") + '" role="option" ' +
              'aria-selected="' + (i === active ? "true" : "false") + '" ' +
              'data-url="' + esc(it.u) + '" data-i="' + i + '">' +
              '<span class="ipk__t">' + esc(it.t) + "</span>" +
              (it.s ? '<span class="ipk__s">' + esc(it.s) + "</span>" : "") +
              "</div>";
    }
    list.innerHTML = html;
    var el = list.querySelector(".is-active");
    if (el && keepQuery) el.scrollIntoView({ block: "nearest" });
  }

  function load() {
    if (items || loading) return;
    loading = true;
    render();
    fetch("/api/palette", { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (d) { items = (d && d.items) || []; })
      .catch(function () { items = []; })
      .then(function () { loading = false; render(); });
  }

  function show() {
    if (open) return;
    if (!root) build();
    lastFocus = document.activeElement;
    open = true;
    root.hidden = false;
    /* next frame so the transition runs */
    requestAnimationFrame(function () { root.classList.add("is-open"); });
    input.value = "";
    active = 0;
    load();
    render();
    input.focus();
  }

  function hide() {
    if (!open) return;
    open = false;
    root.classList.remove("is-open");
    var done = function () { if (!open) root.hidden = true; };
    setTimeout(done, 180);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  document.addEventListener("keydown", function (e) {
    var k = (e.key || "").toLowerCase();
    if ((e.metaKey || e.ctrlKey) && k === "k") { e.preventDefault(); open ? hide() : show(); return; }
    if (k === "escape" && open) { e.preventDefault(); hide(); return; }
    /* "/" opens too, unless the user is already typing somewhere */
    if (k === "/" && !open) {
      var t = e.target, tag = (t && t.tagName || "").toLowerCase();
      if (tag !== "input" && tag !== "textarea" && tag !== "select" && !(t && t.isContentEditable)) {
        e.preventDefault(); show();
      }
    }
  });

  /* Any element with data-ipk-open becomes a trigger (for touch, where there
     is no keyboard to press a chord on). */
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-ipk-open]");
    if (t) { e.preventDefault(); show(); }
  });
})();
