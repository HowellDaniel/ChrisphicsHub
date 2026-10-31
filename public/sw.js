'use strict';

/* Bumped again for the weekly software answer: Settings ▸ Updates is now drawn from
   facts.updates, which only an engine carrying the weekly check fills in, so a device still holding
   v19 would render "undefined" rows over a build that knows nothing about the cadence. The shell
   list itself is unchanged — /api/counter and /api/update-check are API routes, which the worker
   already lets past to the shop, and neither is prefilled: an update answer belongs to the computer
   that got it, not to a phone that read it once on the bus. */
const CACHE_NAME = 'crispprint-shell-v22';
const APP_SHELL = [
  '/',
  '/index.html',
  '/styles.css',
  '/app.js',
  '/manifest.webmanifest',
  '/img/brand.png',
  '/img/mark.png',
  '/img/icon-192.png',
  '/img/icon-512.png',
  '/img/icon-maskable-512.png',
  '/img/apple-touch-icon.png',
  '/img/bg-violet.png',
  '/img/bg-beams.png',
  '/img/bg-circuit.png',
  '/img/bg-constellation.png',
  '/img/bg-dusk.png',
  '/img/bg-bloom.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) => Promise.all(
      names.filter((name) => name.startsWith('crispprint-shell-') && name !== CACHE_NAME)
        .map((name) => caches.delete(name)),
    )),
  );
  self.clients.claim();
});

/* How long a page opening from a bookmark waits for the shop's Mac before it shows the copy of
   itself this device already holds. A Mac that is switched off never refuses the call, so waiting
   for it is a blank screen for the length of a network timeout; a second and a half is more than
   the shop takes when it is awake. */
const SHELL_WAIT_MS = 1500;

function remember(request, response) {
  if (response && response.ok) {
    const copy = response.clone();
    caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
  }
  return response;
}

function fromShop(request) {
  return fetch(request).then((response) => remember(request, response), () => null);
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;

  /* The shell is shown out of this cache the moment the device holds it, and the network only ever
     updates it behind the page's back. That is what lets the dashboard open at all with the shop's
     Mac switched off. A deploy still lands on the next load: a new CACHE_NAME empties the old copies
     when this worker activates, so a device with nothing to show goes and asks the shop. */
  if (request.mode === 'navigate') {
    const fromMemory = new Promise((resolve) => {
      setTimeout(() => {
        caches.match(request)
          .then((cached) => cached || caches.match('/'))
          .then((cached) => resolve(cached || null));
      }, SHELL_WAIT_MS);
    });
    event.respondWith(Promise.race([fromShop(request), fromMemory])
      .then((response) => response || fromMemory)
      .then((response) => response || caches.match('/'))
      .then((response) => response || Response.error()));
    return;
  }

  event.respondWith(caches.match(request).then((cached) => {
    if (cached) {
      fromShop(request);
      return cached;
    }
    return fromShop(request).then((response) => response || Response.error());
  }));
});
