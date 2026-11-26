"""
Rastreamento distribuido com OpenTelemetry (Etapa 4 / Aula 15).

Caminho de um job assincrono, tudo no MESMO trace:
  gateway (span SERVER "POST /v1/jobs")
    -> span PRODUCER "inference.jobs publish"  (traceparent vai DENTRO da mensagem)
      -> worker (span CONSUMER "worker.consume", extrai o traceparent da mensagem)
        -> worker (span CLIENT "POST /predict", injeta traceparent no HTTP)
          -> inferencia (registra o mesmo trace_id no log via X-Trace-Id)

Exportacao (por variavel de ambiente, padrao OpenTelemetry):
- OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318  -> envia os spans para o Jaeger (UI em :16686)
- OTEL_TRACES_EXPORTER=console                     -> imprime os spans no stdout (debug)
- nada configurado                                 -> gera os IDs normalmente, so nao exporta

LGPD: os spans carregam so metodo, rota, status, job_id e nome da fila. Nunca o texto
do usuario, token, IP ou cabecalhos.
Se o SDK nao estiver instalado, tudo vira no-op e o gateway continua funcionando.
"""
from __future__ import annotations

import logging
import os
import uuid

try:
    from opentelemetry import propagate, trace
    from opentelemetry.trace import SpanKind, Status, StatusCode
except Exception:  # pragma: no cover - opentelemetry-api ausente
    propagate = None
    trace = None
    SpanKind = Status = StatusCode = None

_configurado = False
_log = logging.getLogger("gateway.tracing")


def configurar_tracing(nome_servico: str) -> None:
    """Instala o TracerProvider do SDK (uma vez). Silencioso se o SDK nao existir."""
    global _configurado
    if _configurado or trace is None:
        return
    _configurado = True
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except Exception:
        return  # so a API instalada: spans no-op

    nome = os.getenv("OTEL_SERVICE_NAME", nome_servico)
    provider = TracerProvider(resource=Resource.create({"service.name": nome}))

    exportador = os.getenv("OTEL_TRACES_EXPORTER", "otlp").lower()
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    try:
        if exportador == "console":
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        elif exportador == "otlp" and endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces"))
            )
    except Exception:  # exportador indisponivel nunca derruba o servico
        _log.warning("exportador de traces indisponivel; seguindo sem exportar")

    trace.set_tracer_provider(provider)


def obter_tracer():
    return trace.get_tracer("gateway") if trace else None


def trace_id_hex(span) -> str:
    """trace_id de 32 hex do span atual (ou um uuid se o tracing estiver desligado)."""
    if span is not None:
        ctx = span.get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x")
    return uuid.uuid4().hex


def extrair_contexto(cabecalhos) -> object:
    return propagate.extract(cabecalhos) if propagate else None


def injetar_contexto(carrier: dict) -> dict:
    """Escreve `traceparent` no dict (cabecalhos HTTP ou mensagem da fila)."""
    if propagate:
        propagate.inject(carrier)
    return carrier


def marcar_erro(span, mensagem: str = "erro") -> None:
    if span is not None and Status is not None:
        span.set_status(Status(StatusCode.ERROR, mensagem))
