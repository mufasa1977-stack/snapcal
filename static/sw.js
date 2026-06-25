/* SnapCal service worker — network-first (always fresh), cache fallback for offline shell.
   NEVER caches /api/ (live data stays live). Minimal + safe for a deployed app. */
const CACHE = 'snapcal-v1';
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
