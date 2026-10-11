-- Schema do catalogo de produtos de informatica.
-- Rode uma vez no Aurora (ex.: via Data API no console RDS, ou um migration runner).
-- O Postgres aqui e a fonte da verdade E o motor de busca.

-- Extensoes de busca -------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fuzzy / tolerancia a erro de digitacao
-- CREATE EXTENSION IF NOT EXISTS vector; -- descomente para busca semantica (pgvector)

-- Tabela de produtos -------------------------------------------------------
CREATE TABLE IF NOT EXISTS produtos (
  id            text PRIMARY KEY,
  nome          text NOT NULL,
  marca         text,
  categoria     text,
  descricao     text,
  preco         numeric(12,2),
  specs         jsonb NOT NULL DEFAULT '{}'::jsonb,
  criado_em     timestamptz NOT NULL DEFAULT now(),
  atualizado_em timestamptz NOT NULL DEFAULT now(),

  -- Coluna gerada: tsvector ponderado (nome pesa mais que descricao).
  -- Mantida pelo proprio Postgres a cada INSERT/UPDATE, sem trigger.
  search_vector tsvector GENERATED ALWAYS AS (
      setweight(to_tsvector('portuguese', coalesce(nome, '')), 'A')      ||
      setweight(to_tsvector('portuguese', coalesce(marca, '')), 'B')     ||
      setweight(to_tsvector('portuguese', coalesce(categoria, '')), 'B') ||
      setweight(to_tsvector('portuguese', coalesce(descricao, '')), 'C')
  ) STORED

  -- Para busca semantica, adicione uma coluna de embedding (dimensao do seu modelo):
  -- , embedding vector(1024)
);

-- Indices ------------------------------------------------------------------
-- FTS: GIN sobre o tsvector.
CREATE INDEX IF NOT EXISTS idx_produtos_fts
  ON produtos USING gin (search_vector);

-- Fuzzy: GIN trigram sobre o nome (habilita o operador % e similarity()).
CREATE INDEX IF NOT EXISTS idx_produtos_nome_trgm
  ON produtos USING gin (nome gin_trgm_ops);

-- Filtros comuns de catalogo.
CREATE INDEX IF NOT EXISTS idx_produtos_categoria ON produtos (categoria);
CREATE INDEX IF NOT EXISTS idx_produtos_marca     ON produtos (marca);
CREATE INDEX IF NOT EXISTS idx_produtos_preco     ON produtos (preco);

-- Semantica (pgvector): descomente junto com a extensao e a coluna acima.
-- CREATE INDEX IF NOT EXISTS idx_produtos_embedding
--   ON produtos USING hnsw (embedding vector_cosine_ops);
