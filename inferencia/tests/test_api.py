import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import llm_com_api

SIGILO = "ZXQ-TEXTO-SIGILOSO-9981"


# ================================================================ /predict
class TestPredict:
    def test_contrato_da_resposta(self, client):
        r = client.post("/predict", json={"texto": "como funciona o circuit breaker?", "top_k": 2})
        assert r.status_code == 200
        d = r.json()
        assert set(d) == {"predicao", "documentos", "resposta", "metadados"}
        assert set(d["predicao"]) == {"sentimento", "confianca"}
        assert len(d["documentos"]) == 2
        assert set(d["documentos"][0]) == {"id", "texto", "score", "metadata"}
        assert d["documentos"][0]["id"] == "seed-circuit-breaker"  # recuperou o contexto certo
        assert d["documentos"][0]["score"] >= d["documentos"][1]["score"]
        lat = d["metadados"]["latencia_ms"]
        assert set(lat) == {"modelo", "busca_vetorial", "llm", "total"} and lat["total"] >= 0
        assert d["metadados"]["llm_modo"] == "simulado"
        assert d["resposta"].startswith("[MODO SIMULADO")

    @pytest.mark.parametrize(
        "consulta,esperado",
        [
            ("o que é o teorema CAP e consistência", "seed-cap"),
            ("como funciona autenticação com token jwt", "seed-jwt"),
            ("retentativa com backoff exponencial e jitter", "seed-retry-backoff"),
            ("rastreamento distribuído opentelemetry traceparent", "seed-tracing"),
            ("prazo de retenção de dados pessoais LGPD", "seed-lgpd"),
            ("fila de mensagens e dead letter queue", "seed-mensageria"),
        ],
    )
    def test_busca_recupera_o_documento_relevante_em_primeiro(self, client, consulta, esperado):
        docs = client.post("/predict", json={"texto": consulta, "top_k": 3}).json()["documentos"]
        assert docs[0]["id"] == esperado

    def test_nao_devolve_o_texto_do_usuario(self, client):
        r = client.post("/predict", json={"texto": f"{SIGILO} adorei o servico"})
        assert SIGILO not in r.text

    def test_payload_exato_enviado_pelo_gateway(self, client):
        """Gateway (Etapa 1) envia InferRequest.model_dump(): top_k=3, use_llm=True."""
        assert client.post("/predict", json={"texto": "teste", "top_k": 3, "use_llm": True}).status_code == 200

    def test_campos_nulos_usam_padrao(self, client):
        r = client.post("/predict", json={"texto": "banco vetorial", "top_k": None, "use_llm": None})
        d = r.json()
        assert r.status_code == 200 and d["metadados"]["top_k"] == 3 and d["resposta"] is not None

    def test_use_llm_false_nao_gera_resposta(self, client):
        d = client.post("/predict", json={"texto": "banco vetorial", "use_llm": False}).json()
        assert d["resposta"] is None and d["metadados"]["llm_modo"] is None

    def test_campos_extras_sao_ignorados(self, client):
        assert client.post("/predict", json={"texto": "x fila", "campo_novo": 1}).status_code == 200

    @pytest.mark.parametrize(
        "corpo",
        [{}, {"texto": ""}, {"texto": "   "}, {"texto": "a" * 10_001}, {"texto": "ok", "top_k": 0}, {"texto": "ok", "top_k": 21}],
    )
    def test_entradas_invalidas_422(self, client, corpo):
        assert client.post("/predict", json=corpo).status_code == 422

    def test_texto_so_com_stopwords_ainda_classifica_sem_contexto(self, client):
        d = client.post("/predict", json={"texto": "de a o que para"}).json()
        assert d["documentos"] == [] and d["metadados"]["contexto"] == "sem_termos_significativos"

    def test_banco_fora_retorna_503_generico(self, settings, modelo, banco_fora):
        with TestClient(create_app(settings, modelo=modelo, store=banco_fora)) as c:
            r = c.post("/predict", json={"texto": "circuit breaker"})
            assert r.status_code == 503
            assert r.json() == {"detail": "Banco vetorial indisponivel"}
            assert "127.0.0.1" not in r.text  # nao vaza topologia


