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

# Keep one Gunicorn worker because the application has process-local session
# state and the legacy Kaggle runner is not designed as a multi-process
# coordinator. Threads still allow concurrent HTTP requests while avoiding
# duplicate worker state, inconsistent fallback Flask secret keys, and races
# during Kaggle job polling/submission.
# The publication bootstrap imports the established Flask app and adds only
# vector-export endpoints plus download controls; it does not replace the app.
CMD ["gunicorn", "--bind", "0.0.0.0:7860", "--workers", "1", "--threads", "8", "--timeout", "900", "--graceful-timeout", "30", "--keep-alive", "5", "publication_bootstrap:app"]
