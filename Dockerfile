# --- Stage 1: Build CSS ---
FROM node:26-slim AS css-builder
WORKDIR /build
COPY package*.json ./
RUN npm install
COPY static/input.css ./static/
# Copy templates to allow Tailwind to scan for classes
COPY app/templates ./app/templates
RUN npx @tailwindcss/cli -i static/input.css -o static/styles.css

# --- Stage 2: Final Image ---
FROM python:3.14-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    libmagic1 \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Copy built CSS from Stage 1
COPY --from=css-builder /build/static/styles.css ./static/styles.css

# Ensure data directory exists and is writable
RUN mkdir -p /app/data && chmod 777 /app/data

# Expose port
EXPOSE 8000

# Start application
# Migrations are handled by the app lifespan in app/main.py
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
