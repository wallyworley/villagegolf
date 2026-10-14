'use strict';

// Village Fairways service worker. Its only job is to show an offline page when
// a page load fails. It never caches app pages, scripts, styles, images, or
// API responses, so signed-in data is never stored or replayed.
const OFFLINE_CACHE = 'vf-offline-__OFFLINE_VERSION__';
const OFFLINE_URL = '/offline';

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(OFFLINE_CACHE)
      .then((cache) => cache.add(new Request(OFFLINE_URL, {cache: 'reload'})))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names.filter((name) => name !== OFFLINE_CACHE).map((name) => caches.delete(name))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET' || request.mode !== 'navigate') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  event.respondWith(fetch(request).catch(() => caches.match(OFFLINE_URL)));
});
