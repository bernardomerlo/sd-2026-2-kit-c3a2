"""
Resiliencia nas chamadas do gateway para o servico de inferencia (Etapa 4 / Aula 11).

Tres mecanismos, todos configuraveis por variavel de ambiente:
- Tempo-limite: cada tentativa usa HTTP_TIMEOUT_SECONDS (aplicado no httpx pelo chamador).
- Retentativa com espera progressiva: backoff exponencial + jitter, SO para falhas
  transitorias (timeout, erro de rede, HTTP 5xx). Erro 4xx nunca e repetido.
- Circuit breaker: depois de N falhas seguidas o circuito ABRE e o gateway responde 503
  na hora, sem martelar um servico que ja esta caido. Depois de CB_RECOVERY_SECONDS
  ele fica MEIO-ABERTO e deixa passar uma chamada de teste: sucesso fecha, falha reabre.

Este modulo nao depende de FastAPI nem de httpx (facil de testar isoladamente).
"""
from __future__ import annotations

import asyncio
import random
import threading
import time
from typing import Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")

FECHADO = "closed"
ABERTO = "open"
MEIO_ABERTO = "half_open"


class CircuitoAbertoError(Exception):
    """O circuito esta aberto: a chamada nem foi feita (fail fast)."""

    def __init__(self, nome: str, retry_after: float):
        super().__init__(f"circuito '{nome}' aberto")
        self.nome = nome
        self.retry_after = max(1, int(round(retry_after)))


class FalhaTransitoriaError(Exception):
    """Falha que vale a pena repetir (5xx, timeout, rede). Guarda a ultima causa."""

    def __init__(self, causa: Optional[BaseException] = None, resultado=None):
        if causa is not None:
            motivo = type(causa).__name__
        elif getattr(resultado, "status_code", None) is not None:
            motivo = f"HTTP {resultado.status_code}"
        else:
            motivo = "falha_transitoria"
        super().__init__(motivo)
        self.causa = causa
        self.resultado = resultado  # ex.: a ultima resposta HTTP 5xx


class CircuitBreaker:
    def __init__(
        self,
        nome: str,
        limite_falhas: int = 5,
        tempo_recuperacao: float = 30.0,
        relogio: Callable[[], float] = time.monotonic,
    ):
        self.nome = nome
        self.limite_falhas = max(1, limite_falhas)
        self.tempo_recuperacao = tempo_recuperacao
        self._relogio = relogio
        self._lock = threading.Lock()
        self._estado = FECHADO
        self._falhas_seguidas = 0
        self._aberto_em = 0.0
        self._teste_em_andamento = False

    # ------------------------------------------------------------------ estado
    @property
    def estado(self) -> str:
        with self._lock:
            return self._estado_atual()

    def _estado_atual(self) -> str:
        if self._estado == ABERTO and self._relogio() - self._aberto_em >= self.tempo_recuperacao:
            self._estado = MEIO_ABERTO
            self._teste_em_andamento = False
        return self._estado

    def resumo(self) -> dict:
        with self._lock:
            return {
                "state": self._estado_atual(),
                "consecutive_failures": self._falhas_seguidas,
            }

    # ---------------------------------------------------------------- controle
    def antes_da_chamada(self) -> None:
        """Lanca CircuitoAbertoError se a chamada nao deve acontecer."""
        with self._lock:
            estado = self._estado_atual()
            if estado == ABERTO:
                restante = self.tempo_recuperacao - (self._relogio() - self._aberto_em)
                raise CircuitoAbertoError(self.nome, restante)
            if estado == MEIO_ABERTO:
                if self._teste_em_andamento:  # so UMA chamada de teste por vez
                    raise CircuitoAbertoError(self.nome, 1)
                self._teste_em_andamento = True

    def registrar_sucesso(self) -> None:
        with self._lock:
            self._estado = FECHADO
            self._falhas_seguidas = 0
            self._teste_em_andamento = False

    def registrar_falha(self) -> None:
        with self._lock:
            self._falhas_seguidas += 1
            if self._estado == MEIO_ABERTO or self._falhas_seguidas >= self.limite_falhas:
                self._estado = ABERTO
                self._aberto_em = self._relogio()
            self._teste_em_andamento = False

    def liberar_teste(self) -> None:
        """Chamada de teste terminou sem dizer nada sobre a saude (ex.: erro 4xx)."""
        with self._lock:
            self._teste_em_andamento = False


def calcular_espera(tentativa: int, base: float, maximo: float) -> float:
    """Backoff exponencial com jitter: base*2^(n-1) (limitado a `maximo`) + ate 30% aleatorio."""
    espera = min(base * (2 ** (tentativa - 1)), maximo)
    return espera + random.uniform(0, espera * 0.3)


async def executar_com_resiliencia(
    operacao: Callable[[], Awaitable[T]],
    breaker: CircuitBreaker,
    tentativas: int = 3,
    espera_base: float = 0.2,
    espera_maxima: float = 2.0,
    dormir: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ao_repetir: Optional[Callable[[int, float, BaseException], None]] = None,
) -> T:
    """
    Executa `operacao` respeitando o circuit breaker e repetindo falhas transitorias.

    `operacao` deve lancar FalhaTransitoriaError para o que vale repetir. Qualquer outra
    excecao (ex.: 4xx) sobe direto, sem retry e sem contar como falha do servico.
    """
    tentativas = max(1, tentativas)
    ultima: Optional[FalhaTransitoriaError] = None
    for tentativa in range(1, tentativas + 1):
        breaker.antes_da_chamada()
        try:
            resultado = await operacao()
        except FalhaTransitoriaError as exc:
            breaker.registrar_falha()
            ultima = exc
            if tentativa < tentativas:
                espera = calcular_espera(tentativa, espera_base, espera_maxima)
                if ao_repetir:
                    ao_repetir(tentativa, espera, exc)
                await dormir(espera)
            continue
        except BaseException:
            breaker.liberar_teste()
            raise
        breaker.registrar_sucesso()
        return resultado
    assert ultima is not None
    raise ultima
