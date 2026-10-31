/* Olympus service worker: keeps the page itself on the phone so it opens instantly. */
const SHELL = 'olympus-shell-1.6';
const SHARED = 'olympus-shared';   // films/songs shared from other phone apps, waiting for the page to upload them
self.addEventListener('install', (e) => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(['/', '/manifest.webmanifest', '/icon-192.png'])).then(() => self.skipWaiting())); });
self.addEventListener('activate', (e) => { e.waitUntil((async () => { for (const k of await caches.keys()) if (k !== SHELL && k !== SHARED) await caches.delete(k); await self.clients.claim(); })()); });
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // "Share to Olympus" from another app: keep the files, then open Olympus, which uploads them to the inbox.
  if (e.request.method === 'POST' && url.origin === location.origin && url.pathname === '/share') {
    e.respondWith((async () => {
      const files = (await e.request.formData()).getAll('media').filter(f => f && f.size);
      const cache = await caches.open(SHARED);
      for (const k of await cache.keys()) await cache.delete(k);
      for (const [i, f] of files.entries())
        await cache.put(`/shared/${i}`, new Response(f, { headers: { 'Content-Type': f.type || 'application/octet-stream', 'X-Name': encodeURIComponent(f.name) } }));
      return Response.redirect(`/?shared=${files.length}`, 303);
    })());
    return;
  }
  if (e.request.method !== 'GET' || url.origin !== location.origin || /^\/(nav|jf|api)\//.test(url.pathname)) return;
  e.respondWith((async () => {
    const cache = await caches.open(SHELL); const key = e.request.mode === 'navigate' ? '/' : url.pathname;
    try { const r = await fetch(e.request); if (r.ok) cache.put(key, r.clone()); return r; }
    catch { return (await cache.match(key)) || new Response('Offline', { status: 503 }); }
  })());
});
