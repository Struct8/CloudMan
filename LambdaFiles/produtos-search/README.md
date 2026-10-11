# Lambdas — Busca e Indexação de Produtos

Código das funções da arquitetura serverless montada no Struct8 (repo `Struct8/CloudMan`, branch `main`).

As Lambdas rodam **fora da VPC** e falam com o **Aurora Serverless v2 (PostgreSQL)** pelo **RDS Data API**, autenticando com o secret gerenciado do cluster. Elas não abrem conexão TCP nem usam RDS Proxy.

## Estrutura

```
lambda/
├── busca/          # file_path_ da aws_lambda_function "lambda-busca"
│   ├── index.mjs   # handler: GET /busca?q=...&limit=...
│   └── package.json
├── ingestao/       # file_path_ da aws_lambda_function "lambda-ingestao"
│   ├── index.mjs   # handler: consome SQS (batch) e faz upsert
│   └── package.json
└── schema.sql      # DDL do catálogo: extensões, tabela, índices
```

Cada pasta é empacotada inteira pelo Terraform no `apply` (o `file_path_` do Struct8 aponta para o diretório, e o `handler` é `index.handler`).

## Variáveis de ambiente

Injetadas automaticamente pelo Struct8 a partir da conexão `Lambda → policy → Aurora`:

- `AWS_RDS_CLUSTER_ARN_0` — ARN do cluster (resourceArn do Data API)
- `AWS_RDS_CLUSTER_SECRET_ARN_0` — ARN do secret gerenciado (secretArn do Data API)
- `AWS_RDS_CLUSTER_DB_NAME_0` — nome do banco (`produtos`)
- `AWS_RDS_CLUSTER_DATA_API_0` — `true`

Nenhum segredo fica no código.

## Busca (`busca/index.mjs`)

1. **Full-Text Search** nativo com ranking (`websearch_to_tsquery` + `ts_rank`), entende frases como "notebook i7 16gb".
2. **Fallback fuzzy** por `pg_trgm` (`similarity`) quando o FTS não retorna nada — cobre erros de digitação.

## Ingestão (`ingestao/index.mjs`)

Acionada pela fila SQS (Event Source Mapping, em batch). Faz `UPSERT` de cada produto. Com `ReportBatchItemFailures`, só as mensagens que falham voltam para a fila.

O `search_vector` (tsvector) é uma **coluna gerada** no banco — não precisa ser calculado no código.

## Antes do primeiro apply

1. Rode o `schema.sql` no Aurora uma vez (cria extensões, tabela e índices).
2. Garanta que estas pastas estão commitadas em `Struct8/CloudMan@main` — o Struct8 faz sparse checkout delas no deploy.
3. Opcional: para busca semântica, habilite `pgvector` no `schema.sql` (extensão + coluna `embedding` + índice HNSW) e gere os embeddings na ingestão.
