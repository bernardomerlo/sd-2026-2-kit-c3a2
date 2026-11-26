"""Logs estruturados (JSON) e trace_id. Politica LGPD identica a do gateway.

Formato: {"timestamp","service","level","trace_id","message", ...campos permitidos}
NUNCA registra: texto/payload do usuario, documentos, IP, tokens, nem tracebacks.
O formatter so serializa uma LISTA BRANCA de campos.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import TextIO

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

SERVICO = "inferencia"
NOME_LOGGER = "inferencia"
CABECALHO_TRACE = "X-Trace-Id"
CAMPOS_PERMITIDOS = ("route", "status_code", "latency_ms", "error_type", "colecao", "documentos")

# Aceita uuid, hex de 32 e traceparent W3C (so letras, numeros, '.', '_' e '-'): evita log injection.
_TRACE_VALIDO = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

trace_id_atual: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)


def aceitar_trace_id(valor: str | None) -> str:
    return valor if valor and _TRACE_VALIDO.match(valor) else uuid.uuid4().hex


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entrada: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "service": SERVICO,
            "level": record.levelname,
            "trace_id": getattr(record, "trace_id", None) or trace_id_atual.get(),
            "message": record.getMessage(),
        }
        for campo in CAMPOS_PERMITIDOS:
            valor = getattr(record, campo, None)
            if valor is not None:
                entrada[campo] = valor
        return json.dumps(entrada, ensure_ascii=False)  # exc_info ignorado de proposito


def configurar_logging(nivel: str = "INFO", stream: TextIO | None = None) -> logging.Logger:
    logger = logging.getLogger(NOME_LOGGER)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, nivel, logging.INFO))
    logger.propagate = False
    logging.getLogger("uvicorn.access").disabled = True  # access log grava IP + path
    for nome in ("httpx", "httpcore", "qdrant_client"):
        logging.getLogger(nome).setLevel(logging.WARNING)
    return logger


def _rota(request: Request) -> str:
    caminho = getattr(request.scope.get("route"), "path", None)
    return f"{request.method} {caminho}" if caminho else f"{request.method} <nao-encontrada>"


class ObservabilidadeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        inicio = time.perf_counter()
        trace_id = aceitar_trace_id(request.headers.get(CABECALHO_TRACE.lower()))
        request.state.trace_id = trace_id
        trace_id_atual.set(trace_id)  # propagado ao handler (inclusive em threadpool)
        tipo_erro = None
        try:
            resposta = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - ultima barreira: 500 generico, sem traceback
            tipo_erro = type(exc).__name__
            resposta = JSONResponse({"detail": "Erro interno do servico de inferencia"}, status_code=500)
        resposta.headers[CABECALHO_TRACE] = trace_id
        logging.getLogger(NOME_LOGGER).info(
            "request",
            extra={
                "trace_id": trace_id,
                "route": _rota(request),
                "status_code": resposta.status_code,
                "latency_ms": round((time.perf_counter() - inicio) * 1000, 2),
                "error_type": tipo_erro,
            },
        )
        return resposta
