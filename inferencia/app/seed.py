"""Documentos tecnicos ficticios (sem dados pessoais) carregados no startup."""
from __future__ import annotations

import json
from pathlib import Path

CAMINHO_SEED = Path(__file__).resolve().parent / "seed_docs.json"


def carregar_seed(caminho: Path = CAMINHO_SEED) -> list[dict]:
    with open(caminho, encoding="utf-8") as f:
        return json.load(f)
