FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY worker.py .
RUN useradd -m -u 10001 appuser
USER appuser
ENV BROKER_URL=redis://broker:6379/0 \
    INFERENCE_URL=http://inference:8001 \
    JOB_TTL_SECONDS=3600
CMD ["python", "worker.py"]
