FROM python:3.12-slim AS base

WORKDIR /app

# Install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir . 2>/dev/null || pip install --no-cache-dir fastapi uvicorn[standard] pydantic websockets

# Copy source
COPY exchange/ exchange/
COPY scripts/ scripts/

# Create data directory for WAL
RUN mkdir -p data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/stats')" || exit 1

CMD ["python", "-m", "uvicorn", "exchange.api:app", "--host", "0.0.0.0", "--port", "8000"]
