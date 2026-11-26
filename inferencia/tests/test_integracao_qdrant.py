"""Testes contra um Qdrant REAL. Pulados por padrao.

Rodar:  docker run -d -p 6333:6333 qdrant/qdrant:v1.12.4
        QDRANT_TEST_URL=http://localhost:6333 pytest -m integracao
"""
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.vectorstore import VectorStore

URL = os.getenv("QDRANT_TEST_URL")
pytestmark = [pytest.mark.integracao, pytest.mark.skipif(not URL, reason="defina QDRANT_TEST_URL")]


@pytest.fixture
def cfg(tmp_path):
    return Settings(model_path=str(tmp_path / "m.joblib"), vector_db_url=URL, vector_collection=f"teste_{uuid.uuid4().hex[:8]}")


def test_ciclo_completo_com_qdrant_real(cfg, modelo):
    store = VectorStore.from_settings(cfg)
    try:
        for _ in range(2):  # sobe duas vezes: criacao da collection e seed sao idempotentes
            with TestClient(create_app(cfg, modelo=modelo, store=store)) as c:
                assert c.get("/health").json()["vector_db"]["documentos"] == 10
        with TestClient(create_app(cfg, modelo=modelo, store=store)) as c:
            c.post("/vectors/upsert", json={"documents": [{"texto": "documento real no qdrant", "id": "a b/c"}]})
            assert c.post("/vectors/search", json={"texto": "documento real qdrant"}).json()["resultados"][0]["id"] == "a b/c"
            assert c.post("/predict", json={"texto": "circuit breaker"}).json()["documentos"][0]["id"] == "seed-circuit-breaker"
    finally:
        store.client.delete_collection(cfg.vector_collection)
