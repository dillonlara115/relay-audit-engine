# The agent service. Small: no browser, no Lighthouse, just Python and clients.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Cloud Run sets PORT. Concurrency is capped at the service level rather than
# here, because per host politeness is enforced by a Firestore lease and not by
# the number of threads in this process.
CMD exec uvicorn app.worker:app --host 0.0.0.0 --port ${PORT} --workers 1
