"""Worker de fila (Etapa 3) - consome jobs do Redis e chama o inference-service."""
import json
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone

import httpx
import redis

try:  # OpenTelemetry (Etapa 4). Sem SDK vira no-op e o worker segue funcionando.
    from opentelemetry import propagate, trace
    from opentelemetry.trace import SpanKind, Status, StatusCode
except Exception:  # pragma: no cover
    propagate = None
    trace = None
    SpanKind = Status = StatusCode = None


def configurar_tracing():
    """Etapa 4: mesmo esquema do gateway (OTEL_EXPORTER_OTLP_ENDPOINT -> Jaeger)."""
    if trace is None:
        return None
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        provider = TracerProvider(resource=Resource.create(
            {"service.name": os.getenv("OTEL_SERVICE_NAME", "worker")}))
        exportador = os.getenv("OTEL_TRACES_EXPORTER", "otlp").lower()
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        if exportador == "console":
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        elif exportador == "otlp" and endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            provider.add_span_processor(BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
        trace.set_tracer_provider(provider)
        return provider
    except Exception:  # SDK/exportador ausente: segue so com a API (no-op)
        return None


_provider = configurar_tracing()
_tracer = trace.get_tracer("worker") if trace else None

BROKER_URL = os.getenv("BROKER_URL", "redis://broker:6379/0")
INFERENCE_URL = os.getenv("INFERENCE_URL", "http://inference:8001").rstrip("/")
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "3600"))
QUEUE_NAME = os.getenv("QUEUE_NAME", "inference.jobs")
PROCESSING_QUEUE = QUEUE_NAME + ".processing"
DLQ_NAME = os.getenv("DLQ_NAME", "inference.dlq")
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "5"))
RECOVER_ON_START = os.getenv("RECOVER_ON_START", "true").lower() == "true"
POLL_TIMEOUT = 5  # segundos do BLMOVE (permite checar o sinal de shutdown)

_stop = False


class JsonFormatter(logging.Formatter):
    """Log estruturado. NUNCA logar payload do usuario (LGPD)."""

    def format(self, record):
        return json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "worker",
            "level": record.levelname,
            "trace_id": getattr(record, "trace_id", None),
            "job_id": getattr(record, "job_id", None),
            "message": record.getMessage(),
        })


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
log = logging.getLogger("worker")
log.addHandler(handler)
log.setLevel(logging.INFO)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _handle_signal(signum, _frame):
    global _stop
    _stop = True
    log.info("sinal recebido, finalizando apos o job atual")


def job_key(job_id):
    return f"job:{job_id}"


def set_status(r, job_id, status, result=None, error=None):
    mapping = {"status": status, "updated_at": _now()}
    if result is not None:
        mapping["result"] = json.dumps(result)
    if error is not None:
        mapping["error"] = error
    key = job_key(job_id)
    r.hset(key, mapping=mapping)
    r.expire(key, JOB_TTL_SECONDS)  # TTL = retencao LGPD


class TransientError(Exception):
    pass


def call_inference(payload, headers):
    span_cm = (_tracer.start_as_current_span("POST /predict", kind=SpanKind.CLIENT)
               if _tracer else None)
    span = span_cm.__enter__() if span_cm else None
    try:
        if propagate:
            propagate.inject(headers)  # traceparent filho deste span CLIENT
        return _call_inference(payload, headers)
    except Exception as exc:
        if span is not None:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
        raise
    finally:
        if span_cm:
            span_cm.__exit__(None, None, None)


def _call_inference(payload, headers):
    """POST /predict. Retry com backoff+jitter so em 5xx/timeout (nunca 4xx).
    Na Etapa 4 pode ser trocado pelo cliente resiliente compartilhado."""
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=2.0)
    last = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = httpx.post(f"{INFERENCE_URL}/predict", json=payload,
                              headers=headers, timeout=timeout)
            if resp.status_code >= 500:
                raise TransientError(f"inference respondeu {resp.status_code}")
            if resp.status_code >= 400:
                raise ValueError(f"inference rejeitou a requisicao ({resp.status_code})")
            return resp.json()
        except (httpx.TimeoutException, httpx.TransportError, TransientError) as exc:
            last = exc
            if attempt < MAX_ATTEMPTS:
                delay = min(0.5 * 2 ** (attempt - 1), 8) + random.uniform(0, 0.3)
                log.warning(f"falha transitoria ({type(exc).__name__}), tentativa "
                            f"{attempt}/{MAX_ATTEMPTS}, novo retry em {delay:.1f}s")
                time.sleep(delay)
    raise TransientError(str(last))


