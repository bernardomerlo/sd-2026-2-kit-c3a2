"""
Rate limit do gateway (Aula 8): no maximo RATE_LIMIT_PER_MINUTE requisicoes por cliente
por minuto nas rotas /v1/*.

- Algoritmo: janela fixa de 60 s (contador por cliente e por minuto).
- Onde fica o contador: no Redis (INCR + EXPIRE), entao o limite vale para TODAS as
  replicas do gateway. Se o Redis cair, usa um contador em memoria (limite por replica)
  em vez de derrubar a API.
- Quem e o "cliente": o `sub` do JWT. A chave guardada e um hash SHA-256 truncado,
  nunca o identificador puro (LGPD: minimizacao), e expira sozinha em ~2 minutos.
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple


@dataclass
class ResultadoLimite:
    permitido: bool
    limite: int
    restantes: int
    reset_em: int  # segundos ate a janela virar


def chave_cliente(identificador: str) -> str:
    return hashlib.sha256(identificador.encode("utf-8")).hexdigest()[:16]


class LimitadorTaxa:
    def __init__(
        self,
        limite_por_janela: int,
        janela_segundos: int = 60,
        obter_redis: Callable[[], object] = lambda: None,
        relogio: Callable[[], float] = time.time,
        prefixo: str = "ratelimit",
    ):
        self.limite = limite_por_janela
        self.janela = janela_segundos
        self._obter_redis = obter_redis
        self._relogio = relogio
        self._prefixo = prefixo
        self._memoria: Dict[Tuple[str, int], int] = {}
        self._lock = threading.Lock()

    def _contar_redis(self, r, chave: str) -> Optional[int]:
        try:
            pipe = r.pipeline()
            pipe.incr(chave)
            pipe.expire(chave, self.janela * 2)
            contagem, _ = pipe.execute()
            return int(contagem)
        except Exception:
            return None  # cai para memoria

    def _contar_memoria(self, cliente: str, janela_atual: int) -> int:
        with self._lock:
            # limpa janelas antigas para a memoria nao crescer
            for k in [k for k in self._memoria if k[1] < janela_atual]:
                del self._memoria[k]
            k = (cliente, janela_atual)
            self._memoria[k] = self._memoria.get(k, 0) + 1
            return self._memoria[k]

    def verificar(self, identificador: str) -> ResultadoLimite:
        agora = self._relogio()
        janela_atual = int(agora // self.janela)
        reset_em = max(1, int(self.janela - (agora % self.janela)))
        if self.limite <= 0:  # 0 desliga o rate limit
            return ResultadoLimite(True, 0, 0, reset_em)

        cliente = chave_cliente(identificador)
        contagem = None
        r = self._obter_redis()
        if r is not None:
            contagem = self._contar_redis(r, f"{self._prefixo}:{cliente}:{janela_atual}")
        if contagem is None:
            contagem = self._contar_memoria(cliente, janela_atual)

        return ResultadoLimite(
            permitido=contagem <= self.limite,
            limite=self.limite,
            restantes=max(0, self.limite - contagem),
            reset_em=reset_em,
        )