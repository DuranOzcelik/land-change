# Land Change API

FastAPI service that downloads Sentinel-2 imagery for any place, classifies it with a
ResNet18 fine-tuned on EuroSAT and reports land-cover statistics and change. In production the
same service also serves the site (`static/`, a copy of `../docs`), so site and API share one origin.

## Endpoints

| Path | Purpose |
|---|---|
| `GET /health` | Liveness check |
| `GET /api/meta` | Class names, groups, supported cell sizes |
| `GET /api/analyze` | Land-cover distribution of a place on one date |
| `GET /api/compare` | Change between two dates |
| `GET /api/timeseries` | Group shares for every summer in a range of years |
| `GET /api/dates` | Sentinel-2 acquisition dates available in a year |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `RATE_LIMIT` | `90` | Requests to `/api/*` allowed per client address in one window |
| `RATE_WINDOW_S` | `600` | Length of that window in seconds |
| `ALLOWED_ORIGINS` | empty | Comma-separated extra origins allowed by CORS (localhost is always allowed) |
| `API_DOCS` | unset | `1` serves interactive documentation at `/docs` |

## Run locally

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
# put the trained weights at weights/resnet18.pt
uvicorn app.main:app --reload
```

## Deploy to Cloud Run

```bash
rm -rf static && cp -r ../docs static
gcloud run deploy land-change --source . --region europe-west1 \
  --allow-unauthenticated --max-instances 1 --memory 2Gi --cpu 2 --timeout 300 --concurrency 4
```

## Tests

`pytest` runs smoke tests that need neither PyTorch, the weights nor network access.
