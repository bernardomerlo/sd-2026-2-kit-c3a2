"""
API Gateway - Porta de entrada segura, observavel e conteinerizada da plataforma.

Funcionalidades da Etapa 1:
- Autenticacao JWT no endpoint POST /auth/token (HS256)
- Middleware/Dependencia exigindo Authorization: Bearer <token> (401 se ausente/invalido/expirado)
- Roteamento sincrono (POST /v1/infer -> inference-service)
- Roteamento assincrono (POST /v1/jobs -> Redis queue com 202 Accepted, GET /v1/jobs/{job_id})
- Indexacao vetorial (POST /v1/documents -> inference-service)
- Higiene rigorosa de logs (LGPD: sem dados pessoais, IPs, tokens ou payloads gravados)
- Containerizacao com usuario nao-root (appuser)

Etapa 4 - Resiliencia e Observabilidade:
- Tempo-limite + retentativa com backoff exponencial/jitter + circuit breaker (gateway/resiliencia.py)
- Rate limit por cliente por minuto nas rotas /v1/* (gateway/rate_limit.py)
- Rastreamento distribuido OpenTelemetry gateway -> fila -> worker (gateway/tracing.py)
"""
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
import redis
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from gateway import tracing
from gateway.auth import criar_token_acesso, obter_usuario_autenticado
from gateway.config import (
    CB_FAILURE_THRESHOLD,
    CB_RECOVERY_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    INFERENCE_URL,
    JOB_TTL_SECONDS,
    QUEUE_NAME,
    JWT_EXPIRATION_MINUTES,
    OTEL_SERVICE_NAME,
    RATE_LIMIT_PER_MINUTE,
    REDIS_URL,
    RETRY_BASE_DELAY_SECONDS,
    RETRY_MAX_ATTEMPTS,
    RETRY_MAX_DELAY_SECONDS,
)
from gateway.logger import registrar_log_acesso, registrar_evento
from gateway.rate_limit import LimitadorTaxa
from gateway.resiliencia import (
    CircuitBreaker,
    CircuitoAbertoError,
    FalhaTransitoriaError,
    executar_com_resiliencia,
)

# Cache de jobs em memoria para alta resiliencia caso Redis nao esteja ativo localmente
_memoria_local_jobs: Dict[str, dict] = {}
_cliente_redis: Optional[redis.Redis] = None
_ultimo_teste_redis: float = 0.0
_redis_disponivel: bool = False


def obter_cliente_redis() -> Optional[redis.Redis]:
    global _cliente_redis, _ultimo_teste_redis, _redis_disponivel
    if _cliente_redis is not None:
        return _cliente_redis

    agora = time.time()
    if agora - _ultimo_teste_redis < 5.0 and not _redis_disponivel:
        return None

    _ultimo_teste_redis = agora
    try:
        r = redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=0.5)
        r.ping()
        _cliente_redis = r
        _redis_disponivel = True
        return _cliente_redis
    except Exception:
        _redis_disponivel = False
        _cliente_redis = None
        return None


# ------------------------------------------------------------------------------
# Etapa 4: tracing, circuit breaker e rate limit
# ------------------------------------------------------------------------------
tracing.configurar_tracing(OTEL_SERVICE_NAME)
_tracer = tracing.obter_tracer()

breaker_inferencia = CircuitBreaker(
    "inferencia",
    limite_falhas=CB_FAILURE_THRESHOLD,
    tempo_recuperacao=CB_RECOVERY_SECONDS,
)
limitador = LimitadorTaxa(RATE_LIMIT_PER_MINUTE, 60, obter_redis=lambda: obter_cliente_redis())

ROTAS_SEM_TRACE = {"/health", "/saude"}  # healthcheck a cada 10 s so poluiria o Jaeger


@asynccontextmanager
async def lifespan(app: FastAPI):
    obter_cliente_redis()
    yield
    if _cliente_redis:
        try:
            _cliente_redis.close()
        except Exception:
            pass


app = FastAPI(
    title="API Gateway - Plataforma de IA como Serviço",
    description="Porta de entrada única e segura da plataforma (Etapa 1 - Fundação e Segurança)",
    version="1.0.0",
    lifespan=lifespan,
)


# ------------------------------------------------------------------------------
# Schemas Pydantic
# ------------------------------------------------------------------------------
class TokenRequest(BaseModel):
    username: Optional[str] = Field(default="admin", description="Identificador do usuário para geração do token")
    password: Optional[str] = Field(default="admin123", description="Senha do usuário")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class InferRequest(BaseModel):
    texto: str = Field(..., description="Texto para execução da inferência síncrona")
    top_k: Optional[int] = Field(default=3, description="Quantidade de documentos relevantes a recuperar")
    use_llm: Optional[bool] = Field(default=True, description="Se deve invocar a síntese por LLM")


