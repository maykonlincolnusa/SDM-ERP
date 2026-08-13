(() => {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;

  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/service-worker.js", { scope: "/" }).catch((error) => {
      console.warn("PWA service worker registration failed.", error);
    });
  });
})();
