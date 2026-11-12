# Operations

Scripts for running the service on Google Cloud. Run them in Cloud Shell from inside the repository.

## Deploy

```bash
git pull
bash ops/deploy.sh
```

Copies `docs/` into the service, fetches the model weights if they are missing (from `~/backend/weights`
or the latest GitHub release) and deploys the `land-change` Cloud Run service.

## Cost limits

Three layers keep the running cost bounded, from the first to fire to the last resort:

| Layer | Default | What happens |
|---|---|---|
| Analyses per address | 40 units per hour (`HEAVY_LIMIT`, `HEAVY_WINDOW_S`) | `429` with `code: analysis_limit` |
| Analyses for the whole service | 400 units per UTC day (`DAILY_LIMIT`) | `503` with `code: daily_quota` until midnight UTC |
| Budget guard | the monthly budget in Cloud Billing | public access to the service is removed |

A unit is one Sentinel-2 scene downloaded and classified: an analysis is 1 unit, a comparison 2, a time
series one per year. Example analyses, the city ranking and the static pages cost nothing and keep working
when the limits are reached. The per-address and daily counters live in memory, which is exact with
`--max-instances 1`; a restart of the instance starts them again.

Change a limit without a new build:

```bash
gcloud run services update land-change --region europe-west1 --update-env-vars DAILY_LIMIT=200
```

## Budget guard

```bash
bash ops/setup-budget-guard.sh
```

Creates a Pub/Sub topic, connects the project's budget to it and deploys `ops/budget-guard/` as a Cloud
Run function. Cloud Billing reports the budget status several times a day; when the cost of the period
reaches the budget, the function removes `allUsers` from the service's invoker role. Google's front end
then refuses requests before they reach the container, so the service stops costing money. Nothing is
deleted.

Billing data arrives with a delay of hours, so the guard limits a runaway bill rather than stopping it at
the exact amount; the two quotas above are what keep the cost low in the first place.

Bring the site back once the cause is understood (a deploy with `ops/deploy.sh` does the same):

```bash
gcloud run services add-iam-policy-binding land-change --region europe-west1 \
  --member allUsers --role roles/run.invoker
```

Tests of the function: `cd ops/budget-guard && pip install -r requirements.txt pytest && pytest -q`.
