#!/usr/bin/env bash
# Builds the site and API into one Cloud Run service and deploys it.
# Run in Cloud Shell from anywhere inside the repository:  bash ops/deploy.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-europe-west1}"
SERVICE="${SERVICE:-land-change}"
WEIGHTS="$ROOT/backend/weights/resnet18.pt"
RELEASE_URL="https://github.com/DuranOzcelik/land-change/releases/latest/download/resnet18.pt"

[ -n "$PROJECT" ] || { echo "No project selected. Run: gcloud config set project PROJECT_ID" >&2; exit 1; }

if [ ! -s "$WEIGHTS" ]; then
  if [ -s "$HOME/backend/weights/resnet18.pt" ]; then
    echo "==> Copying model weights from ~/backend/weights"
    cp "$HOME/backend/weights/resnet18.pt" "$WEIGHTS"
  else
    echo "==> Downloading model weights from the latest GitHub release"
    curl -fsSL -o "$WEIGHTS" "$RELEASE_URL"
  fi
fi

echo "==> Copying the site into the service"
rm -rf "$ROOT/backend/static"
cp -r "$ROOT/docs" "$ROOT/backend/static"

echo "==> Deploying $SERVICE to $REGION in $PROJECT"
cd "$ROOT/backend"
gcloud run deploy "$SERVICE" --source . --region "$REGION" --project "$PROJECT" --allow-unauthenticated \
  --max-instances 1 --memory 2Gi --cpu 2 --timeout 300 --concurrency 40 \
  --service-account "land-change-run@$PROJECT.iam.gserviceaccount.com" --quiet

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format='value(status.url)')"
echo "==> Health check: $(curl -s -o /dev/null -w '%{http_code}' "$URL/health") from $URL/health"
echo "==> Analyses left today: $(curl -s "$URL/api/meta" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("analyses_left_today", "?"))')"
