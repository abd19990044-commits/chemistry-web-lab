# Chemistry Tools — always-on web app for Hugging Face Spaces (Docker SDK)
FROM python:3.11-slim

# RDKit's Cairo drawer needs these system libs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxrender1 libxext6 libsm6 libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Hugging Face Spaces routes external traffic to port 7860 by default.
ENV PORT=7860
EXPOSE 7860

# A dedicated, non-root user (required by some Space runtimes).
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# --timeout is how long gunicorn lets a worker sit on a single request
# before killing it. 120s is too tight for /api/kaggle/download: within a
# single request it both pulls a job's full output from Kaggle AND streams
# it on to the browser, and real result bundles here run to hundreds of MB
# (528 MB in one observed run) — either leg alone can pass 120s on an
# ordinary connection, let alone both. 900s gives that room without
# leaving a truly-hung request stuck forever.
CMD ["gunicorn", "--bind", "0.0.0.0:7860", "--workers", "2", "--threads", "4", "--timeout", "900", "app:app"]
