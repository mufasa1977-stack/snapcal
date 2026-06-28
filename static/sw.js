/* SnapCal service worker — network-first (always fresh), cache fallback for offline shell.
   NEVER caches /api/ (live data stays live). Minimal + safe for a deployed app. */
const CACHE = 'snapcal-v2';
const SHELL = ['/', '/manifest.webmanifest', '/static/icons/icon-192.png'];

self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); }).then(function () { return self.skipWaiting(); }));
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (ks) {
      return Promise.all(ks.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

// Proactive Coach Cal check-ins (web push). Payload = {title, body, url}.
self.addEventListener('push', function (e) {
  var data = {};
  try { data = e.data ? e.data.json() : {}; } catch (err) { data = { body: (e.data && e.data.text()) || '' }; }
  var title = data.title || 'Coach Cal';
  var body = data.body || "Time for a quick check-in.";
  e.waitUntil(self.registration.showNotification(title, {
    body: body,
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    tag: 'coachcal-checkin',          // a newer check-in replaces an unread one
    renotify: true,
    data: { url: data.url || '/' }
  }));
});

// Tapping a check-in focuses an open SnapCal tab, or opens one.
self.addEventListener('notificationclick', function (e) {
  e.notification.close();
  var target = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (wins) {
    for (var i = 0; i < wins.length; i++) {
      if ('focus' in wins[i]) { wins[i].navigate && wins[i].navigate(target); return wins[i].focus(); }
    }
    if (clients.openWindow) return clients.openWindow(target);
  }));
});

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (url.pathname.indexOf('/api/') === 0) return;            // live API: always network, never cached
  e.respondWith(
    fetch(req).then(function (r) {
      if (r && r.status === 200 && url.origin === location.origin) {
        var cp = r.clone(); caches.open(CACHE).then(function (c) { c.put(req, cp); });
      }
      return r;
    }).catch(function () {
      return caches.match(req).then(function (r) { return r || caches.match('/'); });
    })
  );
});
