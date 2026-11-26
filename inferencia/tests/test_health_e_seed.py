import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from app.main import create_app
from app.seed import carregar_seed
from app.vectorstore import VectorStore


def test_seed_tem_entre_5_e_10_documentos_ficticios_sem_pii():
    import re

    docs = carregar_seed()
    assert 5 <= len(docs) <= 10
    assert len({d["id"] for d in docs}) == len(docs)
    texto = " ".join(d["texto"] for d in docs)
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", texto), "e-mail"
    assert not re.search(r"\d{3}\.?\d{3}\.?\d{3}-?\d{2}", texto), "CPF"
    assert not re.search(r"\(?\d{2}\)?\s?9?\d{4}-?\d{4}", texto), "telefone"
    assert "http" not in texto


def test_startup_cria_collection_e_semeia_sem_intervencao(client, settings, qdrant):
    assert qdrant.collection_exists(settings.vector_collection)
    assert client.get("/health").json()["vector_db"]["documentos"] == len(carregar_seed())


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "service": "inferencia",
        "modelo_carregado": True,
        "vector_db": {"status": "up", "colecao": "documentos_tecnicos", "documentos": 10},
        "llm": {"modo": "simulado"},
    }


def test_reiniciar_nao_duplica_o_seed(settings, modelo, store):
    for _ in range(3):  # 3 "boots" no mesmo banco
        with TestClient(create_app(settings, modelo=modelo, store=store)) as c:
            assert c.get("/health").json()["vector_db"]["documentos"] == 10


def test_seed_pode_ser_desligado(settings, modelo, store):
    import dataclasses

    cfg = dataclasses.replace(settings, seed_on_startup=False)
    with TestClient(create_app(cfg, modelo=modelo, store=store)) as c:
        assert c.get("/health").json()["vector_db"]["documentos"] == 0


def test_health_503_quando_banco_vetorial_fora(settings, modelo, banco_fora):
    with TestClient(create_app(settings, modelo=modelo, store=banco_fora)) as c:
        r = c.get("/health")
        assert r.status_code == 503
        assert r.json()["status"] == "degraded"
        assert r.json()["modelo_carregado"] is True
        assert r.json()["vector_db"] == {"status": "down"}


def test_health_recria_collection_apagada(client, qdrant, settings):
    qdrant.delete_collection(settings.vector_collection)
    r = client.get("/health")  # auto-recuperacao
    assert r.status_code == 200 and r.json()["vector_db"]["documentos"] == 10


def test_collection_com_dimensao_diferente_nao_e_destruida(settings, modelo, qdrant):
    qdrant.create_collection(settings.vector_collection, vectors_config=VectorParams(size=8, distance=Distance.COSINE))
    store = VectorStore(qdrant, settings.vector_collection, settings.embedding_dim)
    with TestClient(create_app(settings, modelo=modelo, store=store)) as c:
        r = c.get("/health")
        assert r.status_code == 503 and r.json()["vector_db"] == {"status": "erro_configuracao"}
        assert c.post("/predict", json={"texto": "circuit breaker"}).status_code == 503
    assert qdrant.get_collection(settings.vector_collection).config.params.vectors.size == 8  # intacta


def test_sobe_mesmo_com_banco_fora_e_se_recupera_quando_ele_volta(settings, modelo, qdrant):
    """Ordem de subida nao importa: o servico inicia, reporta 503 e se cura sozinho."""
    store = VectorStore(qdrant, settings.vector_collection, settings.embedding_dim)
    chamadas = {"n": 0}
    original = store.ensure_collection

    def instavel():
        chamadas["n"] += 1
        if chamadas["n"] <= 2:
            from app.vectorstore import VectorDBUnavailable
            raise VectorDBUnavailable("ainda subindo")
        return original()

    store.ensure_collection = instavel
    with TestClient(create_app(settings, modelo=modelo, store=store)) as c:
        assert c.get("/health").status_code in (200, 503)  # nao quebra
        assert c.get("/health").status_code == 200  # 3a tentativa: banco "voltou"
