# gateway: API Gateway (Etapa 1)


---

## 🏛️ Etapa 1 — Fundação, API Gateway e Segurança (Integrante 1)

Esta branch e commit contêm a implementação completa da **Etapa 1**, estabelecendo a porta de entrada segura, autenticada e conteinerizada da plataforma.

### 🛡️ Funcionalidades Implementadas

1. **Autenticação JWT (HS256)**:
   - `POST /auth/token`: Endpoint para geração de tokens de acesso JWT assinados com HS256 (`JWT_SECRET` e `JWT_EXPIRATION_MINUTES`).
   - Dependência `obter_usuario_autenticado`: Bloqueia chamadas sem token válido em todas as rotas de negócio `/v1/*`, retornando `401 Unauthorized` com o cabeçalho `WWW-Authenticate: Bearer`.
2. **Roteamento Síncrono e Assíncrono**:
   - `GET /health` (e `/saude` legada): Rota pública de diagnóstico de conectividade.
   - `POST /v1/infer`: Roteia chamadas síncronas para o serviço de inferência (`URL_INFERENCIA` / `INFERENCE_URL`).
   - `POST /v1/jobs`: Enfileira tarefas assíncronas na fila Redis e retorna `202 Accepted` com `job_id`.
   - `GET /v1/jobs/{job_id}`: Consulta de status (`pending`, `processing`, `completed`, `failed`) com política de retenção temporária (LGPD).
   - `POST /v1/documents`: Encaminha textos para indexação vetorial.
3. **Higiene Rigorosa de Logs (LGPD)**:
   - Formato JSON estritamente estruturado registrando apenas: `timestamp`, `service`, `method`, `path`, `status_code`, `latency_ms`, `trace_id` e `job_id`.
   - **Zero vazamento**: Nenhum payload, IP identificável ou token JWT é gravado nos logs.
4. **Containerização Segura**:
   - `gateway/Dockerfile`: Imagem multi-stage / python:3.12-slim rodando sob usuário de sistema não-root (`appuser`).

---

## 🧪 Como Testar

```bash
# 1. Copie o arquivo de variáveis de ambiente
cp .env.example .env

# 2. Execute a suite de testes unitários do Gateway
python -m pytest tests/test_gateway.py -v
```

### Exemplos com cURL

#### 1. Obter Token JWT
```bash
curl -X POST http://localhost:8000/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username": "professor_avaliador"}'
```

#### 2. Testar Bloqueio 401 Unauthorized (sem token)
```bash
curl -i -X POST http://localhost:8000/v1/infer \
  -H "Content-Type: application/json" \
  -d '{"texto": "teste"}'
```
*Retorno esperado: HTTP 401 Unauthorized*

#### 3. Enfileirar Job Assíncrono (com token)
```bash
curl -X POST http://localhost:8000/v1/jobs \
  -H "Authorization: Bearer <SEU_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"texto": "executar tarefa assincrona"}'
```
*Retorno esperado: HTTP 202 Accepted com job_id*
