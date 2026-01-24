# Dockerfile
FROM python:3.13-slim AS builder

WORKDIR /app

# Install poetry and export plugin
RUN pip install --no-cache-dir poetry poetry-plugin-export

# Copy dependency files
COPY pyproject.toml poetry.lock* ./

# Export dependencies to requirements.txt (without dev dependencies)
RUN poetry export -f requirements.txt --without-hashes -o requirements.txt

# Final image
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Create non-root user
RUN useradd -m -u 10001 k8s-operator

WORKDIR /app

# Install system deps (if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Copy requirements from builder and install
COPY --from=builder /app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && rm requirements.txt

# Copy operator code
COPY rollout_operator.py .

USER k8s-operator

ENTRYPOINT ["kopf", "run", "--standalone", "--all-namespaces", "rollout_operator.py"]
