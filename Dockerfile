# syntax=docker/dockerfile:1
# Pinned by digest so every build of a release uses the same base. Update both together.
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOUSEKEEPING_DB=/data/housekeeping.db \
    FORWARDED_ALLOW_IPS=*

WORKDIR /app

COPY requirements.lock .
RUN pip install -r requirements.lock

COPY app ./app
COPY static ./static

# Run as an unprivileged user. /data holds the database and documents; mount a volume there.
# A bind-mounted host folder must be writable by uid 1000: sudo chown -R 1000:1000 <folder>
RUN useradd --create-home --uid 1000 housekeeping \
    && mkdir -p /data \
    && chown housekeeping:housekeeping /data
USER housekeeping
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

# Trusts X-Forwarded-* from any address, for reverse proxies. Set FORWARDED_ALLOW_IPS to your proxy's address to narrow it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
