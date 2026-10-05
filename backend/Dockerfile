FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# System library required by rasterio (GDAL)
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# CPU-only PyTorch keeps the image small
RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY weights ./weights
COPY static ./static

RUN useradd --create-home runner
USER runner

CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
