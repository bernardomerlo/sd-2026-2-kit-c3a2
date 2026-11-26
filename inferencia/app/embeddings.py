"""Embeddings locais e deterministicos (sem baixar modelo): HashingVectorizer do scikit-learn.

Por que HashingVectorizer e nao TfidfVectorizer (usado na C2)?
  * dimensao FIXA (EMBEDDING_DIM): a collection do banco vetorial tem tamanho de vetor
    definido na criacao. Com TF-IDF a dimensao seria o tamanho do vocabulario e mudaria a
    cada documento novo, obrigando a reindexar tudo;
  * sem estado: nao precisa de `fit`, entao qualquer replica gera o mesmo vetor para o
    mesmo texto;
  * leve: roda no free tier sem GBs de modelo.
Trade-off: sem ponderacao IDF a qualidade e menor que a de embeddings neurais; para uma
base tecnica pequena e perguntas com vocabulario parecido funciona bem.
"""
from __future__ import annotations

from sklearn.feature_extraction.text import HashingVectorizer

# Stopwords do portugues SEM acento (o vetorizador remove acentos antes de comparar).
STOPWORDS_PT = frozenset(
    """a o as os um uma uns umas de da do das dos em no na nos nas ao aos por para com sem
    sob sobre entre ate e ou mas que se como qual quais quando onde porque pois ja nao sim
    ser sao foi eh esta estao esse essa isso este esta isto aquele aquela aquilo ele ela
    eles elas seu sua seus suas meu minha mais menos muito muita tambem so tem ter""".split()
)


class Embedder:
    def __init__(self, dim: int):
        self.dim = dim
        self._vec = HashingVectorizer(
            n_features=dim,
            alternate_sign=False,  # sem sinal alternado: similaridade de cosseno >= 0
            norm="l2",  # vetores unitarios: cosseno == produto escalar
            ngram_range=(1, 2),
            strip_accents="unicode",
            lowercase=True,
            stop_words=sorted(STOPWORDS_PT),
        )

    def embed(self, textos: list[str]) -> list[list[float] | None]:
        """Um vetor por texto; `None` se o texto nao tiver nenhum termo significativo
        (ex.: so stopwords), pois um vetor nulo nao pode ser comparado por cosseno."""
        matriz = self._vec.transform(textos)
        saida: list[list[float] | None] = []
        for i in range(matriz.shape[0]):
            linha = matriz.getrow(i)
            if linha.nnz == 0:
                saida.append(None)
            else:
                saida.append(linha.toarray()[0].tolist())
        return saida

    def embed_um(self, texto: str) -> list[float] | None:
        return self.embed([texto])[0]
