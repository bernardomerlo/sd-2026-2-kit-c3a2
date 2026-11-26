"""Configuracao do servico de inferencia: tudo vem de variaveis de ambiente.

Nenhuma variavel e obrigatoria: os padroes permitem rodar 100% offline
(modelo local, embeddings locais e LLM simulado quando nao ha LLM_API_KEY).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_PADRAO_MODELO = Path(__file__).resolve().parent.parent / "model" / "modelo.joblib"


class ConfigError(RuntimeError):
    """Configuracao invalida."""


def _numero(env: Mapping[str, str], nome: str, padrao, tipo=int, minimo=None):
    bruto = env.get(nome, "").strip()
    if not bruto:
        return padrao
    try:
        valor = tipo(bruto)
    except ValueError as exc:
        raise ConfigError(f"{nome} invalido: {bruto!r}") from exc
    if minimo is not None and valor < minimo:
        raise ConfigError(f"{nome} deve ser >= {minimo} (recebido: {valor})")
    return valor


def _booleano(env: Mapping[str, str], nome: str, padrao: bool) -> bool:
    bruto = env.get(nome, "").strip().lower()
    if not bruto:
        return padrao
    return bruto in ("1", "true", "sim", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # --- modelo e embeddings
    model_path: str = str(_PADRAO_MODELO)
    embedding_dim: int = 1024
    # --- banco vetorial
    vector_db_url: str = "http://localhost:6333"
    vector_db_api_key: str = ""  # so para Qdrant Cloud
    vector_collection: str = "documentos_tecnicos"
    vector_db_timeout_seconds: int = 5
    vector_db_startup_timeout_seconds: int = 120
    seed_on_startup: bool = True
    # --- LLM (vazio = modo simulado, sem internet)
    llm_api_key: str = ""
    llm_api_url: str = "https://api.openai.com/v1/chat/completions"
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 20.0
    # --- logs
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        d = cls()
        return cls(
            model_path=env.get("MODEL_PATH", "").strip() or d.model_path,
            embedding_dim=_numero(env, "EMBEDDING_DIM", d.embedding_dim, int, 16),
            vector_db_url=(env.get("VECTOR_DB_URL", "").strip() or d.vector_db_url).rstrip("/"),
            vector_db_api_key=env.get("VECTOR_DB_API_KEY", "").strip(),
            vector_collection=env.get("VECTOR_COLLECTION", "").strip() or d.vector_collection,
            vector_db_timeout_seconds=_numero(env, "VECTOR_DB_TIMEOUT_SECONDS", d.vector_db_timeout_seconds, int, 1),
            vector_db_startup_timeout_seconds=_numero(
                env, "VECTOR_DB_STARTUP_TIMEOUT_SECONDS", d.vector_db_startup_timeout_seconds, int, 0
            ),
            seed_on_startup=_booleano(env, "SEED_ON_STARTUP", d.seed_on_startup),
            llm_api_key=env.get("LLM_API_KEY", "").strip(),
            llm_api_url=env.get("LLM_API_URL", "").strip() or d.llm_api_url,
            llm_model=env.get("LLM_MODEL", "").strip() or d.llm_model,
            llm_timeout_seconds=_numero(env, "LLM_TIMEOUT_SECONDS", d.llm_timeout_seconds, float, 0.1),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
        )
