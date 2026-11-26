"""Cliente de LLM com fallback offline/simulado (reaproveitado da C2, `comum/llm.py`).

* Com LLM_API_KEY: chama a API (formato chat-completions) com tempo-limite.
* Sem LLM_API_KEY: MODO SIMULADO - monta a resposta a partir do contexto recuperado.
  O sistema funciona 100% offline.
* Com chave, mas a API falha (rede, 5xx, timeout): degrada para o modo simulado
  (`modo = "simulado_fallback"`) em vez de derrubar a inferencia.

LGPD: a resposta simulada NAO repete a pergunta do usuario. Com chave configurada, a pergunta
e o contexto sao enviados ao provedor do LLM (ver README do servico).
Retentativa/circuit breaker das chamadas entre servicos ficam na Etapa 4.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("inferencia")

MODO_API = "api"
MODO_SIMULADO = "simulado"
MODO_SIMULADO_FALLBACK = "simulado_fallback"


@dataclass(frozen=True)
class RespostaLLM:
    texto: str
    modo: str


class ClienteLLM:
    def __init__(
        self,
        api_key: str,
        api_url: str,
        modelo: str,
        timeout: float,
        http: httpx.Client | None = None,
    ):
        self.api_key = api_key
        self.api_url = api_url
        self.modelo = modelo
        self._http = http or httpx.Client(timeout=timeout)

    @property
    def modo(self) -> str:
        return MODO_API if self.api_key else MODO_SIMULADO

    def fechar(self) -> None:
        self._http.close()

    def gerar(self, pergunta: str, contexto: list[str]) -> RespostaLLM:
        if not self.api_key:
            return RespostaLLM(self._simulado(contexto), MODO_SIMULADO)
        try:
            return RespostaLLM(self._chamar_api(pergunta, contexto), MODO_API)
        except Exception as exc:  # noqa: BLE001 - qualquer falha degrada para o simulado
            # So o NOME da excecao: a mensagem pode conter trechos do prompt.
            log.warning("llm_falhou_usando_simulado", extra={"error_type": type(exc).__name__})
            return RespostaLLM(self._simulado(contexto), MODO_SIMULADO_FALLBACK)

    def _chamar_api(self, pergunta: str, contexto: list[str]) -> str:
        prompt = (
            "Responda a pergunta usando SOMENTE o contexto abaixo. "
            "Se a resposta nao estiver no contexto, diga que nao sabe.\n\n"
            "Contexto:\n" + "\n".join(contexto) + f"\n\nPergunta: {pergunta}"
        )
        r = self._http.post(
            self.api_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.modelo,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    @staticmethod
    def _simulado(contexto: list[str]) -> str:
        if not contexto:
            return "[MODO SIMULADO - sem LLM_API_KEY] Nenhum documento relevante foi recuperado para responder."
        trechos = " ".join(contexto)[:600]
        return f"[MODO SIMULADO - sem LLM_API_KEY] Com base nos documentos recuperados: {trechos}"
