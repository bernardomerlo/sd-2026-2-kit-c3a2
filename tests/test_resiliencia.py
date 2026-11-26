"""Etapa 4 - testes unitarios de resiliencia e rate limit (sem rede, sem Docker)."""
import asyncio

import pytest

from gateway.rate_limit import LimitadorTaxa, chave_cliente
from gateway.resiliencia import (
    ABERTO,
    FECHADO,
    MEIO_ABERTO,
    CircuitBreaker,
    CircuitoAbertoError,
    FalhaTransitoriaError,
    calcular_espera,
    executar_com_resiliencia,
)


class Relogio:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


async def _sem_espera(_segundos):
    return None


def rodar(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------- retry
def test_retry_recupera_de_falha_transitoria():
    breaker = CircuitBreaker("t", limite_falhas=10)
    chamadas = []

    async def op():
        chamadas.append(1)
        if len(chamadas) < 3:
            raise FalhaTransitoriaError()
        return "ok"

    assert rodar(executar_com_resiliencia(op, breaker, tentativas=3, dormir=_sem_espera)) == "ok"
    assert len(chamadas) == 3
    assert breaker.estado == FECHADO  # sucesso zera as falhas


def test_retry_desiste_apos_maximo_de_tentativas():
    breaker = CircuitBreaker("t", limite_falhas=10)
    chamadas = []

    async def op():
        chamadas.append(1)
        raise FalhaTransitoriaError()

    with pytest.raises(FalhaTransitoriaError):
        rodar(executar_com_resiliencia(op, breaker, tentativas=3, dormir=_sem_espera))
    assert len(chamadas) == 3


def test_erro_4xx_nao_e_repetido_nem_conta_no_breaker():
    breaker = CircuitBreaker("t", limite_falhas=1)
    chamadas = []

    async def op():
        chamadas.append(1)
        raise ValueError("422")

    with pytest.raises(ValueError):
        rodar(executar_com_resiliencia(op, breaker, tentativas=3, dormir=_sem_espera))
    assert len(chamadas) == 1
    assert breaker.estado == FECHADO


def test_espera_progressiva_com_teto():
    esperas = [calcular_espera(n, base=0.2, maximo=2.0) for n in range(1, 7)]
    assert 0.2 <= esperas[0] <= 0.26
    assert 0.4 <= esperas[1] <= 0.52
    assert 0.8 <= esperas[2] <= 1.04
    assert all(e <= 2.6 for e in esperas)  # teto 2.0 + 30% de jitter


# --------------------------------------------------------- circuit breaker
def test_breaker_abre_apos_limite_e_falha_rapido():
    relogio = Relogio()
    breaker = CircuitBreaker("inferencia", limite_falhas=3, tempo_recuperacao=30, relogio=relogio)
    chamadas = []

    async def op():
        chamadas.append(1)
        raise FalhaTransitoriaError()

    with pytest.raises(CircuitoAbertoError) as erro:
        rodar(executar_com_resiliencia(op, breaker, tentativas=5, dormir=_sem_espera))
    assert len(chamadas) == 3          # parou de chamar ao abrir
    assert breaker.estado == ABERTO
    assert erro.value.retry_after == 30

    # Proxima chamada nem chega no servico
    with pytest.raises(CircuitoAbertoError):
        rodar(executar_com_resiliencia(op, breaker, tentativas=1, dormir=_sem_espera))
    assert len(chamadas) == 3


def test_breaker_meio_aberto_fecha_com_sucesso():
    relogio = Relogio()
    breaker = CircuitBreaker("t", limite_falhas=1, tempo_recuperacao=30, relogio=relogio)
    breaker.antes_da_chamada()
    breaker.registrar_falha()
    assert breaker.estado == ABERTO

    relogio.t += 31
    assert breaker.estado == MEIO_ABERTO

    async def ok():
        return 42

    assert rodar(executar_com_resiliencia(ok, breaker, tentativas=1)) == 42
    assert breaker.estado == FECHADO


def test_breaker_meio_aberto_reabre_com_falha_e_deixa_so_um_teste():
    relogio = Relogio()
    breaker = CircuitBreaker("t", limite_falhas=1, tempo_recuperacao=10, relogio=relogio)
    breaker.antes_da_chamada()
    breaker.registrar_falha()
    relogio.t += 11

    breaker.antes_da_chamada()            # primeira chamada de teste passa
    with pytest.raises(CircuitoAbertoError):
        breaker.antes_da_chamada()        # segunda simultanea e barrada
    breaker.registrar_falha()
    assert breaker.estado == ABERTO


# ---------------------------------------------------------------- rate limit
def test_rate_limit_bloqueia_acima_do_limite_e_reseta_na_janela():
    relogio = Relogio(t=600.0)  # inicio de uma janela de 60 s
    limitador = LimitadorTaxa(3, 60, relogio=relogio)
    resultados = [limitador.verificar("aluno") for _ in range(4)]
    assert [r.permitido for r in resultados] == [True, True, True, False]
    assert resultados[2].restantes == 0

    assert limitador.verificar("outro").permitido  # limite e por cliente

    relogio.t += 60
    assert limitador.verificar("aluno").permitido


def test_rate_limit_zero_desliga():
    limitador = LimitadorTaxa(0, 60)
    assert all(limitador.verificar("x").permitido for _ in range(100))


def test_rate_limit_usa_redis_quando_disponivel():
    class RedisFalso:
        def __init__(self):
            self.dados = {}
            self.ttl = {}

        def pipeline(self):
            return self

        def incr(self, k):
            self._k = k
            return self

        def expire(self, k, s):
            self.ttl[k] = s
            return self

        def execute(self):
            self.dados[self._k] = self.dados.get(self._k, 0) + 1
            return [self.dados[self._k], True]

    falso = RedisFalso()
    limitador = LimitadorTaxa(2, 60, obter_redis=lambda: falso, relogio=Relogio(600.0))
    assert [limitador.verificar("a").permitido for _ in range(3)] == [True, True, False]
    chave = next(iter(falso.dados))
    assert "a" not in chave.split(":")            # identificador nunca vai puro (LGPD)
    assert chave_cliente("a") in chave
    assert falso.ttl[chave] == 120                 # contador expira sozinho


def test_rate_limit_cai_para_memoria_se_redis_falhar():
    class RedisQuebrado:
        def pipeline(self):
            raise ConnectionError("redis fora")

    limitador = LimitadorTaxa(1, 60, obter_redis=lambda: RedisQuebrado(), relogio=Relogio(600.0))
    assert limitador.verificar("a").permitido
    assert not limitador.verificar("a").permitido
