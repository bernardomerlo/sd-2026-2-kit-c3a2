# inferencia: Serviço de Inferência + Armazenamento Vetorial (Etapa 2)

Microsserviço **interno** (rede privada) que encapsula a IA da plataforma. É chamado pelo **gateway** (REST síncrono)
e pelo **worker** (jobs assíncronos). Fala com o banco vetorial **Qdrant** (container próprio).

```
gateway ──/predict, /vectors/upsert──▶ inferencia ──▶ Qdrant (vetores)
worker  ──/predict──────────────────▶     │
                                          ├─ modelo scikit-learn (sentimento, offline)
                                          ├─ embeddings locais (HashingVectorizer)
                                          └─ LLM (API real se houver LLM_API_KEY; senão simulado)
```

## API

| Rota | O que faz |
|---|---|
| `GET /health` | `200` só se o **modelo está carregado** e o **banco vetorial responde** (collection criada). Senão `503` com o detalhe. Usada como healthcheck do container. |
| `POST /predict` | Classifica o texto, recupera os `top_k` documentos mais relevantes e (opcional) gera uma resposta. |
| `POST /vectors/upsert` | Gera o embedding de cada documento e grava no Qdrant. |
| `POST /vectors/search` | Busca os `top_k` vetores mais similares à consulta. |

Swagger em `http://localhost:8001/docs`.

### `POST /predict`
```json
// entrada (é exatamente o que o gateway envia; top_k e use_llm são opcionais)
{"texto": "como o circuit breaker evita falhas em cascata?", "top_k": 3, "use_llm": true}

// saída
{
  "predicao":   {"sentimento": "positivo", "confianca": 0.54},
  "documentos": [{"id": "seed-circuit-breaker", "texto": "...", "score": 0.31, "metadata": {"origem": "seed"}}],
  "resposta":   "[MODO SIMULADO - sem LLM_API_KEY] Com base nos documentos recuperados: ...",
  "metadados":  {"latencia_ms": {"modelo": 1.2, "busca_vetorial": 6.8, "llm": 0.0, "total": 8.3},
                 "top_k": 3, "documentos_recuperados": 3, "contexto": "ok",
                 "llm_modo": "simulado", "colecao": "documentos_tecnicos"}
}
```
O **texto enviado não é devolvido** na resposta (o worker pode gravar o resultado no Redis; ver LGPD abaixo).
`llm_modo`: `api` · `simulado` (sem chave) · `simulado_fallback` (havia chave, mas a API falhou).

### `POST /vectors/upsert`
```json
{"documents": [{"texto": "...", "id": "meu-doc-1", "metadata": {"tema": "k8s"}}]}   // id e metadata opcionais
→ {"indexados": 1, "ids": ["meu-doc-1"], "colecao": "documentos_tecnicos", "latencia_ms": 12.3}
```
Reenviar o mesmo `id` **substitui** o documento (idempotente). Sem `id`, o serviço gera um. Até 100 documentos por chamada e 10 000 caracteres por texto.

### `POST /vectors/search`
```json
{"texto": "escalar pods", "top_k": 3, "score_threshold": 0.1}   // top_k (1..20) e score_threshold opcionais
→ {"resultados": [{"id": "...", "texto": "...", "score": 0.27, "metadata": {}}], "metadados": {...}}
```

### Erros
| Código | Quando |
|---|---|
| `422` | Entrada inválida (texto vazio/grande demais, `top_k` fora de 1..20, documento só com stopwords). O corpo traz só `loc`/`msg`/`type`, **nunca o texto enviado**. |
| `503` | Banco vetorial inacessível ou com configuração inválida (mensagem genérica, sem URL interna). |
| `500` | Erro inesperado: mensagem genérica, sem traceback. |

## Como funciona

- **Modelo scikit-learn** (`app/modelo.py`): classificador de sentimento do kit C1 (TF-IDF + regressão logística, 20 frases embutidas).
  É **treinado no build da imagem** (`RUN python -m app.modelo`) e fica empacotado em `/app/model/modelo.joblib`: não precisa de internet nem de escrita em disco para subir.
- **Embeddings** (`app/embeddings.py`): `HashingVectorizer` (1024 dimensões, 1–2-gramas, sem acento, sem stopwords PT, vetores unitários).
  Dimensão **fixa** e sem treino prévio. Com o TF-IDF da C2 a dimensão mudava a cada documento novo, o que não funciona em banco vetorial com upserts incrementais.
  Trade-off: sem ponderação IDF os scores de similaridade ficam baixos (≈0,1–0,4) e a qualidade é menor que a de embeddings neurais; para uma base técnica pequena é suficiente. Use `score_threshold` com valores baixos.
- **Banco vetorial** (`app/vectorstore.py`): Qdrant, distância cosseno. A collection é **criada no startup se não existir** e os **10 documentos técnicos fictícios** (`app/seed_docs.json`, sem dados pessoais) são carregados (idempotente: reiniciar não duplica).
  O `id` livre do cliente vira UUIDv5 no Qdrant (que só aceita inteiro/UUID); o `id` original é preservado no payload.