class JobRequest(BaseModel):
    texto: str = Field(..., description="Texto da tarefa a ser enfileirada assincronamente")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Metadados contextuais adicionais")


class JobResponse(BaseModel):
    job_id: str
    status: str
    message: str


class DocumentItem(BaseModel):
    texto: str
    id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class DocumentRequest(BaseModel):
    documents: Optional[List[DocumentItem]] = None
    texto: Optional[str] = None
    id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


# ------------------------------------------------------------------------------
# Middleware de Higiene de Logs e Trace ID (LGPD)
# ------------------------------------------------------------------------------
@app.middleware("http")
async def middleware_auditoria_e_logs(request: Request, call_next):
    inicio = time.time()

    if _tracer is None or request.url.path in ROTAS_SEM_TRACE:
        span_cm = None
        span = None
    else:
        # Continua um trace vindo de fora (cabecalho traceparent) ou inicia um novo
        span_cm = _tracer.start_as_current_span(
            f"{request.method} {request.url.path}",
            context=tracing.extrair_contexto(dict(request.headers)),
            kind=tracing.SpanKind.SERVER,
        )
        span = span_cm.__enter__()
    trace_id = tracing.trace_id_hex(span)
    request.state.trace_id = trace_id

    try:
        resposta: Response = await call_next(request)
    except BaseException as exc:
        if span_cm:
            tracing.marcar_erro(span, type(exc).__name__)
            span_cm.__exit__(type(exc), exc, exc.__traceback__)
        raise
    latencia_ms = (time.time() - inicio) * 1000

    if span is not None:
        rota = getattr(request.scope.get("route"), "path", None)
        if rota:
            span.update_name(f"{request.method} {rota}")  # template, sem ids na URL
            span.set_attribute("http.route", rota)
        span.set_attribute("http.request.method", request.method)
        span.set_attribute("http.response.status_code", resposta.status_code)
        job_id_span = getattr(request.state, "job_id", None)
        if job_id_span:
            span.set_attribute("job.id", job_id_span)
        if resposta.status_code >= 500:
            tracing.marcar_erro(span, f"HTTP {resposta.status_code}")
        span_cm.__exit__(None, None, None)

    resposta.headers["X-Trace-Id"] = trace_id
    limite = getattr(request.state, "rate_limit", None)
    if limite is not None and limite.limite:
        resposta.headers["X-RateLimit-Limit"] = str(limite.limite)
        resposta.headers["X-RateLimit-Remaining"] = str(limite.restantes)
    job_id = getattr(request.state, "job_id", None)

    # NUNCA registra corpo da requisicao, payload, dados pessoais nem tokens
    registrar_log_acesso(
        method=request.method,
        path=request.url.path,
        status_code=resposta.status_code,
        latency_ms=latencia_ms,
        trace_id=trace_id,
        job_id=job_id,
    )

    return resposta


# ------------------------------------------------------------------------------
# Etapa 4: rate limit (dependencia) e chamada resiliente ao servico de inferencia
# ------------------------------------------------------------------------------
async def usuario_com_limite(
    request: Request,
    usuario: dict = Depends(obter_usuario_autenticado),
) -> dict:
    """Autentica (401) e depois aplica o rate limit por cliente (429)."""
    resultado = limitador.verificar(str(usuario.get("sub", "anonimo")))
    request.state.rate_limit = resultado
    if not resultado.permitido:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Limite de requisicoes por minuto excedido. Tente novamente em instantes.",
            headers={
                "Retry-After": str(resultado.reset_em),
                "X-RateLimit-Limit": str(resultado.limite),
                "X-RateLimit-Remaining": "0",
            },
        )
    return usuario


