"""Contratos da API interna (consumida pelo gateway e pelo worker)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

MAX_TEXTO = 10_000  # caracteres por texto
MAX_LOTE = 100  # documentos por chamada de upsert


def _nao_vazio(v: str) -> str:
    if not v.strip():
        raise ValueError("texto nao pode ser vazio")
    return v


# ------------------------------------------------------------------ entradas
class PredictRequest(BaseModel):
    texto: str = Field(min_length=1, max_length=MAX_TEXTO)
    # O gateway envia estes campos (podem vir null): padroes aplicados no handler.
    top_k: int | None = Field(default=3, ge=1, le=20, description="Documentos de contexto a recuperar")
    use_llm: bool | None = Field(default=True, description="Gerar resposta com o LLM (ou simulada)")

    _v = field_validator("texto")(_nao_vazio)


class DocumentoIn(BaseModel):
    texto: str = Field(min_length=1, max_length=MAX_TEXTO)
    id: str | None = Field(default=None, min_length=1, max_length=128, description="Reenviar o mesmo id substitui o documento")
    metadata: dict[str, Any] | None = None

    _v = field_validator("texto")(_nao_vazio)


class UpsertRequest(BaseModel):
    documents: list[DocumentoIn] = Field(min_length=1, max_length=MAX_LOTE)


class SearchRequest(BaseModel):
    texto: str = Field(min_length=1, max_length=MAX_TEXTO)
    top_k: int | None = Field(default=3, ge=1, le=20)
    score_threshold: float | None = Field(default=None, ge=-1.0, le=1.0)

    _v = field_validator("texto")(_nao_vazio)


# -------------------------------------------------------------------- saidas
class Predicao(BaseModel):
    sentimento: str
    confianca: float


class DocumentoRecuperado(BaseModel):
    id: str
    texto: str
    score: float
    metadata: dict[str, Any] = {}


class PredictResponse(BaseModel):
    predicao: Predicao
    documentos: list[DocumentoRecuperado]
    resposta: str | None = Field(description="Resposta do LLM (ou simulada); null se use_llm=false")
    metadados: dict[str, Any]


class SearchResponse(BaseModel):
    resultados: list[DocumentoRecuperado]
    metadados: dict[str, Any]


class UpsertResponse(BaseModel):
    indexados: int
    ids: list[str]
    colecao: str
    latencia_ms: float
