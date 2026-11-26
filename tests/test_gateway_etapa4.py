"""Etapa 4 - testes do gateway: rate limit (429), retry, circuit breaker (503) e trace id.

O servico de inferencia e simulado com httpx.MockTransport (nao precisa de Docker).
Rodar: python -m pytest tests/ -v   (com gateway/requirements.txt instalado)
"""
import re

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import gateway.app as gw  # noqa: E402
from gateway.auth import criar_token_acesso  # noqa: E402
from gateway.rate_limit import LimitadorTaxa  # noqa: E402
from gateway.resiliencia import CircuitBreaker  # noqa: E402

client = TestClient(gw.app)
_AsyncClientReal = httpx.AsyncClient


def _headers(sub="avaliador"):
    return {"Authorization": f"Bearer {criar_token_acesso(subject=sub)}"}


@pytest.fixture(autouse=True)
def isolar(monkeypatch):
    """Cada teste com breaker/limitador novos, sem Redis e sem espera entre retries."""
    monkeypatch.setattr(gw, "breaker_inferencia", CircuitBreaker("inferencia", limite_falhas=3, tempo_recuperacao=30))
    monkeypatch.setattr(gw, "limitador", LimitadorTaxa(1000, 60))
    monkeypatch.setattr(gw, "RETRY_BASE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(gw, "RETRY_MAX_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(gw, "RETRY_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(gw, "obter_cliente_redis", lambda: None)


def _simular_inferencia(monkeypatch, handler):
    """Troca o AsyncClient do gateway por um que responde com `handler`."""
    chamadas = []

    def registrar(request):
        chamadas.append(request)
        return handler(request, len(chamadas))

    def fabrica(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(registrar)
        return _AsyncClientReal(*args, **kwargs)

    monkeypatch.setattr(gw.httpx, "AsyncClient", fabrica)
    return chamadas


RESPOSTA_OK = {"predicao": {"sentimento": "positivo", "confianca": 0.9}}


def test_rate_limit_retorna_429_com_retry_after(monkeypatch):
    monkeypatch.setattr(gw, "limitador", LimitadorTaxa(2, 60))
    _simular_inferencia(monkeypatch, lambda req, n: httpx.Response(200, json=RESPOSTA_OK))
    h = _headers("cliente-rate-limit")

    r1 = client.post("/v1/infer", json={"texto": "a"}, headers=h)
    r2 = client.post("/v1/infer", json={"texto": "b"}, headers=h)
    r3 = client.post("/v1/infer", json={"texto": "c"}, headers=h)

    assert [r1.status_code, r2.status_code, r3.status_code] == [200, 200, 429]
    assert r1.headers["X-RateLimit-Limit"] == "2"
    assert "Retry-After" in r3.headers
    # outro cliente nao e afetado
    assert client.post("/v1/infer", json={"texto": "d"}, headers=_headers("outro")).status_code == 200


def test_sem_token_continua_401_antes_do_rate_limit(monkeypatch):
    monkeypatch.setattr(gw, "limitador", LimitadorTaxa(0, 60))
    assert client.post("/v1/infer", json={"texto": "a"}).status_code == 401


def test_retry_recupera_de_503_transitorio(monkeypatch):
    chamadas = _simular_inferencia(
        monkeypatch,
        lambda req, n: httpx.Response(503) if n < 3 else httpx.Response(200, json=RESPOSTA_OK),
    )
    r = client.post("/v1/infer", json={"texto": "oi"}, headers=_headers())
    assert r.status_code == 200
    assert len(chamadas) == 3


def test_erro_4xx_nao_tem_retry(monkeypatch):
    chamadas = _simular_inferencia(monkeypatch, lambda req, n: httpx.Response(422, json={"detail": "invalido"}))
    r = client.post("/v1/infer", json={"texto": "oi"}, headers=_headers())
    assert r.status_code == 422
    assert len(chamadas) == 1


def test_circuit_breaker_abre_e_falha_rapido(monkeypatch):
    def fora_do_ar(req, n):
        raise httpx.ConnectError("conexao recusada")

    chamadas = _simular_inferencia(monkeypatch, fora_do_ar)
    h = _headers()

    r1 = client.post("/v1/infer", json={"texto": "oi"}, headers=h)
    assert r1.status_code == 503
    assert len(chamadas) == 3                      # 3 tentativas -> breaker abriu

    r2 = client.post("/v1/infer", json={"texto": "oi"}, headers=h)
    assert r2.status_code == 503
    assert "circuit breaker" in r2.json()["detail"]
    assert "Retry-After" in r2.headers
    assert len(chamadas) == 3                      # nem chamou a inferencia

    saude = client.get("/health").json()
    assert saude["dependencies"]["inference_circuit_breaker"]["state"] == "open"


def test_timeout_vira_504(monkeypatch):
    def lento(req, n):
        raise httpx.ReadTimeout("demorou")

    _simular_inferencia(monkeypatch, lento)
    r = client.post("/v1/infer", json={"texto": "oi"}, headers=_headers())
    assert r.status_code == 504


def test_trace_propagado_para_inferencia(monkeypatch):
    chamadas = _simular_inferencia(monkeypatch, lambda req, n: httpx.Response(200, json=RESPOSTA_OK))
    r = client.post("/v1/infer", json={"texto": "oi"}, headers=_headers())
    trace_id = r.headers["X-Trace-Id"]
    assert re.fullmatch(r"[0-9a-f]{32}", trace_id)
    assert chamadas[0].headers["X-Trace-Id"] == trace_id   # inferencia loga o mesmo id


def test_trace_continua_traceparent_recebido(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    _simular_inferencia(monkeypatch, lambda req, n: httpx.Response(200, json=RESPOSTA_OK))
    pai = "4bf92f3577b34da6a3ce929d0e0e4736"
    h = {**_headers(), "traceparent": f"00-{pai}-00f067aa0ba902b7-01"}
    r = client.post("/v1/infer", json={"texto": "oi"}, headers=h)
    assert r.headers["X-Trace-Id"] == pai
