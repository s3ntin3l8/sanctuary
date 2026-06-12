# --- Stage 1: Build CSS ---
FROM node:26-slim AS css-builder
WORKDIR /build
COPY package*.json ./
RUN npm install
COPY static/input.css ./static/
# Copy templates to allow Tailwind to scan for classes
COPY app/templates ./app/templates
RUN npx @tailwindcss/cli -i static/input.css -o static/styles.css

# --- Stage 2: Python builder ---
# Compiles/installs every dependency into an isolated venv. build-essential lives
# ONLY here so it never reaches the shipped image (it was ~560 MB of dead weight).
FROM python:3.14-slim AS py-builder
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Prod-only requirements (dev/test deps live in requirements-dev.txt, never shipped).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Stage 3: Final runtime image ---
FROM python:3.14-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Runtime shared libs only — no compilers:
#   libgl1 + libglib2.0-0  → OpenCV / Docling page rendering
#   libmagic1              → file-type sniffing
#   libgomp1               → OpenMP runtime for torch / onnxruntime
#   curl                   → container healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libmagic1 \
    libgomp1 \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy the prebuilt venv from the builder stage (same path keeps shebangs valid).
COPY --from=py-builder /opt/venv /opt/venv

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
