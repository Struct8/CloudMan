// Lambda de BUSCA de produtos de informatica.
//
// Fora da VPC: fala com o Aurora Serverless v2 (PostgreSQL) pelo RDS Data API,
// autenticando com o secret gerenciado do cluster. As variaveis de ambiente
// (AWS_RDS_CLUSTER_*) sao injetadas pelo Struct8 a partir da conexao
// Lambda -> policy -> Aurora.
//
// Busca combinada:
//   - Full-Text Search nativo (websearch_to_tsquery) com ranking
//   - pg_trgm (similarity) como fallback tolerante a erro de digitacao
//   - sem termo (q vazio): devolve produtos em destaque (mais recentes)
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

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Executa um SQL parametrizado via Data API, com retry enquanto o Aurora
// Serverless v2 esta "acordando" do auto-pause (DatabaseResumingException).
// Sem isso, a primeira requisicao depois de ocioso vira erro 500.
async function query(sql, parameters = [], { retries = 5, delayMs = 1500 } = {}) {
  for (let attempt = 0; ; attempt++) {
    try {
      const res = await client.send(
        new ExecuteStatementCommand({
          resourceArn: CLUSTER_ARN,
          secretArn: SECRET_ARN,
          database: DB_NAME,
          sql,
          parameters,
          includeResultMetadata: true,
        })
      );
      const cols = (res.columnMetadata || []).map((c) => c.name);
      return (res.records || []).map((row) => {
        const obj = {};
        row.forEach((field, i) => {
          obj[cols[i]] = unwrap(field);
        });
        return obj;
      });
    } catch (err) {
      const resuming =
        err?.name === "DatabaseResumingException" ||
        /resuming after being auto-paused/i.test(err?.message || "");
      if (resuming && attempt < retries) {
        await sleep(delayMs);
        continue;
      }
      throw err;
    }
  }
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

const COLS = "id, nome, marca, categoria, descricao, preco, imagem_url";

export const handler = async (event) => {
  const params = (event && event.queryStringParameters) || {};
  const termo = (params.q || "").trim();
  const limit = Math.min(parseInt(params.limit, 10) || 24, 50);

  // Sem termo: vitrine de destaques (produtos mais recentes).
  const destaquesSql = `
    SELECT ${COLS}, 0::real AS rank
    FROM produtos
    ORDER BY criado_em DESC
    LIMIT :limit;
  `;

  // Com termo: FTS com ranking; fallback trigram quando o FTS nao acha nada.
  const buscaSql = `
    WITH fts AS (
      SELECT ${COLS},
        ts_rank(search_vector, websearch_to_tsquery('portuguese', :termo)) AS rank,
        1 AS fonte
      FROM produtos
      WHERE search_vector @@ websearch_to_tsquery('portuguese', :termo)
    ),
    trgm AS (
      SELECT ${COLS},
        similarity(nome, :termo) AS rank,
        2 AS fonte
      FROM produtos
      WHERE NOT EXISTS (SELECT 1 FROM fts)
        AND nome % :termo
    )
    SELECT ${COLS}, rank
    FROM (SELECT * FROM fts UNION ALL SELECT * FROM trgm) r
    ORDER BY fonte, rank DESC
    LIMIT :limit;
  `;

  try {
    let produtos;
    if (!termo) {
      produtos = await query(destaquesSql, [
        { name: "limit", value: { longValue: limit } },
      ]);
      return response(200, { termo: "", destaque: true, total: produtos.length, produtos });
    }
    produtos = await query(buscaSql, [
      { name: "termo", value: { stringValue: termo } },
      { name: "limit", value: { longValue: limit } },
    ]);
    return response(200, { termo, destaque: false, total: produtos.length, produtos });
  } catch (err) {
    console.error("erro na busca", err);
    return response(500, { error: "falha ao consultar produtos" });
  }
};
