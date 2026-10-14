'use strict';

// Village Fairways service worker. It shows an offline page when a page load
// fails and shows phone notifications. It never caches app pages, scripts,
// styles, images, or API responses, so signed-in data is never stored or replayed.
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

// Phone notifications. Every push must show a notification: iOS revokes a
// subscription that receives a silent push.
self.addEventListener('push', (event) => {
  let title = 'Village Fairways';
  let body = '';
  let tag;
  let url = '/';
  try {
    const data = event.data ? event.data.json() : null;
    if (data && typeof data === 'object') {
      if (typeof data.title === 'string' && data.title) title = data.title;
      if (typeof data.body === 'string') body = data.body;
      if (typeof data.tag === 'string' && data.tag) tag = data.tag;
      if (typeof data.url === 'string' && data.url) url = data.url;
    }
  } catch (error) { /* unreadable data: show the generic notification */ }
  const options = {body, icon: '/static/icons/icon-192.png', data: {url}};
  if (tag) options.tag = tag;
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const data = event.notification.data;
  let target = '/';
  try {
    const parsed = new URL((data && data.url) || '/', self.location.origin);
    if (parsed.origin === self.location.origin) target = parsed.pathname + parsed.search + parsed.hash;
  } catch (error) { target = '/'; }
  event.waitUntil(
    self.clients.matchAll({type: 'window', includeUncontrolled: true}).then((windows) => {
      const open = windows.find((client) => {
        try { return new URL(client.url).origin === self.location.origin && typeof client.focus === 'function'; } catch (error) { return false; }
      });
      return open ? open.focus() : self.clients.openWindow(target);
    })
  );
});
