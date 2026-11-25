"""
Autenticacao e Seguranca JWT (HS256).
Protege rotas de negocio com Bearer Token e valida expiracao/assinatura.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gateway.config import JWT_ALGORITHM, JWT_EXPIRATION_MINUTES, JWT_SECRET

security_scheme = HTTPBearer(auto_error=False)


def criar_token_acesso(
    subject: str,
    dados_extras: Optional[dict] = None,
    minutos_expiracao: Optional[int] = None,
) -> str:
    """
    Gera token JWT assinado digitalmente com algoritmo HS256 e tempo de expiracao configuravel.
    """
    agora = datetime.now(timezone.utc)
    duracao = timedelta(minutes=minutos_expiracao or JWT_EXPIRATION_MINUTES)
    expiracao = agora + duracao

    payload = {
        "sub": str(subject),
        "iat": int(agora.timestamp()),
        "exp": int(expiracao.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    if dados_extras:
        payload.update(dados_extras)

    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verificar_token_acesso(token: str) -> dict:
    """
    Valida assinatura e expiracao do token JWT.
    Retorna o payload ou lanca HTTPException 401 Unauthorized.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expirado",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token", error_description="The token expired"'},
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token ausente ou inválido",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        )


async def obter_usuario_autenticado(
    credenciais: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
) -> dict:
    """
    Dependencia FastAPI que exige o header 'Authorization: Bearer <token>' nas rotas protegidas.
    Retorna 401 Unauthorized para tokens ausentes, invalidos ou expirados.
    """
    if credenciais is None or not credenciais.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token de autorização ausente",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return verificar_token_acesso(credenciais.credentials)
