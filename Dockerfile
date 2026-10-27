# --- Stage 1: Build the SPA ---
FROM node:26-slim AS frontend-builder
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

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

# Headless OpenCV: docling-ibm-models declares the GUI `opencv-python` wheel,
# which links Qt/GL and forces libgl1 (mesa/LLVM, ~200 MB) into the runtime
# image. Same `cv2` module, so swap it for the headless build (keep the version
# in lockstep with requirements.txt). rapidocr >= 3.10 already depends on the
# headless wheel, so both are installed and share the `cv2/` files: uninstalling
# the GUI wheel deletes them, and a plain `pip install` of headless would then be
# a no-op. Force the reinstall to put the files back.
RUN pip uninstall -y opencv-python \
    && pip install --no-cache-dir --force-reinstall --no-deps opencv-python-headless==5.0.0.93

# Prune dead weight HERE, before the venv is COPYed into the runtime stage
# (deleting after the COPY would only add a layer and save nothing):
#   tests/test dirs, torch headers/test binaries (torch.testing is imported by torch
#   itself, keep it), the 600+ Google API discovery
#   docs (only gmail.v1 is used), and pip/setuptools.
RUN SP="$(/opt/venv/bin/python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && [ -d "$SP/torch" ] \
    && find "$SP" -mindepth 2 -type d \( -name tests -o -name test \) -prune -exec rm -rf {} + \
    && rm -rf "$SP/torch/include" \
    && find "$SP/torch/bin" -type f ! -name torch_shm_manager -delete \
    && find "$SP/googleapiclient/discovery_cache/documents" -type f ! -name gmail.v1.json -delete \
    && rm -rf "$SP"/pip "$SP"/pip-* "$SP"/setuptools "$SP"/setuptools-* "$SP"/wheel "$SP"/wheel-* /opt/venv/bin/pip*

# Fail the build if the prune broke an import the app relies on.
RUN python -c "import cv2, torch, onnxruntime; from rapidocr import RapidOCR; from docling.document_converter import DocumentConverter; from googleapiclient.discovery import build; build('gmail', 'v1', developerKey='x', cache_discovery=False)"

# --- Stage 3: Final runtime image ---
FROM python:3.14-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
# Docling runs several OCR children in parallel; one OpenMP thread per tesseract.
ENV OMP_THREAD_LIMIT=1
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Runtime shared libs only — no compilers:
#   libglib2.0-0          → OpenCV (headless) / Docling page rendering
#   libmagic1              → file-type sniffing
#   libgomp1               → OpenMP runtime for torch / onnxruntime
#   curl                   → container healthcheck
#   tesseract-ocr(+deu,eng)→ the "Docling + Tesseract" extraction engine and the
#                            fallback when Chandra errors; Docling shells out to the
#                            `tesseract` CLI with lang=deu+eng
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libmagic1 \
    libgomp1 \
    curl \
    tesseract-ocr \
    tesseract-ocr-deu \
    tesseract-ocr-eng \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy the prebuilt venv from the builder stage (same path keeps shebangs valid).
COPY --from=py-builder /opt/venv /opt/venv

# Build identity shown in Settings → About (set by the publish workflows).
# COMMIT_SHA is added by the shared docker-publish workflow: the commit it checked
# out (the tag's commit for a release), so callers must not pass github.sha.
ARG SANCTUARY_VERSION=""
ARG COMMIT_SHA=""
ARG SANCTUARY_BUILD_DATE=""
ENV SANCTUARY_VERSION=$SANCTUARY_VERSION \
    SANCTUARY_GIT_SHA=$COMMIT_SHA \
    SANCTUARY_BUILD_DATE=$SANCTUARY_BUILD_DATE

# Copy application code
COPY . .

# Copy the SPA bundle from Stage 1
COPY --from=frontend-builder /build/dist ./frontend/dist

# Ensure data directory exists and is writable
RUN mkdir -p /app/data && chmod 777 /app/data

# Expose port
EXPOSE 8000

# Start application
# Migrations are handled by the app lifespan in app/main.py
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
