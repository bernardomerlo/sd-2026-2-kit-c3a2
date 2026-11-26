"""Armazenamento vetorial (Qdrant). Toda chamada ao banco passa por esta classe.

(Centralizar aqui facilita a Etapa 4: envolver estas chamadas com retry/circuit breaker.)

* A collection e criada no startup se nao existir (idempotente).
* IDs: o cliente pode mandar qualquer string como `id`; o Qdrant so aceita inteiro ou UUID,
  entao o ponto usa UUIDv5(id) (deterministico) e o id original fica no payload.
  Reenviar o mesmo `id` SUBSTITUI o documento (upsert idempotente).
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from qdrant_client.models import Distance, PointStruct, VectorParams

# Namespace fixo: o mesmo `id` gera sempre o mesmo UUID, em qualquer replica.
_NAMESPACE = uuid.UUID("6f1c0c1e-5b0e-4b64-9d0b-2a7f6d3b9a10")


class VectorDBUnavailable(Exception):
    """Banco vetorial inacessivel ou com erro de servidor (vira HTTP 503)."""


class VectorDBConfigError(Exception):
    """Collection existente incompativel com a configuracao (ex.: dimensao diferente)."""


def ponto_id(doc_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, doc_id))


@contextmanager
def _traduzir_erros():
    try:
        yield
    except ResponseHandlingException as exc:  # conexao recusada, timeout, DNS...
        raise VectorDBUnavailable("banco vetorial inacessivel") from exc
    except UnexpectedResponse as exc:
        if exc.status_code >= 500 or exc.status_code == 404:
            raise VectorDBUnavailable(f"banco vetorial respondeu HTTP {exc.status_code}") from exc
        raise


class VectorStore:
    def __init__(self, client: QdrantClient, collection: str, dim: int):
        self.client = client
        self.collection = collection
        self.dim = dim

    @classmethod
    def from_settings(cls, s) -> "VectorStore":
        client = QdrantClient(
            url=s.vector_db_url,
            api_key=s.vector_db_api_key or None,
            timeout=s.vector_db_timeout_seconds,
            prefer_grpc=False,
        )
        return cls(client, s.vector_collection, s.embedding_dim)

    # ------------------------------------------------------------ colecao
    def collection_exists(self) -> bool:
        with _traduzir_erros():
            return bool(self.client.collection_exists(self.collection))

    def ensure_collection(self) -> None:
        """Cria a collection se nao existir; se existir, confere a dimensao do vetor."""
        with _traduzir_erros():
            if not self.client.collection_exists(self.collection):
                try:
                    self.client.create_collection(
                        collection_name=self.collection,
                        vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
                    )
                    return
                except UnexpectedResponse as exc:
                    if exc.status_code != 409:  # 409 = outra replica criou ao mesmo tempo
                        raise
            tamanho = self.client.get_collection(self.collection).config.params.vectors.size
        if tamanho != self.dim:
            raise VectorDBConfigError(
                f"collection '{self.collection}' tem vetores de dimensao {tamanho}, "
                f"mas EMBEDDING_DIM={self.dim}. Use outra VECTOR_COLLECTION ou apague a existente."
            )

    def count(self) -> int:
        with _traduzir_erros():
            return int(self.client.get_collection(self.collection).points_count or 0)

    # -------------------------------------------------------------- dados
    def upsert(self, itens: list[dict[str, Any]]) -> int:
        """itens: [{"id": str, "texto": str, "metadata": dict, "vetor": list[float]}]"""
        pontos = [
            PointStruct(
                id=ponto_id(i["id"]),
                vector=i["vetor"],
                payload={"doc_id": i["id"], "texto": i["texto"], "metadata": i.get("metadata") or {}},
            )
            for i in itens
        ]
        with _traduzir_erros():
            self.client.upsert(collection_name=self.collection, points=pontos, wait=True)
        return len(pontos)

    def search(self, vetor: list[float], top_k: int, score_threshold: float | None = None) -> list[dict[str, Any]]:
        with _traduzir_erros():
            resp = self.client.query_points(
                collection_name=self.collection,
                query=vetor,
                limit=top_k,
                with_payload=True,
                score_threshold=score_threshold,
            )
        return [
            {
                "id": p.payload.get("doc_id", str(p.id)),
                "texto": p.payload.get("texto", ""),
                "score": round(float(p.score), 4),
                "metadata": p.payload.get("metadata", {}),
            }
            for p in resp.points
        ]
