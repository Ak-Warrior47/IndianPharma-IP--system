/* Email suggestion: type "lisa" and get "lisa@pharmaip.com".
 *
 * Attaches to every email box on the page -- input[type=email],
 * input[name=email], or anything marked data-email-suggest -- so a page that
 * adds an email field gets it just by loading this file. Opt out with
 * data-email-suggest="off"; change the domain with data-email-domain.
 *
 * Only ever a suggestion: an address on another domain is typed as normal.
 * Keyboard: Down/Up to move, Enter or Tab to take it, Esc to close.
 * Touch: tap the suggestion. Screen readers hear it as a listbox.
 */
(function () {
  "use strict";
  var DEFAULT_DOMAIN = (window.EMAIL_DOMAIN || "pharmaip.com").replace(/^@/, "");
  var uid = 0;

  function css() {
    if (document.getElementById("es-style")) return;
    var st = document.createElement("style");
    st.id = "es-style";
    st.textContent =
      ".es-wrap{position:relative}" +
      ".es-list{position:absolute;left:0;right:0;top:calc(100% + 6px);z-index:999;margin:0;padding:6px;list-style:none;" +
      "background:#fff;border:1px solid #E2DBCF;border-radius:12px;" +
      "box-shadow:0 18px 40px -16px rgba(22,32,47,.35),0 2px 6px rgba(22,32,47,.06);display:none;" +
      "transform-origin:top center}" +
      ".es-list.on{display:block;animation:es-in .18s cubic-bezier(.2,.7,.2,1)}" +
      "@keyframes es-in{from{opacity:0;transform:translateY(-4px) scale(.98)}to{opacity:1;transform:none}}" +
      ".es-opt{padding:10px 12px;min-height:44px;box-sizing:border-box;display:flex;align-items:center;gap:1px;" +
      "font:inherit;font-size:15.5px;color:#16202F;cursor:pointer;border-radius:8px;word-break:break-all;transition:background .15s}" +
      ".es-opt b{font-weight:600}.es-opt span{color:#146B4A;font-weight:500}" +
      ".es-opt[aria-selected=true],.es-opt:hover{background:#EDF5F0}" +
      ".es-hint{display:block;padding:2px 12px 6px;font-size:12px;color:#6B7280}" +
      "@media (prefers-reduced-motion:reduce){.es-list.on{animation:none}}";
    document.head.appendChild(st);
  }

  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function suggestionFor(value, domain) {
    var v = (value || "").trim();
    if (!v || /\s/.test(v)) return null;
    var at = v.indexOf("@");
    if (at === -1) return v + "@" + domain;
    if (at === 0 || v.indexOf("@", at + 1) !== -1) return null;
    var local = v.slice(0, at), typed = v.slice(at + 1).toLowerCase();
    if (typed === domain) return null;                 // already complete
    if (domain.indexOf(typed) === 0) return local + "@" + domain;
    return null;                                       // another domain: leave it
  }

  function attach(input) {
    if (input.getAttribute("data-es-ready") === "1") return;
    if (input.getAttribute("data-email-suggest") === "off") return;
    input.setAttribute("data-es-ready", "1");
    css();
    var domain = (input.getAttribute("data-email-domain") || DEFAULT_DOMAIN).replace(/^@/, "");
    var parent = input.parentNode;
    if (getComputedStyle(parent).position === "static") parent.classList.add("es-wrap");
    var id = "es-list-" + (++uid);
    var list = document.createElement("ul");
    list.className = "es-list";
    list.id = id;
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "Email suggestion");
    parent.appendChild(list);
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", id);
    input.setAttribute("aria-expanded", "false");
    var current = null, active = false;

    function close() {
      list.classList.remove("on");
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = false;
    }
    function open(sug) {
      current = sug;
      var at = sug.indexOf("@");
      list.innerHTML = '<li class="es-opt" role="option" id="' + id + '-0" aria-selected="false">' +
        '<b>' + esc(sug.slice(0, at)) + '</b><span>' + esc(sug.slice(at)) + '</span></li>' +
        '<li class="es-hint" aria-hidden="true">Tap, or press Enter</li>';
      list.classList.add("on");
      input.setAttribute("aria-expanded", "true");
      active = false;
    }
    function highlight(on) {
      var opt = list.querySelector(".es-opt");
      if (!opt) return;
      active = on;
      opt.setAttribute("aria-selected", on ? "true" : "false");
      if (on) input.setAttribute("aria-activedescendant", opt.id);
      else input.removeAttribute("aria-activedescendant");
    }
    function take() {
      if (!current) return;
      input.value = current;
      close();
      // Let the page react as if it had been typed (form checks, counters).
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      var form = input.form;
      if (form) {
        var fields = Array.prototype.filter.call(form.elements, function (el) {
          return el.tagName === "INPUT" && el.type !== "hidden" && !el.disabled;
        });
        var next = fields[fields.indexOf(input) + 1];
        if (next) next.focus();
      }
    }
    function update() {
      var sug = suggestionFor(input.value, domain);
      if (sug && document.activeElement === input) open(sug); else close();
    }

    input.addEventListener("input", update);
    input.addEventListener("focus", update);
    input.addEventListener("blur", function () { setTimeout(close, 120); });
    input.addEventListener("keydown", function (e) {
      if (!list.classList.contains("on")) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); highlight(true); }
      else if (e.key === "Enter" || (e.key === "Tab" && !e.shiftKey)) { e.preventDefault(); take(); }
      else if (e.key === "Escape") { e.preventDefault(); close(); }
    });
    // pointerdown fires before the input loses focus, so the tap always lands.
    list.addEventListener("pointerdown", function (e) {
      if (e.target.closest(".es-opt")) { e.preventDefault(); take(); }
    });
    list.addEventListener("mousedown", function (e) { e.preventDefault(); });
  }

  function scan(root) {
    (root || document).querySelectorAll(
      'input[type="email"], input[name="email"], input[data-email-suggest]'
    ).forEach(attach);
  }
  window.EmailSuggest = { scan: scan, suggestionFor: suggestionFor };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { scan(); });
  else scan();
  // Boxes added later (modals, panels) are picked up too.
  if (window.MutationObserver) {
    new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        if (muts[i].addedNodes.length) { scan(); break; }
      }
    }).observe(document.documentElement, { childList: true, subtree: true });
  }
})();
