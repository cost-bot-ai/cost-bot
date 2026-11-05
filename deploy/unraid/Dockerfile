FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir "httpx>=0.27,<1" \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /data && chown app /data

WORKDIR /app
# Only the three scripts the check needs; nothing from raw/, docs/ or tests/.
COPY scripts/spike_retailers.py scripts/spike_walmart_sellers.py scripts/guarded_run.py scripts/

USER app

# No default command: `docker compose up` must never start retailer traffic by itself.
ENTRYPOINT ["python", "scripts/guarded_run.py"]
