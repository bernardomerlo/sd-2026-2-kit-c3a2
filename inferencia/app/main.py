"""Servico de inferencia (rede interna). Chamado pelo gateway (REST) e pelo worker.

Rotas:  GET /health · POST /predict · POST /vectors/upsert · POST /vectors/search
Rodar:  uvicorn app.main:create_app --factory --port 8001 --no-access-log
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import Settings
from app.embeddings import Embedder
from app.llm import ClienteLLM
from app.modelo import ModeloSentimento, carregar_modelo
from app.observability import NOME_LOGGER, SERVICO, ObservabilidadeMiddleware, configurar_logging
from app.prontidao import Prontidao
from app.schemas import (
    DocumentoRecuperado,
    PredictRequest,
    PredictResponse,
    Predicao,
    SearchRequest,
    SearchResponse,
    UpsertRequest,
    UpsertResponse,
)
from app.seed import carregar_seed
from app.vectorstore import VectorDBConfigError, VectorDBUnavailable, VectorStore

TOP_K_PADRAO = 3


def _ms(inicio: float) -> float:
    return round((time.perf_counter() - inicio) * 1000, 2)


def create_app(
    settings: Settings | None = None,
    *,
    modelo: ModeloSentimento | None = None,
    store: VectorStore | None = None,
    llm: ClienteLLM | None = None,
) -> FastAPI:
    """`modelo`, `store` e `llm` existem para injetar dubles nos testes."""
    settings = settings or Settings.from_env()
    configurar_logging(settings.log_level)
    log = logging.getLogger(NOME_LOGGER)

    modelo = modelo or carregar_modelo(settings.model_path)
    embedder = Embedder(settings.embedding_dim)
    store = store or VectorStore.from_settings(settings)
    llm = llm or ClienteLLM(
        settings.llm_api_key, settings.llm_api_url, settings.llm_model, settings.llm_timeout_seconds
    )
    prontidao = Prontidao(store, embedder, carregar_seed(), settings.seed_on_startup)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 1a tentativa sincrona (rapida quando o banco ja esta de pe); se falhar, segue tentando.
        parar = None
        if not prontidao.garantir() and prontidao.ultimo_erro != "configuracao":
            parar = prontidao.iniciar_tentativas_em_segundo_plano(settings.vector_db_startup_timeout_seconds)
        log.info("inferencia_iniciada", extra={"colecao": settings.vector_collection})
        yield
        if parar:
            parar.set()
        llm.fechar()

    app = FastAPI(
        title="Servico de Inferencia - Plataforma de IA como Servico",
        version="0.1.0",
        description="API interna (rede privada). Nao exposta diretamente a internet: o acesso e pelo gateway.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.prontidao = prontidao
    app.add_middleware(ObservabilidadeMiddleware)

    # ------------------------------------------------------------------ erros
    @app.exception_handler(RequestValidationError)
    async def _entrada_invalida(request: Request, exc: RequestValidationError):
        # O 422 padrao do FastAPI devolve o campo "input" (= o texto do usuario). O gateway repassa
        # o erro e o worker pode grava-lo em job.error: por LGPD devolvemos so onde/por que falhou.
        erros = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse({"detail": erros}, status_code=422)

    @app.exception_handler(VectorDBUnavailable)
    async def _banco_indisponivel(request: Request, exc: VectorDBUnavailable):
        prontidao.invalidar()  # proxima requisicao tenta recriar a collection
        return JSONResponse({"detail": "Banco vetorial indisponivel"}, status_code=503)

    @app.exception_handler(VectorDBConfigError)
    async def _banco_config(request: Request, exc: VectorDBConfigError):
        return JSONResponse({"detail": "Configuracao do banco vetorial invalida"}, status_code=503)

    def _exigir_banco() -> None:
        if not prontidao.garantir():
            raise VectorDBConfigError() if prontidao.ultimo_erro == "configuracao" else VectorDBUnavailable()

    # ----------------------------------------------------------------- health
    @app.get("/health", tags=["infra"], summary="Modelo carregado e banco vetorial respondendo")
    def health():
        """`200` somente se o modelo esta carregado **e** o banco vetorial responde (com a
        collection criada). Caso contrario `503` - usado como healthcheck do container."""
        banco = prontidao.verificar()
        ok = modelo is not None and banco["status"] == "up"
        corpo = {
            "status": "ok" if ok else "degraded",
            "service": SERVICO,
            "modelo_carregado": modelo is not None,
            "vector_db": banco,
            "llm": {"modo": llm.modo},
        }
        return JSONResponse(corpo, status_code=200 if ok else 503)

    # ---------------------------------------------------------------- predict
    @app.post("/predict", response_model=PredictResponse, tags=["inferencia"], summary="Inferencia com contexto vetorial")
    def predict(req: PredictRequest):
        """Classifica o texto (sentimento), recupera os `top_k` documentos mais relevantes no
        banco vetorial e (opcional) gera uma resposta com o LLM (simulado se nao houver chave).
        O texto enviado **nao** e devolvido na resposta."""
        t_total = time.perf_counter()
        top_k = req.top_k or TOP_K_PADRAO
        usar_llm = True if req.use_llm is None else req.use_llm

        t = time.perf_counter()
        predicao = modelo.prever(req.texto)
        ms_modelo = _ms(t)

        t = time.perf_counter()
        contexto_status, documentos = "ok", []
        vetor = embedder.embed_um(req.texto)
        if vetor is None:
            contexto_status = "sem_termos_significativos"
        else:
            _exigir_banco()
            documentos = store.search(vetor, top_k)
        ms_busca = _ms(t)

        resposta, modo_llm, ms_llm = None, None, 0.0
        if usar_llm:
            t = time.perf_counter()
            r = llm.gerar(req.texto, [d["texto"] for d in documentos])
            resposta, modo_llm, ms_llm = r.texto, r.modo, _ms(t)

        return PredictResponse(
            predicao=Predicao(**predicao),
            documentos=[DocumentoRecuperado(**d) for d in documentos],
            resposta=resposta,
            metadados={
                "latencia_ms": {
                    "modelo": ms_modelo,
                    "busca_vetorial": ms_busca,
                    "llm": ms_llm,
                    "total": _ms(t_total),
                },
                "top_k": top_k,
                "documentos_recuperados": len(documentos),
                "contexto": contexto_status,
                "llm_modo": modo_llm,
                "colecao": settings.vector_collection,
            },
        )

    # ---------------------------------------------------------------- vectors
    @app.post("/vectors/upsert", response_model=UpsertResponse, tags=["vetores"], summary="Gera embeddings e salva no banco vetorial")
    def vectors_upsert(req: UpsertRequest):
        """Gera o embedding local de cada documento e faz upsert. Sem `id` o servico gera um;
        reenviar o mesmo `id` substitui o documento (idempotente)."""
        t = time.perf_counter()
        vetores = embedder.embed([d.texto for d in req.documents])
        vazios = [i for i, v in enumerate(vetores) if v is None]
        if vazios:
            return JSONResponse(
                {"detail": f"documentos nas posicoes {vazios} nao tem termos significativos para indexar"},
                status_code=422,
            )
        itens = [
            {"id": d.id or str(uuid.uuid4()), "texto": d.texto, "metadata": d.metadata, "vetor": v}
            for d, v in zip(req.documents, vetores)
        ]
        _exigir_banco()
        n = store.upsert(itens)
        return UpsertResponse(
            indexados=n, ids=[i["id"] for i in itens], colecao=settings.vector_collection, latencia_ms=_ms(t)
        )

    @app.post("/vectors/search", response_model=SearchResponse, tags=["vetores"], summary="Busca os top_k vetores mais similares")
    def vectors_search(req: SearchRequest):
        t = time.perf_counter()
        top_k = req.top_k or TOP_K_PADRAO
        vetor = embedder.embed_um(req.texto)
        if vetor is None:
            return JSONResponse({"detail": "consulta sem termos significativos"}, status_code=422)
        _exigir_banco()
        resultados = store.search(vetor, top_k, req.score_threshold)
        return SearchResponse(
            resultados=[DocumentoRecuperado(**r) for r in resultados],
            metadados={"latencia_ms": _ms(t), "top_k": top_k, "colecao": settings.vector_collection},
        )

    return app

