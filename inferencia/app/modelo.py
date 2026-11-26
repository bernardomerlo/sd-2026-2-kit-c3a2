"""Modelo de IA scikit-learn: classificador de sentimento (positivo/negativo) em portugues.

Extraido do kit C1.A2 (`app/modelo.py`). TF-IDF + regressao logistica, treinado com uma
mini-base embutida. 100% offline: nao baixa nada da internet.

Diferencas em relacao ao kit C1:
  * o caminho do arquivo vem de MODEL_PATH (nao fica ao lado do codigo);
  * `prever()` NAO devolve o texto de entrada (LGPD: o resultado pode ser gravado em
    fila/cache pelo worker, e nao deve carregar o texto do usuario);
  * se o arquivo nao existir, treina em memoria e tenta salvar sem quebrar se o disco
    for somente-leitura.

Empacotamento: o Dockerfile executa `python -m app.modelo` NO BUILD, entao a imagem ja
nasce com o modelo treinado e nao depende de nada externo para subir.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline, make_pipeline

from app.config import Settings

log = logging.getLogger("inferencia")

# Mini base de treino embutida (do kit C1.A2).
TREINO = [
    ("o atendimento foi excelente e muito rapido", 1),
    ("adorei o produto, recomendo demais", 1),
    ("entrega pontual e embalagem perfeita", 1),
    ("otima qualidade pelo preco cobrado", 1),
    ("funcionou exatamente como prometido", 1),
    ("equipe super atenciosa, resolveram tudo", 1),
    ("melhor compra que fiz esse ano", 1),
    ("chegou antes do prazo, muito bom", 1),
    ("produto maravilhoso, superou expectativas", 1),
    ("simples de usar e muito eficiente", 1),
    ("pessimo atendimento, ninguem resolve nada", 0),
    ("produto quebrou no primeiro dia de uso", 0),
    ("entrega atrasou muito e ninguem avisou", 0),
    ("caro demais pelo que entrega", 0),
    ("nao funcionou como anunciado, decepcionante", 0),
    ("horrivel, quero meu dinheiro de volta", 0),
    ("suporte nunca responde, abandonado", 0),
    ("veio faltando peca e a embalagem rasgada", 0),
    ("qualidade muito ruim, nao recomendo", 0),
    ("perda de tempo e de dinheiro", 0),
]


class ModeloSentimento:
    """Envolve o pipeline treinado. Use apenas `prever()`."""

    def __init__(self, pipeline: Pipeline):
        self._pipeline = pipeline

    def prever(self, texto: str) -> dict:
        proba = self._pipeline.predict_proba([texto])[0]
        idx = int(proba.argmax())
        return {
            "sentimento": "positivo" if idx == 1 else "negativo",
            "confianca": round(float(proba[idx]), 4),
        }


def _treinar() -> Pipeline:
    textos = [t for t, _ in TREINO]
    rotulos = [r for _, r in TREINO]
    pipe = make_pipeline(
        TfidfVectorizer(ngram_range=(1, 2), min_df=1),
        LogisticRegression(max_iter=1000),
    )
    pipe.fit(textos, rotulos)
    return pipe


def treinar_e_salvar(caminho: str) -> Pipeline:
    pipe = _treinar()
    destino = Path(caminho)
    destino.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipe, destino)
    return pipe


def carregar_modelo(caminho: str) -> ModeloSentimento:
    """Carrega do disco; se nao existir (ou estiver ilegivel), treina e tenta salvar."""
    if os.path.exists(caminho):
        try:
            return ModeloSentimento(joblib.load(caminho))
        except Exception:  # noqa: BLE001 - arquivo corrompido/versao incompativel: retreina
            log.warning("modelo_ilegivel_retreinando")
    pipe = _treinar()
    try:
        Path(caminho).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(pipe, caminho)
    except OSError:
        log.warning("modelo_nao_salvo_em_disco")
    return ModeloSentimento(pipe)


if __name__ == "__main__":
    # Chamado no build da imagem: python -m app.modelo
    caminho = Settings.from_env().model_path
    treinar_e_salvar(caminho)
    m = carregar_modelo(caminho)
    print(f"modelo salvo em {caminho}")
    for frase in ["gostei muito do servico", "foi horrivel, nao volto mais"]:
        print(m.prever(frase))