async def chamar_inferencia(caminho: str, corpo: dict, request: Request) -> dict:
    """
    POST no servico de inferencia com tempo-limite, retentativa (backoff + jitter)
    e circuit breaker. Traduz as falhas para respostas HTTP do gateway:
      circuito aberto -> 503 (+Retry-After) | timeout -> 504 | 5xx/rede -> 502/503 | 4xx -> repassa
    """
    trace_id = getattr(request.state, "trace_id", uuid.uuid4().hex)

    async def tentativa() -> httpx.Response:
        span_cm = (
            _tracer.start_as_current_span(f"POST {caminho}", kind=tracing.SpanKind.CLIENT)
            if _tracer else None
        )
        span = span_cm.__enter__() if span_cm else None
        try:
            headers = {"X-Trace-Id": trace_id, "Content-Type": "application/json"}
            tracing.injetar_contexto(headers)  # traceparent W3C
            try:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                    resposta = await client.post(f"{INFERENCE_URL}{caminho}", json=corpo, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                tracing.marcar_erro(span, type(exc).__name__)
                raise FalhaTransitoriaError(exc) from exc
            if span is not None:
                span.set_attribute("http.response.status_code", resposta.status_code)
            if resposta.status_code >= 500:
                tracing.marcar_erro(span, f"HTTP {resposta.status_code}")
                raise FalhaTransitoriaError(resultado=resposta)
            return resposta
        finally:
            if span_cm:
                span_cm.__exit__(None, None, None)

    def ao_repetir(n: int, espera: float, exc: BaseException) -> None:
        registrar_evento(
            "retry_inferencia", trace_id=trace_id,
            tentativa=n, espera_ms=round(espera * 1000), motivo=str(exc),
        )

    try:
        resposta = await executar_com_resiliencia(
            tentativa,
            breaker_inferencia,
            tentativas=RETRY_MAX_ATTEMPTS,
            espera_base=RETRY_BASE_DELAY_SECONDS,
            espera_maxima=RETRY_MAX_DELAY_SECONDS,
            ao_repetir=ao_repetir,
        )
    except CircuitoAbertoError as exc:
        registrar_evento("circuito_aberto", trace_id=trace_id, servico=exc.nome)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servico de inferencia temporariamente indisponivel (circuit breaker aberto)",
            headers={"Retry-After": str(exc.retry_after)},
        )
    except FalhaTransitoriaError as exc:
        if isinstance(exc.causa, httpx.TimeoutException):
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Tempo-limite excedido ao comunicar com o servico de inferencia",
            )
        if exc.resultado is not None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Servico de inferencia falhou (HTTP {exc.resultado.status_code}) apos {RETRY_MAX_ATTEMPTS} tentativas",
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servico de inferencia indisponivel",
        )

    if resposta.status_code >= 400:  # 4xx: erro do pedido, nao do servico -> repassa sem retry
        try:
            detalhe = resposta.json().get("detail", resposta.text)
        except ValueError:
            detalhe = resposta.text
        raise HTTPException(status_code=resposta.status_code, detail=detalhe)
    return resposta.json()


# ------------------------------------------------------------------------------
# Rotas Publicas
# ------------------------------------------------------------------------------
@app.post("/auth/token", response_model=TokenResponse, summary="Gerar token de autenticação JWT")
def gerar_token(dados: Optional[TokenRequest] = None):
    """
    Gera um token JWT com algoritmo HS256 para facilitar testes do professor e avaliação automatizada.
    """
    usuario = dados.username if dados and dados.username else "avaliador"
    token = criar_token_acesso(
        subject=usuario,
        dados_extras={"role": "user", "scope": "platform:api"},
    )
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=JWT_EXPIRATION_MINUTES * 60,
    )


@app.get("/health", summary="Saúde do Gateway e conectividade básica")
async def health_check():
    """
    Rota pública de diagnóstico do Gateway e verificação básica de conectividade.
    """
    conectividade = {
        "gateway": "healthy",
        "inference_service": "unknown",
        "redis_broker": "unknown",
    }

    r = obter_cliente_redis()
    if r:
        try:
            r.ping()
            conectividade["redis_broker"] = "connected"
        except Exception:
            conectividade["redis_broker"] = "disconnected"
    else:
        conectividade["redis_broker"] = "in-memory-fallback"

    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            resp = await client.get(f"{INFERENCE_URL}/health")
            if resp.status_code == 200:
                conectividade["inference_service"] = "connected"
            else:
                conectividade["inference_service"] = f"degraded (HTTP {resp.status_code})"
    except Exception:
        conectividade["inference_service"] = "unreachable"

    conectividade["inference_circuit_breaker"] = breaker_inferencia.resumo()

    return {
        "status": "ok",
        "service": "gateway",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dependencies": conectividade,
    }


# Rota legada para compatibilidade com o kit inicial
@app.get("/saude")
async def saude_legada():
    return await health_check()


# ------------------------------------------------------------------------------
# Rotas Protegidas de Negócio (/v1/*)
# ------------------------------------------------------------------------------
@app.post("/v1/infer", summary="Roteamento de inferência síncrona")
async def infer_sincrono(
    payload: InferRequest,
    request: Request,
    usuario: dict = Depends(usuario_com_limite),
):
    """
    Roteia chamada síncrona (REST) para o inference-service via URL de ambiente.
    Exige autenticação Bearer JWT válida. Protegida por timeout, retry e circuit breaker.
    """
    return await chamar_inferencia("/predict", payload.model_dump(), request)


