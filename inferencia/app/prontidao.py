"""Prontidao do banco vetorial: cria a collection e semeia os documentos, com auto-recuperacao.

O servico NAO depende da ordem de subida dos containers: se o Qdrant ainda nao esta de pe,
uma thread tenta de novo ate VECTOR_DB_STARTUP_TIMEOUT_SECONDS, e /health, /predict e
/vectors/* tambem tentam se recuperar (ex.: Qdrant reiniciado sem volume perdeu a collection).
"""
from __future__ import annotations

import logging
import threading
import time

from app.embeddings import Embedder
from app.vectorstore import VectorDBConfigError, VectorDBUnavailable, VectorStore

log = logging.getLogger("inferencia")


class Prontidao:
    def __init__(self, store: VectorStore, embedder: Embedder, seed_docs: list[dict], semear: bool):
        self.store = store
        self.embedder = embedder
        self.seed_docs = seed_docs
        self.semear = semear
        self.pronto = False
        self.ultimo_erro: str | None = None  # "indisponivel" | "configuracao"
        self._lock = threading.Lock()

    def garantir(self) -> bool:
        """Cria a collection (se faltar) e semeia. Idempotente. Retorna True se esta pronto."""
        if self.pronto:
            return True
        with self._lock:
            if self.pronto:
                return True
            try:
                self.store.ensure_collection()
                if self.semear and self.seed_docs:
                    vetores = self.embedder.embed([d["texto"] for d in self.seed_docs])
                    self.store.upsert(
                        [
                            {"id": d["id"], "texto": d["texto"], "metadata": d.get("metadata"), "vetor": v}
                            for d, v in zip(self.seed_docs, vetores)
                            if v is not None
                        ]
                    )
                self.pronto, self.ultimo_erro = True, None
                log.info("banco_vetorial_pronto", extra={"colecao": self.store.collection, "documentos": self.store.count()})
            except VectorDBUnavailable:
                self.ultimo_erro = "indisponivel"
            except VectorDBConfigError:
                self.ultimo_erro = "configuracao"
                log.error("banco_vetorial_configuracao_invalida")
            return self.pronto

    def invalidar(self) -> None:
        self.pronto = False

    def verificar(self) -> dict:
        """Estado para o /health. Tenta se recuperar se a collection sumiu."""
        try:
            if not self.store.collection_exists():
                self.invalidar()
            if not self.pronto:
                self.garantir()
            if not self.pronto:
                return {"status": "erro_configuracao" if self.ultimo_erro == "configuracao" else "down"}
            return {"status": "up", "colecao": self.store.collection, "documentos": self.store.count()}
        except VectorDBUnavailable:
            self.invalidar()
            return {"status": "down"}

    def iniciar_tentativas_em_segundo_plano(self, limite_segundos: int) -> threading.Event:
        """Thread daemon que insiste ate ficar pronto ou estourar o limite. Retorna o Event de parada."""
        parar = threading.Event()

        def laco():
            prazo = time.monotonic() + limite_segundos
            espera = 1.0
            while not parar.is_set() and not self.pronto and time.monotonic() < prazo:
                if self.garantir() or self.ultimo_erro == "configuracao":
                    return
                log.info("banco_vetorial_aguardando")
                parar.wait(espera)
                espera = min(espera * 2, 10.0)

        threading.Thread(target=laco, name="prontidao-banco", daemon=True).start()
        return parar
