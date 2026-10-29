# The water-app image (DEPLOYMENT.md sections 5 and 10).
# The exact Python version the tests ran on, never "latest".
FROM python:3.14.2-slim

# no .pyc files (the root file system may be read-only) and no buffered output (logs appear at once)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# The packages come first, so a code change does not reinstall them.
# Runtime packages only: the test and audit tools in requirements-dev.txt stay out of the image.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what the app runs. tests/, docs/, .git and every .env file stay out (see .dockerignore).
COPY app/ app/
COPY alembic/ alembic/
COPY alembic.ini .
COPY scripts/ scripts/

# SECURITY.md: the app never runs as root
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin waterapp
USER waterapp

# Port 8001 is never published. Only containers on the shared network (Caddy) reach it.
EXPOSE 8001

# The slim image has no curl, so Python makes the request. Any error exits 1 (unhealthy).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=3)"]

# One worker: the login attempt limit lives in the app's memory (AUTH-S5).
# Proxy headers are trusted from any address because only the proxy can reach this port.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