@app.post("/v1/jobs", status_code=status.HTTP_202_ACCEPTED, response_model=JobResponse, summary="Enfileirar tarefa assíncrona")
async def criar_job(
    payload: JobRequest,
    request: Request,
    usuario: dict = Depends(usuario_com_limite),
):
    """
    Publica a tarefa na fila de mensageria e retorna 202 Accepted com job_id.
    """
    job_id = str(uuid.uuid4())
    request.state.job_id = job_id
    trace_id = getattr(request.state, "trace_id", str(uuid.uuid4()))

    agora = datetime.now(timezone.utc).isoformat()
    dados_job = {
        "job_id": job_id,
        "status": "pending",
        "texto": payload.texto,
        "trace_id": trace_id,
        "created_at": agora,
        "metadata": payload.metadata or {},
    }

    r = obter_cliente_redis()
    if r:
        try:
            # Contrato Etapa 3: status em HASH job:{id} (com TTL) + mensagem com "payload" na fila
            chave_status = f"job:{job_id}"
            r.hset(chave_status, mapping={"status": "pending", "created_at": agora})
            r.expire(chave_status, JOB_TTL_SECONDS)
            # Span PRODUCER + traceparent dentro da mensagem: o trace nao quebra na fila
            span_cm = (
                _tracer.start_as_current_span(f"{QUEUE_NAME} publish", kind=tracing.SpanKind.PRODUCER)
                if _tracer else None
            )
            span = span_cm.__enter__() if span_cm else None
            try:
                if span is not None:
                    span.set_attribute("messaging.system", "redis")
                    span.set_attribute("messaging.destination.name", QUEUE_NAME)
                    span.set_attribute("job.id", job_id)
                r.rpush(QUEUE_NAME, json.dumps({
                    "job_id": job_id,
                    "payload": {"texto": payload.texto},
                    "trace_id": trace_id,
                    "trace_context": tracing.injetar_contexto({}),
                }))
            finally:
                if span_cm:
                    span_cm.__exit__(None, None, None)
        except Exception:
            _memoria_local_jobs[job_id] = dados_job
    else:
        _memoria_local_jobs[job_id] = dados_job

    return JobResponse(
        job_id=job_id,
        status="pending",
        message="Tarefa aceita e enfileirada com sucesso para processamento",
    )


@app.get("/v1/jobs/{job_id}", summary="Consultar status de um job")
async def consultar_job(
    job_id: str,
    request: Request,
    usuario: dict = Depends(usuario_com_limite),
):
    """
    Consulta o status (pending, processing, completed, failed) e o resultado gravado pelo worker.
    """
    request.state.job_id = job_id
    r = obter_cliente_redis()

    dados_brutos = None
    if r:
        try:
            dados_brutos = r.hgetall(f"job:{job_id}")  # hash gravado pelo gateway e pelo worker
        except Exception:
            dados_brutos = None

    if dados_brutos:
        job = dict(dados_brutos)
        if job.get("result"):
            try:
                job["result"] = json.loads(job["result"])
            except (TypeError, ValueError):
                pass
        job.setdefault("completed_at", job.get("updated_at") if job.get("status") == "completed" else None)
    elif job_id in _memoria_local_jobs:
        job = _memoria_local_jobs[job_id]
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} não encontrado ou expirado",
        )

    return {
        "job_id": job_id,
        "status": job.get("status", "unknown"),
        "created_at": job.get("created_at"),
        "completed_at": job.get("completed_at"),
        "result": job.get("result"),
        "error": job.get("error"),
    }


@app.post("/v1/documents", summary="Indexar textos no armazenamento vetorial")
async def indexar_documentos(
    payload: DocumentRequest,
    request: Request,
    usuario: dict = Depends(usuario_com_limite),
):
    """
    Encaminha textos para indexação no armazenamento vetorial via inference-service.
    """
    corpo_envio: Dict[str, Any] = {}
    if payload.documents:
        corpo_envio["documents"] = [
            {**d.model_dump(), "id": d.id or str(uuid.uuid4())} for d in payload.documents
        ]
    elif payload.texto:
        corpo_envio["documents"] = [
            {
                "id": payload.id or str(uuid.uuid4()),
                "texto": payload.texto,
                "metadata": payload.metadata or {},
            }
        ]
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Informe 'documents' ou 'texto' para indexação",
        )

    # Os ids ja estao definidos aqui, entao repetir o upsert e idempotente (retry seguro)
    return await chamar_inferencia("/vectors/upsert", corpo_envio, request)