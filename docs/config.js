// Address of the Land Change API (Cloud Run). Override for local work with ?api=http://localhost:8000
window.LAND_CHANGE_API =
  new URLSearchParams(location.search).get("api") ||
  "https://land-change-api-1098531335280.europe-west1.run.app";
