/**
 * Minimal Aiko service worker.
 *
 * Goal: make the app installable (standalone PWA) without getting in the
 * way of a fast-moving build or the live API / WebSocket traffic. It is
 * deliberately conservative:
 *
 *   - API, WebSocket, avatar, attachment and persona-text requests are
 *     never intercepted (early return -> the browser handles them).
 *   - Navigations are network-first so a fresh ``index.html`` (and thus
 *     the latest hashed bundle) always wins when online; the cached shell
 *     is only used as an offline fallback.
 *   - Hashed build assets under ``/assets/`` are immutable, so they're
 *     cache-first once seen.
 *   - Everything else falls through to the network untouched.
 */
const CACHE = "aiko-shell-v1";

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.add("/").catch(() => undefined)),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)),
      );
      await self.clients.claim();
    })(),
  );
});

const BYPASS_PREFIXES = [
  "/api",
  "/ws",
  "/avatar",
  "/attachment-files",
  "/persona-text",
];

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // cross-origin: leave alone
  if (BYPASS_PREFIXES.some((p) => url.pathname.startsWith(p))) return;

  // Navigations: network-first, cached shell as the offline fallback.
  if (req.mode === "navigate") {
    event.respondWith(
      (async () => {
        try {
          const res = await fetch(req);
          const cache = await caches.open(CACHE);
          cache.put("/", res.clone());
          return res;
        } catch {
          const cached = await caches.match("/");
          return cached || Response.error();
        }
      })(),
    );
    return;
  }

  // Immutable hashed build assets: cache-first.
  if (url.pathname.startsWith("/assets/")) {
    event.respondWith(
      (async () => {
        const cached = await caches.match(req);
        if (cached) return cached;
        const res = await fetch(req);
        if (res && res.ok) {
          const cache = await caches.open(CACHE);
          cache.put(req, res.clone());
        }
        return res;
      })(),
    );
  }
  // Anything else (icons, manifest, live2d, etc.): default network handling.
});
