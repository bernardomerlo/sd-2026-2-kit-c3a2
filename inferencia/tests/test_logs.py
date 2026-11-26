import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import llm_com_api

SIGILO = "CPF-123.456.789-00-SEGREDO-XYZ"


def test_linha_json_com_campos_do_plano(client, logs):
    client.post("/predict", json={"texto": "banco vetorial"})
    (linha,) = [l for l in logs() if l["message"] == "request"]
    assert {"timestamp", "service", "level", "trace_id", "message"} <= set(linha)
    assert linha["service"] == "inferencia" and linha["route"] == "POST /predict" and linha["status_code"] == 200


def test_nunca_loga_texto_documentos_ip_nem_tokens(client, logs):
    h = {"Authorization": "Bearer TOKEN-SECRETO-ABC", "X-Forwarded-For": "203.0.113.9"}
    client.post("/predict", json={"texto": SIGILO}, headers=h)
    client.post("/vectors/upsert", json={"documents": [{"texto": f"{SIGILO} documento", "id": "ID-SIGILOSO-77", "metadata": {"k": SIGILO}}]}, headers=h)
    client.post("/vectors/search", json={"texto": SIGILO}, headers=h)
    saida = logs.texto()
    assert saida.strip()
    for proibido in (SIGILO, "123.456.789", "TOKEN-SECRETO", "203.0.113.9", "testclient", "ID-SIGILOSO", "Bearer"):
        assert proibido not in saida, proibido


def test_trace_id_do_gateway_e_propagado_e_devolvido(client, logs):
    trace = "7d0b6b2e-91c4-4f5e-8c1d-2f6f6f0a1b2c"  # uuid4, como o gateway gera
    r = client.get("/health", headers={"X-Trace-Id": trace})
    assert r.headers["x-trace-id"] == trace
    assert [l["trace_id"] for l in logs() if l["message"] == "request"] == [trace]


def test_trace_id_invalido_e_substituido(client):
    r = client.get("/health", headers={"X-Trace-Id": 'x"}\n{"level":"FALSO'})
    assert len(r.headers["x-trace-id"]) == 32 and "FALSO" not in r.headers["x-trace-id"]


def test_logs_dentro_do_handler_tem_o_trace_id(settings, modelo, store, logs):
    def quebra(req):
        raise httpx.ConnectError("x")

    import logging
    from io import StringIO

    from app.observability import JsonFormatter

    buf = StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(JsonFormatter())
    app = create_app(settings, modelo=modelo, store=store, llm=llm_com_api(quebra))
    logging.getLogger("inferencia").addHandler(h)
    with TestClient(app) as c:
        c.post("/predict", json={"texto": "circuit breaker"}, headers={"X-Trace-Id": "trace-abc-123"})
    import json

    aviso = [json.loads(l) for l in buf.getvalue().splitlines() if "llm_falhou" in l][0]
    assert aviso["trace_id"] == "trace-abc-123" and aviso["error_type"] == "ConnectError"


def test_erro_inesperado_vira_500_generico_sem_vazar(settings, modelo, store):
    app = create_app(settings, modelo=modelo, store=store)

    @app.get("/boom")
    def boom():
        raise ValueError(f"mensagem com dado pessoal {SIGILO}")

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
    assert r.status_code == 500 and SIGILO not in r.text and r.json() == {"detail": "Erro interno do servico de inferencia"}


def test_rota_inexistente_nao_registra_o_path(client, logs):
    client.get("/dados-do-usuario/123")
    assert "dados-do-usuario" not in logs.texto() and "<nao-encontrada>" in logs.texto()