class TestErrosNaoEcoamEntrada:
    """O 422 padrao do FastAPI inclui o campo `input` (texto do usuario). Aqui nao pode."""

    def test_422_nao_devolve_o_texto_enviado(self, client):
        for rota, corpo in [
            ("/predict", {"texto": SIGILO, "top_k": 99}),
            ("/predict", {"texto": SIGILO * 1000}),  # grande demais
            ("/vectors/search", {"texto": SIGILO, "top_k": 0}),
            ("/vectors/upsert", {"documents": [{"texto": SIGILO * 1000}]}),
        ]:
            r = client.post(rota, json=corpo)
            assert r.status_code == 422 and SIGILO not in r.text, rota
            assert set(r.json()["detail"][0]) == {"loc", "msg", "type"}

    def test_json_malformado_422_sem_eco(self, client):
        r = client.post("/predict", content=f'{{"texto": "{SIGILO}"'.encode(), headers={"Content-Type": "application/json"})
        assert r.status_code == 422 and SIGILO not in r.text


# ============================================================ /vectors/upsert
class TestUpsert:
    def test_indexa_e_a_busca_encontra(self, client):
        r = client.post(
            "/vectors/upsert",
            json={"documents": [{"texto": "Kubernetes escala pods automaticamente conforme a carga", "id": "k8s-1", "metadata": {"tema": "k8s"}}]},
        )
        assert r.status_code == 200
        assert r.json()["indexados"] == 1 and r.json()["ids"] == ["k8s-1"]
        achados = client.post("/vectors/search", json={"texto": "escalar pods kubernetes"}).json()["resultados"]
        assert achados[0]["id"] == "k8s-1" and achados[0]["metadata"] == {"tema": "k8s"}

    def test_mesmo_id_substitui_em_vez_de_duplicar(self, client):
        antes = client.get("/health").json()["vector_db"]["documentos"]
        for texto in ("primeira versao do documento", "segunda versao do documento"):
            client.post("/vectors/upsert", json={"documents": [{"texto": texto, "id": "doc-x"}]})
        depois = client.get("/health").json()["vector_db"]["documentos"]
        assert depois == antes + 1
        assert client.post("/vectors/search", json={"texto": "segunda versao"}).json()["resultados"][0]["texto"].startswith("segunda")

    def test_sem_id_o_servico_gera_um(self, client):
        r = client.post("/vectors/upsert", json={"documents": [{"texto": "documento sem identificador"}, {"texto": "outro documento sem id", "id": None, "metadata": None}]})
        ids = r.json()["ids"]
        assert r.status_code == 200 and len(ids) == 2 and len(set(ids)) == 2 and all(ids)

    def test_aceita_id_que_nao_e_uuid(self, client):
        """O Qdrant so aceita int/UUID como id de ponto: o servico converte e preserva o original."""
        r = client.post("/vectors/upsert", json={"documents": [{"texto": "documento com id livre", "id": "Aula 11 / slide #3"}]})
        assert r.status_code == 200
        assert client.post("/vectors/search", json={"texto": "id livre"}).json()["resultados"][0]["id"] == "Aula 11 / slide #3"

    @pytest.mark.parametrize(
        "corpo",
        [{}, {"documents": []}, {"documents": [{"texto": ""}]}, {"documents": [{"texto": "x" * 10_001}]}, {"documents": [{"texto": "ok"}] * 101}],
    )
    def test_invalidos_422(self, client, corpo):
        assert client.post("/vectors/upsert", json=corpo).status_code == 422

    def test_documento_sem_termos_significativos_422_e_nada_e_gravado(self, client):
        antes = client.get("/health").json()["vector_db"]["documentos"]
        r = client.post("/vectors/upsert", json={"documents": [{"texto": "fila e worker"}, {"texto": "de a o que"}]})
        assert r.status_code == 422 and "[1]" in r.json()["detail"]
        assert client.get("/health").json()["vector_db"]["documentos"] == antes

    def test_payload_exato_enviado_pelo_gateway(self, client):
        """Gateway (Etapa 1) envia {"documents":[{"texto","id","metadata"}]} (id/metadata podem ser null)."""
        corpo = {"documents": [{"texto": "indexacao via gateway", "id": None, "metadata": None}]}
        assert client.post("/vectors/upsert", json=corpo).status_code == 200


