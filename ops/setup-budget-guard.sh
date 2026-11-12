#!/usr/bin/env bash
# Connects the project's budget to a small Cloud Run function that takes the site offline
# when the monthly budget is spent. Safe to run again.
# Run in Cloud Shell from anywhere inside the repository:  bash ops/setup-budget-guard.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-europe-west1}"
SERVICE="${SERVICE:-land-change}"
TOPIC="${TOPIC:-budget-alerts}"
FUNCTION="${FUNCTION:-budget-guard}"
SA="budget-guard@$PROJECT.iam.gserviceaccount.com"

[ -n "$PROJECT" ] || { echo "No project selected. Run: gcloud config set project PROJECT_ID" >&2; exit 1; }

retry() {   # new service accounts take a little while to become usable
  local n
  for n in 1 2 3 4 5 6; do "$@" && return 0; echo "   retrying in 10 s ($n/6)"; sleep 10; done
  return 1
}

echo "==> Enabling APIs"
gcloud services enable pubsub.googleapis.com cloudfunctions.googleapis.com eventarc.googleapis.com \
  cloudbuild.googleapis.com run.googleapis.com artifactregistry.googleapis.com \
  billingbudgets.googleapis.com cloudbilling.googleapis.com --project "$PROJECT"

echo "==> Pub/Sub topic $TOPIC"
gcloud pubsub topics describe "$TOPIC" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud pubsub topics create "$TOPIC" --project "$PROJECT"

echo "==> Service account for the guard"
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud iam service-accounts create budget-guard --display-name "Budget guard" --project "$PROJECT"

echo "==> Allowing the guard to change access to $SERVICE (and nothing else)"
retry gcloud run services add-iam-policy-binding "$SERVICE" --region "$REGION" --project "$PROJECT" \
  --member "serviceAccount:$SA" --role roles/run.admin --quiet >/dev/null

echo "==> Deploying the function (takes a few minutes)"
retry gcloud functions deploy "$FUNCTION" --gen2 --runtime python312 --region "$REGION" --project "$PROJECT" \
  --source "$HERE/budget-guard" --entry-point on_budget --trigger-topic "$TOPIC" \
  --service-account "$SA" --trigger-service-account "$SA" \
  --set-env-vars "PROJECT_ID=$PROJECT,REGION=$REGION,SERVICE=$SERVICE" \
  --memory 256Mi --max-instances 1 --no-allow-unauthenticated --quiet
# The trigger delivers budget messages as the guard's account, which must be allowed to call the function.
retry gcloud run services add-iam-policy-binding "$FUNCTION" --region "$REGION" --project "$PROJECT" \
  --member "serviceAccount:$SA" --role roles/run.invoker --quiet >/dev/null

echo "==> Connecting the budget to the topic"
BILLING="$(gcloud billing projects describe "$PROJECT" --format='value(billingAccountName)')"
BILLING="${BILLING#billingAccounts/}"
[ -n "$BILLING" ] || { echo "The project has no billing account." >&2; exit 1; }
mapfile -t BUDGETS < <(gcloud billing budgets list --billing-account "$BILLING" --format='value(name,displayName)')
if [ "${#BUDGETS[@]}" -eq 0 ]; then
  echo "No budget found. Create one in the console (Billing > Budgets & alerts) and run this again." >&2
  exit 1
fi
CHOSEN=""
for line in "${BUDGETS[@]}"; do [[ "$line" == *land-change* ]] && CHOSEN="$line" && break; done
[ -n "$CHOSEN" ] || CHOSEN="${BUDGETS[0]}"
BUDGET="${CHOSEN%%$'\t'*}"
echo "   budget: ${CHOSEN#*$'\t'} (${BUDGET##*/})"
gcloud pubsub topics add-iam-policy-binding "$TOPIC" --project "$PROJECT" \
  --member serviceAccount:billing-budget-alert@system.gserviceaccount.com --role roles/pubsub.publisher >/dev/null
gcloud billing budgets update "${BUDGET##*/}" --billing-account "$BILLING" \
  --notifications-rule-pubsub-topic "projects/$PROJECT/topics/$TOPIC" >/dev/null

echo "==> Test: a notification below the budget must change nothing"
echo "   waiting 60 s for the new permissions to take effect"
sleep 60
PASSED=""
for attempt in 1 2 3; do
  gcloud pubsub topics publish "$TOPIC" --project "$PROJECT" \
    --message '{"costAmount": 0, "budgetAmount": 1, "currencyCode": "TEST"}' >/dev/null
  sleep 45
  if gcloud functions logs read "$FUNCTION" --region "$REGION" --project "$PROJECT" --limit 20 \
      --format='value(log)' | grep -q "Within budget"; then
    PASSED=1; break
  fi
  echo "   no answer from the function yet; trying again ($attempt/3)"
done
if [ -n "$PASSED" ]; then
  echo "   the function received the test message and did nothing, as expected"
else
  echo "WARNING: the function did not answer the test message. Run this script again in a few minutes;" >&2
  echo "         if it still fails, check the logs: gcloud functions logs read $FUNCTION --region $REGION" >&2
fi
URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --project "$PROJECT" --format='value(status.url)')"
echo "==> Site still public: HTTP $(curl -s -o /dev/null -w '%{http_code}' "$URL/health") (200 expected)"
echo "Done. When the budget is spent the site goes offline; see ops/README.md to bring it back."
