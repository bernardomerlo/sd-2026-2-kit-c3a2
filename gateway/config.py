"""
Modulo de configuracao do API Gateway via variaveis de ambiente.
"""
import os
from dotenv import load_dotenv

load_dotenv()

GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", os.getenv("PORT", "8000")))
JWT_SECRET = os.getenv("JWT_SECRET", os.getenv("JWT_SEGREDO", "super-segredo-sd-2026-faesa-jwt-token-key-32bytes"))
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRATION_MINUTES = int(os.getenv("JWT_EXPIRATION_MINUTES", "60"))

# Suporta tanto INFERENCE_URL quanto URL_INFERENCIA do kit
INFERENCE_URL = os.getenv("INFERENCE_URL", os.getenv("URL_INFERENCIA", "http://localhost:8001")).rstrip("/")
REDIS_URL = os.getenv("REDIS_URL", os.getenv("BROKER_URL", "redis://localhost:6379/0"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "10.0"))
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "3600"))
QUEUE_NAME = os.getenv("QUEUE_NAME", "inference.jobs")  # contrato da Etapa 3 (docs/etapa3-contrato.md)
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))  # 0 desliga o limite
