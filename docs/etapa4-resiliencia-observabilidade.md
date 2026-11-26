# Etapa 4 - Resiliencia, Rate Limit e Observabilidade

Cobre os itens **2 (rate limit)**, **6 (resiliencia)** e **7 (observabilidade)** do `TAREFAS.md`.

| Item | Onde | Resumo |
|---|---|---|
| Tempo-limite | `gateway/app.py` (`chamar_inferencia`), `worker/worker.py` | Cada chamada HTTP a inferencia tem `HTTP_TIMEOUT_SECONDS`. Estourou -> `504`. |
| Retentativa com espera progressiva | `gateway/resiliencia.py` | Ate `RETRY_MAX_ATTEMPTS` tentativas, espera `base * 2^(n-1)` + jitter de ate 30%, com teto. So repete timeout, erro de rede e 5xx. **4xx nunca e repetido.** |
| Circuit breaker | `gateway/resiliencia.py` | `CB_FAILURE_THRESHOLD` falhas seguidas -> **aberto**: o gateway responde `503` + `Retry-After` sem chamar a inferencia. Apos `CB_RECOVERY_SECONDS` -> **meio-aberto**: deixa passar 1 chamada de teste (sucesso fecha, falha reabre). Estado visivel em `GET /health`. |
| Rate limit | `gateway/rate_limit.py` | `RATE_LIMIT_PER_MINUTE` requisicoes por cliente (`sub` do JWT) por minuto nas rotas `/v1/*`. Acima disso -> `429` + `Retry-After`. Contador no Redis (vale para todas as replicas); se o Redis cair, usa memoria. |
| Rastreamento distribuido | `gateway/tracing.py`, `worker/worker.py` | OpenTelemetry. O `traceparent` W3C vai **dentro da mensagem da fila**, entao o trace nao quebra: gateway -> fila -> worker -> inferencia. Spans exportados para o **Jaeger**. |

## Fluxo de um job no Jaeger (um unico trace)

```
gateway   POST /v1/jobs                (SERVER)
gateway   └─ inference.jobs publish    (PRODUCER)   traceparent -> mensagem Redis
worker       └─ worker.consume         (CONSUMER)   extrai o traceparent da mensagem
worker          └─ POST /predict       (CLIENT)     injeta traceparent + X-Trace-Id
inferencia         (log JSON com o MESMO trace_id)
```

O `trace_id` de 32 hex aparece:
- no cabecalho de resposta `X-Trace-Id` do gateway;
- nos logs JSON do gateway, do worker e da inferencia (correlacao de logs);
- no Jaeger (busca pelo Trace ID).

## Como testar

```bash
cp .env.example .env
docker compose up --build -d

# token
TOKEN=$(curl -s -X POST localhost:8000/auth/token -H 'Content-Type: application/json' \
  -d '{"username":"professor"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# 1) Rastreamento: cria um job e guarda o trace id
curl -si -X POST localhost:8000/v1/jobs -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"texto":"o produto chegou otimo"}' | grep -i x-trace-id
#    Abra http://localhost:16686 -> Service "gateway" -> Find Traces -> o trace mostra gateway + worker
docker compose logs worker | grep <trace_id>       # mesmo id no log do worker
docker compose logs inferencia | grep <trace_id>   # e no da inferencia

# 2) Rate limit (padrao 60/min; para ver rapido use RATE_LIMIT_PER_MINUTE=5 no .env)
for i in $(seq 1 70); do curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/v1/infer \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"texto":"oi"}'; done | sort | uniq -c
#    -> 60x 200 e 10x 429

# 3) Retry + circuit breaker: derruba a inferencia
docker compose stop inferencia
curl -s -X POST localhost:8000/v1/infer -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"texto":"oi"}'      # 503 (apos 3 tentativas, ver log "retry_inferencia")
#    repita 2x -> "circuit breaker aberto" (resposta imediata, sem tentar)
curl -s localhost:8000/health        # inference_circuit_breaker.state = "open"
docker compose start inferencia      # apos CB_RECOVERY_SECONDS (30 s) o circuito fecha sozinho
```

Testes automaticos (nao precisam de Docker):

```bash
pip install -r gateway/requirements.txt pytest
python -m pytest tests/ -v
```

## Decisoes e trade-offs

- **Breaker em memoria (por replica)**: simples e sem dependencia extra. Com varias replicas do
  gateway cada uma abre o seu circuito separadamente - aceitavel, porque cada uma so precisa
  se proteger.
- **Rate limit em janela fixa**: permite ate 2x o limite na virada do minuto; em troca e 1 `INCR`
  no Redis por requisicao (barato). Token bucket seria mais preciso, mas mais complexo.
- **Retry so no que e idempotente**: `/predict` nao altera estado e `/vectors/upsert` recebe os
  ids ja definidos pelo gateway, entao repetir nao duplica documentos.
- **Pior caso de latencia** com os padroes: 3 x `HTTP_TIMEOUT_SECONDS` (10 s) + esperas (~0,6 s).
  Para respostas mais rapidas em falha, reduza `HTTP_TIMEOUT_SECONDS` ou `RETRY_MAX_ATTEMPTS`.
- **Jaeger em memoria**: guarda no maximo 5000 traces e perde tudo ao reiniciar (bom para LGPD
  e para o free tier). Na nuvem, se faltar RAM, deixe `OTEL_EXPORTER_OTLP_ENDPOINT=` vazio:
  os trace ids continuam nos logs, so nao sao exportados.

## LGPD

Spans e eventos levam somente metodo, rota (template, sem ids), status, nome da fila e `job_id`.
Nunca o texto do usuario, token, IP ou cabecalhos. A chave do rate limit e um hash SHA-256
truncado do `sub` e expira em 2 minutos.
