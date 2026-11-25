# Gateway Dockerfile - Producao enxuta e segura (Usuario nao-root)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Cria usuario de sistema nao-root para execucao segura (LGPD / Seguranca)
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

# Instala dependencias do sistema de forma enxuta
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o codigo do gateway
COPY . /app/gateway
WORKDIR /app

# Ajusta permissoes
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