# ============================================================ /vectors/search
class TestSearch:
    def test_top_k(self, client):
        assert len(client.post("/vectors/search", json={"texto": "servico distribuido", "top_k": 4}).json()["resultados"]) == 4
        assert len(client.post("/vectors/search", json={"texto": "servico distribuido"}).json()["resultados"]) == 3

    def test_score_threshold_filtra(self, client):
        todos = client.post("/vectors/search", json={"texto": "circuit breaker", "top_k": 10}).json()["resultados"]
        corte = (todos[0]["score"] + todos[-1]["score"]) / 2  # entre o melhor e o pior
        filtrados = client.post("/vectors/search", json={"texto": "circuit breaker", "top_k": 10, "score_threshold": corte}).json()["resultados"]
        assert 0 < len(filtrados) < len(todos) and all(r["score"] >= corte for r in filtrados)

    def test_nao_expoe_vetores(self, client):
        r = client.post("/vectors/search", json={"texto": "gateway"}).json()["resultados"][0]
        assert "vector" not in r and "vetor" not in r

    @pytest.mark.parametrize("corpo", [{}, {"texto": " "}, {"texto": "x", "top_k": 0}, {"texto": "x", "score_threshold": 2}, {"texto": "de a o"}])
    def test_invalidos_422(self, client, corpo):
        assert client.post("/vectors/search", json=corpo).status_code == 422

    def test_banco_fora_503(self, settings, modelo, banco_fora):
        with TestClient(create_app(settings, modelo=modelo, store=banco_fora)) as c:
            assert c.post("/vectors/search", json={"texto": "gateway"}).status_code == 503
            assert c.post("/vectors/upsert", json={"documents": [{"texto": "documento valido"}]}).status_code == 503


# ==================================================================== LLM
class TestLLM:
    def _app(self, settings, modelo, store, llm):
        return TestClient(create_app(settings, modelo=modelo, store=store, llm=llm))

    def test_modo_api_usa_a_resposta_do_llm_e_envia_a_chave(self, settings, modelo, store):
        vistos = {}

        def handler(req: httpx.Request):
            vistos["auth"] = req.headers["authorization"]
            vistos["corpo"] = json.loads(req.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "resposta do LLM"}}]})

        with self._app(settings, modelo, store, llm_com_api(handler)) as c:
            d = c.post("/predict", json={"texto": "circuit breaker"}).json()
        assert d["resposta"] == "resposta do LLM" and d["metadados"]["llm_modo"] == "api"
        assert vistos["auth"] == "Bearer chave-de-teste" and vistos["corpo"]["model"] == "modelo-x"
        assert "circuit breaker" in vistos["corpo"]["messages"][0]["content"]  # contexto + pergunta

    @pytest.mark.parametrize("falha", ["rede", "5xx", "timeout", "json_estranho"])
    def test_falha_da_api_degrada_para_simulado_sem_derrubar(self, settings, modelo, store, falha):
        def handler(req):
            if falha == "rede":
                raise httpx.ConnectError("sem rede")
            if falha == "timeout":
                raise httpx.ReadTimeout("lento")
            if falha == "5xx":
                return httpx.Response(502)
            return httpx.Response(200, json={"inesperado": 1})

        with self._app(settings, modelo, store, llm_com_api(handler)) as c:
            r = c.post("/predict", json={"texto": "circuit breaker"})
        assert r.status_code == 200
        assert r.json()["metadados"]["llm_modo"] == "simulado_fallback"
        assert r.json()["resposta"].startswith("[MODO SIMULADO")

    def test_health_informa_o_modo(self, settings, modelo, store):
        with self._app(settings, modelo, store, llm_com_api(lambda r: httpx.Response(200))) as c:
            assert c.get("/health").json()["llm"] == {"modo": "api"}
