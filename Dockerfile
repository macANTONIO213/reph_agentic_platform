# syntax=docker/dockerfile:1
# Production image for the REPH Agentic Platform control plane.
# Multi-stage: build wheels once, ship a slim runtime running as non-root.

FROM python:3.12-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt constraints.txt ./
RUN pip install --upgrade pip && pip wheel --wheel-dir /wheels -r requirements.txt -c constraints.txt

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DJANGO_DEBUG=False
WORKDIR /app

# Non-root runtime user — never run the app as root.
RUN groupadd --system app && useradd --system --gid app --home-dir /app app

COPY --from=build /wheels /wheels
COPY requirements.txt constraints.txt ./
RUN pip install --upgrade pip && pip install --no-index --find-links=/wheels \
    -r requirements.txt -c constraints.txt && rm -rf /wheels

COPY . .
RUN mkdir -p /app/staticfiles && chown -R app:app /app
USER app

# collectstatic at build so the image is self-contained; migrate runs at deploy.
RUN DJANGO_SECRET_KEY=build-time-only python manage.py collectstatic --noinput

EXPOSE 8000

# Container liveness probe hits the unauthenticated /healthz endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

CMD ["gunicorn", "agentic_platform.wsgi:application", \
     "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "120", \
     "--graceful-timeout", "30", "--max-requests", "1000", "--max-requests-jitter", "100", \
     "--access-logfile", "-", "--error-logfile", "-"]
