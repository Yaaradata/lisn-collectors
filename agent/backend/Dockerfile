# LiSN collector diagnostic agent — read-only backend
#
# Build from this directory:
#   docker build -t lisn-diagnostic-agent .
#
# Runtime needs ADC (or a SA key) for BigQuery + Cloud Run, plus the
# SELECT-only DSNs and optional SigNoz query credentials.

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_PORT=8090

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8090

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
