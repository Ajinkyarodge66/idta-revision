// Offline support. Pages: network first (always the latest version), cached copy only when offline.
// Images, icons and fonts: cached copy first, refreshed in the background.
const CACHE = 'idta-v15';
const FILES = ['./', './index.html', './privacy.html', './manifest.json', './icon.svg', './icon-192.png', './icon-512.png', './assets/shot-practice.png', './assets/shot-mock.png', './assets/shot-lab.png', './assets/shot-progress.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES.map(f => new Request(f, { cache: 'reload' })))));
  self.skipWaiting();
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
const keep = (req, res) => {
  if (res && res.ok && (req.url.startsWith(self.location.origin) || req.url.includes('fonts.g'))) {
    const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy));
  }
  return res;
};
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET' || req.url.includes('script.google')) return;
  const isPage = req.mode === 'navigate' || req.destination === 'document' || /\.html?(\?|$)|\/$/.test(new URL(req.url).pathname);
  if (isPage) {
    e.respondWith(fetch(req, { cache: 'no-store' }).then(res => keep(req, res)).catch(() => caches.match(req).then(hit => hit || caches.match('./index.html'))));
    return;
  }
  e.respondWith(caches.match(req).then(hit => {
    const net = fetch(req).then(res => keep(req, res)).catch(() => hit);
    return hit || net;
  }));
});
