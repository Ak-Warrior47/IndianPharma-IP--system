/* Minimise / maximise for report sections. Any .pa-box, .st-box, .st-p,
   .mi-box or .pm-box gets a − / + button on its title; folded, only the title
   shows. Remembered per page and title on this device. */
(function () {
  "use strict";
  function boot() {
    var KEY = "ip-fold:" + location.pathname, saved = {};
    try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) {}
    var css = document.createElement("style");
    css.textContent = ".fd-head{display:flex!important;align-items:center;justify-content:space-between;gap:10px}" +
      ".fd-btn{flex:none;width:34px;height:34px;border:1.5px solid var(--line,#dfe4ec);border-radius:9px;background:transparent;color:inherit;font:700 18px/1 system-ui,sans-serif;cursor:pointer}" +
      ".fd-btn:hover,.fd-btn:focus-visible{border-color:var(--brand,#1e40af)}" +
      ".is-fold>:not(.fd-head){display:none!important}.is-fold>.fd-head{margin-bottom:0!important;border-bottom:0!important;cursor:pointer}";
    document.head.appendChild(css);
    Array.prototype.forEach.call(document.querySelectorAll(".pa-box, .st-box, .st-p, .mi-box, .pm-box"), function (box, i) {
      var head = box.querySelector(":scope > h2, :scope > header");
      if (!head || head.querySelector(".fd-btn")) return;
      var title = (head.textContent || "").replace(/\s+/g, " ").trim().slice(0, 50) || ("section" + i);
      // a title that carries a count ("Bills 12") keeps its place when the count changes
      var key = title.replace(/[\d().·,:]+/g, " ").replace(/\s+/g, " ").trim() || title;
      var wrap = document.createElement("span");
      while (head.firstChild) wrap.appendChild(head.firstChild);
      head.appendChild(wrap);
      head.classList.add("fd-head");
      var b = document.createElement("button");
      b.type = "button"; b.className = "fd-btn";
      function set(f) {
        box.classList.toggle("is-fold", f);
        b.textContent = f ? "+" : "−";
        b.setAttribute("aria-expanded", String(!f));
        b.setAttribute("aria-label", (f ? "Maximise " : "Minimise ") + key);
        b.title = f ? "Maximise" : "Minimise";
      }
      function keep(f) { saved[key] = f; try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) {} }
      b.addEventListener("click", function (e) { e.stopPropagation(); var f = !box.classList.contains("is-fold"); set(f); keep(f); });
      head.addEventListener("click", function (e) {
        if (!box.classList.contains("is-fold") || e.target.closest("a, button, input, select")) return;
        set(false); keep(false);
      });
      head.appendChild(b);
      set(!!saved[key]);
      box._fdOpen = function () { set(false); };
    });
    // a link that jumps to something inside a folded section opens it
    function openHash() {
      var el = location.hash.length > 1 && document.getElementById(location.hash.slice(1));
      var box = el && el.closest(".is-fold");
      if (box && box._fdOpen) { box._fdOpen(); el.scrollIntoView(); }
    }
    window.addEventListener("hashchange", openHash);
    openHash();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
