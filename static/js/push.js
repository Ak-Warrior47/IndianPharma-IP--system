/* "Turn on phone alerts": asks once, subscribes this phone or browser to the
   app's alerts (new and urgent bills, late, missing items, waiting bills),
   and shows a small bar until alerts are on or the person closes it. */
(function () {
  "use strict";
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    var ios = /iPhone|iPad/.test(navigator.userAgent) && !window.navigator.standalone;
    if (ios) bar("On iPhone: tap Share, then 'Add to Home Screen', open the app from there, then turn on alerts.", null);
    window.ipPushOn = function () { alert("This browser cannot show phone alerts. Use Chrome, or on iPhone add the app to the Home Screen first."); };
    return;
  }
  var tok = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  function keyBytes(b64) {
    var s = (b64 + "===".slice((b64.length + 3) % 4)).replace(/-/g, "+").replace(/_/g, "/"), raw = atob(s), a = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) a[i] = raw.charCodeAt(i);
    return a;
  }
  function post(url, body) {
    return fetch(url, {method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json", "X-CSRFToken": tok},
                       body: JSON.stringify(body)}).then(function (r) { return r.json(); });
  }
  function closed() { try { return localStorage.getItem("ipPushBar") === "closed"; } catch (e) { return false; } }
  function bar(text, action) {
    if (closed() || document.getElementById("ipPush")) return;
    var d = document.createElement("div");
    d.id = "ipPush"; d.setAttribute("role", "region"); d.setAttribute("aria-label", "Phone alerts");
    d.style.cssText = "position:fixed;left:12px;right:12px;bottom:12px;z-index:9998;max-width:520px;margin:0 auto;display:flex;gap:10px;align-items:center;" +
      "background:#16202f;color:#fff;border-radius:14px;padding:10px 12px;font:500 14px/1.35 system-ui,sans-serif;box-shadow:0 10px 30px rgba(0,0,0,.25)";
    d.innerHTML = '<span style="flex:1">' + text + "</span>" +
      (action ? '<button type="button" id="ipPushOn" style="border:0;border-radius:10px;padding:9px 12px;font:700 14px system-ui;background:#2fbf7f;color:#08321f;cursor:pointer">Turn on</button>' : "") +
      '<button type="button" id="ipPushX" aria-label="Close" style="border:0;background:transparent;color:#cbd5e1;font-size:20px;cursor:pointer;padding:4px 6px">×</button>';
    document.body.appendChild(d);
    document.getElementById("ipPushX").onclick = function () { try { localStorage.setItem("ipPushBar", "closed"); } catch (e) {} d.remove(); };
    if (action) document.getElementById("ipPushOn").onclick = action;
  }
  function turnOn() {
    var b = document.getElementById("ipPushOn"); if (b) { b.disabled = true; b.textContent = "…"; }
    Notification.requestPermission().then(function (perm) {
      if (perm !== "granted") throw new Error("blocked");
      return Promise.all([navigator.serviceWorker.register("/sw.js", {scope: "/"}), fetch("/push/key", {credentials: "same-origin"}).then(function (r) { return r.json(); })]);
    }).then(function (x) {
      return navigator.serviceWorker.ready.then(function (reg) {
        return reg.pushManager.getSubscription().then(function (old) {
          return old || reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: keyBytes(x[1].key)});
        });
      });
    }).then(function (sub) { return post("/push/subscribe", {subscription: sub.toJSON()}); })
      .then(function (j) {
        var d = document.getElementById("ipPush"); if (d) d.remove();
        if (j && j.ok) { var t = document.createElement("div"); t.textContent = "Phone alerts are on for this device.";
          t.style.cssText = "position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:9998;background:#0f5a3d;color:#fff;padding:10px 14px;border-radius:12px;font:600 14px system-ui";
          document.body.appendChild(t); setTimeout(function () { t.remove(); }, 4000); }
      }).catch(function (e) {
        var d = document.getElementById("ipPush"); if (d) d.remove();
        try { localStorage.removeItem("ipPushBar"); } catch (er) {}
        bar(e && e.message === "blocked" ? "Alerts are blocked for this site. Allow notifications in the browser settings, then reload." :
          "Could not turn on alerts here. Try Chrome, or add the app to the home screen.", null);
      });
  }
  window.ipPushOn = turnOn;          // the picking screen's "Turn on alerts" button
  // Already on? Refresh the subscription quietly. Otherwise offer the button.
  if (Notification.permission === "granted") {
    navigator.serviceWorker.register("/sw.js", {scope: "/"}).then(function () { return navigator.serviceWorker.ready; })
      .then(function (reg) { return reg.pushManager.getSubscription(); })
      .then(function (sub) { if (sub) post("/push/subscribe", {subscription: sub.toJSON()}); else turnOn(); }).catch(function () {});
  } else if (Notification.permission === "default") {
    bar("Get alerts on this phone for new bills, urgent bills and late picks.", turnOn);
  }
})();
