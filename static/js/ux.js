/* ============================================================================
   Interaction layer: theme, animated counters, toasts.

   Everything here is an enhancement. If this file fails to load the app works
   exactly as before: the theme falls back to light, counters show their final
   value as plain text, and flash messages render as ordinary blocks.
   ========================================================================= */
(function () {
  "use strict";
  if (window.__ipUX) return;
  window.__ipUX = true;


  /* ------------------------------------------------------------------ csrf
     Every same-origin state-changing fetch carries the session's CSRF token.
     Patching fetch once covers the calls that exist and any added later, which
     is safer than remembering to add a header at each call site. The token is
     read from the meta tag the server renders, so it is never guessable by a
     cross-site page. */
  var CSRF = (function () {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  })();
  if (CSRF && window.fetch) {
    var _fetch = window.fetch;
    window.fetch = function (input, init) {
      init = init || {};
      var url = typeof input === "string" ? input : (input && input.url) || "";
      var method = (init.method || (input && input.method) || "GET").toUpperCase();
      var sameOrigin = !/^https?:\/\//i.test(url) || url.indexOf(location.origin) === 0;
      if (sameOrigin && ["POST", "PUT", "PATCH", "DELETE"].indexOf(method) > -1) {
        var h = new Headers(init.headers || (input && input.headers) || {});
        if (!h.has("X-CSRFToken")) h.set("X-CSRFToken", CSRF);
        init.headers = h;
        if (!init.credentials) init.credentials = "same-origin";
      }
      return _fetch.call(this, input, init);
    };
  }

  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------------------------------------------------------------- theme */
  var KEY = "ip-theme";

  function store(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }
  function read() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }

  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var btns = document.querySelectorAll(".ip-theme");
    for (var i = 0; i < btns.length; i++) {
      btns[i].setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
      btns[i].setAttribute("aria-label",
        theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
    }
  }

  function current() {
    return document.documentElement.getAttribute("data-theme") || "light";
  }

  function toggle() {
    var next = current() === "dark" ? "light" : "dark";
    store(next);
    /* Morph the whole page between themes where supported. */
    if (document.startViewTransition && !reduced) {
      document.startViewTransition(function () { apply(next); });
    } else {
      apply(next);
    }
  }

  document.addEventListener("click", function (e) {
    if (e.target.closest(".ip-theme")) { e.preventDefault(); toggle(); }
  });
  /* Shift+D toggles, unless the user is typing. */
  document.addEventListener("keydown", function (e) {
    if (!e.shiftKey || (e.key || "").toLowerCase() !== "d") return;
    var t = e.target, tag = (t && t.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || (t && t.isContentEditable)) return;
    e.preventDefault(); toggle();
  });

  /* -------------------------------------------------------------- counters
     Any element with data-count="123" ticks up to that number on first sight.
     The element must already contain the final value in markup, so a failure
     here leaves the correct figure on screen. */
  function easeOut(t) { return 1 - Math.pow(1 - t, 3); }

  function countUp(el) {
    var target = parseFloat(el.getAttribute("data-count"));
    if (isNaN(target)) return;
    var dp = (el.getAttribute("data-dp") | 0);
    var suffix = el.getAttribute("data-suffix") || "";
    var dur = 760, started = null;
    function frame(ts) {
      if (started === null) started = ts;
      var p = Math.min((ts - started) / dur, 1);
      var v = target * easeOut(p);
      el.textContent = (dp ? v.toFixed(dp) : Math.round(v).toLocaleString()) + suffix;
      if (p < 1) requestAnimationFrame(frame);
      else el.textContent = (dp ? target.toFixed(dp) : Math.round(target).toLocaleString()) + suffix;
    }
    requestAnimationFrame(frame);
  }

  function initCounters() {
    var els = document.querySelectorAll("[data-count]");
    if (!els.length) return;
    if (reduced || !("IntersectionObserver" in window)) return;   /* leave markup as-is */
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { countUp(en.target); io.unobserve(en.target); }
      });
    }, { threshold: 0.4 });
    for (var i = 0; i < els.length; i++) io.observe(els[i]);
  }

  /* ---------------------------------------------------------------- toasts */
  function toastHost() {
    var h = document.querySelector(".ip-toasts");
    if (!h) {
      h = document.createElement("div");
      h.className = "ip-toasts";
      h.setAttribute("aria-live", "polite");
      h.setAttribute("aria-atomic", "false");
      document.body.appendChild(h);
    }
    return h;
  }

  function toast(message, kind, ms) {
    var el = document.createElement("div");
    el.className = "ip-toast " + (kind || "info");
    el.setAttribute("role", "status");
    var span = document.createElement("span");
    span.textContent = message;                 /* textContent, never innerHTML */
    var x = document.createElement("button");
    x.className = "ip-toast__x";
    x.type = "button";
    x.setAttribute("aria-label", "Dismiss");
    x.textContent = "×";
    el.appendChild(span);
    el.appendChild(x);
    toastHost().appendChild(el);

    var done = false;
    function dismiss() {
      if (done) return;
      done = true;
      el.classList.add("is-out");
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 240);
    }
    x.addEventListener("click", dismiss);
    var timer = setTimeout(dismiss, ms || 5200);
    el.addEventListener("mouseenter", function () { clearTimeout(timer); });
    el.addEventListener("mouseleave", function () { timer = setTimeout(dismiss, 2200); });
    return dismiss;
  }
  window.ipToast = toast;

  /* Server-rendered flashes become toasts, so feedback is noticed rather than
     pushing the page down. Falls back to the static block if JS never runs. */
  function liftFlashes() {
    var flashes = document.querySelectorAll(".ip-flash[data-toast]");
    for (var i = 0; i < flashes.length; i++) {
      var f = flashes[i];
      var kind = "info";
      if (f.classList.contains("success")) kind = "success";
      else if (f.classList.contains("danger")) kind = "danger";
      else if (f.classList.contains("warning")) kind = "warning";
      toast((f.textContent || "").trim(), kind);
      f.parentNode.removeChild(f);
    }
  }

  function boot() { initCounters(); liftFlashes(); }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else { boot(); }
})();

/* ---------- Minimize ----------
   Every card gets a button that folds it down to its title, so a page can be
   cut to just the panels in use. Remembered per page, per card. */
(function () {
  "use strict";
  function boot() {
    var KEY = "ip-min:" + location.pathname, saved = {};
    try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
    var cards = document.querySelectorAll(".card, .ip-card");
    Array.prototype.forEach.call(cards, function (card, i) {
      if (card.hasAttribute("data-no-min")) return;
      var head = card.querySelector(":scope > .card-header, :scope > .ip-card__head, :scope > .card-title");
      if (!head || head.querySelector(".mz-btn")) return;
      var tEl = head.querySelector(".card-title, .ip-card__title") || head;
      var title = (tEl.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60) || ("card" + i);
      head.classList.add("mz-head");
      var b = document.createElement("button");
      b.type = "button"; b.className = "mz-btn";
      function set(min) {
        card.classList.toggle("is-min", min);
        b.setAttribute("aria-expanded", String(!min));
        b.setAttribute("aria-label", (min ? "Expand " : "Minimize ") + title);
        b.title = min ? "Expand" : "Minimize";
        b.textContent = min ? "+" : "−";
      }
      b.addEventListener("click", function (ev) {
        ev.preventDefault(); ev.stopPropagation();
        var m = !card.classList.contains("is-min");
        set(m); saved[title] = m;
        try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) {}
      });
      head.appendChild(b);
      set(!!saved[title]);
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
