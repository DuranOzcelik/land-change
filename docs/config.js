// Address of the Land Change API. The published site and its API share one origin.
// For local work (page opened from localhost), the public API is used; ?api=http://localhost:8000
// overrides it, but only towards a local server: on the published site a crafted link must not
// send visitors' searches to, or show results from, another server.
window.LAND_CHANGE_API = (() => {
  const PUBLIC = "https://land-change-1098531335280.europe-west1.run.app";
  const local = (host) => host === "localhost" || host === "127.0.0.1";
  if (location.protocol !== "file:" && !local(location.hostname)) return location.origin;
  const wanted = new URLSearchParams(location.search).get("api");
  if (!wanted) return PUBLIC;
  try {
    const url = new URL(wanted);
    return /^https?:$/.test(url.protocol) && local(url.hostname) ? url.origin : PUBLIC;
  } catch {
    return PUBLIC;
  }
})();
