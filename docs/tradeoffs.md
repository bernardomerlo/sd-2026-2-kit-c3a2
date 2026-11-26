# Relatório de trade-offs: CAP, custo e latência

Este documento explica as escolhas da plataforma e o que cada uma ganha e perde.

## 1. CAP (Consistência × Disponibilidade sob Partição)

Pelo teorema CAP, quando a rede entre dois serviços falha (partição), cada parte do sistema precisa
escolher entre **responder mesmo sem a informação mais atual (A)** ou **recusar até ter certeza (C)**.
A plataforma não faz a mesma escolha em todo lugar:

| Componente | Escolha | Por quê |
|---|---|---|
| **Fila assíncrona** (`POST /v1/jobs` → Redis → worker) | **AP** | O gateway aceita o job (`202`) mesmo que a inferência esteja fora. O resultado chega depois (**consistência eventual**): o cliente pode ver `pending` por um tempo antes de `completed`. |
| **Fallback em memória do gateway** | **A acima de C** | Se o Redis cair, o gateway guarda o job na própria memória para não recusar o pedido. Ganho: continua respondendo. Perda: esse job não chega ao worker, não sobrevive a um reinício e não é visto por outras réplicas. |
| **Inferência síncrona** (`POST /v1/infer`) | **CP** | Se a inferência não responde, o gateway devolve erro (`503`/`504`) em vez de inventar uma resposta. |
| **Autenticação JWT** | **CP** | Token inválido ou expirado é sempre recusado (`401`). Como o JWT é validado só com a chave, não depende de nenhum outro serviço e continua funcionando mesmo com partição. |
| **Redis e Qdrant** | Instância única | Não há réplicas, então não existe divergência entre cópias (consistência forte), mas cada um é um **ponto único de falha**. É uma escolha consciente para caber no free tier. |

**Garantia de entrega:** o worker usa `BLMOVE` para uma fila de processamento e só remove a mensagem
depois de terminar (ack). Se ele cair no meio, a mensagem é recuperada ao reiniciar: entrega
**pelo menos uma vez** (*at-least-once*). O custo é que, em caso raro, um job pode ser processado duas
vezes; como a inferência não altera dados, reprocessar é seguro (idempotente).

## 2. Custo

| Escolha | Economia | O que se perde |
|---|---|---|
| **Railway (PaaS)** no lugar de uma VM | Crédito de teste de US$ 5 por 30 dias, sem cartão; HTTPS, domínio e deploy automático sem configurar servidor | Menos controle; recursos limitados; depois do teste passa a ser pago |
| **Só o gateway exposto** | Uma única URL e um único certificado | Nenhum, além de depender do gateway para tudo |
| **Modelo scikit-learn + embeddings locais (HashingVectorizer)** | Nenhum custo por chamada, sem GPU, imagem pequena, roda offline | Qualidade menor que modelos neurais |
| **LLM simulado por padrão** | Zero custo de API e nenhum dado enviado a terceiros | A resposta é um resumo simples dos documentos, não um texto gerado |
| **Redis como fila e como armazenamento de status** | Um serviço a menos (sem RabbitMQ/Kafka + banco separado) | Menos recursos de mensageria (sem roteamento avançado) e dados em memória |
| **TTL de 1 hora nos jobs** | Memória do Redis não cresce sem limite | O resultado some depois de 1 hora |

**Estimativa:** com tráfego de avaliação, os 5 serviços consomem poucos centavos por dia, dentro do crédito de teste.

## 3. Latência

| Caminho | Comportamento | Trade-off |
|---|---|---|
| **Síncrono** (`/v1/infer`) | Resposta na mesma requisição | Mais simples para o cliente, mas ele espera; se a inferência estiver lenta ou fora, o cliente recebe o erro (tempo-limite de 10 s no gateway) |
| **Assíncrono** (`/v1/jobs`) | `202` imediato; resultado consultado depois | Aceitar o pedido é muito rápido, mas o resultado leva mais tempo total (fila + processamento + consulta) e o cliente precisa consultar o status. Medido no deploy: cerca de **0,7 s** entre criar e concluir o job |
| **Retentativa com espera progressiva** (worker) | Até 3 tentativas: ~0,5 s, ~1 s (+ variação aleatória) | Aumenta a chance de sucesso em falhas passageiras, mas um job com falha demora alguns segundos a mais para virar `failed` |
| **Tempo-limite curto** (5 s no worker, 1 s no `/health`) | Falha rápido em vez de travar | Uma inferência legitimamente lenta pode ser cortada |
| **Modelo leve e local** | Inferência em milissegundos, sem chamada externa | Qualidade menor (ver Custo) |
| **Rede privada do Railway** | Chamadas internas sem passar pela internet | Todos os serviços precisam estar na mesma região (US East); para usuários no Brasil, cada requisição pública atravessa até os EUA |

## Resumo

A plataforma prioriza **disponibilidade** na entrada (aceita jobs mesmo com partes fora) e
**consistência** onde errar é pior (autenticação e respostas síncronas). Mantém o **custo perto de zero**
com componentes leves e instâncias únicas, aceitando pontos únicos de falha e uma IA mais simples.
A fila troca **latência total maior** por **resposta imediata e resiliência**.
