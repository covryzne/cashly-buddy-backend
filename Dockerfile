FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
COPY requirements.txt requirements.lock ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium poppler-utils fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 cashly
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
USER cashly
EXPOSE 8000
CMD ["python", "-m", "cashly", "start"]
