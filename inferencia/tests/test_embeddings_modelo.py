import math
import os

import joblib
import pytest

from app.embeddings import Embedder
from app.modelo import TREINO, carregar_modelo


def cos(a, b):
    return sum(x * y for x, y in zip(a, b))


class TestEmbeddings:
    def test_dimensao_fixa_e_vetor_unitario(self):
        e = Embedder(512)
        v = e.embed_um("circuit breaker abre o circuito apos falhas")
        assert len(v) == 512
        assert math.isclose(cos(v, v), 1.0, rel_tol=1e-6)

    def test_deterministico_e_independente_de_acento_e_caixa(self):
        e = Embedder(1024)
        assert e.embed_um("Autenticação JWT") == e.embed_um("autenticacao jwt")
        assert Embedder(1024).embed_um("fila de mensagens") == e.embed_um("fila de mensagens")

    def test_texto_so_com_stopwords_nao_tem_vetor(self):
        assert Embedder(1024).embed_um("de a o que para") is None

    def test_texto_parecido_fica_mais_perto_que_texto_nao_relacionado(self):
        e = Embedder(1024)
        q, perto, longe = e.embed(["como funciona o circuit breaker", "o circuit breaker abre o circuito", "receita de bolo de cenoura"])
        assert cos(q, perto) > cos(q, longe)

    def test_lote_preserva_ordem_e_marca_vazios(self):
        r = Embedder(256).embed(["fila", "de a o", "worker"])
        assert r[0] is not None and r[1] is None and r[2] is not None


class TestModelo:
    def test_classifica_sentimento(self, modelo):
        assert modelo.prever("o atendimento foi excelente, adorei")["sentimento"] == "positivo"
        assert modelo.prever("pessimo, quero meu dinheiro de volta")["sentimento"] == "negativo"

    def test_resposta_so_tem_rotulo_e_confianca_sem_ecoar_o_texto(self, modelo):
        r = modelo.prever("TEXTO-SIGILOSO-123 muito bom")
        assert set(r) == {"sentimento", "confianca"}
        assert 0.5 <= r["confianca"] <= 1.0
        assert "SIGILOSO" not in str(r)

    def test_treina_e_salva_quando_arquivo_nao_existe_e_reaproveita_depois(self, tmp_path):
        caminho = str(tmp_path / "sub" / "modelo.joblib")
        carregar_modelo(caminho)
        assert os.path.exists(caminho)
        assert carregar_modelo(caminho).prever("adorei")["sentimento"] == "positivo"

    def test_arquivo_corrompido_e_retreinado(self, tmp_path):
        caminho = tmp_path / "modelo.joblib"
        caminho.write_bytes(b"isto nao e um joblib")
        assert carregar_modelo(str(caminho)).prever("horrivel")["sentimento"] == "negativo"

    def test_disco_somente_leitura_nao_derruba(self, tmp_path):
        arquivo = tmp_path / "arquivo"
        arquivo.write_text("x")
        # o "diretorio pai" e um arquivo: gravar falha com OSError, o modelo segue funcionando em memoria
        m = carregar_modelo(str(arquivo / "modelo.joblib"))
        assert m.prever("adorei")["sentimento"] == "positivo"

    def test_base_de_treino_embutida_equilibrada(self):
        assert len(TREINO) == 20 and sum(r for _, r in TREINO) == 10
