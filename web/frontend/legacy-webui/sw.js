/* 肤智 PWA Service Worker：缓存应用壳（断网可开壳，AI/RAG 不可用时明确提示） */
const CACHE = "dermagent-shell-v1";
const SHELL = ["/", "/app.js", "/manifest.webmanifest"];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) return; // API 永远直连（失败即提示，不伪造）
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy));
      return res;
    }).catch(() => caches.match("/")))
  );
});
