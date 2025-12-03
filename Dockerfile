# Dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Create non-root user
RUN useradd -m -u 10001 k8s-operator

WORKDIR /app

# Install system deps (if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Copy requirements (pin versions as you like)
RUN pip install --no-cache-dir \
    kopf \
    kubernetes \
    pydantic \
    pydantic-settings

# Copy operator code
COPY rollout_operator.py .

USER k8s-operator

ENTRYPOINT ["kopf", "run", "--standalone", "rollout_operator.py"]
