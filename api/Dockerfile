# rahbar-api: the FastAPI reader. Build context is api/ (docs/ARCHITECTURE.md, "Images").
#   docker build -t rahbar-api:dev api
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Patch the OS packages the slim base ships with (Trivy fails the build on a fixable HIGH).
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so a code change does not invalidate the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Non-root, fixed uid so the pod securityContext (runAsUser: 10001) matches the image.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && chown -R 10001:10001 /app
USER 10001

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