def _send_to_dlq(r, job_id, reason, attempts, trace_id):
    # DLQ guarda so metadados (sem payload do usuario) por causa da LGPD
    r.rpush(DLQ_NAME, json.dumps({
        "job_id": job_id, "reason": reason, "attempts": attempts,
        "trace_id": trace_id, "failed_at": _now()}))


def process(r, raw):
    try:
        msg = json.loads(raw)
        job_id = msg["job_id"]
        payload = msg["payload"]
    except (ValueError, KeyError, TypeError):
        log.error("mensagem malformada enviada para DLQ")
        _send_to_dlq(r, None, "malformed_message", 0, None)
        return

    carrier = msg.get("trace_context") or {}
    ctx = propagate.extract(carrier) if propagate else None
    span_cm = (_tracer.start_as_current_span("worker.consume", context=ctx,
                                             kind=SpanKind.CONSUMER)
               if _tracer else None)
    span = span_cm.__enter__() if span_cm else None
    try:
        trace_id = None
        if span is not None and span.get_span_context().is_valid:
            trace_id = format(span.get_span_context().trace_id, "032x")
        extra = {"job_id": job_id, "trace_id": trace_id or msg.get("trace_id")}

        if span is not None:
            span.set_attribute("messaging.system", "redis")
            span.set_attribute("messaging.source.name", QUEUE_NAME)
            span.set_attribute("job.id", job_id)
        set_status(r, job_id, "processing")
        log.info("job iniciado", extra=extra)
        # X-Trace-Id: o servico de inferencia loga o MESMO trace_id (correlacao de logs)
        headers = {"X-Trace-Id": extra["trace_id"]} if extra["trace_id"] else {}
        t0 = time.time()
        try:
            result = call_inference(payload, headers)
            set_status(r, job_id, "completed", result=result)
            log.info(f"job concluido em {int((time.time() - t0) * 1000)}ms", extra=extra)
        except Exception as exc:
            # mensagem generica: sem stack trace nem dados do usuario
            reason = "inference_unavailable" if isinstance(exc, TransientError) else "invalid_request"
            set_status(r, job_id, "failed", error=reason)
            if span is not None:
                span.set_status(Status(StatusCode.ERROR, reason))
            _send_to_dlq(r, job_id, reason, MAX_ATTEMPTS, extra["trace_id"])
            log.error(f"job falhou: {reason}", extra=extra)
    finally:
        if span_cm:
            span_cm.__exit__(None, None, None)


def recover_orphans(r):
    n = 0
    while r.lmove(PROCESSING_QUEUE, QUEUE_NAME, "RIGHT", "LEFT"):
        n += 1
    if n:
        log.info(f"{n} job(s) orfao(s) devolvido(s) a fila")


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    r = redis.Redis.from_url(BROKER_URL, decode_responses=True)
    log.info("worker iniciado")
    if RECOVER_ON_START:
        while True:
            try:
                recover_orphans(r)
                break
            except redis.RedisError:
                log.warning("broker indisponivel, aguardando")
                time.sleep(2)
    while not _stop:
        try:
            raw = r.blmove(QUEUE_NAME, PROCESSING_QUEUE, POLL_TIMEOUT, "LEFT", "RIGHT")
            if raw is None:
                continue
            try:
                process(r, raw)
            except Exception:  # um job ruim nunca derruba o loop
                log.exception("erro inesperado ao processar job")
            finally:
                r.lrem(PROCESSING_QUEUE, 1, raw)  # ack
        except redis.RedisError:
            log.warning("erro de conexao com o broker, tentando de novo")
            time.sleep(2)
    if _provider is not None:
        _provider.shutdown()  # descarrega os spans pendentes antes de sair
    log.info("worker encerrado")


if __name__ == "__main__":
    main()
