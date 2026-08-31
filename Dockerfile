FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector/ ./collector/
COPY mock/ ./mock/
COPY sql/ ./sql/

# No CMD — the command is supplied per deployment, because the same image will
# later run the mock, the request API and the workers.
