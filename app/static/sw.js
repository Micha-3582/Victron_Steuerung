/* Service Worker – nötig, damit die App installierbar ist.
 *
 * Bewusst zurückhaltend:
 *  - Seiten und /api/ laufen IMMER übers Netz. Sonst könnte eine zwischen-
 *    gespeicherte Seite die Anmeldung aushebeln oder veraltete Live-Werte zeigen.
 *  - Nur /static/ wird zwischengespeichert (erst aus dem Speicher ausliefern,
 *    im Hintergrund erneuern) – das macht den Start spürbar schneller.
 */
'use strict';

const VERSION = 'victron-steuerung-v1';
const STATIC_CACHE = VERSION + '-static';

self.addEventListener('install', ev => {
  self.skipWaiting();               // neue Fassung sofort übernehmen
});

self.addEventListener('activate', ev => {
  ev.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter(n => n !== STATIC_CACHE).map(n => caches.delete(n)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', ev => {
  const req = ev.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;      // fremde Hosts nicht anfassen
  if (!url.pathname.startsWith('/static/')) return;      // Seiten & API: direkt ans Netz

  ev.respondWith((async () => {
    const cache = await caches.open(STATIC_CACHE);
    const hit = await cache.match(req);
    const netz = fetch(req).then(res => {
      if (res && res.status === 200) cache.put(req, res.clone());
      return res;
    }).catch(() => null);
    return hit || (await netz) || new Response('', { status: 504 });
  })());
});
