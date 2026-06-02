# ── Base image: Ubuntu 22.04 with Python 3.11 ──────────────────────────────────
FROM python:3.11-slim-bookworm

# Install FFmpeg + curl (for healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy handler
COPY handler.py .

# RunPod Serverless entrypoint
CMD ["python", "-u", "handler.py"]
