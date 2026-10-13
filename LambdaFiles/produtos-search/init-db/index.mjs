// Lambda INIT-DB: inicializa o banco apos o apply, para o template funcionar
// imediatamente, sem passo manual.
//
// 1) Cria extensao pg_trgm, a tabela `produtos` e os indices (idempotente).
// 2) Se a tabela estiver VAZIA, insere alguns produtos de exemplo.
//
// Disparada por um aws_lambda_invocation no apply. Reexecucoes sao seguras:
// o schema usa IF NOT EXISTS e o seed so roda quando a tabela esta vazia.
//
// Fala com o Aurora pelo RDS Data API (vars AWS_RDS_CLUSTER_* injetadas pela
// conexao Lambda -> policy -> Aurora). Tem retry para o auto-pause do Serverless v2.

import {
  RDSDataClient,
  ExecuteStatementCommand,
} from "@aws-sdk/client-rds-data";

const REGION = process.env.REGION || process.env.AWS_REGION;
const CLUSTER_ARN = process.env.AWS_RDS_CLUSTER_ARN_0;
const SECRET_ARN = process.env.AWS_RDS_CLUSTER_SECRET_ARN_0;
const DB_NAME = process.env.AWS_RDS_CLUSTER_DB_NAME_0 || "produtos";

const client = new RDSDataClient({ region: REGION });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function run(sql, parameters = []) {
  for (let attempt = 0; ; attempt++) {
    try {
      return await client.send(
        new ExecuteStatementCommand({
          resourceArn: CLUSTER_ARN,
          secretArn: SECRET_ARN,
          database: DB_NAME,
          sql,
          parameters,
          includeResultMetadata: true,
        })
      );
    } catch (err) {
      const resuming =
        err?.name === "DatabaseResumingException" ||
        /resuming after being auto-paused/i.test(err?.message || "");
      if (resuming && attempt < 10) {
        await sleep(2000);
        continue;
      }
      throw err;
    }
  }
}

const SCHEMA = [
  `CREATE EXTENSION IF NOT EXISTS pg_trgm`,
  `CREATE TABLE IF NOT EXISTS produtos (
     id text PRIMARY KEY,
     nome text NOT NULL,
     marca text,
     categoria text,
     descricao text,
     preco numeric(12,2),
     imagem_url text,
     specs jsonb NOT NULL DEFAULT '{}'::jsonb,
     criado_em timestamptz NOT NULL DEFAULT now(),
     atualizado_em timestamptz NOT NULL DEFAULT now(),
     search_vector tsvector GENERATED ALWAYS AS (
       setweight(to_tsvector('portuguese', coalesce(nome,'')),'A') ||
       setweight(to_tsvector('portuguese', coalesce(marca,'')),'B') ||
       setweight(to_tsvector('portuguese', coalesce(categoria,'')),'B') ||
       setweight(to_tsvector('portuguese', coalesce(descricao,'')),'C')
     ) STORED
   )`,
  `ALTER TABLE produtos ADD COLUMN IF NOT EXISTS imagem_url text`,
  `CREATE INDEX IF NOT EXISTS idx_produtos_fts ON produtos USING gin (search_vector)`,
  `CREATE INDEX IF NOT EXISTS idx_produtos_nome_trgm ON produtos USING gin (nome gin_trgm_ops)`,
  `CREATE INDEX IF NOT EXISTS idx_produtos_categoria ON produtos (categoria)`,
  `CREATE INDEX IF NOT EXISTS idx_produtos_marca ON produtos (marca)`,
  `CREATE INDEX IF NOT EXISTS idx_produtos_preco ON produtos (preco)`,
];

// Produtos de exemplo. As imagens sao SVGs versionados no repo (site/img/seed/),
// publicados no bucket pelo aws_s3_object e servidos pelo CloudFront no mesmo
// dominio (/img/seed/...). Sem dependencia de sites externos: o template e
// autossuficiente.
const SEED = [
  { id: "seed-laptop", nome: "Dell XPS 13 Laptop i7 16GB", marca: "Dell", categoria: "laptop",
    descricao: "Premium ultrabook with InfinityEdge display, 512GB NVMe SSD", preco: 1399.00,
    imagem_url: "/img/seed/laptop.svg" },
  { id: "seed-monitor", nome: "LG UltraWide 34\" Monitor", marca: "LG", categoria: "monitor",
    descricao: "34-inch QHD IPS ultrawide, 144Hz, USB-C", preco: 549.90,
    imagem_url: "/img/seed/monitor.svg" },
  { id: "seed-keyboard", nome: "Mechanical Keyboard RGB", marca: "Keychron", categoria: "keyboard",
    descricao: "Hot-swappable mechanical keyboard, brown switches", preco: 109.90,
    imagem_url: "/img/seed/keyboard.svg" },
  { id: "seed-mouse", nome: "Logitech MX Master 3S", marca: "Logitech", categoria: "mouse",
    descricao: "Wireless performance mouse, 8K DPI, quiet clicks", preco: 99.90,
    imagem_url: "/img/seed/mouse.svg" },
  { id: "seed-ssd", nome: "Samsung 990 PRO 2TB NVMe SSD", marca: "Samsung", categoria: "storage",
    descricao: "PCIe 4.0 NVMe M.2, up to 7450 MB/s", preco: 169.90,
    imagem_url: "/img/seed/storage.svg" },
  { id: "seed-gpu", nome: "NVIDIA GeForce RTX 4070 GPU", marca: "NVIDIA", categoria: "graphics card",
    descricao: "12GB GDDR6X, ray tracing, DLSS 3", preco: 599.00,
    imagem_url: "/img/seed/gpu.svg" },
];

const UPSERT = `
  INSERT INTO produtos (id, nome, marca, categoria, descricao, preco, imagem_url)
  VALUES (:id, :nome, :marca, :categoria, :descricao, :preco, :imagem_url)
  ON CONFLICT (id) DO NOTHING;
`;

export const handler = async () => {
  // 1) schema (idempotente)
  for (const sql of SCHEMA) {
    await run(sql);
  }

  // 2) seed apenas se a tabela estiver vazia
  const count = await run(`SELECT count(*)::int AS n FROM produtos`);
  const n = count.records?.[0]?.[0]?.longValue ?? 0;
  if (n > 0) {
    console.log(`tabela ja tem ${n} produto(s); seed ignorado`);
    return { ok: true, schema: true, seeded: 0, existing: n };
  }

  let seeded = 0;
  for (const p of SEED) {
    await run(UPSERT, [
      { name: "id", value: { stringValue: p.id } },
      { name: "nome", value: { stringValue: p.nome } },
      { name: "marca", value: { stringValue: p.marca } },
      { name: "categoria", value: { stringValue: p.categoria } },
      { name: "descricao", value: { stringValue: p.descricao } },
      { name: "preco", value: { doubleValue: p.preco } },
      { name: "imagem_url", value: { stringValue: p.imagem_url } },
    ]);
    seeded++;
  }
  console.log(`schema pronto; ${seeded} produto(s) de exemplo inseridos`);
  return { ok: true, schema: true, seeded };
};
