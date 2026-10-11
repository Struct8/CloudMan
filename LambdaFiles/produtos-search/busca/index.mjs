// Lambda de BUSCA de produtos de informatica.
//
// Fora da VPC: fala com o Aurora Serverless v2 (PostgreSQL) pelo RDS Data API,
// autenticando com o secret gerenciado do cluster. As variaveis de ambiente
// (AWS_RDS_CLUSTER_*) sao injetadas pelo Struct8 a partir da conexao
// Lambda -> policy -> Aurora.
//
// Busca combinada:
//   - Full-Text Search nativo (to_tsvector / websearch_to_tsquery) com ranking
//   - pg_trgm (similarity) como fallback tolerante a erro de digitacao
//
// Rota: GET /busca?q=<termo>&limit=<n>

import {
  RDSDataClient,
  ExecuteStatementCommand,
} from "@aws-sdk/client-rds-data";

const REGION = process.env.REGION || process.env.AWS_REGION;
const CLUSTER_ARN = process.env.AWS_RDS_CLUSTER_ARN_0;
const SECRET_ARN = process.env.AWS_RDS_CLUSTER_SECRET_ARN_0;
const DB_NAME = process.env.AWS_RDS_CLUSTER_DB_NAME_0 || "produtos";

const client = new RDSDataClient({ region: REGION });

// Executa um SQL parametrizado via Data API e devolve as linhas ja em objetos.
async function query(sql, parameters = []) {
  const command = new ExecuteStatementCommand({
    resourceArn: CLUSTER_ARN,
    secretArn: SECRET_ARN,
    database: DB_NAME,
    sql,
    parameters,
    includeResultMetadata: true,
  });

  const res = await client.send(command);
  const cols = (res.columnMetadata || []).map((c) => c.name);
  return (res.records || []).map((row) => {
    const obj = {};
    row.forEach((field, i) => {
      obj[cols[i]] = unwrap(field);
    });
    return obj;
  });
}

// Converte o Field do Data API no valor JS correspondente.
function unwrap(field) {
  if (field == null) return null;
  if (field.isNull) return null;
  if (field.stringValue !== undefined) return field.stringValue;
  if (field.longValue !== undefined) return field.longValue;
  if (field.doubleValue !== undefined) return field.doubleValue;
  if (field.booleanValue !== undefined) return field.booleanValue;
  if (field.blobValue !== undefined) return field.blobValue;
  return null;
}

function response(statusCode, body) {
  return {
    statusCode,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "public, max-age=30",
    },
    body: JSON.stringify(body),
  };
}

export const handler = async (event) => {
  const params = (event && event.queryStringParameters) || {};
  const termo = (params.q || "").trim();
  const limit = Math.min(parseInt(params.limit, 10) || 20, 50);

  if (!termo) {
    return response(400, { error: "parametro 'q' obrigatorio" });
  }

  // 1) FTS com ranking. websearch_to_tsquery entende "notebook i7 16gb".
  // 2) Se o FTS nao achar nada, cai para similaridade por trigram (typos).
  const sql = `
    WITH fts AS (
      SELECT
        id, nome, marca, categoria, preco,
        ts_rank(search_vector, websearch_to_tsquery('portuguese', :termo)) AS rank,
        1 AS fonte
      FROM produtos
      WHERE search_vector @@ websearch_to_tsquery('portuguese', :termo)
    ),
    trgm AS (
      SELECT
        id, nome, marca, categoria, preco,
        similarity(nome, :termo) AS rank,
        2 AS fonte
      FROM produtos
      WHERE NOT EXISTS (SELECT 1 FROM fts)
        AND nome % :termo
    )
    SELECT id, nome, marca, categoria, preco, rank
    FROM (SELECT * FROM fts UNION ALL SELECT * FROM trgm) r
    ORDER BY fonte, rank DESC
    LIMIT :limit;
  `;

  const parameters = [
    { name: "termo", value: { stringValue: termo } },
    { name: "limit", value: { longValue: limit } },
  ];

  try {
    const produtos = await query(sql, parameters);
    return response(200, { termo, total: produtos.length, produtos });
  } catch (err) {
    console.error("erro na busca", err);
    return response(500, { error: "falha ao consultar produtos" });
  }
};
