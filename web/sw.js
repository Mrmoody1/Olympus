/* Olympus service worker: keeps the page itself on the phone so it opens instantly. */
const SHELL = 'olympus-shell-1.5';
self.addEventListener('install', (e) => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(['/', '/manifest.webmanifest', '/icon-192.png'])).then(() => self.skipWaiting())); });
self.addEventListener('activate', (e) => { e.waitUntil((async () => { for (const k of await caches.keys()) if (k !== SHELL) await caches.delete(k); await self.clients.claim(); })()); });
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || /^\/(nav|jf|api)\//.test(url.pathname)) return;
  e.respondWith((async () => {
    const cache = await caches.open(SHELL); const key = e.request.mode === 'navigate' ? '/' : url.pathname;
    try { const r = await fetch(e.request); if (r.ok) cache.put(key, r.clone()); return r; }
    catch { return (await cache.match(key)) || new Response('Offline', { status: 503 }); }
  })());
});
