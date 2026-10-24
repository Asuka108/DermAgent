/* v1 极简 Service Worker：壳缓存 + API 直连（Android PWA 安装要求） */
const CACHE = 'derm-shell-v1';
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.add('/')).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys()
    .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return; // API 一律走网络
  e.respondWith(caches.open(CACHE).then((c) => c.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
    if (res.ok && url.origin === location.origin) c.put(e.request, res.clone());
    return res;
  }).catch(() => c.match('/')))));
});
