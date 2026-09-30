/* Service worker - Academia */
const CACHE_NAME = 'ikigai-static-v32';

self.addEventListener('install', (e) => {
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    // borrar cachés viejas para forzar actualizacion del SW en los dispositivos
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/')) return;
  if (url.pathname.startsWith('/static/uploads/')) return;
  e.respondWith(
    fetch(req).then((res) => {
      const copy = res.clone();
      if (res.ok && url.pathname.startsWith('/static/')) {
        caches.open(CACHE_NAME).then((c) => c.put(req, copy));
      }
      return res;
    }).catch(() =>
      caches.match(req))
  );
});

self.addEventListener('push', (e) => {
  let data = {};
  try { data = e.data.json(); } catch (err) {}
  const title = data.title || 'IKIGAI VIEDMA';
  const body = data.body || '';
  const options = {
    body: body,
    tag: 'ikigai-' + (data.url || 'general'),
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    vibrate: [200, 100, 200],
    renotify: true,
    data: { url: data.url || '/app' },
  };
  e.waitUntil(self.registration.showNotification(title, options).catch(() => {}));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/app';
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    for (const client of list) {
      if ('focus' in client) {
        client.postMessage({ type: 'ikigai-nav', url: url });
        return client.focus();
      }
    }
    return clients.openWindow(url);
  }));
});
