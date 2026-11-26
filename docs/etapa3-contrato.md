# Etapa 3 - Mensageria e Worker (Redis)

Escolha: **Redis** (fila + status dos jobs com TTL na mesma instancia; metade da RAM no free tier).

## Contrato (Gateway <-> Worker)
| Item | Valor |
|---|---|
| Fila | lista Redis `inference.jobs` (gateway faz `RPUSH`) |
| Em processamento | `inference.jobs.processing` (worker usa `BLMOVE`; ack = `LREM`) |
| DLQ | `inference.dlq` (somente metadados, sem payload) |
| Status | hash `job:{job_id}` com campos `status` (pending/processing/completed/failed), `result` (JSON string), `error`, `updated_at` |
| TTL | `JOB_TTL_SECONDS` (3600) aplicado em `job:{job_id}` |

Mensagem na fila (JSON):
```json
{"job_id": "<uuid>", "payload": {"text": "...", "top_k": 3}, "trace_context": {"traceparent": "00-..."}}
```

## Codigo do Gateway (Integrante 1) - POST /v1/jobs
```python
import json, uuid, redis
from opentelemetry import propagate
r = redis.Redis.from_url(BROKER_URL, decode_responses=True)

def enqueue(payload: dict) -> str:
    job_id = str(uuid.uuid4())
    carrier = {}
    propagate.inject(carrier)          # Etapa 4: trace nao quebra na fila
    r.hset(f"job:{job_id}", mapping={"status": "pending"})
    r.expire(f"job:{job_id}", JOB_TTL_SECONDS)
    r.rpush("inference.jobs", json.dumps(
        {"job_id": job_id, "payload": payload, "trace_context": carrier}))
    return job_id                      # responder 202 {"job_id": ...}

def get_job(job_id: str):
    d = r.hgetall(f"job:{job_id}")     # vazio => 404 (expirou/nao existe)
    if d.get("result"): d["result"] = json.loads(d["result"])
    return d
```

## Variaveis do worker
`BROKER_URL`, `INFERENCE_URL`, `JOB_TTL_SECONDS` (obrigatorias no edital) + opcionais
`QUEUE_NAME`, `DLQ_NAME`, `MAX_ATTEMPTS` (3), `HTTP_TIMEOUT_SECONDS` (5), `RECOVER_ON_START`.

## Servicos para o docker-compose (Integrante 5)
```yaml
  broker:
    image: redis:7-alpine
    healthcheck: {test: ["CMD","redis-cli","ping"], interval: 5s, timeout: 3s, retries: 5}
  worker:
    build: ./worker
    environment:
      BROKER_URL: ${BROKER_URL:-redis://broker:6379/0}
      INFERENCE_URL: ${INFERENCE_URL:-http://inference:8001}
      JOB_TTL_SECONDS: ${JOB_TTL_SECONDS:-3600}
    depends_on:
      broker: {condition: service_healthy}
      inference: {condition: service_healthy}
    stop_grace_period: 30s
```

## Como testar (criterio de pronto)
1. `docker compose -f docker-compose.etapa3.yml up --build -d`
2. `pip install redis && python tools/publish_test.py` -> status vai de pending a completed
3. Falha: `docker compose -f docker-compose.etapa3.yml stop inference`, rode o teste de novo -> `failed` + item em `inference.dlq` (`docker compose -f docker-compose.etapa3.yml exec broker redis-cli lrange inference.dlq 0 -1`)
4. Shutdown: `docker compose -f docker-compose.etapa3.yml stop worker` -> log "worker encerrado"
5. TTL: o teste imprime o TTL restante (~3600)

## LGPD
Worker nao loga payload; DLQ guarda so job_id/motivo; status e resultado expiram em 1h.
