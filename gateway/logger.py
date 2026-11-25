"""
Higiene de logs estritamente alinhada a LGPD.
Registra apenas job_id, trace_id, rota, status HTTP e latencia.
NUNCA grava payload do usuario, dados pessoais, IPs identificaveis ou tokens JWT.
"""
import json
import logging
import sys
from datetime import datetime, timezone

logger = logging.getLogger("gateway.audit")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def registrar_log_acesso(
    method: str,
    path: str,
    status_code: int,
    latency_ms: float,
    trace_id: str = None,
    job_id: str = None,
):
    """
    Registra evento de auditoria estruturado em formato JSON.
    Garante zero vazamento de informacoes sensiveis (Privacy by Design).
    """
    registro = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "gateway",
        "method": method,
        "path": path,
        "status_code": status_code,
        "latency_ms": round(latency_ms, 2),
    }
    if trace_id:
        registro["trace_id"] = trace_id
    if job_id:
        registro["job_id"] = job_id

    logger.info(json.dumps(registro, ensure_ascii=False))
