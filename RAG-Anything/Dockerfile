# Backend Dockerfile for FastAPI chat API
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    postgresql-client \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip, setuptools, and wheel for faster installs and better wheel support
RUN pip install --upgrade pip setuptools wheel

# Set pip to prefer binary wheels and use parallel downloads
ENV PIP_NO_BUILD_ISOLATION=1
ENV PIP_PREFER_BINARY=1
ENV PIP_DEFAULT_TIMEOUT=100

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
# ⚠️  WARNING: This step can take 15-40 minutes on first build!
# Heavy packages: docling (includes torch/transformers dependencies)
# These packages are large and may need compilation
# Subsequent builds will be faster due to Docker layer caching
RUN echo "📦 Installing Python dependencies (this may take 15-40 minutes)..." && \
    pip install --no-cache-dir --prefer-binary -r requirements.txt && \
    echo "✅ Dependencies installed successfully"

# Copy the entire RAG-Anything directory
COPY . .

# Set Python path to include the project root
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Expose the API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run the FastAPI application
CMD ["python", "scripts/07_chat_api.py"]
