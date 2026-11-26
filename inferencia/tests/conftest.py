import json
import logging
from io import StringIO

import httpx
import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from app.config import Settings
from app.llm import ClienteLLM
from app.main import create_app
from app.modelo import carregar_modelo
from app.observability import NOME_LOGGER, JsonFormatter
from app.vectorstore import VectorStore


@pytest.fixture(scope="session")
def modelo(tmp_path_factory):
    return carregar_modelo(str(tmp_path_factory.mktemp("modelo") / "modelo.joblib"))


@pytest.fixture
def settings(tmp_path):
    return Settings(
        model_path=str(tmp_path / "modelo.joblib"),
        embedding_dim=1024,
        vector_db_startup_timeout_seconds=1,
    )


@pytest.fixture
def qdrant():
    """Qdrant em memoria (mesma API do servidor real)."""
    return QdrantClient(":memory:")


@pytest.fixture
def store(qdrant, settings):
    return VectorStore(qdrant, settings.vector_collection, settings.embedding_dim)


@pytest.fixture
def banco_fora(settings):
    """Store apontando para um Qdrant que NAO existe: erros reais de conexao."""
    return VectorStore(QdrantClient(url="http://127.0.0.1:1", timeout=1), settings.vector_collection, settings.embedding_dim)


@pytest.fixture
def client(settings, modelo, store):
    with TestClient(create_app(settings, modelo=modelo, store=store)) as c:
        yield c


@pytest.fixture
def logs(client):
    """Captura as linhas JSON que o logger do servico escreveria no stdout."""
    buf = StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(JsonFormatter())
    logging.getLogger(NOME_LOGGER).addHandler(h)

    def linhas():
        return [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]

    linhas.texto = buf.getvalue
    yield linhas
    logging.getLogger(NOME_LOGGER).removeHandler(h)


def llm_com_api(handler, api_key="chave-de-teste"):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return ClienteLLM(api_key, "https://llm.test/v1/chat/completions", "modelo-x", 5.0, http=http)
