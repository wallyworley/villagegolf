# Use the official Playwright Python image — includes Chromium pre-installed
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser for Playwright
RUN playwright install chromium

# Copy application code
COPY . .

# Cloud Run sets PORT automatically; default to 8080
ENV PORT=8080

# Run with gunicorn: 1 worker, 1 thread (Playwright sync API is not
# thread-safe). The GolfService uses its own lock for safety.
CMD exec gunicorn \
    --bind "0.0.0.0:$PORT" \
    --workers 1 \
    --threads 4 \
    --timeout 120 \
    app:app
