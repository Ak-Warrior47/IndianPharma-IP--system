/* Enter moves to the next empty field; when every field is filled, Enter
   submits as usual. Works on every form in the app. Skipped when something
   else already handled the key (e.g. the email suggestion), inside text
   areas, and on forms marked data-enter="off". */
(function () {
  "use strict";
  var FIELDS = 'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="submit"]):not([type="button"])' +
               ':not([disabled]):not([readonly]), select:not([disabled]), textarea:not([disabled]):not([readonly])';
  function visible(el) { return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length); }
  function empty(el) { return el.tagName === "SELECT" ? !el.value : !String(el.value || "").trim(); }
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || e.defaultPrevented || e.isComposing || e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target;
    if (!t || !t.matches || !t.matches("input, select") || t.matches('[type="submit"], [type="button"], [type="checkbox"], [type="radio"]')) return;
    var form = t.form;
    if (!form || form.getAttribute("data-enter") === "off") return;
    var all = Array.prototype.filter.call(form.querySelectorAll(FIELDS), visible);
    var i = all.indexOf(t);
    // the next empty field after this one, then any empty field before it
    var next = all.slice(i + 1).concat(all.slice(0, Math.max(i, 0))).filter(function (el) { return el !== t && empty(el); })[0];
    if (next) {
      e.preventDefault();
      next.focus();
      if (next.select && next.tagName === "INPUT") { try { next.select(); } catch (er) {} }
    }
  });
})();
