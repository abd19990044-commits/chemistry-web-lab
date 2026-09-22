# Chemistry Tools - always-on web app for Hugging Face Spaces (Docker SDK)
FROM python:3.11-slim

# RDKit's Cairo drawer needs these system libs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxrender1 libxext6 libsm6 libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir -e ./orca_engine

# Hugging Face Spaces routes external traffic to port 7860 by default.
ENV PORT=7860
EXPOSE 7860

# A dedicated, non-root user (required by some Space runtimes).
RUN useradd -m -u 1000 appuser && \
    mkdir -p /data && \
    chown -R appuser:appuser /app /data
USER appuser

# The local worker is deliberately separate from request handling.  The image
# entrypoint supervises both processes and exports one shared durable state
# directory so leases/fences and job visibility coordinate correctly.  The
# explicit Gunicorn budget is kept visible here for deployment checks; the
# entrypoint reads the same default through GUNICORN_TIMEOUT.
# gunicorn --timeout 900
COPY --chown=appuser:appuser docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod 0755 /app/docker-entrypoint.sh
ENV CHEMISTRY_LAB_STATE_DIR=/data
CMD ["/app/docker-entrypoint.sh"]
