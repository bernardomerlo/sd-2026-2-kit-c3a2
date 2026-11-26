"""
Rate limit do API Gateway: limita requisicoes por cliente por minuto (Aula 8).

Algoritmo: janela fixa de 60 segundos.
  * Cada cliente tem um contador por minuto. Passou de RATE_LIMIT_PER_MINUTE -> 429 Too Many Requests.
  * Com Redis: o contador fica no Redis (INCR + EXPIRE), entao vale para TODAS as replicas do gateway.
  * Sem Redis: contador em memoria (fallback), valido so para esta replica.

Quem e o "cliente":
  * Com token JWT valido -> o usuario do token (campo "sub").
  * Sem token (ex.: /auth/token) -> hash do IP de origem.

LGPD: o IP nunca e gravado em claro. Vira um hash SHA-256 (com o segredo do JWT como "sal"),
fica no Redis por no maximo 60 segundos e nunca aparece nos logs.
"""
import hashlib
import time
from typing import Callable, Dict, Optional, Tuple

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse

from gateway.config import JWT_ALGORITHM, JWT_SECRET, RATE_LIMIT_PER_MINUTE

JANELA_SEGUNDOS = 60
# Rotas protegidas pelo limite (health e docs ficam livres para monitoramento)
PREFIXOS_LIMITADOS = ("/v1/", "/auth/token")

_contadores_memoria: Dict[str, Tuple[int, int]] = {}


def _identificar_cliente(request: Request) -> str:
    """Retorna um identificador do cliente sem expor dados pessoais."""
    autorizacao = request.headers.get("Authorization", "")
    if autorizacao.lower().startswith("bearer "):
        try:
            payload = jwt.decode(autorizacao[7:], JWT_SECRET, algorithms=[JWT_ALGORITHM])
            if payload.get("sub"):
                return f"user:{payload['sub']}"
        except jwt.InvalidTokenError:
            pass  # token invalido: cai no IP (a rota vai responder 401 depois)

    # Atras do proxy do Railway/Caddy, o IP real vem no X-Forwarded-For
    encaminhado = request.headers.get("X-Forwarded-For", "")
    ip = encaminhado.split(",")[0].strip() if encaminhado else (request.client.host if request.client else "desconhecido")
    return "ip:" + hashlib.sha256(f"{JWT_SECRET}:{ip}".encode()).hexdigest()[:32]


def _contar(chave: str, janela: int, obter_redis: Callable) -> int:
    """Incrementa o contador do cliente na janela atual e devolve o total."""
    r = obter_redis()
    if r is not None:
        try:
            chave_redis = f"ratelimit:{chave}:{janela}"
            total = r.incr(chave_redis)
            if total == 1:
                r.expire(chave_redis, JANELA_SEGUNDOS)
            return int(total)
        except Exception:
            pass  # Redis falhou no meio: usa a memoria

    janela_salva, total = _contadores_memoria.get(chave, (janela, 0))
    if janela_salva != janela:
        total = 0
    total += 1
    _contadores_memoria[chave] = (janela, total)
    if len(_contadores_memoria) > 10_000:  # evita crescer sem limite
        _contadores_memoria.clear()
    return total


def aplicar_rate_limit(request: Request, obter_redis: Callable) -> Tuple[Optional[JSONResponse], Dict[str, str]]:
    """
    Retorna (resposta_429_ou_None, cabecalhos_informativos).
    Se a resposta nao for None, a requisicao deve ser bloqueada.
    """
    if RATE_LIMIT_PER_MINUTE <= 0 or not request.url.path.startswith(PREFIXOS_LIMITADOS):
        return None, {}

    agora = int(time.time())
    janela = agora // JANELA_SEGUNDOS
    total = _contar(_identificar_cliente(request), janela, obter_redis)

    restante = max(RATE_LIMIT_PER_MINUTE - total, 0)
    reset = JANELA_SEGUNDOS - (agora % JANELA_SEGUNDOS)
    cabecalhos = {
        "X-RateLimit-Limit": str(RATE_LIMIT_PER_MINUTE),
        "X-RateLimit-Remaining": str(restante),
        "X-RateLimit-Reset": str(reset),
    }

    if total > RATE_LIMIT_PER_MINUTE:
        cabecalhos["Retry-After"] = str(reset)
        resposta = JSONResponse(
            status_code=429,
            content={"detail": f"Limite de {RATE_LIMIT_PER_MINUTE} requisições por minuto excedido. Tente novamente em {reset} s."},
            headers=cabecalhos,
        )
        return resposta, cabecalhos

    return None, cabecalhos
