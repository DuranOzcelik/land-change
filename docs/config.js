// Address of the Land Change API (Cloud Run). For local work, ?api=http://localhost:8000 overrides it,
// but only while the page itself runs locally and only towards a local server: on the published
// site a crafted link must not send visitors' searches to, or show results from, another server.
window.LAND_CHANGE_API = (() => {
  const DEFAULT = "https://land-change-api-1098531335280.europe-west1.run.app";
  const local = (host) => host === "localhost" || host === "127.0.0.1";
  const wanted = new URLSearchParams(location.search).get("api");
  if (!wanted || !(location.protocol === "file:" || local(location.hostname))) return DEFAULT;
  try {
    const url = new URL(wanted);
    return /^https?:$/.test(url.protocol) && local(url.hostname) ? url.origin : DEFAULT;
  } catch {
    return DEFAULT;
  }
})();
