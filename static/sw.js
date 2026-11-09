/* Indian Pharma: shows phone alerts sent by the app (Web Push), and opens the
   right page when one is tapped. */
self.addEventListener("install", function () { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener("push", function (e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = {title: "Indian Pharma", body: e.data ? e.data.text() : ""}; }
  e.waitUntil(self.registration.showNotification(d.title || "Indian Pharma", {
    body: d.body || "", tag: d.tag, renotify: true, requireInteraction: /late|urgent|missing/i.test(d.title || ""),
    icon: "/static/icons/icon-192.png", badge: "/static/icons/badge-72.png",
    vibrate: [200, 100, 200], data: {link: d.link || "/dashboard"}
  }));
});
self.addEventListener("notificationclick", function (e) {
  e.notification.close();
  var url = (e.notification.data && e.notification.data.link) || "/dashboard";
  e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      if (list[i].url.indexOf(self.location.origin) === 0 && "focus" in list[i]) { list[i].navigate(url); return list[i].focus(); }
    }
    return self.clients.openWindow(url);
  }));
});
