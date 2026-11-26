import pytest
from fastapi.testclient import TestClient
from gateway.app import app
from gateway.auth import criar_token_acesso

client = TestClient(app)


def test_health_public():
    """Rota /health deve ser pública e retornar status ok sem necessidade de token."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "gateway"
    assert "dependencies" in data


def test_saude_legada():
    """Rota legada /saude deve responder normalmente."""
    response = client.get("/saude")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_gerar_token_jwt():
    """Endpoint POST /auth/token gera JWT válido assinado com HS256."""
    response = client.post("/auth/token", json={"username": "professor_avaliador"})
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0


def test_rotas_negocio_exigem_jwt_401():
    """Todas as rotas /v1/* devem barrar chamadas sem token com 401 Unauthorized."""
    endpoints = [
        ("post", "/v1/infer", {"texto": "teste"}),
        ("post", "/v1/jobs", {"texto": "tarefa assincrona"}),
        ("get", "/v1/jobs/job-inexistente", None),
        ("post", "/v1/documents", {"texto": "documento"}),
    ]

    for metodo, url, payload in endpoints:
        if metodo == "post":
            resp = client.post(url, json=payload)
        else:
            resp = client.get(url)

        assert resp.status_code == 401, f"Falha na rota {url}: esperava 401, obteve {resp.status_code}"
        assert "WWW-Authenticate" in resp.headers


def test_token_invalido_retorna_401():
    """Token com assinatura inválida ou malformado deve retornar 401."""
    headers = {"Authorization": "Bearer token-invalido-malicioso"}
    response = client.post("/v1/infer", json={"texto": "teste"}, headers=headers)
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


def test_token_expirado_retorna_401():
    """Token expirado deve retornar 401 Unauthorized."""
    token_expirado = criar_token_acesso(subject="usuario_teste", minutos_expiracao=-10)
    headers = {"Authorization": f"Bearer {token_expirado}"}
    response = client.post("/v1/infer", json={"texto": "teste"}, headers=headers)
    assert response.status_code == 401
    assert "expirado" in response.json()["detail"].lower()


def test_criar_e_consultar_job_assincrono():
    """
    POST /v1/jobs deve enfileirar tarefa e retornar 202 com job_id.
    GET /v1/jobs/{job_id} deve permitir consulta posterior do status.
    """
    token = criar_token_acesso(subject="avaliador")
    headers = {"Authorization": f"Bearer {token}"}

    resp_post = client.post(
        "/v1/jobs",
        json={"texto": "Classificar sentimento e buscar contexto"},
        headers=headers,
    )
    assert resp_post.status_code == 202
    job_data = resp_post.json()
    assert "job_id" in job_data
    assert job_data["status"] == "pending"
    job_id = job_data["job_id"]

    resp_get = client.get(f"/v1/jobs/{job_id}", headers=headers)
    assert resp_get.status_code == 200
    status_data = resp_get.json()
    assert status_data["job_id"] == job_id
    assert status_data["status"] in ["pending", "processing", "completed"]


def test_consultar_job_inexistente_retorna_404():
    token = criar_token_acesso(subject="avaliador")
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.get("/v1/jobs/00000000-0000-0000-0000-000000000000", headers=headers)
    assert resp.status_code == 404


def test_rate_limit_retorna_429():
    """Passou do limite por minuto -> 429 com Retry-After (TAREFAS.md item 2)."""
    from gateway import rate_limit

    limite_original = rate_limit.RATE_LIMIT_PER_MINUTE
    rate_limit.RATE_LIMIT_PER_MINUTE = 3
    rate_limit._contadores_memoria.clear()
    try:
        token = client.post("/auth/token", json={"username": "teste_rate_limit"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        codigos = [client.get("/v1/jobs/inexistente", headers=headers).status_code for _ in range(4)]
        assert codigos[:3] == [404, 404, 404]
        assert codigos[3] == 429
        resposta = client.get("/v1/jobs/inexistente", headers=headers)
        assert resposta.status_code == 429
        assert "Retry-After" in resposta.headers
        # /health nunca e limitado (monitoramento)
        assert client.get("/health").status_code == 200
    finally:
        rate_limit.RATE_LIMIT_PER_MINUTE = limite_original
        rate_limit._contadores_memoria.clear()