- **LLM** (`app/llm.py`): sem `LLM_API_KEY` → resposta **simulada** montada do contexto (100 % offline). Com chave → chama a API; se falhar, degrada para o simulado.
- **Ordem de subida não importa** (`app/prontidao.py`): se o Qdrant ainda não está de pé, o serviço sobe, responde `503` no `/health` e **tenta de novo sozinho** (até `VECTOR_DB_STARTUP_TIMEOUT_SECONDS`). Se o Qdrant reiniciar e perder a collection, ela é recriada e re-semeada na próxima chamada.
- Toda chamada ao Qdrant passa por `VectorStore`: **ponto único** para a Etapa 4 aplicar retry/circuit breaker.

## Configuração (variáveis de ambiente)

Nenhuma é obrigatória (padrões rodam offline). Veja o `.env.example` da raiz.

| Variável | Padrão | |
|---|---|---|
| `VECTOR_DB_URL` | `http://localhost:6333` | No compose: `http://qdrant:6333` |
| `VECTOR_COLLECTION` | `documentos_tecnicos` | |
| `VECTOR_DB_API_KEY` | vazio | Só para Qdrant Cloud |
| `VECTOR_DB_TIMEOUT_SECONDS` | `5` | Tempo-limite por chamada ao Qdrant |
| `VECTOR_DB_STARTUP_TIMEOUT_SECONDS` | `120` | Quanto insiste em conectar no boot |
| `SEED_ON_STARTUP` | `true` | Carrega os documentos de exemplo |
| `MODEL_PATH` | `/app/model/modelo.joblib` (imagem) | Treina e salva se não existir |
| `EMBEDDING_DIM` | `1024` | **Mudar exige outra collection** (o serviço recusa dimensão diferente, sem apagar nada) |
| `LLM_API_KEY` | vazio | Vazio = simulado |
| `LLM_API_URL` / `LLM_MODEL` / `LLM_TIMEOUT_SECONDS` | OpenAI chat-completions / `gpt-4o-mini` / `20` | |
| `LOG_LEVEL` | `INFO` | |

## Rodando

```bash
# Container + banco vetorial (a partir da raiz do repo)
cp .env.example .env
docker compose up --build qdrant inferencia
curl localhost:8001/health          # 200 quando modelo + Qdrant OK
curl -X POST localhost:8001/predict -H "Content-Type: application/json" \
     -d '{"texto":"como funciona o circuit breaker?","top_k":2}'
curl -X POST localhost:8001/vectors/search -H "Content-Type: application/json" \
     -d '{"texto":"autenticação jwt","top_k":2}'
```
> Use `qdrant inferencia` (e não `up` sem argumentos) enquanto o `worker/` ainda não tiver Dockerfile.

Local, sem Docker (precisa de um Qdrant em `localhost:6333`, ex.: `docker run -p 6333:6333 qdrant/qdrant:v1.12.4`):
```bash
cd inferencia && python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app.main:create_app --factory --port 8001 --no-access-log
```
> Cuidado: se `VECTOR_DB_URL` estiver exportada no seu terminal, o `docker compose` usa esse valor no lugar do `.env`.

## Testes

```bash
cd inferencia && pytest                                          # 75 testes, sem Docker (Qdrant em memória)
QDRANT_TEST_URL=http://localhost:6333 pytest -m integracao      # contra um Qdrant real
```
Cobrem: contrato de `/predict` (inclusive o payload exato do gateway), recuperação do documento correto, upsert idempotente, ids livres, validações,
`/health` 503 com banco fora, collection criada/semeada sem duplicar, dimensão incompatível, auto-recuperação, fallback do LLM e **ausência de dados do usuário em logs e respostas de erro**.

## Logs e LGPD

- JSON por linha: `timestamp`, `service`, `level`, `trace_id`, `message` (+ `route`, `status_code`, `latency_ms`). `trace_id` vem de `X-Trace-Id` (validado) ou é gerado.
- **Nunca** registra texto do usuário, conteúdo/ids de documentos, IP, tokens nem traceback (lista branca de campos; access log do uvicorn desligado).
- O 422 não ecoa a entrada; o `/predict` não devolve o texto enviado, então o worker pode gravar `result` sem reter dado pessoal.
- Dados retidos neste serviço: **documentos indexados e seus vetores** (no volume `qdrant_data`) até serem removidos (`docker compose down -v` apaga tudo). O seed é fictício.
- ⚠️ Com `LLM_API_KEY` definida, a pergunta e o contexto saem para o provedor do LLM; documente isso na seção LGPD do README principal.

## Para as próximas etapas

- **Worker (Etapa 3):** chame `POST /predict` com `{"texto": ...}` e grave `predicao`, `documentos`, `resposta` e `metadados`. 5xx/timeout = transitório (retentativa); 422 = erro definitivo.
- **Resiliência (Etapa 4):** o ponto de entrada do Qdrant é `app/vectorstore.py`; já mapeia falhas para `VectorDBUnavailable` (→ 503). Para testar falhas: `docker compose stop inferencia` (gateway/worker) ou `docker compose stop qdrant` (inferência → 503).
- **Observabilidade (Etapa 4):** adicione a instrumentação OpenTelemetry em `app/main.py`; o middleware de `app/observability.py` já lê `X-Trace-Id`.
- **Compose (Etapa 5):** o Qdrant tem `healthcheck`; troque o `depends_on` do `inferencia` para `service_healthy` se preferir. O `inferencia` já tem `HEALTHCHECK` no Dockerfile.
