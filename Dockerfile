FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY etl ./etl
COPY scripts ./scripts
RUN pip install --no-cache-dir . \
    && mkdir -p data/raw/inbox data/bronze data/silver data/gold data/runtime/media_library

CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
