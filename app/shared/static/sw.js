// The minimal service worker (PRD section 8, "Installable"). It lets a phone add the app to its
// home screen, and keeps a copy of the app shell: the shared CSS, scripts, fonts and logo.
//
// It never stores a page. Pages hold customer names and figures, and a stored page could be
// out of date or seen after signing out. There is no offline data in this version.
//
// main.py serves this file at /sw.js, so it controls the whole app, not only /static/.

var SHELL_CACHE = "shell-v1";
var SHELL_FILES = [
  "/static/shared/css/tokens.css",
  "/static/shared/css/base.css",
  "/static/shared/css/components.css",
  "/static/shared/js/htmx.min.js",
  "/static/shared/js/app.js",
  "/static/shared/fonts/inter-latin-400-normal.woff2",
  "/static/shared/fonts/inter-latin-600-normal.woff2",
  "/static/shared/fonts/inter-latin-700-normal.woff2",
  "/static/shared/fonts/jetbrains-mono-latin-600-normal.woff2",
  "/static/shared/img/logo-full.png",
  "/static/shared/img/logo-mark.png",
  "/static/shared/img/icon-192.png"
];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(function (cache) {
      return cache.addAll(SHELL_FILES);
    })
  );
});

// A new version of this file removes the copies an older version kept
self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.filter(function (name) {
        return name !== SHELL_CACHE;
      }).map(function (name) {
        return caches.delete(name);
      }));
    })
  );
});

// Shell files: the network first, so an update is seen at once, and the kept copy only when the
// network fails. Everything else (every page and every form) goes to the server as usual.
self.addEventListener("fetch", function (event) {
  var url = new URL(event.request.url);
  var isShellFile = event.request.method === "GET" && url.origin === self.location.origin &&
    SHELL_FILES.indexOf(url.pathname) !== -1;
  if (!isShellFile) {
    return;
  }
  event.respondWith(
    fetch(event.request).catch(function () {
      return caches.match(event.request);
    })
  );
});
