FROM python:3.12-slim

# System deps for Pillow (image generation) and psycopg (PostgreSQL)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc libjpeg62-turbo-dev zlib1g-dev libfreetype6-dev \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data directory for any local files (fonts, temp exports, etc.)
RUN mkdir -p /app/data

EXPOSE 5000

# Use gunicorn for production with enough workers and threads
# Workers handle HTTP requests, threads handle background tasks
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "4", \
     "--threads", "16", \
     "--timeout", "600", \
     "app:app"]
