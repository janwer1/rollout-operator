# Dockerfile.app
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY app.py .

EXPOSE 8080

CMD ["python", "app.py"]
