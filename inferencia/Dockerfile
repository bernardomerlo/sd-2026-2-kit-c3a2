# syntax=docker/dockerfile:1

# ---------- Estagio 1: dependencias num venv isolado ----------
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------- Estagio 2: imagem final ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    MODEL_PATH=/app/model/modelo.joblib

# Usuario sem privilegios
RUN groupadd --system --gid 10001 app \
 && useradd  --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY app ./app

# Modelo scikit-learn treinado AGORA (no build) e empacotado na imagem: o container nao
# precisa de internet nem de permissao de escrita para subir. O diretorio fica somente-leitura
# para o usuario `app` (root e dono).
RUN python -m app.modelo

USER app
EXPOSE 8001

# /health responde 503 se o modelo nao carregou ou o banco vetorial nao responde -> container "unhealthy".
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=2)"]

# --factory: config lida do ambiente ao subir. --no-access-log: o log padrao do uvicorn grava IP + path (LGPD).
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]
