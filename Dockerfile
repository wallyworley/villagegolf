# Use the official Playwright Python image — includes Chromium pre-installed
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Bind to $PORT if the environment sets it, else default to 8080
ENV PORT=8080
ENV USER_DB_PATH=/data/users.db
RUN mkdir -p /data && chown pwuser:pwuser /data
USER pwuser
VOLUME ["/data"]

# One process owns the browser worker and notification watcher. HTTP threads
# enqueue automation; all Playwright objects remain on the dedicated thread.
CMD exec gunicorn \
    --bind "0.0.0.0:$PORT" \
    --workers 1 \
    --threads 4 \
    --timeout 300 \
    app:app
